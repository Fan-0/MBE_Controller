"""
BEP (Beam Equivalent Pressure) Measurement Manager

Sequence for each source:
  1. Record baseline IG pressure (fast-sampled average over BASELINE_DURATION s)
  2. Open shutter for OPEN_DURATION seconds, recording IG at FAST_INTERVAL
  3. Calculate BEP = mean(top TOP_FRACTION of open samples) - baseline
  4. Close shutter, wait SETTLE_TIME for pressure to drop
  5. Store (temperature, BEP, timestamp) → CSV + in-memory history

Indium temperature is not tracked → fixed at INDIUM_TEMP_C = 744.0 °C
Selenium temperature is read from HEAT3 CH1.

The Se/In BEP ratio is displayed and updated after each pair.

BEPTrace stores the full annotated timeline (baseline + open window) for GUI plotting:
  - samples:       all (t_rel, pressure) from t=0 (baseline start)
  - baseline_end_t / open_start_t / open_end_t: phase boundaries
  - top_samples:   subset of open-window samples used in the top-range average
  - baseline_val / peak_val / bep_val: computed results
"""

import csv
import os
import time
import threading
import statistics
from datetime import datetime
from typing import Optional, Callable, List, Tuple


INDIUM_TEMP_C     = 744.0   # fixed — no sensor
OPEN_DURATION     = 2.0     # seconds shutter stays open
FAST_INTERVAL     = 0.05    # 50 ms  → 20 Hz pressure sampling
BASELINE_DURATION = 8.0     # seconds to average baseline pressure
SETTLE_TIME       = 30.0    # seconds to wait after closing shutter
TOP_FRACTION      = 0.5     # use top 50% of open-window samples for BEP mean
BEP_CSV_FILE      = "bep_log.csv"


class BEPTrace:
    """Full annotated trace for one source measurement, used by the fast trace plot."""
    def __init__(self, source: str):
        self.source       = source          # "In" or "Se"
        self.samples:     List[Tuple[float, float]] = []  # (t_rel_s, pressure_Torr)
        self.baseline_end_t: float = 0.0   # t_rel when baseline phase ended
        self.open_start_t:   float = 0.0   # t_rel when shutter opened
        self.open_end_t:     float = 0.0   # t_rel when shutter closed
        self.baseline_val:   Optional[float] = None  # mean of baseline samples
        self.peak_val:       Optional[float] = None  # mean of top-fraction open samples
        self.bep_val:        Optional[float] = None  # peak_val − baseline_val
        self.top_samples: List[Tuple[float, float]] = []  # open samples in top fraction


class BEPRecord:
    def __init__(self, source: str, temp_c: float, baseline: float,
                 peak: float, bep: float, timestamp: str):
        self.source    = source   # "In" or "Se"
        self.temp_c    = temp_c
        self.baseline  = baseline
        self.peak      = peak
        self.bep       = bep      # peak - baseline
        self.timestamp = timestamp


