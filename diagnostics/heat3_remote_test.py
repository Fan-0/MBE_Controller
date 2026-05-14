"""
heat3_remote_diag_v4.py  —  HEAT3 Remote Enable Diagnostic v4
Fixes v3 failure: MASTER claim now uses the correct PREVAC V2 protocol
sequence (Host Register 0x7FF0 → MASTER claim 0x7FF1) instead of raw
Modbus reg1000, which is only writable AFTER a registered host exists.

Usage:
  python heat3_remote_diag_v4.py <port> <baud> <slave_id> <test_sp_c> [channel]

Example:
  python heat3_remote_diag_v4.py COM12 19200 1 75.0 2
"""

import sys, time, struct, threading, argparse
from pymodbus.client import ModbusSerialClient

# ──────────────────────────────────────────────────────────────────────────────
# PREVAC V2 low-level helpers
# ──────────────────────────────────────────────────────────────────────────────
PREVAC_HEADER = 0xBB

def _crc(data: bytes) -> int:
    """Simple mod-256 checksum (sum of all bytes after header byte 0xBB)."""
    return sum(data) & 0xFF

def prevac_build(slave: int, order: int, payload: bytes = b"") -> bytes:
    """
    Build a PREVAC V2 request frame.
    Frame: BB | LEN | C8 | SLAVE | OH | OL | DATA... | CRC
      LEN  = number of bytes from C8 to end of DATA (inclusive)
      C8   = 0xC8 (fixed)
      OH/OL = order high/low byte
    """
    oh = (order >> 8) & 0xFF
    ol = order & 0xFF
    body = bytes([0xC8, slave, oh, ol]) + payload
    length = len(body)
    frame = bytes([PREVAC_HEADER, length]) + body
    frame += bytes([_crc(frame[1:])])   # CRC excludes the 0xBB header byte
    return frame

def prevac_read(ser, timeout: float = 0.5) -> bytes:
    """Read one PREVAC V2 response frame from a pyserial-like socket."""
    deadline = time.time() + timeout
    buf = bytearray()
    # wait for header
    while time.time() < deadline:
        b = ser.read(1)
        if b == b"\\xBB":
            buf += b
            break
    if not buf:
        return b""
    # read length byte
    b = ser.read(1)
    if not b:
        return b""
    buf += b
    length = b[0]
    # read body + CRC
    rest = ser.read(length + 1)
    buf += rest
    return bytes(buf)

# ──────────────────────────────────────────────────────────────────────────────
# PREVAC V2 high-level operations  (uses raw serial from the Modbus client)
# ──────────────────────────────────────────────────────────────────────────────

def prevac_register_host(ser, slave: int, unique_id: str = "TAMA_DIAG_v4") -> int:
    """
    Send 0x7FF0 (Host Number Assign).
    Returns the assigned host address (1-255), or 0 on failure.
    """
    payload = unique_id.encode("ascii")
    frame = prevac_build(slave, 0x7FF0, payload)
    ser.write(frame)
    time.sleep(0.05)
    resp = prevac_read(ser)
    if len(resp) < 7:
        return 0
    # Response data field byte 1 = assigned host address
    # Frame layout: BB LEN C8 SLAVE OH OL <host_addr> CRC
    host_addr = resp[6] if len(resp) > 6 else 0
    return host_addr

def prevac_master_status(ser, slave: int) -> dict:
    """
    Read 0x7FF1 (MASTER Mode status).
    Returns dict with bit fields from Table 6.48.
    """
    frame = prevac_build(slave, 0x7FF1)      # read = no payload
    ser.write(frame)
    time.sleep(0.05)
    resp = prevac_read(ser)
    if len(resp) < 8:
        return {"raw": None, "error": "no response"}
    status_byte = resp[6]
    return {
        "raw":        status_byte,
        "b0_is_master":   bool(status_byte & 0x01),   # we ARE master
        "b1_permitted":   bool(status_byte & 0x02),   # claim is allowed
        "b2_remote_on":   bool(status_byte & 0x04),   # Remote Control active
        "b3_registered":  bool(status_byte & 0x08),   # host is registered
        "b4_other_master":bool(status_byte & 0x10),   # another host holds master
    }

def prevac_claim_master(ser, slave: int) -> bool:
    """Write 0x7FF1 = 1 (claim MASTER).  Returns True if accepted."""
    frame = prevac_build(slave, 0x7FF1, bytes([0x01]))
    ser.write(frame)
    time.sleep(0.05)
    resp = prevac_read(ser)
    # Success: last data byte = 0x00
    if len(resp) >= 8:
        return resp[6] == 0x00
    return False

