"""
HEAT3 Remote-Mode Watchdog Diagnostic
======================================
Simulates every step of the main app's startup/control sequence and checks
whether the device is still in REMOTE MASTER mode after each command.

KEY DISCOVERY (findings_summary.txt):
  The HEAT3 uses TWO protocols on the same RS232 port:
    1. Modbus RTU  — for all register reads/writes
    2. PREVAC V2   — for host registration + MASTER claim/release

  Modbus reg1000 is READ-ONLY — it reflects the current MASTER state but
  cannot SET it.  Writing 1 to reg1000 via Modbus does NOTHING.
  The only way to claim MASTER is via PREVAC V2 order 0x7FF1.

  Without a valid PREVAC V2 MASTER claim, ALL write operations are silently
  ignored by the device firmware (SP writes ACK'd but not applied).

PREVAC V2 Frame format:
  BB | LEN | C8 | SLAVE | OH | OL | DATA... | CRC
    BB     = 0xBB (sync byte, not counted in LEN or CRC)
    LEN    = count of bytes from C8 to end of DATA inclusive
    C8     = 0xC8 (fixed protocol marker)
    SLAVE  = Modbus slave address
    OH, OL = order number, big-endian
    DATA   = optional payload
    CRC    = (LEN + C8 + SLAVE + OH + OL + DATA...) mod 256

  Key orders:
    0x7FF0 — Host Registration (write unique ASCII id → device returns host #)
    0x7FF1 — MASTER mode
              READ  → status byte: B0=we-are-MASTER, B1=claim-permitted,
                                   B2=remote-ON, B3=registered, B4=other-holds
              WRITE 0x01 → claim MASTER
              WRITE 0x00 → release MASTER

Usage:
    python diagnostics/heat3_remote_watchdog.py [port] [baud] [slave] [target_c]
    python diagnostics/heat3_remote_watchdog.py COM12 19200 1 100
"""

import sys, struct, time, serial, io
# Force UTF-8 output so box-drawing chars survive Windows console encoding
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

PORT     = sys.argv[1] if len(sys.argv) > 1 else "COM12"
BAUDRATE = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
SLAVE_ID = int(sys.argv[3]) if len(sys.argv) > 3 else 1
TARGET_C = float(sys.argv[4]) if len(sys.argv) > 4 else 100.0
KELVIN   = 273.15
HOST_ID  = b"TAMA_LABS_DIAG"    # unique name sent to device for registration


# ── Modbus helpers ────────────────────────────────────────────────────────────

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
    time.sleep(0.08)
    resp = ser.read(max(expected, 8))
    if not resp:
        raise IOError("no response (timeout)")
    if len(resp) >= 3 and (resp[1] & 0x80):
        codes = {1: "Illegal function", 2: "Illegal address",
                 3: "Illegal data value", 4: "Device failure", 5: "Busy"}
        code = resp[2] if len(resp) > 2 else 0
        raise IOError(f"Modbus exception {codes.get(code, code)} (code={code})")
    if len(resp) < expected:
        raise IOError(f"short response {len(resp)}/{expected} bytes")
    if _crc16(resp[:expected - 2]) != struct.unpack("<H", resp[expected-2:expected])[0]:
        raise IOError("CRC mismatch")
    return resp[:expected]

def mb_read_words(ser, reg, n):
    resp = mb_transact(ser, mb_frame_read(SLAVE_ID, reg, n), 3 + n * 2 + 2)
    return [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(n)]

def mb_read_uint16(ser, reg):
    return mb_read_words(ser, reg, 1)[0]

def mb_read_int32(ser, reg):
    w = mb_read_words(ser, reg, 2)
    raw = (w[0] << 16) | w[1]
    return raw - 0x100000000 if raw >= 0x80000000 else raw

def mb_write_word(ser, reg, val):
    mb_transact(ser, mb_frame_write(SLAVE_ID, reg, val), 8)

def mb_write_int32(ser, reg, val):
    raw = val & 0xFFFFFFFF
    hi = (raw >> 16) & 0xFFFF; lo = raw & 0xFFFF
    mb_transact(ser, mb_frame_writemulti(SLAVE_ID, reg, [hi, lo]), 8)

def c_to_raw(c): return int(round((c + KELVIN) * 100))
def raw_to_c(r): return r / 100.0 - KELVIN


