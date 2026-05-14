"""
HEAT3 Direct SP Write Test — no PREVAC V2, no MASTER claiming.
Just reads current state, writes SP, and reads back.

Usage:
    python diagnostics/heat3_sp_direct.py [port] [baud] [slave] [target_c]
    python diagnostics/heat3_sp_direct.py COM12 19200 1 100
"""
import sys, struct, time, serial

PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM12"
BAUDRATE = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
SLAVE_ID = int(sys.argv[3]) if len(sys.argv) > 3 else 1
TARGET_C = float(sys.argv[4]) if len(sys.argv) > 4 else 100.0
KELVIN   = 273.15

def _crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def mb_frame_read(slave, reg, n):
    f = struct.pack(">BBHH", slave, 0x03, reg, n)
    return f + struct.pack("<H", _crc16(f))

def mb_frame_write(slave, reg, value):
    f = struct.pack(">BBHH", slave, 0x06, reg, value & 0xFFFF)
    return f + struct.pack("<H", _crc16(f))

def mb_frame_writemulti(slave, reg, words):
    n = len(words)
    data = b''.join(struct.pack(">H", w & 0xFFFF) for w in words)
    body = struct.pack(">BBHHB", slave, 0x10, reg, n, n * 2) + data
    return body + struct.pack("<H", _crc16(body))

def mb_transact(ser, req, expected):
    ser.reset_input_buffer()
    ser.write(req)
    time.sleep(0.1)
    resp = ser.read(max(expected, 8))
    if not resp:
        raise IOError("no response (timeout)")
    if len(resp) >= 3 and (resp[1] & 0x80):
        codes = {1:"Illegal function",2:"Illegal address",3:"Illegal data value",4:"Device failure"}
        raise IOError(f"Modbus exception {codes.get(resp[2], resp[2])}")
    if len(resp) < expected:
        raise IOError(f"short response {len(resp)}/{expected}")
    if _crc16(resp[:expected-2]) != struct.unpack("<H", resp[expected-2:expected])[0]:
        raise IOError("CRC mismatch")
    return resp[:expected]

def read_words(ser, reg, n):
    resp = mb_transact(ser, mb_frame_read(SLAVE_ID, reg, n), 3 + n*2 + 2)
    return [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(n)]

def read_uint16(ser, reg):
    return read_words(ser, reg, 1)[0]

def read_int32(ser, reg):
    w = read_words(ser, reg, 2)
    raw = (w[0] << 16) | w[1]
    return raw - 0x100000000 if raw >= 0x80000000 else raw

def write_word(ser, reg, val):
    mb_transact(ser, mb_frame_write(SLAVE_ID, reg, val), 8)

def write_int32(ser, reg, val):
    raw = val & 0xFFFFFFFF
    hi = (raw >> 16) & 0xFFFF; lo = raw & 0xFFFF
    mb_transact(ser, mb_frame_writemulti(SLAVE_ID, reg, [hi, lo]), 8)

def c_to_raw(c): return int(round((c + KELVIN) * 100))
def raw_to_c(r): return r / 100.0 - KELVIN

def rw(ser, reg, label):
    try:
        v = read_uint16(ser, reg)
        print(f"  {label:<35} = {v}  (0x{v:04X})")
        return v
    except Exception as e:
        print(f"  {label:<35} ERROR: {e}")
        return None

def rw32(ser, reg, label):
    try:
        r = read_int32(ser, reg)
        c = raw_to_c(r)
        print(f"  {label:<35} = {c:.2f}C  (raw={r})")
        return c
    except Exception as e:
        print(f"  {label:<35} ERROR: {e}")
        return None

def ww(ser, reg, val, label):
    try:
        write_word(ser, reg, val)
        print(f"  WRITE {label:<30} = {val}  ACK'd")
    except Exception as e:
        print(f"  WRITE {label:<30} ERROR: {e}")

def ww32(ser, reg, val_raw, label):
    try:
        write_int32(ser, reg, val_raw)
        print(f"  WRITE {label:<30} = raw {val_raw}  ACK'd")
    except Exception as e:
        print(f"  WRITE {label:<30} ERROR: {e}")


def run():
    print("=" * 60)
    print("  HEAT3 Direct SP Write Test (no PREVAC V2, no MASTER claim)")
    print(f"  Port={PORT}  Baud={BAUDRATE}  Slave={SLAVE_ID}  Target={TARGET_C}C")
    print("=" * 60)

    try:
        ser = serial.Serial(port=PORT, baudrate=BAUDRATE,
                            bytesize=8, parity='N', stopbits=1, timeout=1.0)
        print(f"  Port open: {ser.name}\n")
    except serial.SerialException as e:
        print(f"  FAILED to open port: {e}")
        return

    try:
        # ── Initial state ────────────────────────────────────────────
        print("--- INITIAL STATE ---")
        rw(ser,   1000, "reg1000 Remote/MASTER")
        rw(ser,    84,  "reg84  STATUS2")
        rw(ser,    87,  "reg87  PID_STATUS2")
        rw(ser,    97,  "reg97  WMODE2")
        rw32(ser,  88,  "reg88-89 PV2")
        rw32(ser,  90,  "reg90-91 SP2 (current)")
        print()

        # ── Write WMODE=1 (enable SP writing) ────────────────────────
        print("--- WRITE WMODE=1 ---")
        ww(ser, 97, 1, "reg97 WMODE2")
        rw(ser, 97, "reg97  WMODE2 readback")
        print()

        # ── Write SP ─────────────────────────────────────────────────
        sp_raw = c_to_raw(TARGET_C)
        print(f"--- WRITE SP = {TARGET_C}C (raw={sp_raw}) ---")
        ww32(ser, 90, sp_raw, f"reg90-91 SP2 ({TARGET_C}C)")
        time.sleep(0.2)
        sp_rb = rw32(ser, 90, "reg90-91 SP2 readback")
        if sp_rb is not None:
            if abs(sp_rb - TARGET_C) < 1.0:
                print(f"\n  *** SP ACCEPTED: {sp_rb:.2f}C (target={TARGET_C}C) ***")
            else:
                print(f"\n  *** SP REJECTED: readback={sp_rb:.2f}C, target={TARGET_C}C ***")
        print()

        # ── Try OPERATE then SP again if first attempt failed ────────
        if sp_rb is None or abs(sp_rb - TARGET_C) >= 1.0:
            print("--- RETRY: OPERATE first, then SP ---")
            ww(ser, 114, 1, "reg114 OPERATE2")
            time.sleep(0.1)
            ww(ser, 116, 1, "reg116 PIDRUN2")
            time.sleep(0.1)
            ww32(ser, 90, sp_raw, f"reg90-91 SP2 ({TARGET_C}C) retry")
            time.sleep(0.2)
            sp_rb2 = rw32(ser, 90, "reg90-91 SP2 readback after OPERATE")
            if sp_rb2 is not None:
                if abs(sp_rb2 - TARGET_C) < 1.0:
                    print(f"\n  *** SP ACCEPTED after OPERATE: {sp_rb2:.2f}C ***")
                else:
                    print(f"\n  *** SP STILL REJECTED: readback={sp_rb2:.2f}C ***")
            print()

        # ── Final state ───────────────────────────────────────────────
        print("--- FINAL STATE ---")
        rw(ser,   1000, "reg1000 Remote/MASTER")
        rw(ser,    84,  "reg84  STATUS2")
        rw32(ser,  88,  "reg88-89 PV2")
        rw32(ser,  90,  "reg90-91 SP2")

    finally:
        ser.close()
        print("\n  Port closed.")
        print("=" * 60)


if __name__ == "__main__":
    run()
