"""
DataLogger: buffers hardware snapshots and writes CSV.

v2 – "fed" architecture
========================
Data no longer comes from an internal poll thread.  Instead the Poller thread
(which already reads all hardware) calls  DataLogger.feed()  after every cycle.
This eliminates the duplicate serial reads and makes the live plot update at the
same rate as the hardware Poller (~2 Hz at 0.5 s interval) instead of 1 Hz.

API
---
  dl = DataLogger()
  dl.start_logging()          # opens CSV; no thread started
  dl.feed(pressure, ch1, ch2) # called by Poller on every cycle
  dl.stop_logging()           # flushes + closes CSV

  get_pressure_history() / get_temp_history() / get_ch2_temp_history()
  -> (times_list, values_list) for PlotWorker

Backward compat
---------------
  brax / heat3 attributes still exist (set by _resolve) but are unused;
  the old _poll_loop() is preserved but never started.
"""

import csv, os, threading, time
from collections import deque
from datetime import datetime
from typing import Optional, Callable, Deque, Tuple


_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


class DataLogger:
    MAX_POINTS = 20000  # ~1.4 h at 4 Hz; deque auto-evicts oldest

    def __init__(self, poll_interval: float = 1.0):
        # poll_interval kept for API compat (was used by old _poll_loop)
        self.poll_interval = poll_interval
        self.brax  = None   # kept for compat; not used internally
        self.heat3 = None   # kept for compat; not used internally

        # (timestamp_float, value)
        self.pressure_buf:  Deque[Tuple[float, Optional[float]]] = deque(maxlen=self.MAX_POINTS)
        self.ch1_temp_buf:  Deque[Tuple[float, Optional[float]]] = deque(maxlen=self.MAX_POINTS)
        self.ch2_temp_buf:  Deque[Tuple[float, Optional[float]]] = deque(maxlen=self.MAX_POINTS)
        self.in_temp_buf:   Deque[Tuple[float, Optional[float]]] = deque(maxlen=self.MAX_POINTS)
        self.temp_buf = self.ch1_temp_buf   # legacy alias

        self._log_file: Optional[str] = None
        self._lock   = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop   = threading.Event()
        self.running = False

        # CSV file handle – opened in start_logging, closed in stop_logging
        self._fh     = None
        self._writer = None

        self.on_data: Optional[Callable[[Optional[float], Optional[float]], None]] = None

    # ── Start / Stop ─────────────────────────────────────────────────────

    def start_logging(self, log_path: Optional[str] = None):
        """Open CSV file.  No poll thread is started — data comes via feed()."""
        if self.running:
            return
        if log_path is None:
            os.makedirs(_LOG_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(_LOG_DIR, f"pressure_log_{ts}.csv")
        self._log_file = log_path
        try:
            fh = open(log_path, "a", newline="", buffering=1)
            writer = csv.writer(fh)
            if os.path.getsize(log_path) == 0:
                writer.writerow(["Timestamp", "IG_Pressure_Torr", "CH1_Temp_C", "CH2_Temp_C", "In_Temp_C"])
        except OSError:
            return
        with self._lock:
            self._fh     = fh
            self._writer = writer
        self._stop.clear()
        self.running = True

    def stop_logging(self):
        if not self.running:
            return
        self._stop.set()
        # join any legacy poll thread if somehow still alive
        if self._thread:
            self._thread.join(timeout=6.0)
            self._thread = None
        self.running = False
        with self._lock:
            fh = self._fh
            self._fh     = None
            self._writer = None
        if fh:
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass

    # ── Primary data ingest ──────────────────────────────────────────────

    def feed(self, pressure: Optional[float],
             ch1_t:    Optional[float],
             ch2_t:    Optional[float],
             in_t:     Optional[float] = None):
        """Push one hardware snapshot into buffers (and CSV if logging).

        Called by Poller._loop on every poll cycle — runs in the Poller thread.
        Thread-safe via self._lock.
        in_t: Indium source temperature from EPC3004 (optional, None if not connected).
        """
        t_float = time.time()
        ts = datetime.now().isoformat(timespec="seconds")

        with self._lock:
            self.pressure_buf.append((t_float, pressure))
            self.ch1_temp_buf.append((t_float, ch1_t))
            self.ch2_temp_buf.append((t_float, ch2_t))
            self.in_temp_buf.append((t_float, in_t))

            if self._fh is not None:
                p_s  = f"{pressure:.4e}" if pressure  is not None else "N/A"
                t1_s = f"{ch1_t:.2f}"   if ch1_t     is not None else "N/A"
                t2_s = f"{ch2_t:.2f}"   if ch2_t     is not None else "N/A"
                ti_s = f"{in_t:.2f}"    if in_t      is not None else "N/A"
                try:
                    self._writer.writerow([ts, p_s, t1_s, t2_s, ti_s])
                    self._fh.flush()
                except (ValueError, OSError):
                    pass

        if self.on_data:
            try:
                self.on_data(pressure, ch1_t)
            except Exception:
                pass

    # ── Legacy poll loop (preserved but not started by start_logging) ────

    def _poll_loop(self, log_path: str):
        """Old self-contained poll loop — kept for reference, not called."""
        try:
            fh = open(log_path, "a", newline="", buffering=1)
            writer = csv.writer(fh)
            if os.path.getsize(log_path) == 0:
                writer.writerow(["Timestamp", "IG_Pressure_Torr", "CH1_Temp_C", "CH2_Temp_C"])
        except OSError:
            self.running = False
            return
        try:
            while not self._stop.is_set():
                t0 = time.time()
                pressure = ch1_t = ch2_t = None
                if self.brax and getattr(self.brax, "connected", False):
                    try:   pressure = self.brax.read_ion_gauge()
                    except Exception: pass
                if self.heat3 and getattr(self.heat3, "connected", False):
                    try:   ch1_t = self.heat3.ch1_read_process_value()
                    except Exception: pass
                    try:   ch2_t = self.heat3.ch2_read_process_value()
                    except Exception: pass
                t_float = time.time()
                with self._lock:
                    self.pressure_buf.append((t_float, pressure))
                    self.ch1_temp_buf.append((t_float, ch1_t))
                    self.ch2_temp_buf.append((t_float, ch2_t))
                p_s  = f"{pressure:.4e}" if pressure is not None else "N/A"
                t1_s = f"{ch1_t:.2f}"    if ch1_t   is not None else "N/A"
                t2_s = f"{ch2_t:.2f}"    if ch2_t   is not None else "N/A"
                try:
                    writer.writerow([datetime.now().isoformat(timespec="seconds"), p_s, t1_s, t2_s])
                    fh.flush()
                except (ValueError, OSError):
                    break
                if self.on_data:
                    try: self.on_data(pressure, ch1_t)
                    except Exception: pass
                elapsed = time.time() - t0
                self._stop.wait(max(0.0, self.poll_interval - elapsed))
        finally:
            try:
                fh.flush(); fh.close()
            except Exception:
                pass

    # ── Data access ──────────────────────────────────────────────────────

    def _buf_to_lists(self, buf: deque):
        """Return (absolute_unix_timestamps, values).  Callers use DateAxisItem."""
        with self._lock:
            pts = list(buf)
        if not pts:
            return [], []
        return [p[0] for p in pts], [p[1] for p in pts]

    def get_pressure_history(self):  return self._buf_to_lists(self.pressure_buf)
    def get_temp_history(self):      return self._buf_to_lists(self.ch1_temp_buf)
    def get_ch2_temp_history(self):  return self._buf_to_lists(self.ch2_temp_buf)
    def get_in_temp_history(self):   return self._buf_to_lists(self.in_temp_buf)

    @property
    def log_file_path(self) -> Optional[str]:
        return self._log_file
