"""
EPC3004 Ramp Rate Calibration
==============================
Tests multiple SP ramp rates and builds a calibration curve mapping
commanded °C/min → actual measured °C/min.

Procedure (per ramp step)
--------------------------
  1. Write ramp rate to 0x0020 / 0x0021
  2. Set target SP = current_working_SP + SP_STEP (default 25 °C)
  3. Sample working SP (0x0002) and PV (0x0001) every SAMPLE_INTERVAL s
     while working SP is still moving toward target
  4. Fit a linear slope (least squares) to the working SP vs time data
     collected during the ramp phase → actual ramp rate
  5. Wait for PV to stabilise (|PV - target| < STAB_BAND for STAB_SEC s)
  6. Repeat for the next commanded rate

Temperature ceiling: stops adding steps if next target would exceed MAX_TEMP.

At the end: prints a calibration table + ASCII plot, and leaves the device
at its final settled temperature (does NOT come back down).
Ramp rate is restored to original value.

Usage (close GUI first):
  python diagnostics/epc3004_ramp_cal.py [port] [slave_id] [baudrate]
  python diagnostics/epc3004_ramp_cal.py COM13 1 19200

Edit the CONFIG block below to change ramp rates, step size, ceiling, etc.
"""

import sys, struct, time, serial, io, math
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM13"
SLAVE_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BAUDRATE = int(sys.argv[3]) if len(sys.argv) > 3 else 19200
TIMEOUT  = 0.5

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG — edit these
# ═══════════════════════════════════════════════════════════════════════════════
RAMP_RATES_CMIN = [5, 10, 20, 50, 100]  # commanded ramp rates to test (°C/min)
SP_STEP_C       = 25.0                   # SP step per test (°C)
MAX_TEMP_C      = 600.0                  # stop adding steps above this (°C)
SAMPLE_INTERVAL = 2.0                    # seconds between samples
STAB_BAND_C     = 1.0                    # PV within ±this °C = stable
STAB_SEC        = 20                     # seconds PV must stay in band → settled
STAB_TIMEOUT    = 600                    # max seconds to wait for stabilisation
RAMP_TIMEOUT    = 300                    # max seconds for working SP to reach target
# ═══════════════════════════════════════════════════════════════════════════════

RESOLUTION  = 10
R_PV        = 0x0001
R_SP_RO     = 0x0002
R_OP        = 0x0004
R_SP        = 0x0018
R_SP2       = 0x0019
R_SP_RATE   = 0x0020
R_SP_RATE_U = 0x0021
R_AUTO_MAN  = 0x0112

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

def linfit(xs, ys):
    """Least-squares linear fit. Returns (slope, intercept, r²)."""
    n = len(xs)
    if n < 2: return None, None, None
    mx = sum(xs) / n; my = sum(ys) / n
    ssxx = sum((x - mx)**2 for x in xs)
    ssxy = sum((x - mx)*(y - my) for x, y in zip(xs, ys))
    if ssxx == 0: return None, None, None
    slope = ssxy / ssxx
    intercept = my - slope * mx
    sstot = sum((y - my)**2 for y in ys)
    ssres = sum((y - (slope*x + intercept))**2 for x, y in zip(xs, ys))
    r2 = 1 - ssres/sstot if sstot > 0 else 1.0
    return slope, intercept, r2

def sep(t=""):
    if t:
        print(f"\n{'═'*65}\n  {t}\n{'═'*65}")
    else:
        print('─'*65)