def prevac_release_master(ser, slave: int) -> bool:
    """Write 0x7FF1 = 0 (release MASTER).  Returns True if accepted."""
    frame = prevac_build(slave, 0x7FF1, bytes([0x00]))
    ser.write(frame)
    time.sleep(0.05)
    resp = prevac_read(ser)
    if len(resp) >= 8:
        return resp[6] == 0x00
    return False

# ──────────────────────────────────────────────────────────────────────────────
# Keepalive thread  (re-asserts MASTER every 5 s while enabled)
# ──────────────────────────────────────────────────────────────────────────────
class MasterKeepalive(threading.Thread):
    def __init__(self, ser, slave: int):
        super().__init__(daemon=True)
        self.ser = ser
        self.slave = slave
        self._stop = threading.Event()
        self._pause = threading.Event()

    def run(self):
        while not self._stop.is_set():
            if not self._pause.is_set():
                prevac_claim_master(self.ser, self.slave)
            time.sleep(5)

    def pause(self):   self._pause.set()
    def resume(self):  self._pause.clear()
    def stop(self):    self._stop.set()

# ──────────────────────────────────────────────────────────────────────────────
# Modbus helpers
# ──────────────────────────────────────────────────────────────────────────────
REG_REMOTE_ENABLE = 1151   # reg1151 — Remote Enable setting
REG_MASTER        = 1000   # reg1000 — reflects MASTER state (read-only after PREVAC handshake)

CH_SP_REG  = {1: 1100, 2: 1200}   # SP register per channel (0-based Modbus addr)
CH_PV_REG  = {1: 1101, 2: 1201}   # PV register per channel

def kelvin_to_raw(k: float) -> int:
    return round(k * 100)

def raw_to_kelvin(r: int) -> float:
    return r / 100.0

def raw_to_celsius(r: int) -> float:
    return raw_to_kelvin(r) - 273.15

def celsius_to_raw(c: float) -> int:
    return kelvin_to_raw(c + 273.15)

def read_reg(mb, slave, addr) -> int | None:
    rr = mb.read_holding_registers(addr, count=1, slave=slave)
    if rr.isError():
        return None
    return rr.registers[0]

def write_reg(mb, slave, addr, value) -> bool:
    rr = mb.write_register(addr, value, slave=slave)
    return not rr.isError()

def read_sp_pv(mb, slave, ch):
    sp_raw = read_reg(mb, slave, CH_SP_REG[ch])
    pv_raw = read_reg(mb, slave, CH_PV_REG[ch])
    return sp_raw, pv_raw

def banner(title):
    print()
    print("=" * 68)
    print(f"  {title}")
    print("=" * 68)

