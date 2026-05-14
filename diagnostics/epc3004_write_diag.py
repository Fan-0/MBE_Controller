"""
EPC3004 Write-Access Diagnostic
================================
Purpose: Determine which write operations work on the real device.

The main GUI error is:
  EPC3004 write_word reg=0x0004 val=0: Modbus FC=0x06 error=0x02 (Illegal address)

This happens because go_standby() writes 0 to R_OP (0x0004 = Working Output
Power), which is READ-ONLY.  This script:

  SNAPSHOT — saves current mode, SP, ramp-rate, ramp-unit before any writes
  A. Confirms all read registers.
  B. Tests R_AUTO_MAN (0x0112) write → go_operate / go_standby core.
  C. Probes candidate registers for writable Manual Output Demand:
       0x0003  (iTools 3 — common Eurotherm output-demand address)
       0x0004  (R_OP    — known RO; expected FAIL)
       0x0006  (adjacent/unknown)
       0x0065  (iTools 101, sometimes "Output demand" in EPC3000 family)
  D. Tests SP write: R_SP (0x0018) — writes back the same value, no temp change.
  E. Tests ramp-rate write: R_SP_RATE (0x0020) + R_SP_RATE_U (0x0021).
  F. RESTORE — returns device to exact pre-diagnostic state (mode, SP, ramp).
  G. Prints summary with the recommended driver fix.

Usage (close GUI first — exclusive serial port):
    python diagnostics/epc3004_write_diag.py [port] [slave_id] [baudrate]
    python diagnostics/epc3004_write_diag.py COM13 1 19200
"""

import sys
import struct
import time
import serial

# UTF-8 output so Unicode chars print on Windows consoles
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── CLI args ──────────────────────────────────────────────────────────────────
PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM13"
SLAVE_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BAUDRATE = int(sys.argv[3]) if len(sys.argv) > 3 else 19200
TIMEOUT  = 0.5   # generous for diagnostic

# ── EPC3004 register addresses (0-based Modbus frame addresses) ───────────────
R_PV        = 0x0001
R_SP_RO     = 0x0002   # Working SP  — read-only
R_SP        = 0x0018   # Target SP1  — writable (confirmed)
R_OP        = 0x0004   # Working Output % — read-only (causes the error)
R_WSP       = 0x0005
R_STATUS    = 0x0111
R_AUTO_MAN  = 0x0112
R_SP_RATE   = 0x0020
R_SP_RATE_U = 0x0021

STATUS_MANUAL_BIT = 0x0100   # bit 8 of R_STATUS
RESOLUTION = 10               # device configured for 0.1 °C resolution

# Candidate registers for "Manual Output Demand" (writable 0..1000 demand, raw×10=%)
OUTPUT_CANDIDATES = {
    0x0003: "iTools 3   — common Eurotherm manual output demand",
    0x0004: "R_OP (0x0004) — known read-only, expect FAIL",
    0x0006: "reg 0x0006 — adjacent/unknown",
    0x0065: "reg 0x0065 (iTools 101) — EPC3000 alternate output param",
}

# ── Low-level Modbus ──────────────────────────────────────────────────────────

def _crc16(data: bytes) -> int:
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
    """Returns (response_bytes, error_string).  error_string is None on success."""
    ser.reset_input_buffer()
    ser.write(req)
    time.sleep(0.02)
    resp = ser.read(expected)
    if not resp:
        return None, "no response (timeout)"
    if len(resp) >= 3 and (resp[1] & 0x80):
        code = resp[2]
        msgs = {1: "Illegal function", 2: "Illegal address",
                3: "Illegal data/value", 4: "Device error"}
        return None, f"Modbus exception 0x{code:02X} ({msgs.get(code, 'unknown')})"
    if len(resp) < expected:
        return None, f"short response {len(resp)}/{expected} bytes"
    crc_calc = _crc16(resp[:expected - 2])
    crc_recv = struct.unpack("<H", resp[expected - 2:expected])[0]
    if crc_calc != crc_recv:
        return None, f"CRC mismatch (calc 0x{crc_calc:04X}, recv 0x{crc_recv:04X})"
    return resp[:expected], None

def read_reg(ser, reg, signed=True):
    """Read one 16-bit register.  Returns (value, error_str)."""
    resp, err = transact(ser, _frame_read(reg, 1), 7)
    if err:
        return None, err
    raw = struct.unpack(">H", resp[3:5])[0]
    if signed and raw >= 0x8000:
        raw -= 0x10000
    return raw, None

