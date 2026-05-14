"""
EPC3004 Ramp Rate Diagnostic
=============================
Confirms whether R_SP_RATE (0x0020) / R_SP_RATE_U (0x0021) actually control
the SP ramp rate, or whether they control something else (e.g. output limit).

How it works
------------
1. SNAPSHOT  — saves current SP, ramp registers, mode.
2. SET RAMP  — writes a slow test ramp rate (default 20 °C/min) to 0x0020/0x0021.
3. STEP SP   — writes target SP = current_SP + STEP (default +15 °C) to SP1 and SP2.
4. MONITOR   — samples working SP (0x0002) and output (0x0004) every 2 s for
               MONITOR_SEC seconds (default 40 s).
5. VERDICT   — analyses the samples:
     - If working SP changes gradually at ~ramp_rate → RAMP CONFIRMED ✓
     - If working SP jumps instantly to target SP    → ramp register NOT working
     - If output % just jumps without SP changing    → 0x0020 may be output limit
6. RESTORE   — returns SP and ramp to original values.

The test uses only a small SP step (+15 °C by default) to minimise temperature
impact.  The step is completely restored at the end.

Usage (close GUI first):
  python diagnostics/epc3004_ramp_diag.py [port] [slave_id] [baudrate]
  python diagnostics/epc3004_ramp_diag.py COM13 1 19200

Optional args to override defaults (edit constants below or pass as env):
  RAMP_RATE_CMIN  — test ramp rate in °C/min  (default 20)
  SP_STEP_C       — SP step size in °C         (default 15)
  MONITOR_SEC     — monitoring duration in s   (default 40)
  SAMPLE_INTERVAL — seconds between samples    (default 2)
"""

import sys, struct, time, serial, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM13"
SLAVE_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BAUDRATE = int(sys.argv[3]) if len(sys.argv) > 3 else 19200
TIMEOUT  = 0.5

# ── Test parameters (edit these if needed) ────────────────────────────────────
RAMP_RATE_CMIN  = 20.0    # °C/min test ramp rate
SP_STEP_C       = 15.0    # °C step size (small, fully restored at end)
MONITOR_SEC     = 40      # seconds to monitor
SAMPLE_INTERVAL = 2       # seconds between samples

# ── Register addresses ────────────────────────────────────────────────────────
RESOLUTION  = 10
R_PV        = 0x0001
R_SP_RO     = 0x0002   # working/ramped SP (read-only) — the key signal
R_OP        = 0x0004   # output % × 10 (read-only)
R_SP        = 0x0018   # target SP1 (writable)
R_SP2       = 0x0019   # target SP2 (writable)
R_SP_RATE   = 0x0020   # SP ramp rate value × 10
R_SP_RATE_U = 0x0021   # SP ramp rate unit (0=off, 1=°C/s, 2=°C/min, 3=°C/h)
R_AUTO_MAN  = 0x0112   # 0=Auto, 1=Manual

# ── Protocol helpers ──────────────────────────────────────────────────────────
def _crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def _frame_read(reg, n):
    f = struct.pack(">BBHH", SLAVE_ID, 0x03, reg, n)
    return f + struct.pack("<H", _crc16(f))

def _frame_write(reg, value):
    f = struct.pack(">BBHH", SLAVE_ID, 0x06, reg, value & 0xFFFF)
    return f + struct.pack("<H", _crc16(f))

def transact(ser, req, expected):
    ser.reset_input_buffer()
    ser.write(req)
    time.sleep(0.025)
    resp = ser.read(expected)
    if not resp:
        return None, "timeout"
    if len(resp) >= 3 and (resp[1] & 0x80):
        code = resp[2]
        msgs = {1:"Illegal function", 2:"Illegal address", 3:"Illegal data/value", 4:"Device error"}
        return None, f"exc 0x{code:02X} ({msgs.get(code,'?')})"
    if len(resp) < expected:
        return None, f"short {len(resp)}/{expected}"
    if _crc16(resp[:expected-2]) != struct.unpack("<H", resp[expected-2:expected])[0]:
        return None, "CRC"
    return resp[:expected], None

def read_reg(ser, reg, signed=True):
    resp, err = transact(ser, _frame_read(reg, 1), 7)
    if err:
        return None, err
    raw = struct.unpack(">H", resp[3:5])[0]
    if signed and raw >= 0x8000:
        raw -= 0x10000
    return raw, None

