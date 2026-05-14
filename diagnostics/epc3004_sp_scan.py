"""
EPC3004 SP Register Wide Scan
==============================
Purpose: Find the writable register that actually controls the device SP.

Background
----------
- 0x0018 / 0x0019 confirmed NOT the active SP (both read 0, device at 200°C)
- 0x0000 returns 0x8000 (not connected), not writable
- 0x0002 / 0x0005 = 2000 (200°C) but read-only
- Need to find which register IS the live writable SP

Strategy
--------
A. Scan blocks 0x0000-0x00FF and 0x0100-0x01FF for any register = 2000 (±50).
   The writable SP must currently hold 2000 (the active SP raw value).
B. Check programmer / profile state (is a ramp-dwell program running?).
   If a program is running it overrides local SP writes.
C. Check CommTab 0x3C00-0x3C0F (Comms Indirection Table, Eurotherm custom maps).
D. Attempt write-test on all registers that currently read 2000.
   Write SP+10°C (raw +100), wait 0.5s, check if working SP (0x0002) changes.
   Restore each register immediately after test.

Usage:
  python diagnostics/epc3004_sp_scan.py [port] [slave_id] [baudrate]
  python diagnostics/epc3004_sp_scan.py COM13 1 19200
"""

import sys, struct, time, serial, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM13"
SLAVE_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BAUDRATE = int(sys.argv[3]) if len(sys.argv) > 3 else 19200
TIMEOUT  = 0.5
RESOLUTION = 10

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
        msgs = {1: "Illegal function", 2: "Illegal address", 3: "Illegal data/value", 4: "Device error"}
        return None, f"exc 0x{code:02X} ({msgs.get(code,'?')})"
    if len(resp) < expected:
        return None, f"short {len(resp)}/{expected}"
    if _crc16(resp[:expected-2]) != struct.unpack("<H", resp[expected-2:expected])[0]:
        return None, "CRC"
    return resp[:expected], None

def read_reg(ser, reg, signed=False):
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

def scan_block(ser, start, count, label):
    """Scan 'count' registers in chunks of 16; return dict reg->raw for readable ones."""
    print(f"\n  Scanning {label} (0x{start:04X}–0x{start+count-1:04X})...")
    result = {}
    chunk = 16
    for base in range(start, start + count, chunk):
        n = min(chunk, start + count - base)
        req = _frame_read(base, n)
        resp, err = transact(ser, req, 3 + n*2 + 2)
        if err:
            # Try one-by-one fallback for this chunk
            for i in range(n):
                r = base + i
                v, e = read_reg(ser, r, signed=False)
                if e is None or e.startswith("exc 0x02"):
                    pass
                else:
                    result[r] = v if v is not None else None
            continue
        words = [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(n)]
        for i, w in enumerate(words):
            result[base + i] = w
    return result

def sep(t=""):
    if t:
        print(f"\n{'─'*65}\n  {t}\n{'─'*65}")
    else:
        print('─'*65)