def write_reg(ser, reg, value):
    """Write one 16-bit register.  Returns (ack_ok, error_str)."""
    resp, err = transact(ser, _frame_write(reg, value), 8)
    if err:
        return False, err
    echo_reg = struct.unpack(">H", resp[2:4])[0]
    if echo_reg != reg:
        return False, f"echo reg mismatch (sent 0x{reg:04X}, got 0x{echo_reg:04X})"
    return True, None

def sep(title=""):
    if title:
        print(f"\n{'─'*60}")
        print(f"  {title}")
        print('─'*60)
    else:
        print('─'*60)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  EPC3004 Write-Access Diagnostic")
    print(f"  Port={PORT}  SlaveID={SLAVE_ID}  Baud={BAUDRATE}")
    print("=" * 60)

    try:
        ser = serial.Serial(PORT, BAUDRATE, bytesize=8, parity='N',
                            stopbits=1, timeout=TIMEOUT)
    except serial.SerialException as e:
        print(f"\n  FATAL: cannot open {PORT}: {e}")
        sys.exit(1)

    results = {}   # key → (ok, note)

    # ── SNAPSHOT — capture full device state before any writes ───────────────
    sep("SNAPSHOT — saving device state before any writes")

    snap = {}
    snap_regs = [
        (R_AUTO_MAN, "R_AUTO_MAN (0x0112) mode",       False),
        (R_SP,       "R_SP       (0x0018) target SP",   True),
        (R_SP_RATE,  "R_SP_RATE  (0x0020) ramp rate",   False),
        (R_SP_RATE_U,"R_SP_RATE_U(0x0021) ramp unit",   False),
    ]
    snapshot_ok = True
    for reg, label, signed in snap_regs:
        val, err = read_reg(ser, reg, signed=signed)
        if err:
            print(f"  ✗ {label}  → {err}  (cannot snapshot — restore may be incomplete)")
            snapshot_ok = False
        else:
            snap[reg] = val
            if reg == R_AUTO_MAN:
                print(f"  ✓ {label}  = {'Manual' if val else 'Auto'} ({val})")
            elif reg == R_SP:
                print(f"  ✓ {label}  = {val} ({val / RESOLUTION:.1f} °C)")
            elif reg == R_SP_RATE:
                print(f"  ✓ {label}  = {val} ({val / 10.0:.1f} °C/unit)")
            elif reg == R_SP_RATE_U:
                units = {0: "off/instant", 1: "°C/s", 2: "°C/min", 3: "°C/h"}
                print(f"  ✓ {label}  = {val} ({units.get(val, '?')})")

    if not snapshot_ok:
        print("\n  WARNING: snapshot incomplete — some values could not be read.")
        print("  Restoration at end will only cover registers that were read.")

    def restore_state(label="Restoring device state"):
        """Write all snapshotted registers back to their saved values."""
        sep(f"F. {label}")
        any_fail = False
        # Restore SP and ramp first while in current mode, then restore mode last
        restore_order = [R_SP, R_SP_RATE, R_SP_RATE_U, R_AUTO_MAN]
        for reg in restore_order:
            if reg not in snap:
                continue
            val = snap[reg]
            ok, err = write_reg(ser, reg, val & 0xFFFF)
            time.sleep(0.05 if reg == R_AUTO_MAN else 0.02)
            if reg == R_AUTO_MAN:
                # Extra settle + verify
                time.sleep(0.05)
                rb, _ = read_reg(ser, reg, signed=False)
                confirmed = (rb == val) if rb is not None else False
            elif reg == R_SP:
                time.sleep(0.30)   # SP needs settle before readback
                rb, _ = read_reg(ser, reg, signed=True)
                confirmed = (rb == val) if rb is not None else False
            else:
                rb, _ = read_reg(ser, reg, signed=False)
                confirmed = (rb == val) if rb is not None else False

            if not ok:
                print(f"  ✗ reg 0x{reg:04X} restore FAILED: {err}")
                any_fail = True
            elif confirmed:
                if reg == R_AUTO_MAN:
                    print(f"  ✓ R_AUTO_MAN restored → {'Manual' if val else 'Auto'}")
                elif reg == R_SP:
                    print(f"  ✓ R_SP       restored → {val} ({val / RESOLUTION:.1f} °C)")
                elif reg == R_SP_RATE:
                    print(f"  ✓ R_SP_RATE  restored → {val} ({val / 10.0:.1f} °C/unit)")
                elif reg == R_SP_RATE_U:
                    units = {0: "off/instant", 1: "°C/s", 2: "°C/min", 3: "°C/h"}
                    print(f"  ✓ R_SP_RATE_U restored → {val} ({units.get(val, '?')})")
            else:
                print(f"  △ reg 0x{reg:04X} write ACK'd but readback={rb} ≠ {val} (may still apply)")

        if not any_fail:
            print("  Device state fully restored to pre-diagnostic values.")
        else:
            print("  Some registers could not be restored — check device HMI.")

    try:
        # ── A. Confirm reads ──────────────────────────────────────────────────
        sep("A. Confirm read registers")
        READ_MAP = [
            (R_PV,       "R_PV      (0x0001) process value", True),
            (R_SP_RO,    "R_SP_RO   (0x0002) working SP   ", True),
            (R_OP,       "R_OP      (0x0004) output %×10  ", True),
            (R_WSP,      "R_WSP     (0x0005) working SP2  ", True),
            (R_SP,       "R_SP      (0x0018) target SP1   ", True),
            (R_SP_RATE,  "R_SP_RATE (0x0020) ramp rate×10 ", False),
            (R_SP_RATE_U,"R_SP_RATE_U(0x0021) ramp unit   ", False),
            (R_STATUS,   "R_STATUS  (0x0111) status word  ", False),
            (R_AUTO_MAN, "R_AUTO_MAN(0x0112) auto/manual  ", False),
        ]
        for reg, label, signed in READ_MAP:
            raw, err = read_reg(ser, reg, signed=signed)
            if err:
                print(f"  ✗ {label}  → ERROR: {err}")
            else:
                if reg == R_STATUS:
                    mode = "Manual" if (raw & STATUS_MANUAL_BIT) else "Auto"
                    print(f"  ✓ {label}  raw=0x{raw & 0xFFFF:04X}  ({mode})")
                elif reg == R_AUTO_MAN:
                    print(f"  ✓ {label}  raw={raw}  ({'Manual' if raw else 'Auto'})")
                elif reg in (R_PV, R_SP_RO, R_WSP, R_SP):
                    print(f"  ✓ {label}  raw={raw}  ({raw / RESOLUTION:.1f} °C)")
                elif reg == R_OP:
                    print(f"  ✓ {label}  raw={raw}  ({raw / 10.0:.1f} %)")
                elif reg == R_SP_RATE:
                    print(f"  ✓ {label}  raw={raw}  ({raw / 10.0:.1f} °C/unit)")
                elif reg == R_SP_RATE_U:
                    units = {0: "off/instant", 1: "°C/s", 2: "°C/min", 3: "°C/h"}
                    print(f"  ✓ {label}  raw={raw}  ({units.get(raw, '?')})")
                else:
                    print(f"  ✓ {label}  raw={raw}")

        # ── B. Test R_AUTO_MAN write ──────────────────────────────────────────
        sep("B. Test R_AUTO_MAN (0x0112) write → go_operate / go_standby core")
        init_mode = snap.get(R_AUTO_MAN, 0)
        print(f"  Pre-test mode: {'Manual' if init_mode else 'Auto'}")

        # Write Auto (0)
        ok, err = write_reg(ser, R_AUTO_MAN, 0)
        time.sleep(0.05)
        if ok:
            rb, _ = read_reg(ser, R_AUTO_MAN, signed=False)
            confirmed = (rb == 0) if rb is not None else False
            print(f"  Write Auto (0): ✓ ACK  readback={rb}  "
                  f"{'✓ confirmed Auto' if confirmed else '? readback mismatch'}")
            results["R_AUTO_MAN→Auto"] = (confirmed, None)
        else:
            print(f"  Write Auto (0): ✗ FAILED  {err}")
            results["R_AUTO_MAN→Auto"] = (False, err)

        # Write Manual (1)
        ok, err = write_reg(ser, R_AUTO_MAN, 1)
        time.sleep(0.05)
        if ok:
            rb, _ = read_reg(ser, R_AUTO_MAN, signed=False)
            confirmed = (rb == 1) if rb is not None else False
            print(f"  Write Manual(1): ✓ ACK  readback={rb}  "
                  f"{'✓ confirmed Manual' if confirmed else '? readback mismatch'}")
            results["R_AUTO_MAN→Manual"] = (confirmed, None)
        else:
            print(f"  Write Manual(1): ✗ FAILED  {err}")
            results["R_AUTO_MAN→Manual"] = (False, err)

        # Restore to original mode before continuing
        write_reg(ser, R_AUTO_MAN, init_mode)
        time.sleep(0.05)
        print(f"  Interim restore → {'Manual' if init_mode else 'Auto'} (original mode)")

        # ── C. Probe Manual Output Demand candidates ──────────────────────────
        sep("C. Probe writable Manual Output Demand register candidates")
        print("  Strategy: switch to Manual, try writing 0 to each candidate,")
        print("  restore each reg's original value, then restore original mode.\n")

        # Save candidate current values before entering Manual
        cand_originals = {}
        for reg in OUTPUT_CANDIDATES:
            v, e = read_reg(ser, reg)
            cand_originals[reg] = (v, e)

        # Switch to Manual for the output-demand probe
        write_reg(ser, R_AUTO_MAN, 1)
        time.sleep(0.05)

        for reg, desc in OUTPUT_CANDIDATES.items():
            cur, err_r = cand_originals[reg]
            print(f"  0x{reg:04X}  {desc}")
            if err_r:
                print(f"    Read  : ✗ {err_r}  (skipping write probe)")
                results[f"probe_0x{reg:04X}"] = (False, f"read failed: {err_r}")
                continue
            print(f"    Read  : raw={cur}  ({cur / 10.0:.1f} %" if reg != R_OP
                  else f"    Read  : raw={cur}  ({cur / 10.0:.1f} %,  READ-ONLY)")

            # Try writing 0
            ok, err_w = write_reg(ser, reg, 0)
            time.sleep(0.05)
            if not ok:
                print(f"    Write 0: ✗ FAILED — {err_w}")
                results[f"probe_0x{reg:04X}"] = (False, err_w)
            else:
                rb, _ = read_reg(ser, reg)
                matched = (rb == 0) if rb is not None else False
                print(f"    Write 0: ✓ ACK   readback={rb}  "
                      f"{'✓ wrote & confirmed' if matched else '△ ACK but readback unchanged'}")
                results[f"probe_0x{reg:04X}"] = (matched, None)

            # Restore this register's original value
            if cur is not None:
                write_reg(ser, reg, cur & 0xFFFF)
                time.sleep(0.02)
                print(f"    Restored: raw={cur}")

        # Restore original mode after probing
        write_reg(ser, R_AUTO_MAN, init_mode)
        time.sleep(0.05)
        print(f"\n  Interim restore → {'Manual' if init_mode else 'Auto'} (original mode)")

        # ── D. Test SP write: R_SP (0x0018) ──────────────────────────────────
        sep("D. Test SP write: R_SP (0x0018)")
        sp_cur = snap.get(R_SP)
        if sp_cur is None:
            sp_cur, err = read_reg(ser, R_SP)
            if err:
                print(f"  Cannot read current SP: {err}")
        if sp_cur is not None:
            print(f"  Current R_SP = {sp_cur} ({sp_cur / RESOLUTION:.1f} °C)")
            # Write back same value — no temperature change
            ok, err = write_reg(ser, R_SP, sp_cur & 0xFFFF)
            time.sleep(0.30)   # SP settle
            if ok:
                rb, _ = read_reg(ser, R_SP)
                confirmed = (rb == sp_cur) if rb is not None else False
                print(f"  Write same SP ({sp_cur}): ✓ ACK  readback={rb}  "
                      f"{'✓ confirmed' if confirmed else '? mismatch'}")
                results["R_SP_write"] = (confirmed, None)
            else:
                print(f"  Write R_SP: ✗ FAILED — {err}")
                results["R_SP_write"] = (False, err)
        else:
            results["R_SP_write"] = (False, "could not read current SP")

        # ── E. Test ramp-rate write ───────────────────────────────────────────
        sep("E. Test ramp-rate write: R_SP_RATE (0x0020) and R_SP_RATE_U (0x0021)")

        rate_cur = snap.get(R_SP_RATE)
        unit_cur = snap.get(R_SP_RATE_U)

        if rate_cur is None or unit_cur is None:
            rate_cur, err_r = read_reg(ser, R_SP_RATE,  signed=False)
            unit_cur, err_u = read_reg(ser, R_SP_RATE_U, signed=False)
            if err_r or err_u:
                print(f"  Read error — R_SP_RATE: {err_r}  R_SP_RATE_U: {err_u}")
                results["R_SP_RATE_write"]   = (False, "read failed")
                results["R_SP_RATE_U_write"] = (False, "read failed")
                rate_cur = unit_cur = None

        if rate_cur is not None and unit_cur is not None:
            units_map = {0: "off/instant", 1: "°C/s", 2: "°C/min", 3: "°C/h"}
            print(f"  Current R_SP_RATE={rate_cur} ({rate_cur/10:.1f} °C/unit)  "
                  f"R_SP_RATE_U={unit_cur} ({units_map.get(unit_cur, '?')})")

            # Write back same values (no operational change)
            ok1, e1 = write_reg(ser, R_SP_RATE,   rate_cur)
            time.sleep(0.02)
            ok2, e2 = write_reg(ser, R_SP_RATE_U, unit_cur)
            time.sleep(0.02)

            rb_r, _ = read_reg(ser, R_SP_RATE,  signed=False)
            rb_u, _ = read_reg(ser, R_SP_RATE_U, signed=False)

            conf_r = (rb_r == rate_cur) if rb_r is not None else False
            conf_u = (rb_u == unit_cur) if rb_u is not None else False

            s_r = "✓ confirmed" if (ok1 and conf_r) else ("✓ ACK" if ok1 else f"✗ {e1}")
            s_u = "✓ confirmed" if (ok2 and conf_u) else ("✓ ACK" if ok2 else f"✗ {e2}")

            print(f"  R_SP_RATE  (0x0020) write: {s_r}  (readback={rb_r})")
            print(f"  R_SP_RATE_U(0x0021) write: {s_u}  (readback={rb_u})")
            results["R_SP_RATE_write"]   = (ok1 and conf_r, e1)
            results["R_SP_RATE_U_write"] = (ok2 and conf_u, e2)

    finally:
        # ── F. RESTORE — always runs, even on exception ───────────────────────
        restore_state()
        ser.close()

    # ── G. Summary ───────────────────────────────────────────────────────────
    sep("G. Summary & recommended driver fix")

    auto_ok    = results.get("R_AUTO_MAN→Auto",   (False,))[0]
    manual_ok  = results.get("R_AUTO_MAN→Manual", (False,))[0]
    sp_ok      = results.get("R_SP_write",         (False,))[0]
    rate_ok    = results.get("R_SP_RATE_write",    (False,))[0]
    rate_u_ok  = results.get("R_SP_RATE_U_write",  (False,))[0]

    print(f"\n  go_operate()      (R_AUTO_MAN→0 Auto):    {'✓ OK' if auto_ok   else '✗ FAILS'}")
    print(f"  go_standby()      (R_AUTO_MAN→1 Manual):  {'✓ OK' if manual_ok else '✗ FAILS'}")
    print(f"  apply_setpoint()  (R_SP 0x0018):          {'✓ OK' if sp_ok     else '✗ FAILS'}")
    print(f"  write_ramp_rate() (R_SP_RATE  0x0020):    {'✓ OK' if rate_ok   else '✗ FAILS'}")
    print(f"  write_ramp_rate() (R_SP_RATE_U 0x0021):   {'✓ OK' if rate_u_ok else '✗ FAILS'}")

    best_out_reg = None
    for reg in OUTPUT_CANDIDATES:
        if results.get(f"probe_0x{reg:04X}", (False,))[0]:
            best_out_reg = reg
            break

    print(f"\n  Manual Output Demand register (for 0% output in go_standby):")
    if best_out_reg is not None:
        print(f"    ✓ Writable register found: 0x{best_out_reg:04X}")
        print(f"      → {OUTPUT_CANDIDATES[best_out_reg]}")
        print(f"      → Update epc3004.py:  R_OUT_DEMAND = 0x{best_out_reg:04X}")
    else:
        print("    ✗ None of the probed candidates accepted a write.")
        print("      → Set R_OUT_DEMAND = None in epc3004.py to skip the output write.")
        print("        Switching to Manual mode alone stops PID-driven heating.")
        print("        Use iTools to locate the correct 'Manual Output Demand' address.")

    print("\n  ROOT CAUSE:")
    print("    go_standby() wrote 0 to R_OP (0x0004) = Working Output Power.")
    print("    This register is READ-ONLY → Modbus exception 0x02 (Illegal address).")
    print()
    print("  FIX already applied in devices/epc3004.py:")
    print("    Removed self._write_word(self.R_OP, 0) from go_standby().")
    if best_out_reg is not None:
        print(f"    Set R_OUT_DEMAND = 0x{best_out_reg:04X} to zero the output in Manual mode.")
    print()
    print("  Port closed.  Diagnostic complete.\n")


if __name__ == "__main__":
    main()
