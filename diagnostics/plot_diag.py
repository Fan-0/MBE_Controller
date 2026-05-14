"""
GUI Live-Plot Pipeline Diagnostic
==================================
Diagnoses why the live plot update rate is slow.

Runs entirely WITHOUT hardware (no COM ports needed).
Instruments the full pipeline:

  DataLogger._poll_loop  (feeds buffers)
      |
  DataLogger.{pressure_buf, ch1_temp_buf, ch2_temp_buf}
      |
  PlotWorker._tick()  (reads buffers, builds numpy, emits signal)
      |
  MainWindow._on_plot_data()  (calls pyqtgraph setData)

Outputs:
  - DataLogger effective poll rate
  - PlotWorker tick rate and per-tick time
  - data_ready signal emission rate  (= actual plot update rate)
  - end-to-end latency (buffer write -> signal)
  - diagnosis of the bottleneck

Usage:
    python diagnostics/plot_diag.py [--duration 10] [--inject-hz 4]
    python diagnostics/plot_diag.py           # default: 10 s, inject at 2 Hz
"""

import sys, os, time, threading, argparse, statistics
from collections import deque

# Allow running from repo root or from diagnostics/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# --- CLI -----------------------------------------------------------------
parser = argparse.ArgumentParser(description="Plot pipeline diagnostic")
parser.add_argument("--duration",   type=float, default=10.0, help="Run duration in seconds (default 10)")
parser.add_argument("--inject-hz",  type=float, default=2.0,  help="Rate at which fake data is injected into DataLogger buffers (default 2 Hz)")
parser.add_argument("--no-qt",      action="store_true",       help="Skip PlotWorker QThread test (no Qt needed)")
args = parser.parse_args()

SEP  = "-" * 70
SEP2 = "=" * 70

def fmt_ms(s): return f"{s*1000:.1f} ms"
def fmt_hz(r): return f"{r:.2f} Hz"

# =========================================================================
# SECTION 0 – import check
# =========================================================================
print(SEP2)
print("  VacDepositionControl  -  GUI Live-Plot Pipeline Diagnostic")
print(SEP2)
print()

# Check imports
missing = []
try:
    import numpy as np
    print("  [OK] numpy", np.__version__)
except ImportError:
    missing.append("numpy"); print("  [FAIL] numpy not installed")

try:
    import pyqtgraph as pg
    HAS_PG = True
    print("  [OK] pyqtgraph", pg.__version__)
except ImportError:
    HAS_PG = False
    print("  [WARN] pyqtgraph not installed -- plot timing test skipped")

try:
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QTimer
    HAS_QT = True
    print("  [OK] PyQt5")
except ImportError:
    HAS_QT = False
    print("  [WARN] PyQt5 not found -- QThread test skipped")
    args.no_qt = True

try:
    from data_logger import DataLogger
    print("  [OK] DataLogger imported")
except ImportError as e:
    print(f"  [FAIL] Cannot import DataLogger: {e}")
    sys.exit(1)

if not args.no_qt:
    try:
        from gui_app import PlotWorker
        print("  [OK] PlotWorker imported from gui_app")
    except ImportError as e:
        print(f"  [FAIL] Cannot import PlotWorker: {e}")
        args.no_qt = True

print()
if missing:
    print(f"  ERROR: missing packages: {missing}")
    sys.exit(1)

# =========================================================================
# SECTION 1 – DataLogger poll interval check (no hardware, inspect defaults)
# =========================================================================
print(SEP)
print("  SECTION 1 - DataLogger configuration audit")
print(SEP)

dl_default = DataLogger()
print(f"  DataLogger.poll_interval  (default) : {dl_default.poll_interval:.2f} s  "
      f"-> max plot rate = {1/dl_default.poll_interval:.1f} Hz")
print(f"  DataLogger.MAX_POINTS               : {dl_default.MAX_POINTS}")
print(f"  pressure_buf maxlen                 : {dl_default.pressure_buf.maxlen}")
print()

# Detect whether the Poller-feed architecture is active
_has_feed   = hasattr(dl_default, "feed")
_has_logger = False
try:
    from gui_app import Poller as _Poller
    _has_logger = hasattr(_Poller(None), "logger")
except Exception:
    pass

if _has_feed and _has_logger:
    print("  [OK] FIX APPLIED: DataLogger.feed() exists and Poller.logger is wired.")
    print("       DataLogger no longer runs its own poll thread.")
    print("       Poller calls feed() on every cycle -> plot updates at Poller rate.")
    print(f"       Expected plot rate = 1 / Poller.interval  (default 2 Hz at 0.5 s)")