# ── PREVAC V2 helpers ─────────────────────────────────────────────────────────

def pv2_frame(slave, order, data=b""):
    """Build a PREVAC V2 request frame."""
    oh = (order >> 8) & 0xFF
    ol = order & 0xFF
    payload = bytes([0xC8, slave, oh, ol]) + data
    length = len(payload)
    crc = (length + sum(payload)) & 0xFF
    return bytes([0xBB, length]) + payload + bytes([crc])

def pv2_transact(ser, frame, timeout=1.0):
    """
    Send a PREVAC V2 frame, return raw response bytes (everything after 0xBB).
    Returns None if no valid BB-prefixed response received.
    """
    ser.reset_input_buffer()
    ser.write(frame)
    time.sleep(0.12)
    raw = ser.read(64)
    if not raw:
        return None
    # Find 0xBB sync byte
    for i, b in enumerate(raw):
        if b == 0xBB:
            resp = raw[i:]
            if len(resp) >= 2:
                return resp
    return None

def pv2_decode_master_status(data_byte):
    """
    Decode the 0x7FF1 status byte into human-readable fields.
    Returns dict with b0..b4 keys and a summary string.
    """
    b = data_byte
    d = {
        "b0_we_are_master":    bool(b & 0x01),
        "b1_claim_permitted":  bool(b & 0x02),
        "b2_remote_on":        bool(b & 0x04),
        "b3_registered":       bool(b & 0x08),
        "b4_other_is_master":  bool(b & 0x10),
        "raw":                 b,
    }
    parts = []
    parts.append("MASTER=YES" if d["b0_we_are_master"] else "MASTER=no")
    parts.append("REMOTE=ON"  if d["b2_remote_on"]     else "REMOTE=off")
    parts.append("REGISTERED" if d["b3_registered"]    else "not-registered")
    parts.append("CLAIM-OK"   if d["b1_claim_permitted"] else "claim-blocked")
    if d["b4_other_is_master"]: parts.append("OTHER-HOLDS-MASTER")
    d["summary"] = " | ".join(parts)
    return d

def pv2_read_master_status(ser):
    """
    Read PREVAC V2 0x7FF1 and return decoded dict, or None if no PREVAC V2.
    """
    frame = pv2_frame(SLAVE_ID, 0x7FF1)
    resp  = pv2_transact(ser, frame)
    if resp is None:
        return None
    # Extract data bytes after BB LEN C8 SLAVE OH OL  (6 header bytes after BB)
    # Response: BB | LEN | C8 | SLAVE | OH | OL | STATUS? | DATA | CRC
    # LEN = bytes from C8 to CRC-1 (exclusive of CRC)
    if len(resp) < 3:
        return None
    body_len = resp[1]  # LEN
    if len(resp) < 2 + body_len + 1:
        return None
    # Data bytes start at index 6 (after BB LEN C8 SLAVE OH OL)
    data_start = 6
    data_end   = 2 + body_len  # BB + LEN + payload
    data_bytes = resp[data_start:data_end]
    if not data_bytes:
        return None
    return pv2_decode_master_status(data_bytes[0])

def pv2_register_host(ser):
    """
    Send PREVAC V2 0x7FF0 (Host Registration) with HOST_ID.
    Returns assigned host number (int) or None on failure.
    """
    frame = pv2_frame(SLAVE_ID, 0x7FF0, HOST_ID)
    resp  = pv2_transact(ser, frame)
    if resp is None:
        return None
    if len(resp) < 2:
        return None
    body_len = resp[1]
    if len(resp) < 2 + body_len + 1:
        return None
    data_start = 6
    data_end   = 2 + body_len
    data_bytes = resp[data_start:data_end]
    if not data_bytes:
        return None
    return data_bytes[0]  # assigned host number 1-255

def pv2_claim_master(ser):
    """Send PREVAC V2 0x7FF1 = 0x01 to claim MASTER. Returns True on success."""
    frame = pv2_frame(SLAVE_ID, 0x7FF1, bytes([0x01]))
    resp  = pv2_transact(ser, frame)
    time.sleep(0.15)
    # Verify by reading status back
    st = pv2_read_master_status(ser)
    if st is None:
        return False
    return st["b0_we_are_master"]

