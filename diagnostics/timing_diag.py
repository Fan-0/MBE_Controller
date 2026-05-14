"""
Poll-cycle timing diagnostic for Vacuum Deposition Control
============================================================
Measures exactly how long each hardware read takes so you can see where
the bottleneck is.  Run with NO GUI open (it needs exclusive serial access).

Usage:
    python diagnostics/timing_diag.py [heat3_port] [brax_port] [baudrate]
    python diagnostics/timing_diag.py COM12 COM4 19200
    python diagnostics/timing_diag.py COM12          # HEAT3 only, skip BRAX

Outputs a table of per-call timing, then a simulated poller cycle summary,
and finally compares old path (8 individual reads) vs new (poll_all, 3 reads).
"""

import sys, os, time, statistics, struct, threading

# Allow running from repo root or from diagnostics/ subfolder
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from devices.heat3   import HEAT3
from devices.brax3400 import BRAX3400

# --- CLI args ---
HEAT3_PORT  = sys.argv[1] if len(sys.argv) > 1 else "COM12"
BRAX_PORT   = sys.argv[2] if len(sys.argv) > 2 else None
BAUDRATE    = int(sys.argv[3]) if len(sys.argv) > 3 else 19200
REPS        = 5   # how many times to repeat each test for averaging

SEP  = "-" * 68
SEP2 = "=" * 68


def fmt_ms(s): return f"{s*1000:.1f} ms"


def time_call(fn, reps=REPS):
    """Call fn() reps times, return (samples_list, mean, stdev)."""
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as e:
            print(f"      !! {e}")
        samples.append(time.perf_counter() - t0)
    return samples, statistics.mean(samples), statistics.pstdev(samples)


def print_row(label, mean, stdev, n=1):
    total = mean * n
    print(f"  {label:<40s}  {fmt_ms(mean):>9}  +/-{fmt_ms(stdev):>8}   (x{n} = {fmt_ms(total)})")


