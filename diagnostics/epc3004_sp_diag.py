"""
EPC3004 Setpoint Register Diagnostic
=====================================
Purpose: Find which register is the LIVE active setpoint that the device
         actually PID-controls to.

Background
----------
Writes to R_SP (0x0018) and R_SP2 (0x0019) are ACK'd but the device
keeps controlling to the same temperature.  Per the EPC3000 manual the
SCADA address table starts at 0x0000.  Standard Eurotherm mapping puts:

  0x0000  — SP1 (the writable live setpoint)
  0x0001  — PV  (read-only)
  0x0002  — Working Setpoint (read-only; tracks ramp output)

This script:
  A. Reads a block 0x0000–0x0009 so we can see all key values at once.
  B. Checks whether 0x0000 reads as the current device SP (~200°C).
  C. If 0x0000 looks like an SP, tries writing SP+5°C to it and reads
     back after 0.5 s to see if the device responded.
  D. Scans for a "Setpoint Source" register (controls whether the
     device follows Local SP, Programmer, Remote, or Comms SP).
  E. Restores whatever was changed.

Usage (close GUI first — exclusive serial port):
  python diagnostics/epc3004_sp_diag.py [port] [slave_id] [baudrate]
  python diagnostics/epc3004_sp_diag.py COM13 1 19200
"""

import sys, struct, time, serial, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM13"
SLAVE_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BAUDRATE = int(sys.argv[3]) if len(sys.argv) > 3 else 19200
TIMEOUT  = 0.5
RESOLUTION = 10   # 0.1 °C resolution confirmed

# ── Known registers ───────────────────────────────────────────────────────────
R_PV        = 0x0001
R_SP_RO     = 0x0002   # Working SP (read-only, follows ramp)
R_SP_TARG   = 0x0018   # SP1 target (writable but not the live control SP?)
R_SP2_TARG  = 0x0019   # SP2 target (writable)
R_AUTO_MAN  = 0x0112   # 0=Auto, 1=Manual

# Candidate for setpoint source register (controls SP source selection)
# Standard Eurotherm: 0x0271 or similar; we'll scan for it.
SP_SOURCE_CANDIDATES = [
    0x0271, 0x0272, 0x001A, 0x001B, 0x001C, 0x001D, 0x0023, 0x0024,
    0x007A, 0x007B,
]

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
    time.sleep(0.02)
    resp = ser.read(expected)
    if not resp:
        return None, "no response (timeout)"
    if len(resp) >= 3 and (resp[1] & 0x80):
        code = resp[2]
        msgs = {1: "Illegal function", 2: "Illegal address", 3: "Illegal data/value", 4: "Device error"}
        return None, f"Modbus exception 0x{code:02X} ({msgs.get(code, 'unknown')})"
    if len(resp) < expected:
        return None, f"short response {len(resp)}/{expected}"
    if _crc16(resp[:expected-2]) != struct.unpack("<H", resp[expected-2:expected])[0]:
        return None, "CRC mismatch"
    return resp[:expected], None

def read_reg(ser, reg, signed=True):
    resp, err = transact(ser, _frame_read(reg, 1), 7)
    if err:
        return None, err
    raw = struct.unpack(">H", resp[3:5])[0]
    if signed and raw >= 0x8000:
        raw -= 0x10000
    return raw, None

def read_block(ser, start_reg, count):
    """Read 'count' registers starting at start_reg. Returns list of UINT16 or None on error."""
    resp, err = transact(ser, _frame_read(start_reg, count), 3 + count*2 + 2)
    if err:
        return None, err
    words = [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(count)]
    return words, None

def write_reg(ser, reg, value):
    resp, err = transact(ser, _frame_write(reg, value), 8)
    if err:
        return False, err
    return True, None

def sep(title=""):
    if title:
        print(f"\n{'─'*65}")
        print(f"  {title}")
        print('─'*65)
    else:
        print('─'*65)

