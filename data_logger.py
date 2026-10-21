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

v3 – file I/O off the Poller thread (2026-10-21)
================================================
The CSV now lives in log_paths.SYSTEM_DIR — on Box Drive (~/Box/logs/system), a
cloud-backed virtual drive whose writes can STALL or hit a briefly locked file while
it uploads. feed() used to write + flush in the Poller thread, so a Box stall would
have frozen hardware polling. Now feed() only queues the row; a "DataLogger-writer"
thread opens the file, writes in batches (≤ 1 s), flushes, retries on OSError, and
after WRITE_FALLBACK_S of failure switches to the local logs/system folder (rows are
kept in the queue meanwhile, nothing is lost). Problems are reported through
pop_notes() (the GUI logs them) instead of being silently swallowed.
"""

import csv, os, queue, threading, time
from collections import deque
from datetime import datetime
from typing import Optional, Callable, Deque, List, Tuple

import log_paths

_LOG_DIR = log_paths.SYSTEM_DIR             # pressure_log_*.csv → <LOG_ROOT>/system
WRITE_BATCH_S    = 1.0     # writer flushes at least this often
WRITE_RETRY_S    = 2.0     # wait between attempts after an OSError
WRITE_FALLBACK_S = 20.0    # failing this long → switch to the local logs/system folder


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

        # CSV rows → writer thread (see module docstring v3)
        self._fh     = None                 # legacy attributes, unused since v3
        self._writer = None
        self._rows: "queue.Queue[list]" = queue.Queue()
        self._wthread: Optional[threading.Thread] = None
        self._wstop  = threading.Event()
        self._notes: List[str] = []
        self._notes_lock = threading.Lock()
        self.rows_written = 0

        self.on_data: Optional[Callable[[Optional[float], Optional[float]], None]] = None

    # ── Start / Stop ─────────────────────────────────────────────────────

    def start_logging(self, log_path: Optional[str] = None):
        """Start the CSV writer thread. Returns immediately — the file is opened by the
        writer (opening a file on Box can itself stall). Data comes via feed()."""
        if self.running:
            return
        if log_path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(_LOG_DIR, f"pressure_log_{ts}.csv")
        self._log_file = log_path
        self._rows = queue.Queue()
        self._wstop.clear()
        self._stop.clear()
        self.running = True
        self._wthread = threading.Thread(target=self._write_loop, args=(log_path,),
                                         name="DataLogger-writer", daemon=True)
        self._wthread.start()

    def stop_logging(self):
        if not self.running:
            return
        self._stop.set()
        # join any legacy poll thread if somehow still alive
        if self._thread:
            self._thread.join(timeout=6.0)
            self._thread = None
        self.running = False
        self._wstop.set()                    # writer drains the queue, then closes
        if self._wthread:
            self._wthread.join(timeout=10.0)
            self._wthread = None

    def pop_notes(self) -> List[str]:
        """Messages from the writer thread (fallback used, write errors) — GUI thread."""
        with self._notes_lock:
            n, self._notes = self._notes, []
        return n

    def _note(self, msg: str):
        with self._notes_lock:
            self._notes.append(msg)

    def _write_loop(self, path: str):
        header = ["Timestamp", "IG_Pressure_Torr", "CH1_Temp_C", "CH2_Temp_C", "In_Temp_C"]
        fh = writer = None
        fail_since = None
        pending: List[list] = []             # rows taken from the queue, not yet written
        while True:
            stopping = self._wstop.is_set()
            try:                             # gather up to WRITE_BATCH_S worth of rows
                pending.append(self._rows.get(timeout=0.2 if not stopping else 0.0))
                t_end = time.time() + WRITE_BATCH_S
                while time.time() < t_end:
                    pending.append(self._rows.get_nowait())
            except queue.Empty:
                pass
            if fh is None and (pending or not stopping):
                try:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    fh = open(path, "a", newline="", buffering=1)
                    writer = csv.writer(fh)
                    if os.path.getsize(path) == 0:
                        writer.writerow(header)
                except OSError as e:
                    fh = writer = None
                    fail_since = fail_since or time.time()
                    if time.time() - fail_since > WRITE_FALLBACK_S and not path.startswith(log_paths.LOCAL_ROOT):
                        new = os.path.join(log_paths.LOCAL_SYSTEM_DIR, os.path.basename(path))
                        self._note(f"⚠ pressure log: cannot write {path} ({e}) — continuing in {new}")
                        path, self._log_file, fail_since = new, new, None
                    if stopping:
                        break
                    self._wstop.wait(WRITE_RETRY_S)
                    continue
            if pending and fh is not None:
                try:
                    writer.writerows(pending)
                    fh.flush()
                    self.rows_written += len(pending)
                    pending = []
                    fail_since = None
                except OSError as e:         # Box locked / gone: reopen and retry
                    self._note(f"⚠ pressure log write failed ({e}) — retrying")
                    try:
                        fh.close()
                    except Exception:
                        pass
                    fh = writer = None
                    fail_since = fail_since or time.time()
                    if not stopping:
                        self._wstop.wait(WRITE_RETRY_S)
                    continue
            if stopping and self._rows.empty() and not pending:
                break
        if fh is not None:
            try:
                fh.flush(); fh.close()
            except Exception:
                pass
        if pending:
            self._note(f"⚠ pressure log: {len(pending)} rows could not be written to {path}")

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

        if self.running:                     # queue only — never file I/O on this thread
            p_s  = f"{pressure:.4e}" if pressure  is not None else "N/A"
            t1_s = f"{ch1_t:.2f}"   if ch1_t     is not None else "N/A"
            t2_s = f"{ch2_t:.2f}"   if ch2_t     is not None else "N/A"
            ti_s = f"{in_t:.2f}"    if in_t      is not None else "N/A"
            self._rows.put([ts, p_s, t1_s, t2_s, ti_s])

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
