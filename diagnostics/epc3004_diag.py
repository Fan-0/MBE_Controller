"""
EPC3004 Indium Source Controller — Diagnostic Tool
====================================================
Use when: port connects but no values come back from the EPC3004.

What this script does:
  A. Raw comms check — send a single FC 0x03 read, print raw bytes
  B. Slave ID scan — try IDs 1-8 to find the device if ID is wrong
  C. Register probe — read and decode the key registers used by epc3004.py
     (PV, SP, OP, WSP, STATUS, AUTO_MAN, SP_RATE, SP_RATE_U) at both
     RESOLUTION_FACTOR 1 (integer °C) and 10 (0.1 °C display)
  D. Register block scan — scan regs 0-20 and 256-290 raw to find
     what the device actually returns at each address
  E. SP write test — write a known SP value, wait 0.3s, read back
  F. Auto/Manual mode check — read and optionally toggle
  G. Timing sweep — retry each read with 10ms / 30ms / 50ms turnaround
     delays to catch devices that need more RS-485 settle time

Usage:
    python diagnostics/epc3004_diag.py [port] [slave_id] [baudrate]
    python diagnostics/epc3004_diag.py COM13 1 19200

Tip: Run with GUI closed (exclusive serial port).
"""

import sys
import struct
import time
import serial

# Force UTF-8 output so Unicode chars print on Windows consoles
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── CLI args ──────────────────────────────────────────────────────────────────
PORT      = sys.argv[1] if len(sys.argv) > 1 else "COM13"
SLAVE_ID  = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BAUDRATE  = int(sys.argv[3]) if len(sys.argv) > 3 else 19200
TIMEOUT   = 0.5   # seconds — generous for a diagnostic

# ── EPC3004 register map (0-based Modbus frame addresses) ─────────────────────
R_PV         = 0x0001   # Process Variable (INT16 °C)
R_SP         = 0x0002   # Target Setpoint  (INT16 °C)
R_OP         = 0x0004   # Working Output % (INT16, raw/10 = %)
R_WSP        = 0x0005   # Working Setpoint (INT16 °C, read-only)
R_STATUS     = 0x0111   # Instrument Status word (bit8=Manual)
R_AUTO_MAN   = 0x0112   # Auto/Manual (0=Auto, 1=Manual)
R_SP_RATE    = 0x0023   # SP Ramp Rate (INT16, stored × 10)
R_SP_RATE_U  = 0x0024   # SP Rate Unit (0=off,1=°C/s,2=°C/min,3=°C/h)
STATUS_MANUAL_BIT = 0x0100  # bit 8 of R_STATUS

NAMED_REGS = {
    R_PV:        "R_PV        (process value)",
    R_SP:        "R_SP        (target setpoint)",
    0x0003:      "reg 0x0003  (unused / gap)",
    R_OP:        "R_OP        (output %×10)",
    R_WSP:       "R_WSP       (working setpoint)",
    R_SP_RATE:   "R_SP_RATE   (ramp rate × 10)",
    R_SP_RATE_U: "R_SP_RATE_U (ramp unit)",
    R_STATUS:    "R_STATUS    (status word)",
    R_AUTO_MAN:  "R_AUTO_MAN  (0=Auto 1=Manual)",
}

# ── CRC-16 Modbus ─────────────────────────────────────────────────────────────
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def frame_read(slave: int, reg: int, n: int) -> bytes:
    f = struct.pack(">BBHH", slave, 0x03, reg, n)
    return f + struct.pack("<H", crc16(f))

def frame_write_single(slave: int, reg: int, value: int) -> bytes:
    f = struct.pack(">BBHH", slave, 0x06, reg, value & 0xFFFF)
    return f + struct.pack("<H", crc16(f))