def raw_to_c(raw):
    if raw >= 0x8000:
        raw -= 0x10000
    return raw / RESOLUTION

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  EPC3004 Setpoint Register Diagnostic")
    print(f"  Port={PORT}  SlaveID={SLAVE_ID}  Baud={BAUDRATE}")
    print("=" * 65)

    try:
        ser = serial.Serial(PORT, BAUDRATE, bytesize=8, parity='N',
                            stopbits=1, timeout=TIMEOUT)
    except serial.SerialException as e:
        print(f"\n  FATAL: cannot open {PORT}: {e}\n"); sys.exit(1)

    orig_0000 = None   # track if we write to 0x0000 for restore

    try:
        # ── A. Read block 0x0000–0x0009 ──────────────────────────────────────
        sep("A. Read block 0x0000–0x0009 (10 registers)")
        words, err = read_block(ser, 0x0000, 10)
        if err:
            print(f"  Block read failed: {err}")
            print("  Trying individual reads instead...")
            words = []
            for i in range(10):
                v, e = read_reg(ser, 0x0000 + i, signed=False)
                words.append((v, e))
            for i, (v, e) in enumerate(words):
                reg = 0x0000 + i
                if e:
                    print(f"  0x{reg:04X} (dec {reg+1:>3}): ERROR {e}")
                else:
                    c = raw_to_c(v)
                    print(f"  0x{reg:04X} (dec {reg+1:>3}): raw={v:>6}  ({c:>8.1f}°C equiv)  0x{v:04X}")
        else:
            print(f"  {'Reg':6}  {'iTools#':>7}  {'Raw':>6}  {'As°C':>8}  {'Hex':>6}  Note")
            known = {
                0: "← candidate live SP?",
                1: "← PV (confirmed)",
                2: "← Working SP / R_SP_RO (read-only)",
                3: "",
                4: "← Output % (read-only)",
                5: "← R_WSP",
            }
            for i, raw in enumerate(words):
                reg = 0x0000 + i
                c = raw_to_c(raw)
                note = known.get(i, "")
                print(f"  0x{reg:04X}  {reg+1:>7}  {raw:>6}  {c:>8.1f}°C  0x{raw:04X}  {note}")

        # ── B. Check if 0x0000 looks like the SP ─────────────────────────────
        sep("B. Check register 0x0000 (JBUS 1 — standard Eurotherm live SP)")
        val_0000, err = read_reg(ser, 0x0000, signed=True)
        if err:
            print(f"  Read 0x0000: ERROR {err}")
        else:
            c_0000 = val_0000 / RESOLUTION
            print(f"  0x0000 = raw {val_0000}  ({c_0000:.1f}°C)")
            pv, _ = read_reg(ser, R_PV, signed=True)
            pv_c = pv / RESOLUTION if pv is not None else None
            sp_ro, _ = read_reg(ser, R_SP_RO, signed=True)
            sp_ro_c = sp_ro / RESOLUTION if sp_ro is not None else None
            sp_targ, _ = read_reg(ser, R_SP_TARG, signed=True)
            sp_targ_c = sp_targ / RESOLUTION if sp_targ is not None else None
            print(f"  R_PV      (0x0001) = {pv_c}°C")
            print(f"  R_SP_RO   (0x0002) = {sp_ro_c}°C  (working/ramped SP, read-only)")
            print(f"  R_SP_TARG (0x0018) = {sp_targ_c}°C  (target SP1, writable)")
            print()
            if sp_ro_c is not None and abs(c_0000 - sp_ro_c) < 1.0:
                print(f"  >> 0x0000 ({c_0000:.1f}°C) matches R_SP_RO ({sp_ro_c:.1f}°C)")
                print(f"     → 0x0000 is likely the LIVE SETPOINT register.")
            else:
                print(f"  >> 0x0000 ({c_0000:.1f}°C) does NOT match R_SP_RO ({sp_ro_c}°C)")

        # ── C. Write test to 0x0000 ───────────────────────────────────────────
        sep("C. Write test — try writing SP+5°C to 0x0000")
        if val_0000 is None:
            print("  Skipping — could not read 0x0000")
        else:
            orig_0000 = val_0000
            test_val = val_0000 + int(5 * RESOLUTION)   # +5°C in raw
            test_c = test_val / RESOLUTION
            print(f"  Current 0x0000 = raw {val_0000} ({val_0000/RESOLUTION:.1f}°C)")
            print(f"  Writing test value = raw {test_val} ({test_c:.1f}°C)...")
            ok, err = write_reg(ser, 0x0000, test_val)
            if not ok:
                print(f"  Write result: ✗ FAILED — {err}")
                print(f"  → 0x0000 is NOT writable via Modbus")
                orig_0000 = None  # nothing to restore
            else:
                print(f"  Write result: ✓ ACK'd")
                print(f"  Waiting 0.5 s for device to latch...")
                time.sleep(0.50)
                # Read back from all SP registers
                rb_0000, _ = read_reg(ser, 0x0000, signed=True)
                rb_sp_ro, _ = read_reg(ser, R_SP_RO, signed=True)
                rb_sp_targ, _ = read_reg(ser, R_SP_TARG, signed=True)
                print(f"  Readback 0x0000    = {rb_0000}  ({rb_0000/RESOLUTION if rb_0000 is not None else '?'}°C)")
                print(f"  Readback R_SP_RO   = {rb_sp_ro}  ({rb_sp_ro/RESOLUTION if rb_sp_ro is not None else '?'}°C)")
                print(f"  Readback R_SP_TARG = {rb_sp_targ}  ({rb_sp_targ/RESOLUTION if rb_sp_targ is not None else '?'}°C)")
                print()
                if rb_0000 is not None and abs(rb_0000 - test_val) < RESOLUTION:
                    print(f"  >> 0x0000 readback = {rb_0000/RESOLUTION:.1f}°C ✓ write took effect!")
                    if rb_sp_ro is not None and abs(rb_sp_ro - test_val) < RESOLUTION:
                        print(f"  >> R_SP_RO also updated → THIS IS THE LIVE SP REGISTER")
                    else:
                        print(f"  >> R_SP_RO did NOT change → write taken but may need SP source change")
                else:
                    print(f"  >> 0x0000 readback mismatch — write may have been silently ignored")

        # ── D. Scan setpoint source candidates ───────────────────────────────
        sep("D. Scan setpoint source register candidates")
        print("  These registers control which SP source the device follows")
        print("  (Local SP1/SP2, Remote, Programmer, Comms).")
        print()
        for reg in SP_SOURCE_CANDIDATES:
            val, err = read_reg(ser, reg, signed=False)
            if err:
                print(f"  0x{reg:04X} (iTools {reg+1}): {err}")
            else:
                print(f"  0x{reg:04X} (iTools {reg+1}): raw={val}  0x{val:04X}")
        print()
        print("  If any of these = 3 or 4, the device is in Programmer or Remote SP mode.")
        print("  That would explain why writes to SP1/SP2 have no effect on control.")

        # ── E. Scan around 0x0018/0x0019 to see what actually responded ──────
        sep("E. Broader SP register scan: 0x0015–0x0025")
        block, err2 = read_block(ser, 0x0015, 17)
        if err2:
            print(f"  Block read failed: {err2}")
        else:
            known2 = {0x0018: "← R_SP_TARG SP1 (0x0018)", 0x0019: "← R_SP2_TARG SP2 (0x0019)",
                      0x0020: "← R_SP_RATE", 0x0021: "← R_SP_RATE_U"}
            for i, raw in enumerate(block):
                reg = 0x0015 + i
                c = raw_to_c(raw)
                note = known2.get(reg, "")
                print(f"  0x{reg:04X}  iTools {reg+1:>3}  raw={raw:>6}  {c:>8.1f}°C  {note}")

    finally:
        # ── Restore 0x0000 if we wrote to it ─────────────────────────────────
        if orig_0000 is not None:
            sep("RESTORE — writing original value back to 0x0000")
            ok, err = write_reg(ser, 0x0000, orig_0000 & 0xFFFF)
            time.sleep(0.3)
            if ok:
                rb, _ = read_reg(ser, 0x0000, signed=True)
                if rb is not None and abs(rb - orig_0000) < RESOLUTION:
                    print(f"  ✓ 0x0000 restored to raw {orig_0000} ({orig_0000/RESOLUTION:.1f}°C)")
                else:
                    print(f"  △ 0x0000 write ACK'd, readback={rb} (may still apply)")
            else:
                print(f"  ✗ Could not restore 0x0000: {err}")
                print(f"  !! Manually set SP back to {orig_0000/RESOLUTION:.1f}°C on the front panel !!")
        ser.close()

    sep()
    print("  Diagnostic complete.")
    print("  Look at section C readback: if 0x0000 write changed R_SP_RO,")
    print("  that is the register to use in apply_setpoint().")
    sep()

if __name__ == "__main__":
    main()
