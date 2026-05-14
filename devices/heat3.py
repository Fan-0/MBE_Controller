"""
HEAT3 Dual-Mode Heating Power Supply — Modbus RTU
Baud rate configurable (default 19200), 8N1

CH1 = Selenium source   CH2 = Substrate heater

Register encoding (per manual §6.4.7 / §6.4.8):
  - Temperature values (PV, SP) are INT32, multiplier=100, unit=Kelvin.
    e.g. 100 °C → (100 + 273.15) × 100 = 37315 raw
  - Single-word control registers (work mode, operate, standby, etc.)
    are UINT8/UINT16 stored in one Modbus word.

Write protocol (per manual §6.4.5):
  - _write_int32 tries FC 0x10 first; falls back to two FC 0x06 writes
    if the firmware returns a Modbus exception (older firmware compat).

MASTER / REMOTE mode (confirmed by diagnostics, 2026-05-06):
  - R_REMOTE (reg 1000) is READ-ONLY on this firmware.  Writing 1 to it
    is ACK'd by the device but has no effect — reg 1000 stays 0.
  - Modbus writes (SP, OPERATE, STANDBY, WMODE) are accepted regardless
    of the reg 1000 value, as long as Remote Enable is ON in the menu.
  - The 0.5 s poller keeps the device alive (10 s REMOTE timeout) via
    normal FC 0x03 reads — no write to reg 1000 needed.
  - Do NOT write reg 1000 = 0 on disconnect: old firmware interprets
    this as "turn Remote Enable OFF" in the device settings, requiring
    a manual front-panel trip to restore.

Internal SP ramp rate (regs 43/44 for CH1, 101/102 for CH2):
  These registers control how fast the HEAT3 PID controller ramps
  toward any new setpoint written via Modbus.  Encoding: INT16 ×10,
  unit register 0=per/s 1=per/min 2=per/h.  0 = instant (disabled).
  Factory defaults: CH1(Se) ≈ 4 °C/min, CH2(Sub) ≈ 100 °C/min.
  Use read_ramp_rate(ch) / write_ramp_rate(ch, °C/min) to read/write.

Setup (one-time, persists across power cycles):
  Device front panel → Setup → Device Settings → Communication
  → Remote Enable → ON
"""

import struct, threading, time, logging
from typing import Optional
import serial

log = logging.getLogger(__name__)


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class ModbusError(Exception):
    CODES = {1:"Illegal function", 2:"Illegal address",
             3:"Illegal data/value", 4:"Device error", 5:"Acknowledge (busy)"}
    def __init__(self, fc, code):
        self.fc = fc; self.code = code
        super().__init__(f"Modbus FC=0x{fc:02X} error=0x{code:02X} ({self.CODES.get(code,'unknown')})")