class BEPManager:
    """Orchestrates BEP measurements with fast pressure sampling."""

    STATE_IDLE      = "IDLE"
    STATE_BASELINE  = "BASELINE"
    STATE_OPEN      = "OPEN"
    STATE_SETTLE    = "SETTLE"
    STATE_DONE      = "DONE"
    STATE_ABORTED   = "ABORTED"

    def __init__(self):
        self.brax    = None
        self.heat3   = None
        self.shutter = None
        self.indium  = None   # EPC3004 — used to read live Indium temperature

        self.history_in: List[BEPRecord]  = []  # In records
        self.history_se: List[BEPRecord]  = []  # Se records

        self._stop    = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.state    = self.STATE_IDLE

        # Latest results
        self.last_in_bep: Optional[float] = None
        self.last_se_bep: Optional[float] = None

        # Full annotated traces (populated after each source measurement)
        self.trace_in: Optional[BEPTrace] = None
        self.trace_se: Optional[BEPTrace] = None

        # Fast pressure buffer: open-window samples (t_rel_to_open, pressure)
        # Kept for backward compat; trace_in/trace_se contain richer data
        self.fast_buf: List[Tuple[float, float]] = []

        # Callbacks
        self.on_status:   Optional[Callable[[str], None]]  = None
        self.on_state:    Optional[Callable[[str], None]]  = None
        self.on_progress: Optional[Callable[[float], None]]= None  # 0..1
        self.on_complete: Optional[Callable[[], None]]     = None

        self._ensure_csv()

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────

    def start_measurement(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="BEP")
        self._thread.start()

    def abort(self):
        self._stop.set()
        try:
            if self.shutter: self.shutter.both_close()
        except Exception: pass
        self._set_state(self.STATE_ABORTED)

    @property
    def se_in_ratio(self) -> Optional[float]:
        if self.last_se_bep and self.last_in_bep and self.last_in_bep > 0:
            return self.last_se_bep / self.last_in_bep
        return None

    # ─────────────────────────────────────────────────────────────────
    # Measurement sequence
    # ─────────────────────────────────────────────────────────────────

    def _run(self):
        try:
            # ── Indium ──────────────────────────────────────────────
            self._msg("Measuring Indium BEP...")
            in_temp = self._get_in_temp()
            in_record = self._measure_source("In", in_temp,
                                             open_fn=self.shutter.in_open,
                                             close_fn=self.shutter.in_close)
            if in_record is None: return
            self.last_in_bep = in_record.bep

            if self._stop.is_set(): return

            # ── Wait for pressure to settle ──────────────────────────
            self._msg(f"Waiting {SETTLE_TIME:.0f} s for pressure to stabilize...")
            self._set_state(self.STATE_SETTLE)
            self._wait_with_progress(SETTLE_TIME, label="Settle")
            if self._stop.is_set(): return

            # ── Selenium ─────────────────────────────────────────────
            se_temp = self._get_se_temp()
            self._msg(f"Measuring Selenium BEP (T={se_temp:.1f}°C)...")
            se_record = self._measure_source("Se", se_temp,
                                             open_fn=self.shutter.se_open,
                                             close_fn=self.shutter.se_close)
            if se_record is None: return
            self.last_se_bep = se_record.bep

            ratio = self.se_in_ratio
            if ratio:
                self._msg(f"✓ Se/In BEP ratio = {ratio:.4f}  "
                          f"(Se={se_record.bep:.2e}, In={in_record.bep:.2e})")
            self._set_state(self.STATE_DONE)
            if self.on_complete: self.on_complete()

        except Exception as e:
            self._msg(f"BEP error: {e}")
            self._set_state(self.STATE_ABORTED)
            try:
                if self.shutter: self.shutter.both_close()
            except Exception: pass

    def _measure_source(self, name: str, temp_c: float,
                        open_fn, close_fn) -> Optional[BEPRecord]:
        trace = BEPTrace(source=name)
        t_start = time.time()

        # 1. Baseline — sample IG at 20 Hz for BASELINE_DURATION seconds
        self._msg(f"{name}: recording baseline for {BASELINE_DURATION:.0f} s...")
        self._set_state(self.STATE_BASELINE)
        baseline_samples = []
        t_end = time.time() + BASELINE_DURATION
        while time.time() < t_end and not self._stop.is_set():
            p = self._read_ig()
            t_rel = time.time() - t_start
            if p is not None and p > 0:
                baseline_samples.append(p)
                trace.samples.append((t_rel, p))
            time.sleep(FAST_INTERVAL)
            progress = 1.0 - (t_end - time.time()) / BASELINE_DURATION
            if self.on_progress: self.on_progress(max(0.0, min(1.0, progress)) * 0.4)

        if self._stop.is_set(): return None
        if not baseline_samples:
            self._msg(f"{name}: no baseline data – is BRAX connected?"); return None

        baseline = statistics.mean(baseline_samples)
        trace.baseline_end_t = time.time() - t_start
        trace.baseline_val = baseline
        self._msg(f"{name}: baseline = {baseline:.3e} Torr  (n={len(baseline_samples)})")

        # 2. Open shutter and fast-sample for OPEN_DURATION seconds
        self.fast_buf = []
        self._set_state(self.STATE_OPEN)
        open_fn()
        t_open_abs = time.time()
        trace.open_start_t = t_open_abs - t_start
        self._msg(f"{name}: shutter OPEN ({OPEN_DURATION:.0f} s)")

        t_end_open = t_open_abs + OPEN_DURATION
        while time.time() < t_end_open and not self._stop.is_set():
            p = self._read_ig()
            t_rel     = time.time() - t_start
            t_open_rel = time.time() - t_open_abs
            if p is not None and p > 0:
                trace.samples.append((t_rel, p))
                self.fast_buf.append((t_open_rel, p))
            time.sleep(FAST_INTERVAL)
            progress = (time.time() - t_open_abs) / OPEN_DURATION
            if self.on_progress: self.on_progress(0.4 + max(0.0, min(1.0, progress)) * 0.5)

        close_fn()
        trace.open_end_t = time.time() - t_start
        self._msg(f"{name}: shutter CLOSED")

        # 3. BEP = mean(top TOP_FRACTION of open-window samples) - baseline
        if not self.fast_buf:
            self._msg(f"{name}: no open-shutter data"); return None

        open_pressures = [p for _, p in self.fast_buf]
        open_pressures_sorted = sorted(open_pressures, reverse=True)
        top_n    = max(1, int(len(open_pressures_sorted) * TOP_FRACTION))
        top_vals = open_pressures_sorted[:top_n]   # highest TOP_FRACTION of samples
        peak     = statistics.mean(top_vals)
        bep      = max(0.0, peak - baseline)

        # Mark which open-window samples are in the top fraction (for plot overlay)
        threshold = top_vals[-1]  # lowest value still in the top range
        trace.top_samples = [
            (t, p) for t, p in trace.samples
            if t >= trace.open_start_t and p >= threshold
        ]
        trace.peak_val = peak
        trace.bep_val  = bep

        # Store trace on self so GUI can access it
        if name == "In":
            self.trace_in = trace
        else:
            self.trace_se = trace

        self._msg(
            f"{name}: BEP = {bep:.3e} Torr  "
            f"(top-{int(TOP_FRACTION*100)}%-mean={peak:.3e}, baseline={baseline:.3e}, "
            f"n_open={len(open_pressures)}, n_top={top_n})"
        )
        if self.on_progress: self.on_progress(0.95)

        # 4. Save record
        ts = datetime.now().isoformat(timespec="seconds")
        record = BEPRecord(name, temp_c, baseline, peak, bep, ts)
        if name == "In": self.history_in.append(record)
        else:            self.history_se.append(record)
        self._save_csv(record)
        return record

    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────

    def _read_ig(self) -> Optional[float]:
        try:
            if self.brax and getattr(self.brax, "connected", False):
                return self.brax.read_ion_gauge()
        except Exception: pass
        return None

    def _get_in_temp(self) -> float:
        """Read live Indium temperature from EPC3004 if connected, else use fixed fallback."""
        try:
            if self.indium and getattr(self.indium, "connected", False):
                v = self.indium.ch1_read_process_value()
                if v is not None: return v
        except Exception: pass
        return INDIUM_TEMP_C   # fixed fallback when EPC3004 not connected

    def _get_se_temp(self) -> float:
        try:
            if self.heat3 and getattr(self.heat3, "connected", False):
                v = self.heat3.ch1_read_process_value()
                if v is not None: return v
        except Exception: pass
        return 0.0

    def _wait_with_progress(self, duration: float, label: str = ""):
        t_start = time.time()
        while not self._stop.is_set():
            elapsed = time.time() - t_start
            if elapsed >= duration: break
            if self.on_progress:
                self.on_progress(0.95 * elapsed / duration)
            time.sleep(0.5)

    def _msg(self, txt: str):
        if self.on_status: self.on_status(f"[BEP] {txt}")

    def _set_state(self, s: str):
        self.state = s
        if self.on_state: self.on_state(s)

    # ─────────────────────────────────────────────────────────────────
    # CSV persistence
    # ─────────────────────────────────────────────────────────────────

    def _ensure_csv(self):
        if not os.path.exists(BEP_CSV_FILE):
            with open(BEP_CSV_FILE, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["Timestamp","Source","Temperature_C","Baseline_Torr","Peak_Torr","BEP_Torr"]
                )

    def _save_csv(self, r: BEPRecord):
        with open(BEP_CSV_FILE, "a", newline="") as f:
            csv.writer(f).writerow(
                [r.timestamp, r.source, f"{r.temp_c:.1f}",
                 f"{r.baseline:.4e}", f"{r.peak:.4e}", f"{r.bep:.4e}"]
            )

    def load_history_from_csv(self):
        """Re-load historical records from CSV on startup."""
        if not os.path.exists(BEP_CSV_FILE): return
        try:
            with open(BEP_CSV_FILE) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    r = BEPRecord(
                        source   =row["Source"],
                        temp_c   =float(row["Temperature_C"]),
                        baseline =float(row["Baseline_Torr"]),
                        peak     =float(row["Peak_Torr"]),
                        bep      =float(row["BEP_Torr"]),
                        timestamp=row["Timestamp"],
                    )
                    if r.source == "In": self.history_in.append(r)
                    else:                self.history_se.append(r)
        except Exception:
            pass