def write_reg(ser, reg, value):
    resp, err = transact(ser, _frame_write(reg, value), 8)
    if err:
        return False, err
    return True, None

def to_c(raw):
    if raw is None: return None
    v = raw if raw < 0x8000 else raw - 0x10000
    return v / RESOLUTION

def sep(t=""):
    if t:
        print(f"\n{'─'*65}\n  {t}\n{'─'*65}")
    else:
        print('─'*65)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  EPC3004 Ramp Rate Diagnostic")
    print(f"  Port={PORT}  SlaveID={SLAVE_ID}  Baud={BAUDRATE}")
    print(f"  Test ramp: {RAMP_RATE_CMIN} °C/min   SP step: +{SP_STEP_C} °C")
    print(f"  Monitor: {MONITOR_SEC} s at {SAMPLE_INTERVAL} s intervals")
    print("=" * 65)

    try:
        ser = serial.Serial(PORT, BAUDRATE, bytesize=8, parity='N',
                            stopbits=1, timeout=TIMEOUT)
    except serial.SerialException as e:
        print(f"\n  FATAL: cannot open {PORT}: {e}"); sys.exit(1)

    snap = {}

    try:
        # ── SNAPSHOT ─────────────────────────────────────────────────────────
        sep("SNAPSHOT — saving device state")
        snap_regs = [
            (R_AUTO_MAN,  "R_AUTO_MAN  (0x0112)", False),
            (R_SP,        "R_SP        (0x0018)", True),
            (R_SP2,       "R_SP2       (0x0019)", True),
            (R_SP_RATE,   "R_SP_RATE   (0x0020)", False),
            (R_SP_RATE_U, "R_SP_RATE_U (0x0021)", False),
        ]
        for reg, label, signed in snap_regs:
            v, err = read_reg(ser, reg, signed)
            if err:
                print(f"  ✗ {label} → {err}  (restore may be incomplete)")
            else:
                snap[reg] = v
                if reg == R_AUTO_MAN:
                    print(f"  ✓ {label} = {'Manual' if v else 'Auto'}")
                elif reg in (R_SP, R_SP2):
                    print(f"  ✓ {label} = raw {v}  ({to_c(v):.1f} °C)")
                elif reg == R_SP_RATE:
                    print(f"  ✓ {label} = raw {v}  ({v/10.0:.1f} °C/unit)")
                elif reg == R_SP_RATE_U:
                    units = {0:"off/instant", 1:"°C/s", 2:"°C/min", 3:"°C/h"}
                    print(f"  ✓ {label} = {v}  ({units.get(v,'?')})")

        # Read baseline process values
        pv_raw,  _ = read_reg(ser, R_PV,    signed=True)
        wsp_raw, _ = read_reg(ser, R_SP_RO, signed=True)
        op_raw,  _ = read_reg(ser, R_OP,    signed=True)
        print(f"\n  Baseline:  PV={to_c(pv_raw):.1f}°C   "
              f"WorkSP={to_c(wsp_raw):.1f}°C   Output={op_raw/10.0 if op_raw else '?'}%")

        if wsp_raw is None:
            print("  FATAL: cannot read working SP — aborting"); return

        # ── SETUP ─────────────────────────────────────────────────────────────
        sep("SETUP — writing test ramp rate and target SP")

        # 1. Ensure Auto mode
        ok, err = write_reg(ser, R_AUTO_MAN, 0)
        print(f"  Auto mode: {'✓' if ok else f'✗ {err}'}")
        time.sleep(0.05)

        # 2. Write test ramp rate
        rate_raw = int(round(RAMP_RATE_CMIN * 10))
        ok_r, err_r = write_reg(ser, R_SP_RATE, rate_raw)
        time.sleep(0.02)
        ok_u, err_u = write_reg(ser, R_SP_RATE_U, 2)   # 2 = per/min
        time.sleep(0.05)
        rb_rate, _ = read_reg(ser, R_SP_RATE, signed=False)
        rb_unit, _ = read_reg(ser, R_SP_RATE_U, signed=False)
        units = {0:"off/instant", 1:"°C/s", 2:"°C/min", 3:"°C/h"}
        print(f"  R_SP_RATE  write {rate_raw}: {'✓' if ok_r else f'✗ {err_r}'}"
              f"  readback={rb_rate} ({rb_rate/10.0 if rb_rate is not None else '?'} °C/unit)")
        print(f"  R_SP_RATE_U write 2:     {'✓' if ok_u else f'✗ {err_u}'}"
              f"  readback={rb_unit} ({units.get(rb_unit,'?')})")

        if not ok_r or not ok_u:
            print("\n  WARNING: ramp register write(s) failed — 0x0020/0x0021 may be read-only.")
            print("  Continuing monitor to observe what happened to output/SP...")

        # 3. Write target SP = current working SP + step (both SP1 and SP2)
        target_raw = wsp_raw + int(round(SP_STEP_C * RESOLUTION))
        target_c   = to_c(target_raw)
        ok1, e1 = write_reg(ser, R_SP,  target_raw)
        time.sleep(0.02)
        ok2, e2 = write_reg(ser, R_SP2, target_raw)
        print(f"  SP target → {target_c:.1f}°C (raw {target_raw}):")
        print(f"    R_SP  (0x0018): {'✓' if ok1 else f'✗ {e1}'}")
        print(f"    R_SP2 (0x0019): {'✓' if ok2 else f'✗ {e2}'}")

        t_step = time.time()

        # ── MONITOR ───────────────────────────────────────────────────────────
        sep(f"MONITOR — {MONITOR_SEC} s of working SP and output samples")
        print(f"  {'t(s)':>5}  {'WorkSP':>8}  {'Δ WorkSP':>9}  {'Output%':>8}  Note")
        print(f"  {'─'*5}  {'─'*8}  {'─'*9}  {'─'*8}  {'─'*20}")

        samples = []
        prev_wsp = wsp_raw
        t0 = time.time()

        for i in range(int(MONITOR_SEC / SAMPLE_INTERVAL) + 1):
            t_elapsed = time.time() - t0
            wsp, _ = read_reg(ser, R_SP_RO, signed=True)
            op,  _ = read_reg(ser, R_OP,    signed=True)

            wsp_c   = to_c(wsp) if wsp is not None else None
            op_pct  = op / 10.0 if op is not None else None
            delta   = (wsp - prev_wsp) / RESOLUTION if (wsp is not None and prev_wsp is not None) else None

            note = ""
            if i == 0:
                note = "← start"
            elif wsp is not None and abs(wsp - target_raw) < RESOLUTION:
                note = "← reached target SP"

            wsp_str   = f"{wsp_c:.1f}°C"   if wsp_c  is not None else "  ?"
            delta_str = f"{delta:+.2f}°C"  if delta  is not None else "      ?"
            op_str    = f"{op_pct:.1f}%"   if op_pct is not None else "  ?"
            print(f"  {t_elapsed:>5.1f}  {wsp_str:>8}  {delta_str:>9}  {op_str:>8}  {note}")

            if wsp is not None:
                samples.append((t_elapsed, wsp, op))
                prev_wsp = wsp

            if wsp is not None and abs(wsp - target_raw) < RESOLUTION:
                print(f"\n  Working SP reached target after {t_elapsed:.0f} s — stopping early.")
                break

            # Wait until next sample interval (account for time taken by reads)
            sleep_remaining = (SAMPLE_INTERVAL * (i + 1)) - (time.time() - t0)
            if sleep_remaining > 0:
                time.sleep(sleep_remaining)

        # ── VERDICT ───────────────────────────────────────────────────────────
        sep("VERDICT")
        if len(samples) < 2:
            print("  Not enough samples to analyse.")
        else:
            t_start, wsp_start, op_start = samples[0]
            t_end,   wsp_end,   op_end   = samples[-1]
            total_wsp_change = (wsp_end - wsp_start) / RESOLUTION  # °C
            total_time       = t_end - t_start                      # s
            op_change        = (op_end - (op_start or 0)) / 10.0   # %

            # Expected ramp distance over monitored time
            expected_change = RAMP_RATE_CMIN * total_time / 60.0   # °C

            print(f"  Working SP change : {total_wsp_change:+.1f} °C over {total_time:.0f} s")
            print(f"  Expected at {RAMP_RATE_CMIN:.0f}°C/min: ~{expected_change:.1f} °C")
            print(f"  Output % change   : {op_change:+.1f}%")
            print()

            # Check for instant SP jump (ramp not working)
            first_delta = (samples[1][1] - samples[0][1]) / RESOLUTION if len(samples) >= 2 else 0
            instant_jump = abs(first_delta) > SP_STEP_C * 0.8

            if instant_jump:
                print("  RESULT: Working SP jumped instantly to target.")
                print("  → The ramp rate write had NO effect on SP ramping.")
                if abs(op_change) > 5:
                    print(f"  → Output changed by {op_change:+.1f}% → 0x0020 may be affecting output directly")
                    print("     (possible: 0x0020 is output limit, not SP ramp rate)")
                else:
                    print("  → Output barely changed → device may be in instant-jump mode regardless.")
                print("\n  ACTION: 0x0020 / 0x0021 need to be verified against iTools browser.")

            elif abs(total_wsp_change) < 0.5:
                print("  RESULT: Working SP did NOT change.")
                print("  → Either the SP write failed OR the ramp+SP writes had no effect.")
                if abs(op_change) > 5:
                    print(f"  → But output changed by {op_change:+.1f}% → 0x0020 likely changes output directly")
                    print("     (possible: 0x0020 is output high limit or power level register)")
                    print("\n  ACTION: 0x0020 / 0x0021 are probably NOT the SP ramp registers.")
                else:
                    print("  → Output also unchanged → SP and ramp writes may both be silently ignored.")
                    print("\n  ACTION: Verify actual SP register and ramp register via iTools.")

            else:
                # SP changed gradually — check if rate matches
                actual_rate = total_wsp_change / (total_time / 60.0) if total_time > 0 else 0
                rate_match  = abs(actual_rate - RAMP_RATE_CMIN) / RAMP_RATE_CMIN < 0.35  # 35% tolerance

                if rate_match:
                    print(f"  RESULT: ✓ Working SP ramped at ~{actual_rate:.1f} °C/min")
                    print(f"          (target rate: {RAMP_RATE_CMIN:.1f} °C/min — within tolerance)")
                    print(f"  → 0x0020 / 0x0021 ARE the SP ramp rate registers.")
                    print(f"  → Output change ({op_change:+.1f}%) is the PID responding to the ramping SP.")
                    print(f"  → This is correct EPC behaviour — the EPC controls output, not our code.")
                else:
                    print(f"  RESULT: Working SP changed at ~{actual_rate:.1f} °C/min (expected {RAMP_RATE_CMIN:.1f})")
                    if actual_rate < RAMP_RATE_CMIN * 0.5:
                        print("  → SP ramped but slower than expected — ramp register may use different units.")
                    else:
                        print("  → SP ramped faster than expected — ramp register may use different scaling.")
                    print(f"  → Partial confirmation: ramping IS happening, but scaling may need adjustment.")

    finally:
        # ── RESTORE ───────────────────────────────────────────────────────────
        sep("RESTORE — returning device to original state")
        # Restore order: ramp off first so SP jump is instant, then SP, then mode
        restore_order = [
            (R_SP_RATE_U, "R_SP_RATE_U", False),
            (R_SP_RATE,   "R_SP_RATE",   False),
            (R_SP,        "R_SP",        True),
            (R_SP2,       "R_SP2",       True),
            (R_AUTO_MAN,  "R_AUTO_MAN",  False),
        ]
        for reg, label, signed in restore_order:
            if reg not in snap:
                continue
            val = snap[reg]
            ok, err = write_reg(ser, reg, val & 0xFFFF)
            time.sleep(0.05)
            rb, _ = read_reg(ser, reg, signed)
            match = rb == val if rb is not None else False
            if ok and match:
                if reg in (R_SP, R_SP2):
                    print(f"  ✓ {label} → {to_c(val):.1f}°C")
                elif reg == R_AUTO_MAN:
                    print(f"  ✓ {label} → {'Manual' if val else 'Auto'}")
                elif reg == R_SP_RATE:
                    units = {0:"off/instant", 1:"°C/s", 2:"°C/min", 3:"°C/h"}
                    u = snap.get(R_SP_RATE_U, 0)
                    print(f"  ✓ {label} → {val/10.0:.1f} ({units.get(u,'?')})")
                else:
                    print(f"  ✓ {label} → {val}")
            elif ok:
                print(f"  △ {label} write OK, readback={rb} ≠ {val} (may still apply)")
            else:
                print(f"  ✗ {label} FAILED: {err}")

        # Final state check
        time.sleep(0.5)
        wsp_f, _ = read_reg(ser, R_SP_RO, signed=True)
        op_f,  _ = read_reg(ser, R_OP,    signed=True)
        print(f"\n  Final: WorkSP={to_c(wsp_f):.1f}°C   Output={op_f/10.0 if op_f else '?'}%")
        ser.close()

    sep()
    print("  Done.")
    sep()

if __name__ == "__main__":
    main()