# ── Low-level transact ────────────────────────────────────────────────────────
def transact(ser: serial.Serial, req: bytes, expected: int,
             label: str = "", turnaround_ms: int = 10,
             silent: bool = False) -> bytes:
    """Send req, sleep turnaround_ms, read expected bytes. Returns bytes or raises."""
    if not silent:
        print(f"    TX [{label}]: {req.hex(' ').upper()}")
    ser.reset_input_buffer()
    ser.write(req)
    time.sleep(turnaround_ms / 1000.0)
    resp = ser.read(expected)
    if not silent:
        if resp:
            print(f"    RX [{label}]: {resp.hex(' ').upper()}  ({len(resp)} bytes)")
        else:
            print(f"    RX [{label}]: <no response>")
    if not resp:
        raise IOError(f"{label}: no response (timeout at {TIMEOUT*1000:.0f}ms)")
    if len(resp) >= 3 and (resp[1] & 0x80):
        CODES = {1: "Illegal function", 2: "Illegal address",
                 3: "Illegal data/value", 4: "Device error", 5: "Busy/Ack"}
        code = resp[2] if len(resp) > 2 else 0
        raise IOError(f"{label}: Modbus exception FC={resp[1]&0x7F:#04x} "
                      f"code={code} ({CODES.get(code, '?')})")
    if len(resp) < expected:
        raise IOError(f"{label}: short response {len(resp)}/{expected} bytes")
    calc = crc16(resp[:expected - 2])
    recv = struct.unpack("<H", resp[expected - 2:expected])[0]
    if calc != recv:
        raise IOError(f"{label}: CRC mismatch (calc={calc:#06x} recv={recv:#06x})")
    return resp[:expected]

def read_reg(ser, slave, reg, label="", turnaround_ms=10, silent=False):
    """Read 1 register (unsigned 16-bit). Raises on error."""
    req = frame_read(slave, reg, 1)
    resp = transact(ser, req, 7, label, turnaround_ms, silent)
    return struct.unpack(">H", resp[3:5])[0]

def read_regs(ser, slave, start_reg, n, label="", turnaround_ms=10, silent=False):
    """Read n consecutive registers. Returns list of uint16."""
    req = frame_read(slave, start_reg, n)
    resp = transact(ser, req, 3 + n * 2 + 2, label, turnaround_ms, silent)
    return [struct.unpack(">H", resp[3 + i * 2:5 + i * 2])[0] for i in range(n)]

def as_int16(v: int) -> int:
    return v - 0x10000 if v >= 0x8000 else v

def decode_pv(raw: int, factor: int) -> str:
    i16 = as_int16(raw)
    return f"{i16 / factor:.1f} °C  (raw={raw:#06x} / factor={factor})"

def sep(title: str = ""):
    print()
    print("─" * 70)
    if title:
        print(f"  {title}")
    print("─" * 70)