class HEAT3:
    KELVIN = 273.15

    # ── CH1 (Selenium) registers ──────────────────────────────────────
    R_STATUS1 = 0;   R_PID_ST1  = 1
    R_PV1     = 2;   R_TC1      = 4;   R_TC2      = 6
    R_SP1     = 14;  R_WMODE1   = 21
    R_OP1     = 70;  R_STBY1    = 71;  R_PIDRUN1  = 72;  R_PIDHOLD1 = 73
    R_OUT1    = 151; R_WATTS1   = 248

    # ── CH2 (Substrate) registers ─────────────────────────────────────
    R_STATUS2 = 84;  R_PID_ST2  = 87
    R_PV2     = 88;  R_SP2      = 90;  R_WMODE2   = 97
    R_OP2     = 114; R_STBY2    = 115; R_PIDRUN2  = 116; R_PIDHOLD2 = 117
    R_OUT2    = 153; R_WATTS2   = 249

    # ── Ramp rate registers (internal SP ramp, §6.4.8 manual) ─────────
    # INT16, multiplier=10 (store rate×10), unit reg: 0=per/s, 1=per/min, 2=per/h
    R_RAMP1      = 43;  R_RAMP_UNIT1 = 44   # CH1 (Selenium)
    R_RAMP2      = 101; R_RAMP_UNIT2 = 102  # CH2 (Substrate, **)

    R_REMOTE  = 1000

    def __init__(self, port, slave_id=1, baudrate=19200):
        self.port = port; self.slave_id = slave_id; self.baudrate = baudrate
        self._ser: Optional[serial.Serial] = None
        self._port_lock = threading.Lock()   # one Modbus transaction at a time
        self._seq_lock  = threading.Lock()   # held for entire multi-step sequences
        self.connected  = False
        self.debug_callback = None   # set to callable(str) for raw TX/RX logging

    # ── Connection ────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self._ser = serial.Serial(
                port=self.port, baudrate=self.baudrate,
                bytesize=8, parity='N', stopbits=1, timeout=0.25)
            self.connected = self._ser.is_open
        except serial.SerialException as e:
            self.connected = False
            raise ConnectionError(f"HEAT3 {self.port}: {e}")
        # R_REMOTE (reg 1000) is read-only on this firmware — writing it is
        # a no-op.  Writes work as long as Remote Enable is ON in the menu.
        log.info("HEAT3: port open — Remote Enable must be ON in device menu")
        return self.connected

    def disconnect(self):
        # Do NOT write R_REMOTE=0 here.
        # Writing 0 to address 1000 turns the device's "Remote Enable" menu
        # setting permanently OFF, requiring a manual front-panel trip to restore.
        # Instead, just close the port — the device releases MASTER on its own
        # after 10 s of silence, while leaving Remote Enable ON in its menu so
        # the next connect() call can immediately re-assert MASTER.
        self.connected = False
        try:
            if self._ser and self._ser.is_open: self._ser.close()
        except: pass
        self._ser = None

    def _is_open(self):
        try: return self._ser is not None and self._ser.is_open
        except: return False

    # ── Low-level Modbus ──────────────────────────────────────────────

    def _frame_read(self, reg, n):
        """FC 0x03 — read n holding registers starting at reg."""
        f = struct.pack(">BBHH", self.slave_id, 0x03, reg, n)
        return f + struct.pack("<H", _crc16(f))

    def _frame_write_single(self, reg, value):
        """FC 0x06 — preset single register (1 word)."""
        f = struct.pack(">BBHH", self.slave_id, 0x06, reg, value & 0xFFFF)
        return f + struct.pack("<H", _crc16(f))

    def _frame_write_multi(self, reg, words):
        """FC 0x10 — write multiple consecutive registers.
        words: list of uint16 values, high-word first for INT32.
        Response: 8 bytes (addr + 0x10 + reg_hi + reg_lo + cnt_hi + cnt_lo + CRC×2).
        """
        n    = len(words)
        data = b''.join(struct.pack(">H", w & 0xFFFF) for w in words)
        body = struct.pack(">BBHHB", self.slave_id, 0x10, reg, n, n * 2) + data
        return body + struct.pack("<H", _crc16(body))

    def _transact(self, req: bytes, expected: int) -> bytes:
        """Send Modbus request, return response bytes. Raises IOError or ModbusError."""
        if not self._is_open():
            raise IOError("HEAT3 port not open")
        if self.debug_callback:
            self.debug_callback(f"TX: {req.hex(' ').upper()}")
        with self._port_lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(req)
                time.sleep(0.01)   # 10 ms: RS-485 turnaround + device processing headroom
                resp = self._ser.read(expected)
            except serial.SerialException as e:
                self.connected = False
                raise IOError(f"HEAT3 serial error: {e}")
        if self.debug_callback and resp:
            self.debug_callback(f"RX: {resp.hex(' ').upper()}")

        if not resp:
            raise IOError("HEAT3: no response (timeout)")

        # Modbus exception frame — function code has bit 7 set
        if len(resp) >= 3 and (resp[1] & 0x80):
            raise ModbusError(resp[1] & 0x7F, resp[2])

        if len(resp) < expected:
            raise IOError(f"HEAT3: short response {len(resp)}/{expected}")

        if _crc16(resp[:expected-2]) != struct.unpack("<H", resp[expected-2:expected])[0]:
            raise IOError("HEAT3: CRC mismatch")

        return resp[:expected]

    # ── Register read helpers ─────────────────────────────────────────

    def _read_words(self, reg, n):
        resp = self._transact(self._frame_read(reg, n), 3 + n * 2 + 2)
        return [struct.unpack(">H", resp[3+i*2:5+i*2])[0] for i in range(n)]

    def _read_int32(self, reg):
        w = self._read_words(reg, 2)
        raw = (w[0] << 16) | w[1]
        return raw - 0x100000000 if raw >= 0x80000000 else raw

    def _read_uint16(self, reg):
        return self._read_words(reg, 1)[0]

    def _read_int16s(self, reg):
        v = self._read_words(reg, 1)[0]
        return v - 0x10000 if v >= 0x8000 else v

    # ── Register write helpers ────────────────────────────────────────

    def _write_word(self, reg: int, value: int) -> bool:
        """FC 0x06 — write one 16-bit register. Returns True on ACK."""
        try:
            self._transact(self._frame_write_single(reg, value), 8)
            return True
        except ModbusError as e:
            log.warning(f"HEAT3 write_word reg={reg} val={value}: {e}")
            return False
        except IOError as e:
            log.warning(f"HEAT3 write_word reg={reg} val={value}: {e}")
            return False

    def _write_int32(self, reg: int, value: int) -> bool:
        """Write a 2-word INT32 value.

        Tries FC 0x10 (write multiple registers) first — required per manual
        §6.4.5 for 2-word values.  Falls back to two FC 0x06 single-register
        writes if the firmware returns a Modbus exception (older firmware
        compatibility).  High word is written to reg, low word to reg+1.
        """
        raw = value & 0xFFFFFFFF
        hi  = (raw >> 16) & 0xFFFF
        lo  =  raw        & 0xFFFF
        log.info(f"HEAT3 write_int32 reg={reg} val={value} "
                 f"(0x{raw:08X}: hi=0x{hi:04X} lo=0x{lo:04X})")

        # ── Try FC 0x10 first (manual-recommended) ──
        try:
            self._transact(self._frame_write_multi(reg, [hi, lo]), 8)
            return True
        except ModbusError:
            log.warning(f"HEAT3: FC 0x10 rejected for reg={reg}, "
                        "falling back to two FC 0x06 writes")
        except IOError:
            raise   # real serial failure — propagate, don't mask

        # ── Fallback: two FC 0x06 single-register writes ──
        ok1 = self._write_word(reg,   hi)
        time.sleep(0.02)
        ok2 = self._write_word(reg+1, lo)
        return ok1 and ok2

    def _c_to_raw(self, c: float) -> int:
        """Celsius → INT32 Kelvin×100 (matches manual INT32/MUL=100/[K] format)."""
        return int(round((c + self.KELVIN) * 100))

    def _k_to_c(self, raw: int) -> float:
        return raw / 100.0 - self.KELVIN

    def _safe_read(self, fn) -> Optional[float]:
        """Run fn() only when no command sequence holds the seq_lock."""
        acquired = self._seq_lock.acquire(blocking=True, timeout=0.5)
        if not acquired:
            return None
        try:
            return fn()
        except Exception:
            return None
        finally:
            self._seq_lock.release()

    # ── High-level command sequences ──────────────────────────────────
    # Each sequence holds _seq_lock so the poller can't interleave.

    def apply_setpoint(self, ch: int, celsius: float) -> dict:
        """
        Full sequence: re-assert MASTER → Auto mode → write SP → readback.
        Returns {'ok': bool, 'readback': float|None, 'message': str}
        """
        sp_reg   = self.R_SP1    if ch == 1 else self.R_SP2
        mode_reg = self.R_WMODE1 if ch == 1 else self.R_WMODE2
        raw_val  = self._c_to_raw(celsius)
        ch_name  = "CH1(Se)" if ch == 1 else "CH2(Sub)"

        log.info(f"HEAT3 apply_setpoint {ch_name} = {celsius:.1f}°C (raw={raw_val})")

        with self._seq_lock:
            try:
                # 1. Set work mode = Auto (PID won't accept a new SP in Manual mode)
                self._write_word(mode_reg, 1)
                time.sleep(0.02)

                # 3. Write setpoint (FC 0x10 preferred, FC 0x06×2 fallback)
                ok = self._write_int32(sp_reg, raw_val)
                time.sleep(0.3)   # device latches SP before it appears in readback

                # 4. Readback to verify
                try:
                    rb_raw = self._read_int32(sp_reg)
                    rb_c   = self._k_to_c(rb_raw)
                    match  = abs(rb_c - celsius) < 1.0
                except Exception as re:
                    return {"ok": ok, "readback": None,
                            "message": f"{ch_name} SP {'written' if ok else 'failed'}, "
                                       f"readback error: {re}"}

                if ok and match:
                    msg = f"{ch_name} SP → {rb_c:.1f}°C ✓"
                    log.info(msg)
                    return {"ok": True, "readback": rb_c, "message": msg}
                elif ok and not match:
                    msg = (f"{ch_name} SP write sent but readback={rb_c:.1f}°C "
                           f"≠ {celsius:.1f}°C — is device in REMOTE/Auto mode?")
                    log.warning(msg)
                    return {"ok": False, "readback": rb_c, "message": msg}
                else:
                    msg = f"{ch_name} SP write failed (no Modbus ACK)"
                    log.warning(msg)
                    return {"ok": False, "readback": None, "message": msg}

            except Exception as e:
                msg = f"{ch_name} apply_setpoint exception: {e}"
                log.warning(msg)
                return {"ok": False, "readback": None, "message": msg}

    def go_operate(self, ch: int) -> dict:
        """Sequence: MASTER → Auto mode → OPERATE → PID RUN."""
        mode_reg = self.R_WMODE1  if ch == 1 else self.R_WMODE2
        op_reg   = self.R_OP1     if ch == 1 else self.R_OP2
        pid_reg  = self.R_PIDRUN1 if ch == 1 else self.R_PIDRUN2
        st_reg   = self.R_STATUS1 if ch == 1 else self.R_STATUS2
        ch_name  = "CH1(Se)" if ch == 1 else "CH2(Sub)"

        with self._seq_lock:
            try:
                self._write_word(mode_reg, 1);       time.sleep(0.02)
                self._write_word(op_reg,   1);       time.sleep(0.02)
                self._write_word(pid_reg,  1);       time.sleep(0.05)
                operating = bool(self._read_uint16(st_reg) & 0x0001)
                msg = f"{ch_name} OPERATE {'✓ confirmed' if operating else '(sent, verify on device)'}"
                log.info(msg)
                return {"ok": operating, "message": msg}
            except Exception as e:
                msg = f"{ch_name} OPERATE error: {e}"
                log.warning(msg)
                return {"ok": False, "message": msg}

    def go_standby(self, ch: int) -> dict:
        stby_reg = self.R_STBY1 if ch == 1 else self.R_STBY2
        ch_name  = "CH1(Se)" if ch == 1 else "CH2(Sub)"
        with self._seq_lock:
            try:
                ok  = self._write_word(stby_reg, 1)
                msg = f"{ch_name} STANDBY {'✓' if ok else '✗'}"
                log.info(msg)
                return {"ok": ok, "message": msg}
            except Exception as e:
                msg = f"{ch_name} STANDBY error: {e}"
                log.warning(msg)
                return {"ok": False, "message": msg}

    def read_ramp_rate(self, ch: int) -> Optional[float]:
        """Read HEAT3 internal SP ramp rate for channel ch (°C/min).
        Returns None on error.  0.0 means instant (device ramping disabled).
        Normalises to °C/min regardless of the device's unit register value.
        """
        ramp_reg = self.R_RAMP1      if ch == 1 else self.R_RAMP2
        unit_reg = self.R_RAMP_UNIT1 if ch == 1 else self.R_RAMP_UNIT2
        acquired = self._seq_lock.acquire(blocking=True, timeout=0.5)
        if not acquired:
            return None
        try:
            ramp_raw = self._read_words(ramp_reg, 1)[0]   # INT16, ×10
            unit_raw = self._read_words(unit_reg,  1)[0]   # 0=per/s 1=per/min 2=per/h
        except Exception:
            return None
        finally:
            self._seq_lock.release()
        rate = ramp_raw / 10.0
        if unit_raw == 0:   rate *= 60.0   # per/s → per/min
        elif unit_raw == 2: rate /= 60.0   # per/h → per/min
        return rate

    def write_ramp_rate(self, ch: int, rate_per_min: float) -> bool:
        """Write HEAT3 internal SP ramp rate for channel ch.
        rate_per_min: ramp speed in °C/min (0 = instant, device ramping off).
        Always sets unit register to per/min (1).
        Returns True if both register writes succeeded.
        """
        ramp_reg = self.R_RAMP1      if ch == 1 else self.R_RAMP2
        unit_reg = self.R_RAMP_UNIT1 if ch == 1 else self.R_RAMP_UNIT2
        raw = int(round(max(0.0, rate_per_min) * 10))   # ×10, clamped ≥ 0
        raw = min(raw, 10000)                             # cap at 1000.0 °C/min
        ch_name = "CH1(Se)" if ch == 1 else "CH2(Sub)"
        log.info(f"HEAT3 write_ramp_rate {ch_name} = {rate_per_min:.1f}°C/min (raw={raw})")
        with self._seq_lock:
            ok1 = self._write_word(ramp_reg, raw)
            time.sleep(0.02)
            ok2 = self._write_word(unit_reg, 1)   # always store as per/min
        return ok1 and ok2

    # ── Per-channel read methods (called by the 1 s poller) ───────────
    # The poller runs every 1 s while connected, which keeps the device
    # in REMOTE MASTER mode (10 s timeout per manual §6.2).

    def ch1_read_process_value(self) -> Optional[float]:
        return self._safe_read(lambda: self._k_to_c(self._read_int32(self.R_PV1)))

    def ch1_read_setpoint(self) -> Optional[float]:
        return self._safe_read(lambda: self._k_to_c(self._read_int32(self.R_SP1)))

    def ch1_read_output_pct(self) -> Optional[float]:
        return self._safe_read(lambda: self._read_int16s(self.R_OUT1) / 100.0)

    def ch1_is_operating(self) -> Optional[bool]:
        return self._safe_read(lambda: bool(self._read_uint16(self.R_STATUS1) & 0x0001))

    def ch2_read_process_value(self) -> Optional[float]:
        return self._safe_read(lambda: self._k_to_c(self._read_int32(self.R_PV2)))

    def ch2_read_setpoint(self) -> Optional[float]:
        return self._safe_read(lambda: self._k_to_c(self._read_int32(self.R_SP2)))

    def ch2_read_output_pct(self) -> Optional[float]:
        return self._safe_read(lambda: self._read_int16s(self.R_OUT2) / 100.0)

    def ch2_is_operating(self) -> Optional[bool]:
        return self._safe_read(lambda: bool(self._read_uint16(self.R_STATUS2) & 0x0001))

    def poll_all(self) -> dict:
        """Batch poll: 3 FC 0x03 reads instead of 8 individual ones.

        Read 1 — CH1 block: regs 0-21 (22 words)
            → STATUS1 (reg 0)  → ch1_op
            → PV1     (reg 2-3) → ch1_pv
            → SP1     (reg 14-15) → ch1_sp

        Read 2 — CH2 block: regs 84-97 (14 words)
            → STATUS2 (reg 84)  → ch2_op
            → PV2     (reg 88-89) → ch2_pv
            → SP2     (reg 90-91) → ch2_sp

        Read 3 — Output block: regs 151-153 (3 words)
            → R_OUT1 (reg 151) → ch1_out %
            → R_OUT2 (reg 153) → ch2_out %

        Returns a dict with all 8 Poller keys.  Missing values stay None.
        ~4× faster than 8 individual reads at same baud rate.
        """
        result = {k: None for k in ("ch1_pv", "ch1_sp", "ch1_op", "ch1_out",
                                     "ch2_pv", "ch2_sp", "ch2_op", "ch2_out")}
        acquired = self._seq_lock.acquire(blocking=True, timeout=0.5)
        if not acquired:
            return result
        try:
            # ── Read 1: CH1 block regs 0-21 (22 words) ───────────────
            try:
                words = self._read_words(self.R_STATUS1, 22)
                # STATUS1 at offset 0 (reg 0), bit 0 = OPERATE
                result["ch1_op"] = bool(words[0] & 0x0001)
                # PV1 at offsets 2-3 (regs 2-3)
                raw = (words[2] << 16) | words[3]
                if raw >= 0x80000000: raw -= 0x100000000
                result["ch1_pv"] = self._k_to_c(raw)
                # SP1 at offsets 14-15 (regs 14-15)
                raw = (words[14] << 16) | words[15]
                if raw >= 0x80000000: raw -= 0x100000000
                result["ch1_sp"] = self._k_to_c(raw)
            except Exception:
                pass

            # ── Read 2: CH2 block regs 84-97 (14 words) ──────────────
            try:
                words = self._read_words(self.R_STATUS2, 14)
                # STATUS2 at offset 0 (reg 84), bit 0 = OPERATE
                result["ch2_op"] = bool(words[0] & 0x0001)
                # PV2 at offsets 4-5 (regs 88-89)
                raw = (words[4] << 16) | words[5]
                if raw >= 0x80000000: raw -= 0x100000000
                result["ch2_pv"] = self._k_to_c(raw)
                # SP2 at offsets 6-7 (regs 90-91)
                raw = (words[6] << 16) | words[7]
                if raw >= 0x80000000: raw -= 0x100000000
                result["ch2_sp"] = self._k_to_c(raw)
            except Exception:
                pass

            # ── Read 3: Output% regs 151-153 (3 words) ───────────────
            try:
                words = self._read_words(self.R_OUT1, 3)
                # R_OUT1 at offset 0 (reg 151), R_OUT2 at offset 2 (reg 153)
                v1 = words[0]; result["ch1_out"] = (v1 - 0x10000 if v1 >= 0x8000 else v1) / 100.0
                v2 = words[2]; result["ch2_out"] = (v2 - 0x10000 if v2 >= 0x8000 else v2) / 100.0
            except Exception:
                pass

        finally:
            self._seq_lock.release()
        return result

    # ── Legacy / recipe engine compatibility aliases ───────────────────
    def ch1_go_operate(self)      -> bool: return self.go_operate(1)["ok"]
    def ch1_go_standby(self)      -> bool: return self.go_standby(1)["ok"]
    def ch1_set_setpoint(self, c) -> bool: return self.apply_setpoint(1, c)["ok"]
    def ch1_set_mode_auto(self)   -> bool: return self._write_word(self.R_WMODE1, 1)

    def ch2_go_operate(self)      -> bool: return self.go_operate(2)["ok"]
    def ch2_go_standby(self)      -> bool: return self.go_standby(2)["ok"]
    def ch2_set_setpoint(self, c) -> bool: return self.apply_setpoint(2, c)["ok"]
    def ch2_set_mode_auto(self)   -> bool: return self._write_word(self.R_WMODE2, 1)

    def read_process_value_celsius(self) -> Optional[float]: return self.ch1_read_process_value()
    def set_setpoint_celsius(self, c)    -> bool:            return self.ch1_set_setpoint(c)
    def go_operate_ch1(self)             -> bool:            return self.ch1_go_operate()
    def is_operating(self)               -> Optional[bool]:  return self.ch1_is_operating()