def progress_bar(fraction, width=30):
    filled = int(fraction * width)
    return '[' + '█' * filled + '░' * (width - filled) + ']'

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  EPC3004 Ramp Rate Calibration")
    print(f"  Port={PORT}  SlaveID={SLAVE_ID}  Baud={BAUDRATE}")
    print(f"  Rates to test: {RAMP_RATES_CMIN} °C/min")
    print(f"  SP step: {SP_STEP_C} °C   Ceiling: {MAX_TEMP_C} °C")
    print("=" * 65)

    try:
        ser = serial.Serial(PORT, BAUDRATE, bytesize=8, parity='N',
                            stopbits=1, timeout=TIMEOUT)
    except serial.SerialException as e:
        print(f"\n  FATAL: cannot open {PORT}: {e}"); sys.exit(1)

    # Snapshot ramp registers only (not SP — we're going up and staying there)
    snap_rate  = None
    snap_rateu = None

    results = []   # list of (commanded_rate, actual_rate, r2, target_c)

    try:
        # ── Read baseline ─────────────────────────────────────────────────────
        sep("Baseline")
        pv_raw,  _ = read_reg(ser, R_PV,    signed=True)
        wsp_raw, _ = read_reg(ser, R_SP_RO, signed=True)
        op_raw,  _ = read_reg(ser, R_OP,    signed=True)
        rate_raw, _ = read_reg(ser, R_SP_RATE,   signed=False)
        rateu_raw, _ = read_reg(ser, R_SP_RATE_U, signed=False)
        snap_rate  = rate_raw
        snap_rateu = rateu_raw
        units_map = {0:"off", 1:"°C/s", 2:"°C/min", 3:"°C/h"}
        print(f"  PV        = {to_c(pv_raw):.1f} °C")
        print(f"  WorkSP    = {to_c(wsp_raw):.1f} °C")
        print(f"  Output    = {op_raw/10.0 if op_raw else '?'} %")
        print(f"  SP_RATE   = {rate_raw} raw ({rate_raw/10.0 if rate_raw else '?'} °C/unit)")
        print(f"  SP_RATE_U = {rateu_raw} ({units_map.get(rateu_raw,'?')})")

        if wsp_raw is None or pv_raw is None:
            print("  FATAL: cannot read device — aborting"); return

        # Ensure Auto mode
        write_reg(ser, R_AUTO_MAN, 0)
        time.sleep(0.05)

        current_wsp_raw = wsp_raw

        # ── Test each ramp rate ───────────────────────────────────────────────
        for step_idx, cmd_rate in enumerate(RAMP_RATES_CMIN):
            target_c   = to_c(current_wsp_raw) + SP_STEP_C
            target_raw = current_wsp_raw + int(round(SP_STEP_C * RESOLUTION))

            if target_c > MAX_TEMP_C:
                print(f"\n  Next target {target_c:.1f}°C would exceed ceiling {MAX_TEMP_C}°C — stopping.")
                break

            sep(f"Step {step_idx+1}/{len(RAMP_RATES_CMIN)}: "
                f"commanded {cmd_rate} °C/min → target {target_c:.1f} °C")

            # 1. Write ramp rate
            rate_raw_w = int(round(cmd_rate * 10))
            ok_r, e_r = write_reg(ser, R_SP_RATE,   rate_raw_w)
            time.sleep(0.02)
            ok_u, e_u = write_reg(ser, R_SP_RATE_U, 2)   # per/min
            time.sleep(0.05)
            rb_r, _ = read_reg(ser, R_SP_RATE,   signed=False)
            rb_u, _ = read_reg(ser, R_SP_RATE_U, signed=False)
            print(f"  R_SP_RATE  → {rate_raw_w} raw ({cmd_rate} °C/min): "
                  f"{'✓' if ok_r else f'✗ {e_r}'}  rb={rb_r}")
            print(f"  R_SP_RATE_U → 2 (°C/min): "
                  f"{'✓' if ok_u else f'✗ {e_u}'}  rb={rb_u}")

            # 2. Write target SP (both SP1 + SP2)
            ok1, e1 = write_reg(ser, R_SP,  target_raw)
            time.sleep(0.02)
            ok2, e2 = write_reg(ser, R_SP2, target_raw)
            time.sleep(0.05)
            print(f"  SP → {target_c:.1f}°C (raw {target_raw}): "
                  f"R_SP={'✓' if ok1 else f'✗ {e1}'}  R_SP2={'✓' if ok2 else f'✗ {e2}'}")

            t_ramp_start = time.time()

            # 3. Monitor ramp phase — collect (t, wsp_c) samples until working SP reaches target
            print(f"\n  {'t(s)':>5}  {'WorkSP':>8}  {'PV':>8}  {'Out%':>6}  {'ΔWorkSP/s':>10}")
            print(f"  {'─'*5}  {'─'*8}  {'─'*8}  {'─'*6}  {'─'*10}")

            ramp_ts   = []   # times during ramp (s)
            ramp_wsps = []   # working SP values during ramp (°C)
            prev_wsp_c = to_c(current_wsp_raw)
            ramp_done  = False

            deadline = t_ramp_start + RAMP_TIMEOUT
            i = 0
            while time.time() < deadline:
                t_now = time.time() - t_ramp_start
                wsp_r, _ = read_reg(ser, R_SP_RO, signed=True)
                pv_r,  _ = read_reg(ser, R_PV,    signed=True)
                op_r,  _ = read_reg(ser, R_OP,    signed=True)

                wsp_c  = to_c(wsp_r)  if wsp_r  is not None else None
                pv_c   = to_c(pv_r)   if pv_r   is not None else None
                op_pct = op_r / 10.0  if op_r   is not None else None

                if wsp_c is not None:
                    rate_now = (wsp_c - prev_wsp_c) / SAMPLE_INTERVAL * 60 if i > 0 else 0
                    rate_str = f"{rate_now:+.1f} °C/min"
                    ramp_ts.append(t_now)
                    ramp_wsps.append(wsp_c)
                    prev_wsp_c = wsp_c
                else:
                    rate_str = "?"

                wsp_str = f"{wsp_c:.1f}°C" if wsp_c is not None else "?"
                pv_str  = f"{pv_c:.1f}°C"  if pv_c  is not None else "?"
                op_str  = f"{op_pct:.1f}%"  if op_pct is not None else "?"
                print(f"  {t_now:>5.1f}  {wsp_str:>8}  {pv_str:>8}  {op_str:>6}  {rate_str:>10}")

                # Done when working SP reaches target
                if wsp_c is not None and wsp_c >= target_c - 0.5:
                    print(f"  → Working SP reached {target_c:.1f}°C in {t_now:.0f} s")
                    ramp_done = True
                    break

                i += 1
                sleep_rem = SAMPLE_INTERVAL - (time.time() - (t_ramp_start + t_now))
                if sleep_rem > 0:
                    time.sleep(sleep_rem)
                else:
                    time.sleep(0.1)

            if not ramp_done:
                print(f"  ⚠ Ramp timeout ({RAMP_TIMEOUT}s) — working SP may not have reached target.")

            # 4. Fit slope to working SP samples (exclude first sample — device latency)
            actual_rate = None
            r2_val      = None
            if len(ramp_wsps) >= 4:
                # Use samples from index 1 onward (skip first which is t=0 baseline)
                fit_ts   = ramp_ts[1:]
                fit_wsps = ramp_wsps[1:]
                slope, intercept, r2 = linfit(fit_ts, fit_wsps)
                if slope is not None:
                    actual_rate = slope * 60.0   # convert °C/s → °C/min
                    r2_val = r2
                    print(f"\n  Linear fit: slope={slope*60:.2f} °C/min  r²={r2:.4f}")

            results.append((cmd_rate, actual_rate, r2_val, target_c))

            # 5. Wait for PV to stabilise at target
            print(f"\n  Waiting for PV to stabilise at {target_c:.1f}°C "
                  f"(±{STAB_BAND_C}°C for {STAB_SEC}s, timeout {STAB_TIMEOUT}s)...")
            stab_in_band_since = None
            stab_start = time.time()
            stab_deadline = stab_start + STAB_TIMEOUT
            stab_i = 0
            while time.time() < stab_deadline:
                t_stab = time.time() - stab_start
                pv_r, _ = read_reg(ser, R_PV,    signed=True)
                wsp_r, _= read_reg(ser, R_SP_RO, signed=True)
                op_r, _ = read_reg(ser, R_OP,    signed=True)
                pv_c  = to_c(pv_r)   if pv_r  is not None else None
                wsp_c = to_c(wsp_r)  if wsp_r is not None else None
                op_pct= op_r / 10.0  if op_r  is not None else None

                in_band = pv_c is not None and abs(pv_c - target_c) <= STAB_BAND_C
                if in_band and stab_in_band_since is None:
                    stab_in_band_since = time.time()
                elif not in_band:
                    stab_in_band_since = None

                time_in_band = time.time() - stab_in_band_since if stab_in_band_since else 0
                bar = progress_bar(time_in_band / STAB_SEC)
                pv_str  = f"{pv_c:.1f}°C"  if pv_c  is not None else "?"
                wsp_str = f"{wsp_c:.1f}°C" if wsp_c is not None else "?"
                op_str  = f"{op_pct:.1f}%" if op_pct is not None else "?"
                print(f"  [{t_stab:>5.0f}s] PV={pv_str}  SP={wsp_str}  Out={op_str}  "
                      f"stable {bar} {time_in_band:.0f}/{STAB_SEC}s", end='\r')

                if stab_in_band_since and time_in_band >= STAB_SEC:
                    print()  # newline after \r
                    print(f"  ✓ Stable at {pv_c:.1f}°C after {t_stab:.0f} s")
                    break

                stab_i += 1
                time.sleep(SAMPLE_INTERVAL)
            else:
                print()
                pv_r2, _ = read_reg(ser, R_PV, signed=True)
                print(f"  ⚠ Stabilisation timeout — PV={to_c(pv_r2):.1f}°C  (continuing anyway)")

            # Update current working SP for next step
            wsp_now, _ = read_reg(ser, R_SP_RO, signed=True)
            current_wsp_raw = wsp_now if wsp_now is not None else target_raw

        # ── Calibration table + ASCII plot ────────────────────────────────────
        sep("Calibration Results")
        print(f"  {'Commanded':>12}  {'Measured':>12}  {'Ratio':>8}  {'R²':>6}  {'At target'}")
        print(f"  {'(°C/min)':>12}  {'(°C/min)':>12}  {'':>8}  {'':>6}")
        print(f"  {'─'*12}  {'─'*12}  {'─'*8}  {'─'*6}  {'─'*12}")
        valid = []
        for cmd, actual, r2, tgt in results:
            if actual is not None:
                ratio = actual / cmd if cmd else 0
                r2_str = f"{r2:.4f}" if r2 is not None else "  n/a"
                print(f"  {cmd:>12.1f}  {actual:>12.2f}  {ratio:>8.3f}  {r2_str:>6}  {tgt:.1f}°C")
                valid.append((cmd, actual))
            else:
                print(f"  {cmd:>12.1f}  {'no data':>12}  {'':>8}  {'':>6}  {tgt:.1f}°C")

        if len(valid) >= 2:
            # Linear calibration fit: actual = m * commanded + b
            xs = [v[0] for v in valid]
            ys = [v[1] for v in valid]
            m, b, r2_cal = linfit(xs, ys)
            print(f"\n  Calibration fit (actual = m × commanded + b):")
            print(f"    m = {m:.4f}   b = {b:.4f}   R² = {r2_cal:.4f}")
            if abs(b) < 1 and 0.85 < m < 1.15:
                print(f"    → Excellent linearity. Device ramp tracks commanded rate well.")
            elif 0.7 < m < 1.3:
                print(f"    → Acceptable linearity but some deviation (slope {m:.2f}).")
            else:
                print(f"    → Significant deviation (slope {m:.2f}). "
                      f"0x0020 may NOT be the SP ramp register.")

            # ASCII scatter plot
            sep("ASCII Calibration Curve (commanded vs measured °C/min)")
            max_x = max(xs); max_y = max(max(ys), max_x)
            H, W = 15, 50
            grid = [[' '] * W for _ in range(H)]
            for cx, cy in valid:
                px = int((cx / max_x) * (W - 1))
                py = H - 1 - int((cy / max_y) * (H - 1))
                if 0 <= px < W and 0 <= py < H:
                    grid[py][px] = '●'
            # Ideal 1:1 line
            for i in range(W):
                cx = (i / (W-1)) * max_x
                cy = cx   # 1:1
                py = H - 1 - int((cy / max_y) * (H - 1))
                if 0 <= py < H and grid[py][i] == ' ':
                    grid[py][i] = '·'
            print(f"  {'measured':>8} ↑")
            for row_i, row in enumerate(grid):
                y_val = max_y * (H - 1 - row_i) / (H - 1)
                print(f"  {y_val:>7.0f} │ {''.join(row)}")
            print(f"  {'':>7}  └{'─'*W}→ commanded")
            print(f"  {'':>8}  {0:>4}{max_x/2:>{W//2}.0f}{max_x:{W//2}.0f}")
            print(f"\n  ● = measured point   · = ideal 1:1 line")

        elif len(valid) == 1:
            print(f"\n  Only 1 valid result — cannot fit calibration curve.")
        else:
            print(f"\n  No valid results collected.")

    finally:
        # ── Restore ramp registers only (NOT SP — device stays at final temp) ─
        sep("Restore")
        print("  Restoring original ramp rate (SP left at final temperature).")
        if snap_rate is not None:
            ok, err = write_reg(ser, R_SP_RATE, snap_rate)
            time.sleep(0.02)
            ok2, err2 = write_reg(ser, R_SP_RATE_U, snap_rateu if snap_rateu is not None else 0)
            units_map = {0:"off", 1:"°C/s", 2:"°C/min", 3:"°C/h"}
            u = snap_rateu if snap_rateu is not None else 0
            print(f"  R_SP_RATE  → {snap_rate} ({snap_rate/10.0:.1f} {units_map.get(u,'?')}): "
                  f"{'✓' if ok else f'✗ {err}'}")
            print(f"  R_SP_RATE_U→ {u} ({units_map.get(u,'?')}): "
                  f"{'✓' if ok2 else f'✗ {err2}'}")

        pv_f, _ = read_reg(ser, R_PV, signed=True)
        sp_f, _ = read_reg(ser, R_SP_RO, signed=True)
        print(f"\n  Final state: PV={to_c(pv_f):.1f}°C   WorkSP={to_c(sp_f):.1f}°C")
        ser.close()

    sep()
    print("  Calibration complete.")
    sep()

if __name__ == "__main__":
    main()