def pv2_release_master(ser):
    """Send PREVAC V2 0x7FF1 = 0x00 to release MASTER."""
    frame = pv2_frame(SLAVE_ID, 0x7FF1, bytes([0x00]))
    pv2_transact(ser, frame)
    time.sleep(0.10)


# ── Watchdog: check + log MASTER status ──────────────────────────────────────

_pv2_available = False
_step_results = []  # list of (step_name, master_ok, detail)

def check_master(ser, label):
    """
    Check current MASTER state via PREVAC V2 (if available) and via Modbus reg1000.
    Print a clear one-line status and record result.
    """
    pv2_ok = None
    mb_reflect = None

    if _pv2_available:
        st = pv2_read_master_status(ser)
        if st:
            pv2_ok = st["b0_we_are_master"]

    try:
        mb_reflect = mb_read_uint16(ser, 1000)
    except Exception:
        pass

    # Determine overall status
    if pv2_ok is True:
        overall = "MASTER=YES"
        ok = True
    elif pv2_ok is False:
        overall = "MASTER=NO !"
        ok = False
    else:
        # PREVAC V2 unavailable — fall back to reg1000 mirror
        if mb_reflect is not None:
            overall = f"reg1000={mb_reflect} (PREVAC V2 unavailable)"
            ok = (mb_reflect == 1)
        else:
            overall = "UNKNOWN (no response)"
            ok = False

    pv2_str = f"PV2={pv2_ok}" if pv2_ok is not None else "PV2=n/a"
    mb_str  = f"reg1000={mb_reflect}" if mb_reflect is not None else "reg1000=err"

    marker = "   OK" if ok else "  !!! DROPPED !!!"
    print(f"  [{overall}]{marker}   after: {label}   ({pv2_str}, {mb_str})")
    _step_results.append((label, ok, overall))
    return ok


# ── Main watchdog ─────────────────────────────────────────────────────────────