else:
    print("  *** DIAGNOSIS: poll_interval = 1.0 s means buffers only update once/sec.")
    print("      The PlotWorker emits only when buffer tail changes, so the plot")
    print("      cannot update faster than DataLogger.poll_interval regardless of")
    print("      how fast PlotWorker or pyqtgraph run.")
    print()
    print("  *** ROOT CAUSE: Two independent polling loops:")
    print("      1. Poller._loop  -> Snap  (labels, 0.5 s by default)")
    print("      2. DataLogger._poll_loop -> buffers  (plots+CSV, 1.0 s default)")
    print("      These compete for the serial port AND the plot lags the labels.")
print()


# =========================================================================
# SECTION 2 – Buffer injection rate test (pure Python, no Qt)
# =========================================================================
print(SEP)
print("  SECTION 2 - Buffer injection latency (pure Python, no Qt)")
print(SEP)

# We manually inject data into a DataLogger's buffers at a known rate
# and measure how fast we can do it.
dl = DataLogger()
N = 200
inject_times = []
t_start = time.perf_counter()
for i in range(N):
    t0 = time.perf_counter()
    t_now = time.time()
    dl.pressure_buf.append((t_now, 1e-5 + i * 1e-7))
    dl.ch1_temp_buf.append((t_now, 100.0 + i * 0.1))
    dl.ch2_temp_buf.append((t_now, 200.0 + i * 0.1))
    inject_times.append(time.perf_counter() - t0)

total_inject = time.perf_counter() - t_start
print(f"  {N} buffer appends:")
print(f"    Total time   : {fmt_ms(total_inject)}  ({N/total_inject:.0f} appends/s)")
print(f"    Per append   : {fmt_ms(statistics.mean(inject_times))}  +/- {fmt_ms(statistics.pstdev(inject_times))}")
print()

# =========================================================================
# SECTION 3 – PlotWorker _tick() timing (no Qt event loop)
# =========================================================================
print(SEP)
print("  SECTION 3 - PlotWorker._tick() timing  (synthetic buffer, no hardware)")
print(SEP)

if args.no_qt or not HAS_QT:
    print("  (skipped - no Qt)")
else:
    # We need a minimal Qt app to instantiate PlotWorker (it is a QThread subclass)
    _app = QApplication.instance() or QApplication(sys.argv)

    # Fill buffers with realistic data (7200 points = 2h at 1Hz)
    N_FILL = 500   # realistic mid-session buffer
    dl2 = DataLogger()
    t_base = time.time() - N_FILL
    for i in range(N_FILL):
        t = t_base + i
        dl2.pressure_buf.append((t, max(1e-8, 1e-6 * (1 + 0.01 * (i % 100)))))
        dl2.ch1_temp_buf.append((t, 150.0 + i * 0.05))
        dl2.ch2_temp_buf.append((t, 300.0 + i * 0.02))

    pw = PlotWorker(dl2)

    # Connect a counter slot to count emissions (signals are read-only in PyQt5)
    emit_count = [0]
    pw.data_ready.connect(lambda d: emit_count.__setitem__(0, emit_count[0] + 1))

    REPS = 20
    print(f"  Buffer size: {N_FILL} points  (realistic mid-session)")
    print(f"  Running _tick() x{REPS}  (no event loop -- signal counts may be 0)")

    # Time two scenarios:
    #  (a) buffer tail changed -> full numpy rebuild + emit
    #  (b) no change -> early return
    changed_ticks   = []
    unchanged_ticks = []

    for i in range(REPS):
        if i % 3 == 0:
            # Force a change so _tick will rebuild
            dl2.pressure_buf.append((time.time(), 1e-6 + i * 1e-9))
            pw._last_p = None
            t0 = time.perf_counter()
            pw._tick()
            changed_ticks.append(time.perf_counter() - t0)
        else:
            # No change -- should early-return immediately
            t0 = time.perf_counter()
            pw._tick()
            unchanged_ticks.append(time.perf_counter() - t0)

    if changed_ticks:
        print(f"\n  _tick() WITH new data (rebuilds numpy arrays + emits):")
        print(f"    mean {fmt_ms(statistics.mean(changed_ticks))}  "
              f"+/- {fmt_ms(statistics.pstdev(changed_ticks))}")
        print(f"    -> at 250ms PlotWorker interval, headroom = "
              f"{fmt_ms(0.25 - statistics.mean(changed_ticks))}")
    if unchanged_ticks:
        print(f"\n  _tick() with NO new data (early return):")
        print(f"    mean {fmt_ms(statistics.mean(unchanged_ticks))}  "
              f"+/- {fmt_ms(statistics.pstdev(unchanged_ticks))}")

    print(f"\n  NOTE: emit_count={emit_count[0]} (may be 0 without a running event loop)")
    print("        Signals fire but slots won't process until Qt processes events.")