# ── Main diagnostic ───────────────────────────────────────────────────────────
def run():
    print("=" * 70)
    print(f"  EPC3004 Indium Source Diagnostic")
    print(f"  Port={PORT}  Slave={SLAVE_ID}  Baud={BAUDRATE}")
    print("=" * 70)

    try:
        ser = serial.Serial(
            port=PORT, baudrate=BAUDRATE,
            bytesize=8, parity='N', stopbits=1, timeout=TIMEOUT)
        print(f"\n  ✓ Serial port open: {ser.name}")
    except serial.SerialException as e:
        print(f"\n  ✗ Cannot open port: {e}")
        return

    try:
        # ── A. Raw comms check ────────────────────────────────────────────────
        sep("A. Raw comms check — read R_PV (reg 0x0001) with 10ms turnaround")
        try:
            raw = read_reg(ser, SLAVE_ID, R_PV, "R_PV raw", turnaround_ms=10)
            i16 = as_int16(raw)
            print(f"\n  ✓ Got response. Raw uint16={raw}  signed={i16}")
            print(f"    Decoded as °C (factor 1)  : {i16:.1f} °C")
            print(f"    Decoded as °C (factor 10) : {i16 / 10:.1f} °C  (if display shows 0.1°C)")
        except IOError as e:
            print(f"\n  ✗ No response from slave {SLAVE_ID}: {e}")
            print("    → Check slave ID, baud rate, RS-485 wiring.")
            print("    → Proceeding to slave-ID scan (section B).")

        # ── B. Slave ID scan ──────────────────────────────────────────────────
        sep("B. Slave ID scan — try IDs 1 through 8")
        found_ids = []
        for sid in range(1, 9):
            req = frame_read(sid, R_PV, 1)
            ser.reset_input_buffer()
            ser.write(req)
            time.sleep(0.015)
            resp = ser.read(7)
            if resp and not (resp[1] & 0x80) and len(resp) >= 7:
                raw = struct.unpack(">H", resp[3:5])[0]
                print(f"  Slave {sid:2d}: ✓ RESPONSE  raw R_PV = {raw}  "
                      f"({as_int16(raw)} INT16)")
                found_ids.append(sid)
            else:
                status = "exception" if (resp and len(resp) >= 2 and (resp[1] & 0x80)) \
                         else ("short" if resp else "no response")
                print(f"  Slave {sid:2d}: — {status}")
        if found_ids:
            print(f"\n  Responding slave IDs: {found_ids}")
            if SLAVE_ID not in found_ids:
                print(f"  ⚠ Configured slave {SLAVE_ID} did not respond!")
                print(f"  → Change SLAVE_ID at top of this script or in Settings.")
        else:
            print("\n  ✗ No device responded on any slave ID 1–8.")
            print("  → Check RS-485 wiring, termination resistors, baud rate.")

        # ── C. Register probe with both resolution factors ─────────────────────
        sep("C. Key register probe (slave ID from CLI arg)")
        NAMED_LIST = [
            (R_PV,        "R_PV        process value"),
            (R_SP,        "R_SP        target setpoint"),
            (R_OP,        "R_OP        output %×10"),
            (R_WSP,       "R_WSP       working setpoint"),
            (R_SP_RATE,   "R_SP_RATE   ramp rate ×10"),
            (R_SP_RATE_U, "R_SP_RATE_U ramp unit (0=off,1=/s,2=/min,3=/h)"),
            (R_STATUS,    "R_STATUS    status word"),
            (R_AUTO_MAN,  "R_AUTO_MAN  0=Auto 1=Manual"),
        ]
        for reg, label in NAMED_LIST:
            try:
                raw = read_reg(ser, SLAVE_ID, reg, label, turnaround_ms=10, silent=True)
                i16 = as_int16(raw)
                extra = ""
                if reg in (R_PV, R_SP, R_WSP):
                    extra = (f"  →  {i16:.1f}°C (×1)  or  {i16/10:.1f}°C (×10)")
                elif reg == R_OP:
                    extra = (f"  →  {i16/10:.1f}%  (raw÷10)")
                elif reg == R_STATUS:
                    auto_man = "Manual" if raw & STATUS_MANUAL_BIT else "Auto"
                    extra = (f"  →  mode={auto_man}  bit8={(raw>>8)&1}  "
                             f"sensor_break={bool(raw>>3&1)}")
                elif reg == R_AUTO_MAN:
                    extra = f"  →  {'Manual' if raw else 'Auto'}"
                elif reg == R_SP_RATE_U:
                    units = {0: "off (instant)", 1: "°C/s", 2: "°C/min", 3: "°C/h"}
                    extra = f"  →  {units.get(raw, '?')}"
                elif reg == R_SP_RATE:
                    extra = f"  →  {i16/10:.1f} (in unit-reg units)"
                print(f"  0x{reg:04X}  {label:35s}  raw={raw:#06x} ({raw:6d}){extra}")
            except IOError as e:
                print(f"  0x{reg:04X}  {label:35s}  ✗ {e}")

        # ── D. Register block scan ─────────────────────────────────────────────
        sep("D. Block scan — regs 0x0000–0x0009 and 0x010F–0x0115")
        for start, n, title in [
            (0x0000, 10, "Process block (regs 0–9)"),
            (0x010F,  7, "Status area   (regs 271–277)"),
        ]:
            print(f"\n  {title}:")
            try:
                words = read_regs(ser, SLAVE_ID, start, n,
                                  f"scan {start:#06x}", turnaround_ms=10, silent=True)
                for i, w in enumerate(words):
                    reg = start + i
                    i16 = as_int16(w)
                    name = NAMED_REGS.get(reg, "")
                    temp_hint = ""
                    if abs(i16) < 2000:   # plausible °C range
                        temp_hint = f"  could be {i16:.0f}°C (×1) or {i16/10:.1f}°C (×10)"
                    print(f"    reg 0x{reg:04X} ({reg:4d}):  "
                          f"raw={w:#06x} ({w:6d})  int16={i16:6d}"
                          f"  {name}{temp_hint}")
            except IOError as e:
                print(f"    ✗ {e}")

        # ── E. SP write test ───────────────────────────────────────────────────
        sep("E. SP write test — write 100°C (factor 1) then read back")
        try:
            sp_before_raw = read_reg(ser, SLAVE_ID, R_SP, "SP before",
                                     turnaround_ms=10, silent=True)
            print(f"  SP before:  raw={sp_before_raw}  "
                  f"→ {as_int16(sp_before_raw):.1f}°C (×1)  "
                  f"or {as_int16(sp_before_raw)/10:.1f}°C (×10)")

            # Write 100 for factor=1 (integer °C display)
            target_raw_f1 = 100
            req = frame_write_single(SLAVE_ID, R_SP, target_raw_f1)
            transact(ser, req, 8, "write SP=100 (factor 1)")
            time.sleep(0.30)
            rb_raw = read_reg(ser, SLAVE_ID, R_SP, "SP readback",
                              turnaround_ms=10, silent=True)
            rb_i16 = as_int16(rb_raw)
            print(f"\n  After writing raw=100 (= 100°C if factor=1, = 10.0°C if factor=10):")
            print(f"    SP readback raw={rb_raw}  int16={rb_i16}")
            if abs(rb_i16 - 100) < 2:
                print("    ✓ Matches! → RESOLUTION_FACTOR = 1 (integer °C) is correct")
            elif abs(rb_i16 - 1000) < 20:
                print("    ✓ Device stored 1000 → RESOLUTION_FACTOR = 10 (0.1°C display)")
            elif rb_i16 == as_int16(sp_before_raw):
                print("    ✗ SP did not change — write may be blocked (Manual mode? "
                      "OEM lock? wrong register?)")
            else:
                print(f"    ? Unexpected readback {rb_i16} — "
                      f"may need RESOLUTION_FACTOR = {rb_i16 // 100 or 1}")

            # Restore original SP
            req = frame_write_single(SLAVE_ID, R_SP, sp_before_raw & 0xFFFF)
            transact(ser, req, 8, "restore SP", silent=True)
            time.sleep(0.15)
            print(f"  SP restored to raw={sp_before_raw}")

        except IOError as e:
            print(f"  ✗ SP write test failed: {e}")

        # ── F. Auto/Manual mode check ──────────────────────────────────────────
        sep("F. Auto / Manual mode check")
        try:
            status = read_reg(ser, SLAVE_ID, R_STATUS, "R_STATUS",
                              turnaround_ms=10, silent=True)
            am_reg = read_reg(ser, SLAVE_ID, R_AUTO_MAN, "R_AUTO_MAN",
                              turnaround_ms=10, silent=True)
            in_manual_status = bool(status & STATUS_MANUAL_BIT)
            in_manual_am     = bool(am_reg)
            mode_str = "Manual" if in_manual_status else "Auto"
            print(f"  R_STATUS    = {status:#06x}  bit8(Manual) = {int(in_manual_status)}"
                  f"  → {mode_str}")
            print(f"  R_AUTO_MAN  = {am_reg}  → {'Manual' if in_manual_am else 'Auto'}")
            if in_manual_status or in_manual_am:
                print("\n  ⚠ Device is in Manual mode.")
                print("    → In Manual mode, SP changes do not drive the output.")
                print("    → Write R_AUTO_MAN = 0 (Auto) to enable PID.")
                print("    Testing: writing Auto mode (R_AUTO_MAN = 0)...")
                req = frame_write_single(SLAVE_ID, R_AUTO_MAN, 0)
                transact(ser, req, 8, "set Auto", silent=True)
                time.sleep(0.05)
                new_status = read_reg(ser, SLAVE_ID, R_STATUS, "", silent=True)
                still_manual = bool(new_status & STATUS_MANUAL_BIT)
                print(f"    After write: R_STATUS bit8 = {int(still_manual)}"
                      f"  → {'still Manual ✗' if still_manual else 'Now Auto ✓'}")
            else:
                print("  ✓ Device is in Auto (PID) mode — output follows SP.")
        except IOError as e:
            print(f"  ✗ Mode check failed: {e}")

        # ── G. Timing sweep ────────────────────────────────────────────────────
        sep("G. RS-485 turnaround timing sweep — read R_PV at 10/30/50ms delay")
        print("  (Some devices need longer turnaround time than 10ms)")
        print()
        for delay_ms in [10, 30, 50]:
            try:
                req = frame_read(SLAVE_ID, R_PV, 1)
                ser.reset_input_buffer()
                ser.write(req)
                t0 = time.monotonic()
                time.sleep(delay_ms / 1000.0)
                resp = ser.read(7)
                elapsed = (time.monotonic() - t0) * 1000
                if resp and len(resp) >= 7 and not (resp[1] & 0x80):
                    raw = struct.unpack(">H", resp[3:5])[0]
                    print(f"  {delay_ms:3d}ms delay: ✓ got {len(resp)} bytes  "
                          f"raw={raw}  {elapsed:.0f}ms total")
                elif resp:
                    print(f"  {delay_ms:3d}ms delay: ✗ bad response {resp.hex(' ')}  "
                          f"{elapsed:.0f}ms total")
                else:
                    print(f"  {delay_ms:3d}ms delay: ✗ no response  {elapsed:.0f}ms total")
            except Exception as e:
                print(f"  {delay_ms:3d}ms delay: ✗ {e}")

        # ── H. Baud rate sweep ────────────────────────────────────────────────
        sep("H. Baud rate sweep -- try 9600 / 19200 / 38400 on slave IDs 1 and 2")
        print("  (EPC3004 factory default baud is 9600; may differ from 19200)")
        print()
        ser.close()
        found_baud = None
        for baud in [9600, 19200, 38400]:
            try:
                test_ser = serial.Serial(
                    port=PORT, baudrate=baud,
                    bytesize=8, parity='N', stopbits=1, timeout=0.3)
            except serial.SerialException as e:
                print(f"  {baud:6d} baud: cannot open - {e}")
                continue
            for sid in [1, 2]:
                req = frame_read(sid, R_PV, 1)
                test_ser.reset_input_buffer()
                test_ser.write(req)
                time.sleep(0.03)
                resp = test_ser.read(7)
                if resp and len(resp) >= 7 and not (resp[1] & 0x80):
                    raw = struct.unpack(">H", resp[3:5])[0]
                    i16 = as_int16(raw)
                    print(f"  {baud:6d} baud  slave {sid}: *** RESPONSE ***  "
                          f"R_PV raw={raw} int16={i16}  "
                          f"-> {i16:.1f}C (x1) or {i16/10:.1f}C (x10)")
                    found_baud = baud
                elif resp:
                    print(f"  {baud:6d} baud  slave {sid}: partial/exception  {resp.hex(' ')}")
                else:
                    print(f"  {baud:6d} baud  slave {sid}: no response")
            test_ser.close()
        # Reopen at original baud for any final cleanup
        try:
            ser = serial.Serial(
                port=PORT, baudrate=BAUDRATE,
                bytesize=8, parity='N', stopbits=1, timeout=TIMEOUT)
        except Exception:
            pass
        if found_baud:
            print(f"\n  --> Device found at {found_baud} baud!")
            print(f"      Update baudrate in Settings dialog or DCFG['epc3004_baud'] in gui_app.py.")
        else:
            print("\n  No response at any baud rate. Likely a wiring or protocol issue.")
            print("  Check: RS-485 A/B polarity, termination resistors, COMM LIST -> PROT = m.Rtu.")

        # ── I. Resolution factor check ─────────────────────────────────────────
        sep("I. Resolution factor check (RESOLUTION_FACTOR in epc3004.py)")
        try:
            pv_raw = read_reg(ser, SLAVE_ID, R_PV, silent=True)
            sp_raw = read_reg(ser, SLAVE_ID, R_SP, silent=True)
            pv_i16 = as_int16(pv_raw)
            sp_i16 = as_int16(sp_raw)
            print(f"  PV raw={pv_raw}  SP raw={sp_raw}")
            print()
            print(f"  If RESOLUTION_FACTOR=1  (integer °C display):  PV={pv_i16:.0f}°C  SP={sp_i16:.0f}°C")
            print(f"  If RESOLUTION_FACTOR=10 (0.1°C display):       PV={pv_i16/10:.1f}°C  SP={sp_i16/10:.1f}°C")
            print()
            if pv_i16 > 1500:
                print("  CONCLUSION: PV > 1500 with factor=1 → physically impossible temperature.")
                print("  --> Set EPC3004.RESOLUTION_FACTOR = 10 in devices/epc3004.py")
                print(f"      Actual temperature = {pv_i16/10:.1f}°C")
            elif pv_i16 > 0:
                print("  PV is in plausible range for factor=1. Verify against device display.")
        except IOError as e:
            print(f"  Read failed: {e}")

        # ── J. Writable SP register scan ────────────────────────────────────────
        sep("J. Writable SP register scan -- find the correct write address")
        print("  Trying FC 0x06 write (raw=500 = 50.0°C at x10) to candidate addresses.")
        print("  A successful write (no exception) + matching readback = correct register.")
        print()
        # Eurotherm EPC3000 candidate SP write addresses (JBUS 0-based frame addresses)
        sp_candidates = [
            (0x0002, "Standard SP1 (usual EPC3000 address)"),
            (0x0018, "SP1 alternate (some 3xxx firmware)"),
            (0x001A, "SP2 alternate"),
            (0x002D, "Setpoint block candidate"),
            (0x0039, "Operator SP (39 decimal)"),
            (0x0060, "Extended block candidate"),
            (0x00C8, "200 decimal"),
        ]
        writable_sp = None
        test_raw = 500   # 50.0°C at factor=10, or 500°C at factor=1
        for reg_addr, desc in sp_candidates:
            # Read current value first
            try:
                before = read_reg(ser, SLAVE_ID, reg_addr, silent=True)
            except IOError:
                before = None
            # Try write
            req = frame_write_single(SLAVE_ID, reg_addr, test_raw)
            try:
                resp = transact(ser, req, 8, silent=True)
                time.sleep(0.15)
                # Readback
                try:
                    after = read_reg(ser, SLAVE_ID, reg_addr, silent=True)
                    match = abs(as_int16(after) - test_raw) < 5
                    flag = "ACCEPTED + READBACK OK" if match else "write ack'd but readback mismatch"
                    print(f"  0x{reg_addr:04X}  {desc}")
                    print(f"          write ACK  before={before}  after={after}  {flag}")
                    if match:
                        writable_sp = reg_addr
                        print(f"          *** THIS IS THE WRITABLE SP REGISTER ***")
                        # Restore original value
                        if before is not None:
                            req2 = frame_write_single(SLAVE_ID, reg_addr, before)
                            transact(ser, req2, 8, silent=True)
                            time.sleep(0.10)
                            print(f"          Restored to {before}")
                except IOError as re:
                    print(f"  0x{reg_addr:04X}  {desc}: write ACK but readback failed: {re}")
            except IOError as e:
                err = str(e)
                print(f"  0x{reg_addr:04X}  {desc}: {err}")
        print()
        if writable_sp is not None:
            print(f"  --> Writable SP found at 0x{writable_sp:04X} ({writable_sp} decimal)")
            print(f"      Update R_SP = 0x{writable_sp:04X} in devices/epc3004.py")
        else:
            print("  No writable SP found among candidates.")
            print("  --> Use iTools to find the correct SP address (Browser -> search 'Setpoint 1')")
            print("  --> Try running with OEM/operator password unlocked on device HMI")

        # ── K. Ramp rate register scan ──────────────────────────────────────────
        sep("K. Ramp rate register scan -- find SP rate registers")
        print("  Looking for non-0x8000 (non-sentinel) values near expected ramp addresses.")
        print()
        ramp_scan_ranges = [
            (0x0020, 12, "around 0x0023/0x0024 (current driver guess)"),
            (0x0038, 10, "around 0x003C-0x003E (alternate Eurotherm location)"),
            (0x006E, 10, "around 0x0070-0x0075"),
        ]
        for start, n, desc in ramp_scan_ranges:
            print(f"  Scanning {desc}:")
            try:
                words = read_regs(ser, SLAVE_ID, start, n, silent=True)
                for i, w in enumerate(words):
                    reg = start + i
                    i16 = as_int16(w)
                    sentinel = " <-- 0x8000 sentinel (not applicable)" if w == 0x8000 else ""
                    rate_hint = ""
                    if w != 0x8000 and 0 < w < 10000:
                        rate_hint = f"  could be ramp rate {w/10:.1f} units/min"
                    print(f"    0x{reg:04X} ({reg:4d}):  raw={w:#06x} ({w:6d}){sentinel}{rate_hint}")
            except IOError as e:
                print(f"    Read failed: {e}")
            print()

        # ── Summary ───────────────────────────────────────────────────────────
        sep("Summary / Next Steps")
        print("""
  1. If section A got no response:
       → Wrong slave ID → check device HMI: COMM LIST → NODE
       → Wrong baud rate → check HMI: COMM LIST → BAUD (try 9600, 38400)
       → RS-485 wiring / termination issue

  2. If section B found a different slave ID:
       → Update slave_id in Settings dialog or DCFG in gui_app.py

  3. If R_PV returned a value but it looked wrong:
       → Check RESOLUTION_FACTOR. If display shows 744.0 and raw=7440,
         set EPC3004.RESOLUTION_FACTOR = 10 in devices/epc3004.py

  4. If R_STATUS bit8 = 1 (Manual mode):
       → Device is not running PID. In GUI: click OPERATE button.
       → Or: HMI → Auto/Manual → set Auto.

  5. If SP write was not accepted (section E):
       → Device may be in Manual mode (fix: write R_AUTO_MAN = 0 first)
       → OEM security may be active — check HMI Access level.

  6. If timing sweep shows response only at 30ms+ (section G):
       → Increase _transact() sleep in epc3004.py from 0.01 to 0.03

  7. If section H found a different baud rate:
       → Go to Settings dialog, change EPC3004 baud to match.
       → Or update DCFG['epc3004_baud'] in gui_app.py.

  8. If nothing responded at any baud rate:
       → Check COMM LIST on device HMI: PROT must be set to m.Rtu
       → Check RS-485 A/B wiring polarity (swap A/B if in doubt)
       → Check 220 ohm termination resistors at both ends of the RS-485 line
       → Verify the USB-RS485 adapter is the correct one for COM13
        """)

    finally:
        ser.close()
        print("  Port closed.")
    print("=" * 70)


if __name__ == "__main__":
    run()