def main():
    print(SEP2)
    print("  VacDepositionControl  -  Poll-Cycle Timing Diagnostic")
    print(f"  HEAT3 port : {HEAT3_PORT}  baud={BAUDRATE}")
    print(f"  BRAX  port : {BRAX_PORT or '(skipped)'}")
    print(SEP2)

    # Connect HEAT3
    h = HEAT3(HEAT3_PORT, slave_id=1, baudrate=BAUDRATE)
    try:
        h.connect()
        print(f"  [OK] HEAT3 connected on {HEAT3_PORT}")
    except ConnectionError as e:
        print(f"  [FAIL] HEAT3 connect failed: {e}")
        print("  Cannot continue without HEAT3.")
        return

    # Connect BRAX (optional)
    b = None
    if BRAX_PORT:
        b = BRAX3400(BRAX_PORT, address=1, baudrate=19200)
        try:
            b.connect()
            print(f"  [OK] BRAX  connected on {BRAX_PORT}")
            # Raw comms check — send VER command, show raw bytes and timing
            import serial as _serial
            _ser = b._serial
            _ser.reset_input_buffer()
            cmd = b"#01VER\r"
            t0 = time.perf_counter()
            _ser.write(cmd)
            time.sleep(0.02)
            raw = _ser.read(32)
            dt = (time.perf_counter() - t0) * 1000
            if raw:
                print(f"  BRAX raw VER response ({dt:.1f} ms): {raw!r}")
            else:
                print(f"  BRAX VER -> NO RESPONSE after {dt:.1f} ms (timeout)")
                print("  WARNING: BRAX may be on wrong port/baud or IG comms issue")
        except ConnectionError as e:
            print(f"  [FAIL] BRAX  connect failed (skipping): {e}")
            b = None

    print()

    # ==========================================================================
    # SECTION 1 - individual HEAT3 reads (old slow path)
    # ==========================================================================
    print(SEP)
    print("  SECTION 1 - Individual HEAT3 reads  (old slow path)")
    print(f"  {'Call':<40s}  {'mean':>9}  {'stdev':>12}   (xN = total)")
    print(SEP)

    h_reads = [
        ("ch1_read_process_value()",  h.ch1_read_process_value),
        ("ch1_read_setpoint()",       h.ch1_read_setpoint),
        ("ch1_read_output_pct()",     h.ch1_read_output_pct),
        ("ch1_is_operating()",        h.ch1_is_operating),
        ("ch2_read_process_value()",  h.ch2_read_process_value),
        ("ch2_read_setpoint()",       h.ch2_read_setpoint),
        ("ch2_read_output_pct()",     h.ch2_read_output_pct),
        ("ch2_is_operating()",        h.ch2_is_operating),
    ]

    individual_means = []
    for label, fn in h_reads:
        samples, mean, stdev = time_call(fn)
        print_row(label, mean, stdev)
        individual_means.append(mean)

    old_total = sum(individual_means)
    print(SEP)
    print(f"  {'TOTAL  8 reads (old path)':<40s}  {fmt_ms(old_total):>9}")
    print()

    # ==========================================================================
    # SECTION 2 - poll_all() batch path (new fast path)
    # ==========================================================================
    print(SEP)
    print("  SECTION 2 - poll_all() batch read  (new fast path)")
    print(SEP)

    if hasattr(h, "poll_all"):
        samples, mean, stdev = time_call(h.poll_all)
        print_row("poll_all()  (3 batch FC 0x03 reads)", mean, stdev)
        new_total_h3 = mean
        speedup = old_total / mean if mean > 0 else float("inf")
        print(f"\n  HEAT3 speedup: {speedup:.1f}x  ({fmt_ms(old_total)} -> {fmt_ms(mean)})")
        # Show returned values
        d = h.poll_all()
        print(f"\n  Values returned by poll_all():")
        for k, v in d.items():
            print(f"    {k}: {v}")
    else:
        print("  poll_all() NOT FOUND on HEAT3 driver -- still using 8 individual reads")
        new_total_h3 = old_total
    print()

    # ==========================================================================
    # SECTION 3 - BRAX reads
    # ==========================================================================
    print(SEP)
    print("  SECTION 3 - BRAX reads")
    print(SEP)

    if b:
        brax_reads = [
            ("read_ion_gauge()",  b.read_ion_gauge),
            ("ig_status()",      b.ig_status),
        ]
        brax_means = []
        for label, fn in brax_reads:
            samples, mean, stdev = time_call(fn)
            print_row(label, mean, stdev)
            brax_means.append(mean)
        brax_total = sum(brax_means)
        print(f"  {'TOTAL  BRAX reads':<40s}  {fmt_ms(brax_total):>9}")
    else:
        print("  (BRAX not connected -- skipped)")
        brax_total = 0.025   # estimated 25ms
        print(f"  Using estimated BRAX time: {fmt_ms(brax_total)}")
    print()

    # ==========================================================================
    # SECTION 4 - Simulated full poller cycle
    # ==========================================================================
    print(SEP)
    print("  SECTION 4 - Simulated full poller cycle")
    print(SEP)

    poll_interval = 1.0   # old default

    old_cycle_reads = old_total + brax_total
    old_effective_rate = 1.0 / poll_interval
    print(f"  OLD path (8 individual HEAT3 reads + BRAX):")
    print(f"    Hardware read time  : {fmt_ms(old_cycle_reads)}")
    print(f"    Poller interval     : {poll_interval*1000:.0f} ms")
    print(f"    Effective data rate : {old_effective_rate:.1f} Hz")

    print()

    if hasattr(h, "poll_all"):
        new_cycle_reads = new_total_h3 + brax_total
        for interval_s, lbl in [(0.5, "0.5 s (new default)"), (0.33, "0.33 s"), (0.25, "0.25 s")]:
            wait = max(0.0, interval_s - new_cycle_reads)
            actual_cycle = new_cycle_reads + wait
            rate = 1.0 / actual_cycle
            print(f"  NEW path, interval={lbl}:")
            print(f"    Hardware read time  : {fmt_ms(new_cycle_reads)}")
            print(f"    Post-read wait      : {fmt_ms(wait)}")
            print(f"    Effective data rate : {rate:.1f} Hz")
            print()
    else:
        print("  (no poll_all -- same as old path)")
        print()

    # ==========================================================================
    # SECTION 5 - Serial settings audit
    # ==========================================================================
    print(SEP)
    print("  SECTION 5 - Serial settings in current driver")
    print(SEP)
    timeout = h._ser.timeout if h._ser else "N/A"
    print(f"  HEAT3  serial timeout : {timeout} s  (target: 0.25 s)")
    if b and b._serial:
        print(f"  BRAX   serial timeout : {b._serial.timeout} s  (target: 0.25 s)")
    print()
    print("  NOTE: If timeout > 0.25 s, a single missed response costs >=1 s.")
    print("  The 10ms sleep in _transact() adds 10x3 = 30ms per poll_all() call.")
    print()

    # Disconnect
    h.disconnect()
    if b:
        b.disconnect()

    print(SEP2)
    print("  Diagnostic complete.")
    print(SEP2)


if __name__ == "__main__":
    main()