# =========================================================================
# SECTION 4 – Live pipeline rate test (Qt event loop, real signal)
# =========================================================================
print()
print(SEP)
print("  SECTION 4 - Live pipeline rate test  (Qt event loop, real signals)")
print(f"  Injecting fake data at {args.inject_hz:.1f} Hz for {args.duration:.0f} s")
print(SEP)

if args.no_qt or not HAS_QT:
    print("  (skipped - no Qt)")
    print()
else:
    _app = QApplication.instance() or QApplication(sys.argv)

    dl3 = DataLogger()
    pw3 = PlotWorker(dl3)

    signal_times = []
    signal_sizes = []

    def on_data(d):
        signal_times.append(time.perf_counter())
        signal_sizes.append(len(d.get('px', [])))

    pw3.data_ready.connect(on_data)

    # Background injector thread
    injector_times = []
    stop_ev = threading.Event()

    def injector():
        i = 0
        interval = 1.0 / args.inject_hz
        while not stop_ev.is_set():
            t0 = time.perf_counter()
            t_now = time.time()
            dl3.pressure_buf.append((t_now, max(1e-8, 1e-6 + i * 1e-9)))
            dl3.ch1_temp_buf.append((t_now, 100.0 + i * 0.01))
            dl3.ch2_temp_buf.append((t_now, 200.0 + i * 0.01))
            injector_times.append(time.perf_counter())
            i += 1
            elapsed = time.perf_counter() - t0
            time.sleep(max(0, interval - elapsed))

    inj_thread = threading.Thread(target=injector, daemon=True)
    inj_thread.start()
    pw3.start()

    # Run event loop for the test duration
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(_app.quit)
    timer.start(int(args.duration * 1000))
    _app.exec_()

    stop_ev.set()
    pw3.stop()

    # Measure rates
    if len(injector_times) >= 2:
        inj_intervals = [injector_times[i+1] - injector_times[i] for i in range(len(injector_times)-1)]
        actual_inject_hz = 1.0 / statistics.mean(inj_intervals) if inj_intervals else 0
    else:
        actual_inject_hz = 0

    if len(signal_times) >= 2:
        sig_intervals = [signal_times[i+1] - signal_times[i] for i in range(len(signal_times)-1)]
        actual_signal_hz = 1.0 / statistics.mean(sig_intervals) if sig_intervals else 0
        sig_jitter = statistics.pstdev(sig_intervals) * 1000
    else:
        actual_signal_hz = 0
        sig_jitter = 0

    print(f"\n  Injector:")
    print(f"    Target rate     : {args.inject_hz:.1f} Hz")
    print(f"    Actual rate     : {fmt_hz(actual_inject_hz)}")
    print(f"    Total injections: {len(injector_times)}")

    print(f"\n  PlotWorker data_ready signal (= actual plot update rate):")
    print(f"    Signals received: {len(signal_times)}")
    if len(signal_times) >= 2:
        print(f"    Observed rate   : {fmt_hz(actual_signal_hz)}  (jitter: {sig_jitter:.1f} ms)")
        print(f"    Mean interval   : {fmt_ms(statistics.mean(sig_intervals))}")
        print(f"    Min interval    : {fmt_ms(min(sig_intervals))}")
        print(f"    Max interval    : {fmt_ms(max(sig_intervals))}")
    else:
        print("    WARNING: fewer than 2 signals received -- PlotWorker may not be emitting!")

    if signal_sizes:
        print(f"    Avg points/emit : {statistics.mean(signal_sizes):.0f}")

    print()
    if actual_signal_hz < args.inject_hz * 0.8 and len(signal_times) >= 2:
        print(f"  NOTE: signal rate ({fmt_hz(actual_signal_hz)}) << inject rate ({fmt_hz(actual_inject_hz)})")
        print("        PlotWorker is the rate limiter (250 ms sleep floor).")
    elif len(signal_times) < 2:
        print("  NOTE: very few signals -- check if buffers are being populated correctly.")

# =========================================================================
# SECTION 5 – End-to-end latency (inject -> signal)
# =========================================================================
print(SEP)
print("  SECTION 5 - End-to-end latency: buffer inject -> data_ready signal")
print(SEP)

if args.no_qt or not HAS_QT:
    print("  (skipped - no Qt)")
    print()
