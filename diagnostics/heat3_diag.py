"""
HEAT3 Substrate (CH2) Diagnostic Tool  v4
==========================================
Focused investigation: why does CH2 SP stay locked at 50C?

Tests:
  A. Assert REMOTE MASTER
  B. Read TC2 sensor configuration (type + range registers)
  C. Read CH1 block incl. max-SP / range registers (regs 0-50)
  D. Try writing CH1 SP to 55C  -> does it accept? (control test)
  E. Try writing CH2 SP to 25C  (BELOW current 50C) -> does it accept?
  F. Try writing CH2 SP to 200C (ABOVE current 50C) -> does it accept?
  G. Read the "operating/limit" region regs 40-84 for CH1 to spot T-max
  H. Try STANDBY CH2, then write SP, then OPERATE
  I. Final state dump

Usage:
    python heat3_diag.py [port] [baudrate] [slave_id] [target_celsius]
    python heat3_diag.py COM12 19200 1 100
"""

import sys
import struct
import time
import serial

PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM12"
BAUDRATE = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
SLAVE_ID = int(sys.argv[3]) if len(sys.argv) > 3 else 1
TARGET_C = float(sys.argv[4]) if len(sys.argv) > 4 else 100.0
KELVIN   = 273.15

def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def frame_read(slave, reg, n):
    f = struct.pack(">BBHH", slave, 0x03, reg, n)
    return f + struct.pack("<H", crc16(f))

def frame_write_single(slave, reg, value):
    f = struct.pack(">BBHH", slave, 0x06, reg, value & 0xFFFF)
    return f + struct.pack("<H", crc16(f))

def frame_write_multi(slave, reg, words):
    n = len(words)
    data = b''.join(struct.pack(">H", w & 0xFFFF) for w in words)
    body = struct.pack(">BBHHB", slave, 0x10, reg, n, n * 2) + data
    return body + struct.pack("<H", crc16(body))

def transact(ser, req, expected, label="", silent=False):
    if not silent:
        print(f"    TX: {req.hex(' ').upper()}")
    ser.reset_input_buffer()
    ser.write(req)
    time.sleep(0.08)
    resp = ser.read(max(expected, 8))
    if not silent:
        if resp:
            print(f"    RX: {resp.hex(' ').upper()}  ({len(resp)} bytes)")
        else:
            print(f"    RX: <no response>")
    if not resp:
        raise IOError(f"{label}: no response")
    if len(resp) >= 3 and (resp[1] & 0x80):
        codes = {1: "Illegal function", 2: "Illegal address",
                 3: "Illegal data value", 4: "Device failure", 5: "Busy"}
        code = resp[2] if len(resp) > 2 else 0
        raise IOError(f"{label}: Modbus exception FC={resp[1]&0x7F:#04x} "
                      f"code={code} ({codes.get(code, '?')})")
    if len(resp) < expected:
        raise IOError(f"{label}: short {len(resp)}/{expected} bytes")
    calc = crc16(resp[:expected - 2])
    recv = struct.unpack("<H", resp[expected - 2:expected])[0]
    if calc != recv:
        raise IOError(f"{label}: CRC mismatch")
    return resp[:expected]

def write_reg(ser, reg, value, label="", silent=False):
    req = frame_write_single(SLAVE_ID, reg, value)
    transact(ser, req, 8, label, silent=silent)

def read_words(ser, reg, n, label="", silent=False):
    req = frame_read(SLAVE_ID, reg, n)
    resp = transact(ser, req, 3 + n * 2 + 2, label, silent=silent)
    return [struct.unpack(">H", resp[3 + i * 2:5 + i * 2])[0] for i in range(n)]

def write_int32(ser, reg, raw_val, label="", silent=False):
    hi = (raw_val >> 16) & 0xFFFF
    lo = raw_val & 0xFFFF
    req = frame_write_multi(SLAVE_ID, reg, [hi, lo])
    transact(ser, req, 8, label, silent=silent)

def read_int32(ser, reg, label="", silent=False):
    w = read_words(ser, reg, 2, label, silent=silent)
    raw = (w[0] << 16) | w[1]
    return raw - 0x100000000 if raw >= 0x80000000 else raw