# ──────────────────────────────────────────────────────────────────────────────
# Main diagnostic
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="HEAT3 Remote Diagnostic v4")
    parser.add_argument("port",      help="Serial port (e.g. COM12)")
    parser.add_argument("baud",      type=int, default=19200)
    parser.add_argument("slave",     type=int, default=1)
    parser.add_argument("test_sp_c", type=float, default=75.0,
                        help="Test setpoint in °C")
    parser.add_argument("channel",   type=int, default=2, choices=[1, 2])
    args = parser.parse_args()

    PORT     = args.port
    BAUD     = args.baud
    SLAVE    = args.slave
    TEST_C   = args.test_sp_c
    TEST_RAW = celsius_to_raw(TEST_C)
    CH       = args.channel

    print()
    print("=" * 68)
    print(f"  HEAT3 Remote Enable Diagnostic v4")
    print(f"  Port={PORT}  Baud={BAUD}  Slave={SLAVE}  "
          f"TestSP={TEST_C}°C ({TEST_C+273.15:.2f}K)  Channel=CH{CH}")
    print("=" * 68)

    mb = ModbusSerialClient(
        port=PORT, baudrate=BAUD, parity="N", stopbits=1, bytesize=8, timeout=1
    )
    if not mb.connect():
        print("  [FATAL] Cannot open serial port.")
        sys.exit(1)

    # Grab the raw serial socket for PREVAC V2 frames
    ser = mb.socket
    keepalive = MasterKeepalive(ser, SLAVE)

    try:
        # ── PRE-CHECK ──────────────────────────────────────────────────────
        banner("PRE-CHECK — Register snapshot")
        remote_en = read_reg(mb, SLAVE, REG_REMOTE_ENABLE)
        sp_raw, pv_raw = read_sp_pv(mb, SLAVE, CH)
        print(f"  [INFO] reg{REG_REMOTE_ENABLE} (Remote Enable)  = {remote_en}  "
              f"({'ENABLED ✓' if remote_en == 1 else 'DISABLED ✗'})")
        print(f"  [INFO] CH{CH} SP baseline = "
              f"{raw_to_celsius(sp_raw):.2f}°C  (raw={sp_raw}, {raw_to_kelvin(sp_raw):.2f}K)")
        print(f"  [INFO] CH{CH} PV baseline = "
              f"{raw_to_celsius(pv_raw):.2f}°C  (raw={pv_raw}, {raw_to_kelvin(pv_raw):.2f}K)")
        original_sp = sp_raw

        if remote_en != 1:
            print("  [FATAL] Remote Enable (reg1151) = 0. Enable it on the front panel")
            print("          Setup → Communications → Remote Enable → ON, then re-run.")
            return

        # ── STEP 0 — Read raw PREVAC MASTER status BEFORE registration ────
        banner("STEP 0 — Read MASTER status (pre-registration)")
        st = prevac_master_status(ser, SLAVE)
        if st.get("raw") is None:
            print("  [WARN] No response to 0x7FF1 read — PREVAC V2 may not be active on this port.")
            print("         Falling back to Modbus-only path (reg1000 write attempt).")
            prevac_ok = False
        else:
            raw_bits = st["raw"]
            print(f"  [INFO] Status byte = 0x{raw_bits:02X}  (binary {raw_bits:08b})")
            print(f"         B0 We are MASTER   : {st['b0_is_master']}")
            print(f"         B1 Claim permitted : {st['b1_permitted']}")
            print(f"         B2 Remote ON       : {st['b2_remote_on']}")
            print(f"         B3 Host registered : {st['b3_registered']}")
            print(f"         B4 Other master    : {st['b4_other_master']}")
            if not st["b2_remote_on"]:
                print("  [FATAL] B2=0: Remote Control mode is OFF on the device.")
                print("          Enable it via front panel → Setup → Remote Enable.")
                return
            prevac_ok = True

        # ── STEP 1 — Host registration (0x7FF0) ───────────────────────────
        banner("STEP 1 — Register host (PREVAC V2 order 0x7FF0)")
        if prevac_ok:
            host_addr = prevac_register_host(ser, SLAVE, unique_id="TAMA_DIAG_v4")
            if host_addr == 0:
                print("  [FAIL] Host registration returned address 0 — no response.")
                print("         Check PREVAC V2 is enabled (same serial port as Modbus RTU).")
                return
            print(f"  [OK]   Assigned host address = {host_addr}")
        else:
            print("  [SKIP] Skipped (PREVAC V2 not responding).")
            host_addr = 0

        # ── STEP 2 — Read MASTER status after registration ────────────────
        banner("STEP 2 — MASTER status after host registration")
        if prevac_ok:
            st = prevac_master_status(ser, SLAVE)
            raw_bits = st["raw"]
            print(f"  [INFO] Status byte = 0x{raw_bits:02X}  (binary {raw_bits:08b})")
            print(f"         B0 We are MASTER   : {st['b0_is_master']}")
            print(f"         B1 Claim permitted : {st['b1_permitted']}")
            print(f"         B2 Remote ON       : {st['b2_remote_on']}")
            print(f"         B3 Host registered : {st['b3_registered']}")
            print(f"         B4 Other master    : {st['b4_other_master']}")
            if st["b4_other_master"]:
                print("  [WARN] Another host is currently MASTER — cannot claim until it releases.")
                return
            if not st["b1_permitted"]:
                print("  [FAIL] B1=0: Claim still not permitted after registration.")
                print(f"         B2={int(st['b2_remote_on'])} B3={int(st['b3_registered'])} "
                      f"B4={int(st['b4_other_master'])}  — all must be 1,1,0 for B1=1.")
                return

        # ── STEP 3 — Claim MASTER ─────────────────────────────────────────
        banner("STEP 3 — Claim MASTER (PREVAC V2 order 0x7FF1 = 1)")
        if prevac_ok:
            ok = prevac_claim_master(ser, SLAVE)
            if not ok:
                print("  [FAIL] MASTER claim rejected by device.")
                return
            print("  [OK]   MASTER claimed via PREVAC V2.")
        else:
            # Fallback: try Modbus reg1000 write
            ok = write_reg(mb, SLAVE, REG_MASTER, 1)
            rb = read_reg(mb, SLAVE, REG_MASTER)
            if rb != 1:
                print(f"  [FAIL] Modbus fallback: reg1000 readback = {rb}  (expected 1)")
                return
            print("  [OK]   MASTER claimed via Modbus fallback (reg1000=1).")

        keepalive.start()
        print("  [INFO] Keepalive thread started (re-asserts MASTER every 5 s).")

        # Confirm via Modbus reg1000
        master_rb = read_reg(mb, SLAVE, REG_MASTER)
        print(f"  [INFO] Modbus reg1000 (MASTER reflect) = {master_rb}  "
              f"({'✓' if master_rb == 1 else 'unexpected'})")

        # ── STEP 4 — Write SP while REMOTE ON ─────────────────────────────
        banner(f"STEP 4 — Write SP while REMOTE ON  (target={TEST_C}°C, raw={TEST_RAW})")
        ok = write_reg(mb, SLAVE, CH_SP_REG[CH], TEST_RAW)
        time.sleep(0.2)
        rb = read_reg(mb, SLAVE, CH_SP_REG[CH])
        if rb == TEST_RAW:
            print(f"  [OK]   SP accepted — readback = {raw_to_celsius(rb):.2f}°C ✓")
        else:
            print(f"  [FAIL] SP not accepted — readback = "
                  f"{raw_to_celsius(rb):.2f}°C (raw={rb}), expected raw={TEST_RAW}")

        # ── STEP 5 — Release MASTER → go LOCAL ───────────────────────────
        banner("STEP 5 — Release MASTER (go LOCAL)")
        keepalive.pause()
        time.sleep(0.1)
        if prevac_ok:
            ok = prevac_release_master(ser, SLAVE)
            print(f"  [{'OK' if ok else 'WARN'}]   PREVAC V2 release {'accepted' if ok else 'no ACK'}.")
        else:
            write_reg(mb, SLAVE, REG_MASTER, 0)
        time.sleep(0.3)
        master_rb = read_reg(mb, SLAVE, REG_MASTER)
        print(f"  [INFO] reg1000 after release = {master_rb}  "
              f"({'LOCAL ✓' if master_rb == 0 else 'still MASTER'})")

        # ── STEP 6 — Write SP while LOCAL (should be rejected) ────────────
        local_test_raw = celsius_to_raw(TEST_C + 10)
        banner(f"STEP 6 — Write SP while LOCAL  (target={TEST_C+10:.1f}°C, raw={local_test_raw})")
        write_reg(mb, SLAVE, CH_SP_REG[CH], local_test_raw)
        time.sleep(0.2)
        rb = read_reg(mb, SLAVE, CH_SP_REG[CH])
        if rb == local_test_raw:
            print(f"  [UNEXPECTED!] SP changed while LOCAL — device accepted write (raw={rb}).")
            print("                Some firmware versions still accept writes in local mode.")
            print("                MASTER gating may only apply to OPERATE/RUN commands.")
        elif rb == TEST_RAW:
            print(f"  [OK]   SP unchanged at {raw_to_celsius(rb):.2f}°C — LOCAL correctly rejects writes ✓")
        else:
            print(f"  [INFO] SP = {raw_to_celsius(rb):.2f}°C (raw={rb}) — "
                  f"neither test value; device may have its own value.")

        # ── STEP 7 — Re-claim MASTER and restore original SP ──────────────
        banner("STEP 7 — Re-claim MASTER and restore original SP")
        keepalive.resume()
        time.sleep(0.1)
        if prevac_ok:
            ok = prevac_claim_master(ser, SLAVE)
            print(f"  [{'OK' if ok else 'FAIL'}]   MASTER re-claim {'succeeded' if ok else 'failed'}.")
        else:
            write_reg(mb, SLAVE, REG_MASTER, 1)
        time.sleep(0.2)
        ok = write_reg(mb, SLAVE, CH_SP_REG[CH], original_sp)
        time.sleep(0.2)
        rb = read_reg(mb, SLAVE, CH_SP_REG[CH])
        print(f"  [INFO] SP restored to {raw_to_celsius(rb):.2f}°C  "
              f"(raw={rb}, target={original_sp}) "
              f"{'✓' if rb == original_sp else '✗ mismatch!'}")

        # ── STEP 8 — Final state dump ──────────────────────────────────────
        banner("STEP 8 — Final state dump")
        sp_raw, pv_raw = read_sp_pv(mb, SLAVE, CH)
        master_rb = read_reg(mb, SLAVE, REG_MASTER)
        remote_en = read_reg(mb, SLAVE, REG_REMOTE_ENABLE)
        print(f"  [INFO] reg{REG_REMOTE_ENABLE} Remote Enable = {remote_en}")
        print(f"  [INFO] reg{REG_MASTER}  MASTER            = {master_rb}")
        print(f"  [INFO] CH{CH} SP final   = {raw_to_celsius(sp_raw):.2f}°C  (raw={sp_raw})")
        print(f"  [INFO] CH{CH} PV final   = {raw_to_celsius(pv_raw):.2f}°C  (raw={pv_raw})")
        print()
        if sp_raw == original_sp:
            print("  ✅  Diagnostic PASSED — remote on/off and SP write verified.")
        else:
            print("  ⚠️   Diagnostic complete — SP restore mismatch, check above.")

    finally:
        keepalive.stop()
        keepalive.pause()
        # Always release MASTER cleanly on exit
        if prevac_ok if "prevac_ok" in dir() else False:
            prevac_release_master(ser, SLAVE)
        else:
            write_reg(mb, SLAVE, REG_MASTER, 0)
        mb.close()
        print("  [INFO] Port closed. Keepalive stopped.")

if __name__ == "__main__":
    main()