else:
    # Design: inject one point, wait for signal (with Qt event loop running via
    # processEvents), record round-trip latency.  Repeat 12 times.
    from PyQt5.QtWidgets import QApplication as _QApp
    _app2 = _QApp.instance() or _QApp(sys.argv)

    dl4 = DataLogger()
    pw4 = PlotWorker(dl4)

    latencies = []
    _sig_event = threading.Event()
    _sig_t     = [0.0]

    def on_data4(d):
        _sig_t[0] = time.perf_counter()
        _sig_event.set()

    pw4.data_ready.connect(on_data4)
    pw4.start()
    time.sleep(0.05)   # let PlotWorker settle

    for i in range(12):
        _sig_event.clear()
        # Inject one new point so PlotWorker will emit on next tick
        t_inject = time.perf_counter()
        t_now = time.time()
        dl4.pressure_buf.append((t_now, 1e-6 + i * 1e-9))
        dl4.ch1_temp_buf.append((t_now, 150.0 + i))
        dl4.ch2_temp_buf.append((t_now, 300.0 + i))
        # Spin-wait with processEvents so Qt can deliver the signal
        deadline = time.perf_counter() + 0.6   # 600ms max wait
        while not _sig_event.is_set() and time.perf_counter() < deadline:
            _app2.processEvents()
            time.sleep(0.005)
        if _sig_event.is_set():
            latencies.append(_sig_t[0] - t_inject)
        time.sleep(0.05)   # brief gap between rounds

    pw4.stop()

    if latencies:
        print(f"  Latency samples: {len(latencies)}")
        print(f"    Mean    : {fmt_ms(statistics.mean(latencies))}")
        print(f"    Min     : {fmt_ms(min(latencies))}")
        print(f"    Max     : {fmt_ms(max(latencies))}")
        print(f"    Stdev   : {fmt_ms(statistics.pstdev(latencies))}")
        print()
        print("  NOTE: latency = time from buffer write until data_ready fires.")
        print("        Expected range: 0 - 260 ms (PlotWorker 250ms poll period).")
        if statistics.mean(latencies) > 0.35:
            print("  WARNING: mean latency > 350 ms -- PlotWorker may be blocked!")
        else:
            print("  [OK] latency within expected range.")
    else:
        print("  No latency samples collected -- PlotWorker did not emit in time.")
    print()

# =========================================================================
# SECTION 6 – Root cause summary & fix recommendation
# =========================================================================
print(SEP2)
print("  SECTION 6 - Root cause summary & recommended fix")
print(SEP2)
print()
print("  Pipeline paths that feed the live plot:")
print()
print("  PATH A (normal live logging, no recipe):")
print("    DataLogger._poll_loop  (poll_interval=1.0 s default)")
print("      -> does its OWN serial reads (separate from Poller)")
print("      -> appends to pressure_buf / ch1_temp_buf / ch2_temp_buf")
print("      -> PlotWorker picks up at next 250ms tick")
print("      -> RESULT: plot updates at 1 Hz  <-- bottleneck")
print()
print("  PATH B (during recipe run, rec_log active):")
print("    _sync_rec_log()  (200ms QTimer, main thread)")
print("      -> copies from rec_log._rows_pressure/_rows_temp into buffers")
print("      -> PlotWorker picks up at next 250ms tick")
print("      -> RESULT: plot updates at ~4 Hz (limited by PlotWorker 250ms)")
print()
print("  PATH C (Poller -> Snap, feeds LABELS only):")
print("    Poller._loop  (0.5 s interval)")
print("      -> writes to Snap dict only")
print("      -> _vals() reads Snap and updates QLabel text")
print("      -> does NOT feed DataLogger buffers")
print("      -> RESULT: labels update at 2 Hz but plot ignores this data")
print()
print("  THE FIX (two options):")
print()
print("  Option A -- Reduce DataLogger.poll_interval (quickest):")
print("    In gui_app.py, change:")
print("      self.logger = DataLogger()")
print("    to:")
print("      self.logger = DataLogger(poll_interval=0.5)")
print("    This makes logging + plots update at 2 Hz instead of 1 Hz.")
print("    Downside: DataLogger still makes redundant serial reads alongside Poller.")
print()
print("  Option B -- Feed DataLogger from Poller snap (best, eliminates dual reads):")
print("    Have Poller._loop call logger.feed() instead of DataLogger doing its own reads.")
print("    - Removes serial contention between the two polling threads")
print("    - Plot and labels update at the same rate (Poller.interval)")
print("    - DataLogger becomes a pure buffer+CSV writer, no serial access")
print()
print(SEP2)
print("  Diagnostic complete.")
print(SEP2)