def run():
    global _pv2_available

    print("=" * 70)
    print("  HEAT3 Remote-Mode Watchdog Diagnostic")
    print(f"  Port={PORT}  Baud={BAUDRATE}  Slave={SLAVE_ID}  Target={TARGET_C}C")
    print("=" * 70)
    print()
    print("  Checking MASTER status after every command.")
    print("  Any 'DROPPED' line = the operation before it killed MASTER.")
    print()

    try:
        ser = serial.Serial(port=PORT, baudrate=BAUDRATE,
                            bytesize=8, parity='N', stopbits=1, timeout=1.0)
        print(f"  Serial port open: {ser.name}")
    except serial.SerialException as e:
        print(f"  FAILED to open port: {e}")
        return

    try:
        # ── PHASE 1: Establish PREVAC V2 session ─────────────────────────
        print()
        print("─" * 70)
        print("  PHASE 1 — PREVAC V2 host registration + MASTER claim")
        print("─" * 70)

        # 1a. Read initial status (before any action)
        print("\n  [1a] Initial PREVAC V2 status (before registration) ...")
        st0 = pv2_read_master_status(ser)
        if st0 is None:
            print("      No PREVAC V2 response.")
            print("      => Check front panel: Setup -> Communication -> Protocol")
            print("         Must include 'PREVAC V2' (not Modbus-only).")
            print("      => Falling back to Modbus-only diagnostics.")
            _pv2_available = False
        else:
            _pv2_available = True
            print(f"      Status byte = 0x{st0['raw']:02X}  |  {st0['summary']}")
            if not st0["b2_remote_on"]:
                print()
                print("      *** REMOTE CONTROL IS OFF ON THE DEVICE ***")
                print("      Go to front panel: Setup -> Device Settings -> Communication")
                print("      -> Remote Enable -> ON")
                print("      Then re-run this script.")
                return

        # 1b. Host registration (0x7FF0)
        if _pv2_available:
            print(f"\n  [1b] Registering host ID '{HOST_ID.decode()}' (order 0x7FF0) ...")
            host_num = pv2_register_host(ser)
            if host_num is None:
                print("      No response or invalid response to 0x7FF0.")
                print("      Continuing without registration (will check if MASTER claim works).")
            else:
                print(f"      Assigned host number = {host_num}")

        # 1c. Re-read status (post-registration)
        if _pv2_available:
            print("\n  [1c] PREVAC V2 status after registration ...")
            st1 = pv2_read_master_status(ser)
            if st1:
                print(f"      Status byte = 0x{st1['raw']:02X}  |  {st1['summary']}")
                if not st1["b3_registered"]:
                    print("      WARNING: B3 (registered) is still 0 after 0x7FF0.")
                    print("      Registration may have failed — trying MASTER claim anyway.")
                if st1["b4_other_is_master"]:
                    print("      WARNING: Another host currently holds MASTER.")
                    print("      Close any other connection to the device and re-run.")
                    return

        # 1d. Claim MASTER (0x7FF1 = 0x01)
        if _pv2_available:
            print("\n  [1d] Claiming MASTER (order 0x7FF1 = 0x01) ...")
            ok = pv2_claim_master(ser)
            st2 = pv2_read_master_status(ser)
            if st2:
                print(f"      Status byte = 0x{st2['raw']:02X}  |  {st2['summary']}")
            if ok:
                print("      MASTER CLAIMED via PREVAC V2.")
            else:
                print("      MASTER CLAIM FAILED.")
                print("      B1 (permitted) must be 1, which requires B2+B3=1 and B4=0.")
                if st2 and not st2["b1_claim_permitted"]:
                    print(f"      B2(remote_on)={st2['b2_remote_on']}  "
                          f"B3(registered)={st2['b3_registered']}  "
                          f"B4(other_master)={st2['b4_other_is_master']}")
                print("      Continuing with Modbus-only diagnostics for comparison.")
        else:
            # No PREVAC V2: try Modbus reg1000 write (old method, likely does nothing)
            print("\n  [1d] Trying Modbus reg1000 = 1 (old method, likely ignored) ...")
            try:
                mb_write_word(ser, 1000, 1)
                v = mb_read_uint16(ser, 1000)
                print(f"      reg1000 readback = {v}  ({'MASTER' if v else 'not MASTER'})")
            except Exception as e:
                print(f"      Failed: {e}")

        # ── PHASE 2: Simulate main app startup sequence ───────────────────
        print()
        print("─" * 70)
        print("  PHASE 2 — Simulate main app sequence (checking MASTER after each)")
        print("─" * 70)

        baseline = check_master(ser, "baseline (after MASTER claim)")
        print()

        # Sequence mirrors connect() + apply_setpoint() + go_operate() from heat3.py

        # connect() step: write_word(R_REMOTE=1000, 1)
        print("  Simulating heat3.py connect(): write_word(reg1000, 1) ...")
        try:
            mb_write_word(ser, 1000, 1)
            print("    ACK'd")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "connect(): write_word(reg1000=1000, 1)")

        print()
        # apply_setpoint() step 2: write_word(WMODE=97, 1)
        print("  Simulating apply_setpoint(): write_word(reg97/WMODE2, 1) ...")
        try:
            mb_write_word(ser, 97, 1)
            print("    ACK'd")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "apply_setpoint(): write_word(WMODE2=97, 1)")

        print()
        # apply_setpoint() step 3: write_int32(SP=90, raw_val)
        sp_raw = c_to_raw(TARGET_C)
        hi = (sp_raw >> 16) & 0xFFFF; lo = sp_raw & 0xFFFF
        print(f"  Simulating apply_setpoint(): write_int32(reg90, {TARGET_C}C = raw {sp_raw}) ...")
        try:
            mb_write_int32(ser, 90, sp_raw)
            print("    ACK'd")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, f"apply_setpoint(): write_int32(SP2=90, {TARGET_C}C)")

        print()
        # apply_setpoint() step 4: read_int32(SP=90) — readback
        print("  Simulating apply_setpoint(): read_int32(reg90) readback ...")
        try:
            rb_raw = mb_read_int32(ser, 90)
            rb_c = raw_to_c(rb_raw)
            diff = abs(rb_c - TARGET_C)
            sp_ok = diff < 1.0
            print(f"    Readback = {rb_c:.2f}C  (target={TARGET_C}C  diff={diff:.1f})")
            if sp_ok:
                print("    *** SP WRITE ACCEPTED — PREVAC V2 handshake working! ***")
            else:
                print("    SP readback mismatch — write still not taking effect.")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "apply_setpoint(): read_int32(SP2=90) readback")

        print()
        # go_operate() step 1: write_word(R_REMOTE=1000, 1) again
        print("  Simulating go_operate(): write_word(reg1000, 1) ...")
        try:
            mb_write_word(ser, 1000, 1)
            print("    ACK'd")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "go_operate(): write_word(reg1000=1000, 1)")

        print()
        # go_operate() step 2: write_word(WMODE=97, 1)
        print("  Simulating go_operate(): write_word(reg97/WMODE2, 1) ...")
        try:
            mb_write_word(ser, 97, 1)
            print("    ACK'd")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "go_operate(): write_word(WMODE2=97, 1)")

        print()
        # go_operate() step 3: write_word(OPERATE=114, 1)
        print("  Simulating go_operate(): write_word(reg114/OPERATE, 1) ...")
        try:
            mb_write_word(ser, 114, 1)
            print("    ACK'd")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "go_operate(): write_word(OPERATE2=114, 1)")

        print()
        # go_operate() step 4: write_word(PIDRUN=116, 1)
        print("  Simulating go_operate(): write_word(reg116/PIDRUN, 1) ...")
        try:
            mb_write_word(ser, 116, 1)
            print("    ACK'd")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "go_operate(): write_word(PIDRUN2=116, 1)")

        print()
        # Poller reads: read_int32(PV=88), read_int32(SP=90), read_uint16(STATUS=84)
        print("  Simulating 1s poller reads (PV, SP, STATUS) ...")
        try:
            pv = raw_to_c(mb_read_int32(ser, 88))
            sp = raw_to_c(mb_read_int32(ser, 90))
            st = mb_read_uint16(ser, 84)
            print(f"    PV={pv:.2f}C  SP={sp:.2f}C  STATUS=0x{st:04X}  operating={bool(st&1)}")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "poller: read PV/SP/STATUS")

        print()
        # ── PHASE 3: Test write_word(reg1000, 0) — the old disconnect bug ──
        print("─" * 70)
        print("  PHASE 3 — Danger test: write_word(reg1000, 0)  [old disconnect bug]")
        print("─" * 70)
        print()
        print("  This tests what happens when the old disconnect() wrote reg1000=0.")
        print("  Expected: MASTER should drop (or Remote Enable gets permanently disabled).")
        print()
        print("  write_word(reg1000, 0) ...")
        try:
            mb_write_word(ser, 1000, 0)
            print("    ACK'd")
        except Exception as e:
            print(f"    Exception: {e}")
        check_master(ser, "write_word(reg1000=1000, 0)  [OLD DISCONNECT BUG]")

        # Try to re-claim if dropped
        print()
        if _pv2_available:
            print("  Re-claiming MASTER after the disconnect-bug test ...")
            ok_reclaim = pv2_claim_master(ser)
            st_r = pv2_read_master_status(ser)
            if st_r:
                print(f"    Status = 0x{st_r['raw']:02X}  |  {st_r['summary']}")
            check_master(ser, "re-claim after disconnect-bug test")

        # ── PHASE 4: Summary ──────────────────────────────────────────────
        print()
        print("═" * 70)
        print("  SUMMARY — steps that changed MASTER state:")
        print("═" * 70)
        prev_ok = None
        for (label, ok, detail) in _step_results:
            if prev_ok is not None and ok != prev_ok:
                change = "DROPPED" if not ok else "RESTORED"
                print(f"  *** {change} after: {label}")
            prev_ok = ok
        print()
        drops = [label for (label, ok, _) in _step_results if not ok]
        if not drops:
            print("  No drops detected — MASTER held through all steps.")
        else:
            print(f"  Total drops at: {drops}")
        print()
        # SP verdict
        try:
            final_sp = raw_to_c(mb_read_int32(ser, 90))
            final_pv = raw_to_c(mb_read_int32(ser, 88))
            print(f"  Final SP = {final_sp:.2f}C   PV = {final_pv:.2f}C   Target = {TARGET_C}C")
            if abs(final_sp - TARGET_C) < 1.0:
                print("  RESULT: SETPOINT ACCEPTED")
            else:
                print("  RESULT: SETPOINT STILL NOT APPLIED — check PREVAC V2 handshake above.")
        except Exception as e:
            print(f"  Final read error: {e}")

    finally:
        ser.close()
        print()
        print("  Port closed (MASTER releases automatically after 10s silence).")
        print("═" * 70)


if __name__ == "__main__":
    run()