def raw_to_c(raw):
    v = raw if raw < 0x8000 else raw - 0x10000
    return v / RESOLUTION

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  EPC3004 SP Register Wide Scan")
    print(f"  Port={PORT}  SlaveID={SLAVE_ID}  Baud={BAUDRATE}")
    print("=" * 65)

    try:
        ser = serial.Serial(PORT, BAUDRATE, bytesize=8, parity='N',
                            stopbits=1, timeout=TIMEOUT)
    except serial.SerialException as e:
        print(f"\n  FATAL: cannot open {PORT}: {e}\n"); sys.exit(1)

    try:
        # Baseline readings
        working_sp_raw, _ = read_reg(ser, 0x0002, signed=False)
        pv_raw, _         = read_reg(ser, 0x0001, signed=False)
        working_sp_c = raw_to_c(working_sp_raw) if working_sp_raw is not None else None
        pv_c = raw_to_c(pv_raw) if pv_raw is not None else None
        print(f"\n  Baseline: PV = {pv_c}°C   Working SP (0x0002) = {working_sp_c}°C")
        print(f"  Target: find register that reads {working_sp_raw} (= {working_sp_c}°C)\n")

        target = working_sp_raw if working_sp_raw else 2000
        tolerance = 100   # ±10°C with factor-10

        # ── A. Scan 0x0000–0x01FF for registers containing target value ───────
        sep("A. Scan 0x0000-0x00FF")
        block1 = scan_block(ser, 0x0000, 0x100, "block 1")

        sep("A2. Scan 0x0100-0x01FF")
        block2 = scan_block(ser, 0x0100, 0x100, "block 2")

        all_regs = {**block1, **block2}

        # Show registers that contain the SP value
        hits = {r: v for r, v in all_regs.items()
                if v is not None and abs(v - target) <= tolerance}
        # Also show unsigned hits where signed interpretation matches
        for r, v in all_regs.items():
            signed_v = v - 0x10000 if v >= 0x8000 else v
            if r not in hits and abs(signed_v - (target - 0x10000 if target >= 0x8000 else target)) <= tolerance:
                hits[r] = v

        print(f"\n  Registers containing ~{working_sp_c}°C (raw {target} ±{tolerance}):")
        known = {0x0001: "PV", 0x0002: "Working SP (R_SP_RO, read-only)",
                 0x0004: "Output % (read-only)", 0x0005: "R_WSP",
                 0x0018: "R_SP_TARG (prev written, was SP1)", 0x0019: "R_SP2_TARG",
                 0x0020: "R_SP_RATE", 0x0021: "R_SP_RATE_U",
                 0x0112: "R_AUTO_MAN", 0x0111: "R_STATUS"}
        if hits:
            for r in sorted(hits):
                v = hits[r]
                note = known.get(r, "")
                print(f"    0x{r:04X}  iTools {r+1:>4}  raw={v:>6}  ({raw_to_c(v):>8.1f}°C)  {note}")
        else:
            print("    None found in 0x0000-0x01FF")

        # Also show notable non-zero registers
        print(f"\n  All non-zero/non-sentinel readable registers (sample):")
        notable = {r: v for r, v in all_regs.items()
                   if v is not None and v != 0 and v != 0x8000 and r not in hits}
        for r in sorted(notable)[:40]:
            v = notable[r]
            note = known.get(r, "")
            print(f"    0x{r:04X}  iTools {r+1:>4}  raw={v:>6}  ({raw_to_c(v):>8.1f}°C)  {note}")

        # ── B. Programmer / profile state ─────────────────────────────────────
        sep("B. Programmer / profile state registers")
        prog_regs = [
            (0x00C8, "Programmer State (0=Reset,1=Run,2=Hold,3=Complete)"),
            (0x00C9, "Current Program Number"),
            (0x00CA, "Current Segment Number"),
            (0x00CB, "Segment type"),
            (0x00CC, "Target SP of current segment"),
            (0x00CD, "Segment time remaining"),
            (0x0190, "Programmer State alt (0x0190)"),
            (0x0191, "Program number alt"),
            (0x0192, "Segment number alt"),
        ]
        for reg, desc in prog_regs:
            v, err = read_reg(ser, reg, signed=False)
            if err:
                print(f"  0x{reg:04X}: {err:30s}  {desc}")
            else:
                print(f"  0x{reg:04X}: raw={v:>6}  0x{v:04X}   {desc}")

        # ── C. CommTab (Comms Indirection Table) ──────────────────────────────
        sep("C. CommTab 0x3C00-0x3C1F (Comms Indirection Table)")
        try:
            ct_block, ct_err = {}, None
            for base in range(0x3C00, 0x3C20, 16):
                req = _frame_read(base, 16)
                resp, err = transact(ser, req, 3 + 32 + 2)
                if err:
                    print(f"  0x{base:04X}-0x{base+15:04X}: {err}")
                    continue
                words = [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(16)]
                for i, w in enumerate(words):
                    r = base + i
                    if w != 0 and w != 0x8000:
                        print(f"  0x{r:04X}: raw={w:>6}  0x{w:04X}  ({raw_to_c(w):.1f}°C equiv)")
                    else:
                        print(f"  0x{r:04X}: raw={w:>6}  0x{w:04X}")
        except Exception as e:
            print(f"  CommTab scan error: {e}")

        # ── D. Write-test hits from section A ─────────────────────────────────
        sep("D. Write test on candidate registers")
        # Only test registers not already confirmed read-only
        ro_confirmed = {0x0001, 0x0002, 0x0004, 0x0005}
        candidates = [r for r in sorted(hits) if r not in ro_confirmed]
        if not candidates:
            print("  No new candidates from section A scan.")
            print("  Trying 0x0002 write test anyway (confirm read-only):")
            candidates = [0x0002]

        test_offset = int(10 * RESOLUTION)  # +10°C

        for reg in candidates:
            orig_v, e = read_reg(ser, reg, signed=False)
            if e:
                print(f"\n  0x{reg:04X}: cannot read ({e}), skipping")
                continue
            test_v = (orig_v + test_offset) & 0xFFFF
            print(f"\n  Testing 0x{reg:04X} (iTools {reg+1}):")
            print(f"    Before: 0x{reg:04X}={orig_v} ({raw_to_c(orig_v):.1f}°C)  "
                  f"WorkSP(0x0002)={working_sp_raw} ({working_sp_c}°C)")
            ok, err = write_reg(ser, reg, test_v)
            if not ok:
                print(f"    Write {test_v}: ✗ FAILED — {err}")
                continue
            print(f"    Write {test_v} ({raw_to_c(test_v):.1f}°C): ✓ ACK'd")
            time.sleep(0.50)
            # Read back both the register and the working SP
            rb_reg, _ = read_reg(ser, reg, signed=False)
            rb_wsp, _ = read_reg(ser, 0x0002, signed=False)
            print(f"    After:  0x{reg:04X}={rb_reg} ({raw_to_c(rb_reg) if rb_reg else '?'}°C)  "
                  f"WorkSP(0x0002)={rb_wsp} ({raw_to_c(rb_wsp) if rb_wsp else '?'}°C)")
            if rb_wsp is not None and rb_reg is not None and abs(rb_wsp - test_v) <= RESOLUTION:
                print(f"    *** WorkSP CHANGED → 0x{reg:04X} IS THE LIVE SP REGISTER ***")
            elif rb_reg is not None and abs(rb_reg - test_v) <= RESOLUTION:
                print(f"    Write latched in register but WorkSP unchanged — not the active SP")
            else:
                print(f"    Write ACK'd but register value unchanged — silently ignored")
            # Restore
            write_reg(ser, reg, orig_v)
            time.sleep(0.30)
            rb2, _ = read_reg(ser, reg, signed=False)
            print(f"    Restored: 0x{reg:04X}={rb2}")

    finally:
        ser.close()

    sep()
    print("  Scan complete.")
    print("  Key: look for '*** WorkSP CHANGED ***' in section D.")
    print("  If programmer registers (B) show State=1 (Run), the device")
    print("  is in programmer mode and local SP writes will be ignored.")
    sep()

if __name__ == "__main__":
    main()