def c_to_raw(c):  return int(round((c + KELVIN) * 100))
def raw_to_c(r):  return r / 100.0 - KELVIN

def words_to_i32(hi, lo):
    raw = (hi << 16) | lo
    return raw - 0x100000000 if raw >= 0x80000000 else raw

def sep(title=""):
    print()
    print("-" * 66)
    if title:
        print(f"  {title}")
    print("-" * 66)

def test_sp_write(ser, reg_hi, name, test_c):
    raw = c_to_raw(test_c)
    hi  = (raw >> 16) & 0xFFFF
    lo  =  raw & 0xFFFF
    try:
        req = frame_write_multi(SLAVE_ID, reg_hi, [hi, lo])
        transact(ser, req, 8, f"SP write {name}", silent=True)
        time.sleep(0.30)
        rb = read_int32(ser, reg_hi, "SP readback", silent=True)
        rb_c = raw_to_c(rb)
        diff = abs(rb_c - test_c)
        ok = diff < 1.0
        marker = "OK " if ok else "---"
        print(f"    [{marker}] Write {test_c:7.1f}C  ->  readback={rb_c:.2f}C  "
              f"diff={diff:.1f}  {'ACCEPTED' if ok else 'REJECTED/CLAMPED'}")
        return ok, rb_c
    except IOError as e:
        print(f"    [ERR] Write {test_c:.1f}C failed: {e}")
        return False, None


def run():
    print("=" * 66)
    print(f"  HEAT3 CH2 Diagnostic v4  target={TARGET_C}C")
    print(f"  Port={PORT}  Baud={BAUDRATE}  Slave={SLAVE_ID}")
    print("=" * 66)

    try:
        ser = serial.Serial(port=PORT, baudrate=BAUDRATE,
                            bytesize=8, parity='N', stopbits=1, timeout=1.0)
        print(f"  Port open: {ser.name}")
    except serial.SerialException as e:
        print(f"  FAILED: {e}")
        return

    try:
        # ── A. Assert REMOTE MASTER ───────────────────────────────────
        sep("A. Assert REMOTE MASTER")
        try:
            write_reg(ser, 1000, 1, "REMOTE MASTER")
            print("    OK")
        except IOError as e:
            print(f"    FAILED: {e}  =>  enable REMOTE on front panel first")
            return
        time.sleep(0.05)

        # ── B. Read sensor configuration block (regs 20-30) ──────────
        # CH1-level sensor config: TC types, diode types, RTD, etc.
        sep("B. Sensor config block (regs 20-30)")
        try:
            sc = read_words(ser, 20, 11, "sensor config", silent=True)
            tc_types = ["K", "C", "E", "N"]
            print(f"    reg20 TC1 temp      = {sc[0]}")
            print(f"    reg21 work mode CH1 = {sc[1]}  (0=Manual,1=Auto)")
            print(f"    reg22 heating mode  = {sc[2]}  (0=RES,1=EB)")
            print(f"    reg23 PID type CH1  = {sc[3]}  (0=T,1=dT)")
            print(f"    reg24 TC1 type      = {sc[4]}  ({tc_types[sc[4]] if sc[4]<4 else '?'})")
            print(f"    reg25 TC2 type      = {sc[5]}  ({tc_types[sc[5]] if sc[5]<4 else '?'})")
            print(f"    reg26 D1 diode type = {sc[6]}")
            print(f"    reg27 D2 diode type = {sc[7]}")
            print(f"    reg28 TC1 sensor CH = {sc[8]}")
            print(f"    reg29               = {sc[9]}")
            print(f"    reg30               = {sc[10]}")
        except IOError as e:
            print(f"    FAILED: {e}")
        time.sleep(0.05)

        # ── C. Read CH1 mid-block: regs 14-83 (look for T-max / SP-max)
        sep("C. CH1 extended block regs 14-83 (looking for SP max limits)")
        try:
            mb = read_words(ser, 14, 70, "CH1 mid block", silent=True)
            def m(reg): return mb[reg - 14]

            sp1  = words_to_i32(m(14), m(15))
            sdt1 = m(16)   # dT mode SP CH1
            print(f"    reg14-15 SP CH1 T-mode   = {raw_to_c(sp1):.2f}C  (raw={sp1})")
            print(f"    reg16    SP CH1 dT-mode   = {sdt1/10.0:.1f} K/s")
            print(f"    reg17    = {m(17)}")
            print(f"    reg18    = {m(18)}")
            print(f"    reg19    = {m(19)}")
            print(f"    reg20    TC1 temp = {raw_to_c(words_to_i32(m(20),m(21))):.2f}C")
            # Scan for registers that might hold T-max or T-min for CH1 SP
            print()
            print("    Scanning regs 30-83 for non-zero values that might be limits:")
            for reg in range(30, 84):
                v = m(reg)
                if v != 0:
                    v_as_i32_hi = words_to_i32(v, m(reg+1) if reg<83 else 0)
                    t_if_temp = raw_to_c(v_as_i32_hi) if 27000 < v_as_i32_hi < 200000 else None
                    t_single = raw_to_c(v) if 27000 < v < 60000 else None
                    annotation = ""
                    if t_if_temp is not None:
                        annotation += f"  <- could be {t_if_temp:.1f}C (INT32 w/ next)"
                    if t_single is not None:
                        annotation += f"  <- could be {t_single:.1f}C (INT16)"
                    print(f"      reg{reg:3d} = 0x{v:04X} ({v:6d}){annotation}")
        except IOError as e:
            print(f"    FAILED: {e}")
        time.sleep(0.05)

        # ── D. Control test: write CH1 SP from 50C to 55C ────────────
        sep("D. Control test: can CH1 SP be changed via Modbus?")
        try:
            sp1_before = read_int32(ser, 14, "CH1 SP before", silent=True)
            print(f"    CH1 SP before = {raw_to_c(sp1_before):.2f}C")
            print(f"    Attempting CH1 SP writes (will restore afterward):")
            ok55, rb55 = test_sp_write(ser, 14, "CH1", 55.0)
            ok45, rb45 = test_sp_write(ser, 14, "CH1", 45.0)
            ok200, rb200 = test_sp_write(ser, 14, "CH1", 200.0)
            # Restore CH1 SP
            write_int32(ser, 14, sp1_before, "CH1 SP restore", silent=True)
            sp1_after = read_int32(ser, 14, "CH1 SP verify", silent=True)
            print(f"    CH1 SP restored to {raw_to_c(sp1_after):.2f}C")
            if ok200:
                print("    => CH1 SP accepts writes above 50C. SP limit is CH2-specific.")
            elif ok55:
                print("    => CH1 SP accepts small increments from 50C but not large ones.")
            else:
                print("    => CH1 SP is also locked! Problem may be global (e.g. front-panel lock).")
        except IOError as e:
            print(f"    FAILED: {e}")
        time.sleep(0.05)

        # ── E/F. Test CH2 SP at various values ───────────────────────
        sep("E/F. CH2 SP write tests at multiple temperatures")
        sp2_before = None
        try:
            sp2_before = read_int32(ser, 90, "CH2 SP before", silent=True)
            print(f"    CH2 SP before = {raw_to_c(sp2_before):.2f}C")
            print(f"    CH2 PV before = {raw_to_c(read_int32(ser, 88, '', silent=True)):.2f}C")
            print()
            print("    Testing writes to CH2 SP (reg 90):")
            test_sp_write(ser, 90, "CH2",  25.0)   # below current SP
            test_sp_write(ser, 90, "CH2",  40.0)   # just below SP
            test_sp_write(ser, 90, "CH2",  50.0)   # same as current SP
            test_sp_write(ser, 90, "CH2",  51.0)   # just above SP
            test_sp_write(ser, 90, "CH2",  75.0)   # above
            test_sp_write(ser, 90, "CH2", 100.0)   # target
            test_sp_write(ser, 90, "CH2", 200.0)   # well above
            print()
            print("    Testing single FC 0x06 writes to reg 90 (hi-word only, value=0x0000):")
            try:
                write_reg(ser, 90, 0x0000, "CH2 SP hi FC06", silent=True)
                time.sleep(0.05)
                v = read_words(ser, 90, 1, "", silent=True)[0]
                print(f"    After FC 0x06 write 0 to reg90: reg90 hi-word = 0x{v:04X} ({v})")
            except IOError as e:
                print(f"    FC 0x06 to reg90 failed: {e}")
            time.sleep(0.05)
            print()
            print("    Testing FC 0x06 write to reg 91 (lo-word = 0x0001 = invalid):")
            try:
                write_reg(ser, 91, 0x0001, "CH2 SP lo FC06 invalid", silent=True)
                time.sleep(0.05)
                v = read_words(ser, 91, 1, "", silent=True)[0]
                print(f"    After FC 0x06 write 1 to reg91: reg91 lo-word = 0x{v:04X} ({v})")
            except IOError as e:
                print(f"    FC 0x06 to reg91 raised exception (expected for read-only): {e}")
        except IOError as e:
            print(f"    FAILED: {e}")
        time.sleep(0.05)

        # ── G. Try STANDBY then SP write then OPERATE ─────────────────
        sep("G. Sequence: explicit STANDBY -> SP write -> OPERATE -> PID RUN")
        try:
            print("    [G1] Send STANDBY (reg115=1) ...")
            write_reg(ser, 115, 1, "CH2 STANDBY")
            time.sleep(0.10)
            st = read_words(ser, 84, 4, "", silent=True)
            print(f"    STATUS2=0x{st[0]:04X} operating={bool(st[0]&1)}  PID-state={st[3]}")

            print("    [G2] Write T-mode + Auto mode ...")
            write_reg(ser, 98, 0, "", silent=True)
            write_reg(ser, 97, 1, "", silent=True)
            time.sleep(0.05)

            print(f"    [G3] Write SP = {TARGET_C}C (reg 90-91) ...")
            raw = c_to_raw(TARGET_C)
            hi = (raw >> 16) & 0xFFFF
            lo = raw & 0xFFFF
            req = frame_write_multi(SLAVE_ID, 90, [hi, lo])
            transact(ser, req, 8, "SP write")
            time.sleep(0.30)
            rb = read_int32(ser, 90, "SP readback", silent=True)
            print(f"    SP readback = {raw_to_c(rb):.2f}C  (target={TARGET_C})")
            if abs(raw_to_c(rb) - TARGET_C) < 1.0:
                print("    *** SP ACCEPTED WHILE IN STANDBY! ***")

            print("    [G4] Send OPERATE (reg114=1) then PID RUN (reg116=1) ...")
            write_reg(ser, 114, 1, "CH2 OPERATE")
            time.sleep(0.10)
            write_reg(ser, 116, 1, "CH2 PID RUN")
            time.sleep(0.20)
            st2 = read_words(ser, 84, 4, "", silent=True)
            print(f"    STATUS2=0x{st2[0]:04X} operating={bool(st2[0]&1)}  PID-state={st2[3]}")
            rb2 = read_int32(ser, 90, "final SP readback", silent=True)
            print(f"    SP after OPERATE = {raw_to_c(rb2):.2f}C")

        except IOError as e:
            print(f"    FAILED: {e}")
        time.sleep(0.05)

        # ── H. Final dump ─────────────────────────────────────────────
        sep("H. Final state dump (regs 84-99)")
        try:
            fd = read_words(ser, 84, 16, "final dump", silent=True)
            pv_f = words_to_i32(fd[4], fd[5])
            sp_f = words_to_i32(fd[6], fd[7])
            print(f"    STATUS2=0x{fd[0]:04X} op={bool(fd[0]&1)}  "
                  f"PID-state={fd[3]}  PV={raw_to_c(pv_f):.2f}C  SP={raw_to_c(sp_f):.2f}C")
            print(f"    WorkMode={fd[13]}  PIDtype={fd[14]}")
        except IOError as e:
            print(f"    FAILED: {e}")

    finally:
        ser.close()
        print()
        print("Port closed.")

    print()
    print("=" * 66)
    print("Diagnostic v4 complete.")
    print("=" * 66)


if __name__ == "__main__":
    run()
