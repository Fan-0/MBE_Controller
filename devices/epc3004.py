"""
EPC3004 Single-Loop Temperature Controller — Modbus RTU over RS-485
Manufacturer: Eurotherm (Schneider Electric)
Manual: HA032842ENG Issue 6 (models EPC3016 / EPC3008 / EPC3004)

Used in VacDepositionControl as the Indium evaporation source
temperature controller.

── Protocol ─────────────────────────────────────────────────────────────────
  Baud rate:    19200 (default Modbus RTU; set via HMI: COMM LIST → BAUD)
  Data format:  8N1 (8 data bits, No parity, 1 stop bit)
  Node address: 1–254 (HMI: COMM LIST → NODE; factory default 1)
  Protocol:     Modbus RTU — FC 0x03 (read) + FC 0x06 (write single register)
  Wiring:       RS-485 terminals COM / A / B on rear of controller.
                220Ω termination resistors required at BOTH ends of the
                RS-485 line (per manual §8 verbatim, p. 61).
  Comms watchdog: configurable via W.TIME parameter (0 = disabled).
                  Ensure poller keeps sending frames within W.TIME seconds,
                  or set W.TIME = 0 to disable.

── Temperature encoding ──────────────────────────────────────────────────────
  Unlike HEAT3 (INT32, Kelvin × 100), EPC3004 uses display units directly.
  PV, SP, WSP are INT16 values in °C (or °F if device configured for °F).
  Resolution depends on device display configuration:
    0 decimal places → raw = temperature in integer °C
    1 decimal place  → raw = temperature × 10  (e.g. 7440 = 744.0 °C)
  Adjust RESOLUTION_FACTOR (class constant) to match device configuration.
  Default: 1 (integer °C, typical for high-temperature furnace use).

── Register addresses ────────────────────────────────────────────────────────
  All register constants below are 0-based values as placed in the Modbus RTU
  frame (i.e., the 16-bit starting address field of FC 0x03 / FC 0x06 frames).
  Eurotherm iTools Browser displays register numbers 1-based (JBUS convention):
    frame address = iTools register number − 1
  Addresses marked "# VERIFY" have been inferred from standard Eurotherm
  3xxx/EPC3000 parameter layouts.  Confirm them with iTools before live use:
    Connect iTools → Browser view → search parameter name → read "Address".

── Single-channel device ─────────────────────────────────────────────────────
  The EPC3004 is a single-loop controller; all methods accept ch = 1 only.
  The ch parameter is present for API compatibility with the HEAT3 driver.

── Auto / Manual / Standby ───────────────────────────────────────────────────
  "Operate"  (go_operate)  → Auto mode → PID runs, output tracks SP.
  "Standby"  (go_standby)  → Manual mode + 0 % demand → output off.
  These are the functional equivalents of HEAT3 OPERATE / STANDBY states.

── Comms prerequisites ───────────────────────────────────────────────────────
  No software REMOTE ENABLE register (unlike HEAT3 reg 1000).
  Modbus writes are accepted as long as:
    1. RS-485 comms is enabled: HMI COMM LIST → PROT → m.Rtu
    2. W.TIME = 0 OR the poller is running within the watchdog window.
  The HMI password / access level does NOT block Modbus writes by default,
  but OEM security (if active) may restrict write access to some parameters.
"""

import struct
import threading
import time
import logging
from typing import Optional

import serial

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  TYPE C THERMOCOUPLE — J-PROXY CONVERSION
#  ─────────────────────────────────────────────────────────────────────────────
#  The EPC3004 does not natively support Type C (W5%Re/W26%Re).  When configured
#  as a Type J thermocouple (AI.1.Type=0, AI.1.Linearisation=0), it reads the
#  Type C EMF and displays a J-apparent temperature.  These functions convert
#  between the J-apparent display value and the true Type C temperature.
#
#  Derivation:
#    EPC3004 computes:  T_J = J_inv(V_C(T_hot) + Δ_CJC)
#    Δ_CJC = V_J(25°C) − V_C(25°C) = 1.277 − 0.353 = +0.924 mV  (at T_cj = 25°C)
#
#  Sources: ASTM E988 (Type C), NIST ITS-90 (Type J)
#  To activate J-proxy mode:  run  diagnostics/configure_epc3004_cstm1.py --set-j-mode
# ══════════════════════════════════════════════════════════════════════════════

# 16-point lookup table:  (T_J_display °C,  T_C_true °C)
_J_PROXY_TABLE = [
    ( 17.9,    0.0),
    ( 45.9,  100.0),
    ( 76.6,  200.0),
    (109.5,  300.0),
    (143.6,  400.0),
    (213.6,  600.0),
    (283.6,  800.0),
    (351.7, 1000.0),
    (416.3, 1200.0),
    (476.4, 1400.0),
    (531.4, 1600.0),
    (581.1, 1800.0),
    (625.1, 2000.0),
    (645.0, 2100.0),
    (663.0, 2200.0),
    (679.1, 2300.0),
]
_J_PROXY_TABLE_INV = [(tc, tj) for tj, tc in _J_PROXY_TABLE]  # (T_C_true, T_J_display)


def _interp(table: list, x: float) -> float:
    """Piecewise-linear interpolation / extrapolation on a sorted (x, y) table."""
    if x <= table[0][0]:
        slope = (table[1][1] - table[0][1]) / (table[1][0] - table[0][0])
        return table[0][1] + slope * (x - table[0][0])
    if x >= table[-1][0]:
        slope = (table[-1][1] - table[-2][1]) / (table[-1][0] - table[-2][0])
        return table[-1][1] + slope * (x - table[-1][0])
    for i in range(len(table) - 1):
        x0, y0 = table[i]
        x1, y1 = table[i + 1]
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float('nan')


def j_display_to_c(t_j: float) -> float:
    """Convert EPC3004 J-apparent PV reading → true Type C temperature (°C).

    Use this when reading the PV register in J-proxy mode.
    Assumes cold-junction temperature = 25°C (Δ_CJC = +0.924 mV).
    """
    return _interp(_J_PROXY_TABLE, t_j)


def c_to_j_display(t_c: float) -> float:
    """Convert true Type C temperature (°C) → J-apparent value for EPC3004 setpoint.

    Use this when writing to the SP register in J-proxy mode.  The EPC3004's PID
    loop operates on the J-scale, so desired Type C setpoints must be converted.
    """
    return _interp(_J_PROXY_TABLE_INV, t_c)


# ── CRC-16 Modbus (shared with heat3.py — same polynomial 0xA001) ─────────────

def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class EPC3000Error(Exception):
    CODES = {1: "Illegal function", 2: "Illegal address",
             3: "Illegal data/value", 4: "Device error", 5: "Acknowledge (busy)"}

    def __init__(self, fc: int, code: int):
        self.fc = fc
        self.code = code
        super().__init__(f"Modbus FC=0x{fc:02X} error=0x{code:02X} "
                         f"({self.CODES.get(code, 'unknown')})")


class EPC3004:
    """Eurotherm EPC3004 single-loop temperature controller — Modbus RTU driver.

    Designed as the Indium evaporation source controller for VacDepositionControl.
    API mirrors HEAT3 (ch1_* methods, apply_setpoint, go_operate, go_standby,
    read/write_ramp_rate, poll_all) for drop-in compatibility with the recipe
    engine and GUI patterns.
    """

    # ── Resolution ────────────────────────────────────────────────────────────
    # Confirmed by diagnostic (2026-05-11): device display is configured for
    # 0.1 °C resolution (one decimal place).  raw 2000 = 200.0 °C.
    #   1  → integer °C display  (raw 744  = 744.0 °C)
    #   10 → 0.1 °C display      (raw 7440 = 744.0 °C)
    RESOLUTION_FACTOR: int = 10

    # ── Modbus register addresses (0-based, as placed in the Modbus frame) ────
    #
    # Confirmed by epc3004_diag.py diagnostic (2026-05-11):
    #
    R_PV          = 0x0001   #    1: Process Variable (°C INT16, read-only)
    #                                Confirmed readable; raw 2000 = 200.0 °C (factor 10).

    # Setpoint registers:
    R_SP_RO       = 0x0002   #    2: Working Setpoint (°C INT16, READ-ONLY)
    #                                Tracks the SP after ramping; use for display only.
    #                                Write returns Modbus exception code 2 (Illegal address).
    R_SP          = 0x0018   #   24: Target Setpoint 1 (°C INT16, read/write)
    #                                Confirmed writable. apply_setpoint() writes here first.
    R_SP2         = 0x0019   #   25: Target Setpoint 2 (°C INT16, read/write)
    #                                Confirmed writable (epc3004_write_diag 2026-05-11).
    #                                Device tracks whichever SP is currently selected;
    #                                apply_setpoint() writes BOTH R_SP and R_SP2 so the
    #                                active setpoint is updated regardless of which is selected.

    R_OP          = 0x0004   #    4: Working Output Power (INT16; ÷10 = %; READ-ONLY)
    #                                Confirmed readable; raw ~375 = 37.5 % output at 200 °C.
    #                                FC 0x06 write returns exception 0x02 (Illegal address).
    #                                Do NOT write to this register — use R_OUT_DEMAND instead.

    # Writable manual output demand register.
    # Confirmed writable by epc3004_write_diag.py (2026-05-11):
    #   0x0003 → Modbus exception 0x02 (Illegal address) — NOT the demand reg
    #   0x0006 → write+readback confirmed ✓ — this is the Manual Output Demand
    # Value encoding: raw ÷ 10 = % (e.g. 200 raw = 20.0 %).
    R_OUT_DEMAND  = 0x0006   #    6: Manual Output Demand (INT16; ÷10 = %; read/write)
    #                                CAUTION: R_AUTO_MAN → Manual (0x0112 = 1) is ACK'd
    #                                but the device does NOT actually switch to Manual mode
    #                                via Modbus on this firmware (readback stays 0).
    #                                go_standby() writes this register as a best-effort;
    #                                see go_standby() docstring for details.
    R_WSP         = 0x0005   #    5: Working Setpoint (°C INT16, read-only; same as R_SP_RO)

    # Instrument status word — confirmed readable:
    R_STATUS      = 0x0111   #  273: Instrument Status (UINT16, read-only)
    #                                bit 0  = Summary alarm active
    #                                bit 2  = Auto-tune in progress
    #                                bit 3  = Sensor break
    #                                bit 8  = Manual mode (1 = Manual, 0 = Auto)
    _STATUS_MANUAL_BIT = 0x0100   # bit 8 mask

    # Auto / Manual mode control register:
    R_AUTO_MAN    = 0x0112   #  274: Auto/Manual (0 = Auto, 1 = Manual, read/write)
    #                                Confirmed readable. Write ACK'd by device BUT
    #                                does not switch mode on this firmware — readback
    #                                stays 0 (Auto) after writing 1 (Manual).
    #                                iTools path: Loop → Auto/Manual station → Manual

    # SP ramp rate registers — confirmed readable by diagnostic (2026-05-11):
    # Diagnostic read: reg 0x0020 = 1000 (= 100.0 °C/min, ×10 encoding)
    #                  reg 0x0021 = 0    (= off/instant — ramp currently disabled)
    # Write access not yet tested; update after confirming with iTools or write test.
    R_SP_RATE     = 0x0020   #   32: SP Ramp Rate (INT16; stored as rate × 10)
    R_SP_RATE_U   = 0x0021   #   33: SP Rate Unit  (0=off, 1=°C/s, 2=°C/min, 3=°C/h)

    def __init__(self, port: str, slave_id: int = 1, baudrate: int = 19200,
                 j_proxy: bool = False):
        self.port       = port
        self.slave_id   = slave_id
        self.baudrate   = baudrate
        self._ser:       Optional[serial.Serial] = None
        self._port_lock  = threading.Lock()   # one Modbus transaction at a time
        self._seq_lock   = threading.Lock()   # held for entire multi-step sequences
        self.connected   = False
        self.debug_callback = None           # set to callable(str) for TX/RX hex logging

        # J-proxy mode: EPC3004 is configured as Type J TC; Python converts PV/SP.
        # Enable when AI.1.Type=Thermocouple, AI.1.Linearisation=J and a Type C TC
        # is physically connected.  See j_display_to_c() / c_to_j_display() above.
        self.j_proxy: bool = j_proxy

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self._ser = serial.Serial(
                port=self.port, baudrate=self.baudrate,
                bytesize=8, parity='N', stopbits=1, timeout=0.25)
            self.connected = self._ser.is_open
        except serial.SerialException as e:
            self.connected = False
            raise ConnectionError(f"EPC3004 {self.port}: {e}")
        log.info(f"EPC3004: connected on {self.port} at {self.baudrate} baud, "
                 f"node {self.slave_id}")
        return self.connected

    def disconnect(self):
        self.connected = False
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    def _is_open(self) -> bool:
        try:
            return self._ser is not None and self._ser.is_open
        except Exception:
            return False

    # ── Low-level Modbus ──────────────────────────────────────────────────────

    def _frame_read(self, reg: int, n: int) -> bytes:
        """FC 0x03 — read n holding registers starting at reg."""
        f = struct.pack(">BBHH", self.slave_id, 0x03, reg, n)
        return f + struct.pack("<H", _crc16(f))

    def _frame_write_single(self, reg: int, value: int) -> bytes:
        """FC 0x06 — preset single register (1 word)."""
        f = struct.pack(">BBHH", self.slave_id, 0x06, reg, value & 0xFFFF)
        return f + struct.pack("<H", _crc16(f))

    def _transact(self, req: bytes, expected: int) -> bytes:
        """Send Modbus request, return verified response bytes.
        Raises IOError on serial/timeout/CRC errors, EPC3000Error on exception frames."""
        if not self._is_open():
            raise IOError("EPC3004 port not open")
        if self.debug_callback:
            self.debug_callback(f"TX: {req.hex(' ').upper()}")
        with self._port_lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(req)
                time.sleep(0.01)          # 10 ms RS-485 turnaround + device headroom
                resp = self._ser.read(expected)
            except serial.SerialException as e:
                self.connected = False
                raise IOError(f"EPC3004 serial error: {e}")
        if self.debug_callback and resp:
            self.debug_callback(f"RX: {resp.hex(' ').upper()}")
        if not resp:
            raise IOError("EPC3004: no response (timeout)")
        # Modbus exception frame — bit 7 of function code is set
        if len(resp) >= 3 and (resp[1] & 0x80):
            raise EPC3000Error(resp[1] & 0x7F, resp[2])
        if len(resp) < expected:
            raise IOError(f"EPC3004: short response {len(resp)}/{expected}")
        if _crc16(resp[:expected - 2]) != struct.unpack("<H", resp[expected - 2:expected])[0]:
            raise IOError("EPC3004: CRC mismatch")
        return resp[:expected]

    # ── Register read/write helpers ───────────────────────────────────────────

    def _read_words(self, reg: int, n: int) -> list:
        """Read n consecutive 16-bit registers; returns list of UINT16 values."""
        resp = self._transact(self._frame_read(reg, n), 3 + n * 2 + 2)
        return [struct.unpack(">H", resp[3 + i * 2:5 + i * 2])[0] for i in range(n)]

    def _read_int16(self, reg: int) -> int:
        """Read one register as signed INT16."""
        v = self._read_words(reg, 1)[0]
        return v - 0x10000 if v >= 0x8000 else v

    def _read_uint16(self, reg: int) -> int:
        """Read one register as unsigned UINT16."""
        return self._read_words(reg, 1)[0]

    def _write_word(self, reg: int, value: int) -> bool:
        """FC 0x06 single-register write. Returns True on ACK, False on error."""
        try:
            self._transact(self._frame_write_single(reg, value), 8)
            return True
        except (EPC3000Error, IOError) as e:
            log.warning(f"EPC3004 write_word reg=0x{reg:04X} val={value}: {e}")
            return False

    def _c_to_raw(self, celsius: float) -> int:
        """°C → INT16 raw register value (× RESOLUTION_FACTOR).

        In J-proxy mode: converts true Type C °C → J-apparent °C before encoding,
        so the EPC3004's PID loop tracks the correct J-scale setpoint.
        """
        if self.j_proxy:
            celsius = c_to_j_display(celsius)
        return int(round(celsius * self.RESOLUTION_FACTOR))

    def _raw_to_c(self, raw: int) -> float:
        """INT16 raw register value → °C.

        In J-proxy mode: decodes J-apparent °C from register then converts to
        true Type C °C using the J-proxy lookup table.
        """
        t = raw / self.RESOLUTION_FACTOR
        if self.j_proxy:
            t = j_display_to_c(t)
        return t

    def _safe_read(self, fn) -> Optional[float]:
        """Run fn() only when no command sequence holds _seq_lock.
        Returns None on lock timeout or any exception in fn()."""
        acquired = self._seq_lock.acquire(blocking=True, timeout=0.5)
        if not acquired:
            return None
        try:
            return fn()
        except Exception:
            return None
        finally:
            self._seq_lock.release()

    # ── High-level command sequences ──────────────────────────────────────────
    # Each sequence holds _seq_lock so the poller cannot interleave reads.

    def apply_setpoint(self, ch: int, celsius: float) -> dict:
        """Write target setpoint and readback to verify.

        Sequence:
          1. Ensure Auto mode (PID ignores SP writes in Manual mode).
          2. Write SP.
          3. Wait 0.30 s for device to latch the new SP.
          4. Read SP back; verify |readback − target| < 1.0 °C.

        ch must be 1 (single-loop device).
        Returns {'ok': bool, 'readback': float|None, 'message': str}
        """
        if ch != 1:
            return {"ok": False, "readback": None,
                    "message": "EPC3004: only ch=1 supported (single-loop device)"}
        raw = self._c_to_raw(celsius)
        log.info(f"EPC3004 apply_setpoint = {celsius:.1f}°C (raw={raw})")
        with self._seq_lock:
            try:
                # 1. Auto mode — PID must be active before SP write is effective
                self._write_word(self.R_AUTO_MAN, 0)
                time.sleep(0.02)

                # 2. Write to BOTH setpoint registers (SP1 = 0x0018, SP2 = 0x0019).
                #    The device selects either SP1 or SP2 as its active target;
                #    writing both ensures the new setpoint takes effect regardless
                #    of which one is currently selected.
                ok1 = self._write_word(self.R_SP,  raw)   # SP1
                time.sleep(0.02)
                ok2 = self._write_word(self.R_SP2, raw)   # SP2
                ok  = ok1 and ok2
                time.sleep(0.50)   # device latches SP before readback is valid

                # 3. Readback verify from SP1 (primary writable register)
                try:
                    rb_raw = self._read_int16(self.R_SP)
                    rb_c   = self._raw_to_c(rb_raw)
                    match  = abs(rb_c - celsius) < 1.0
                except Exception as re:
                    return {"ok": ok, "readback": None,
                            "message": f"EPC3004 SP {'written' if ok else 'failed'}, "
                                       f"readback error: {re}"}

                if ok and match:
                    msg = f"EPC3004 SP → {rb_c:.1f}°C ✓"
                    log.info(msg)
                    return {"ok": True, "readback": rb_c, "message": msg}
                elif ok and not match:
                    msg = (f"EPC3004 SP written but readback={rb_c:.1f}°C "
                           f"≠ {celsius:.1f}°C — verify Modbus write access")
                    log.warning(msg)
                    return {"ok": False, "readback": rb_c, "message": msg}
                else:
                    msg = f"EPC3004 SP write failed (SP1={'✓' if ok1 else '✗'} SP2={'✓' if ok2 else '✗'})"
                    log.warning(msg)
                    return {"ok": False, "readback": None, "message": msg}

            except Exception as e:
                msg = f"EPC3004 apply_setpoint exception: {e}"
                log.warning(msg)
                return {"ok": False, "readback": None, "message": msg}

    def go_operate(self, ch: int) -> dict:
        """Set Auto (PID) mode — controller runs toward setpoint.

        Equivalent to HEAT3 go_operate / OPERATE state.
        Confirms by reading the Manual bit of R_STATUS after the write.
        Returns {'ok': bool, 'message': str}
        """
        if ch != 1:
            return {"ok": False, "message": "EPC3004: only ch=1 supported"}
        with self._seq_lock:
            try:
                ok = self._write_word(self.R_AUTO_MAN, 0)    # 0 = Auto mode
                time.sleep(0.05)
                status  = self._read_uint16(self.R_STATUS)
                in_auto = not bool(status & self._STATUS_MANUAL_BIT)
                msg = (f"EPC3004 AUTO/OPERATE "
                       f"{'✓ confirmed' if (ok and in_auto) else '(sent, verify on device)'}")
                log.info(msg)
                return {"ok": ok and in_auto, "message": msg}
            except Exception as e:
                msg = f"EPC3004 go_operate error: {e}"
                log.warning(msg)
                return {"ok": False, "message": msg}

    def go_standby(self, ch: int) -> dict:
        """Ensure Auto (PID) mode — the EPC3004 is kept in Auto at all times.

        Unlike HEAT3 where standby means a hard STANDBY state, the EPC3004 does
        not support Modbus-controlled Manual mode on this firmware (R_AUTO_MAN=1
        is ACK'd but silently ignored — confirmed by epc3004_write_diag.py).
        Keeping the device in Auto and driving the SP low via apply_setpoint() is
        the correct standby strategy for this controller.

        Returns {'ok': bool, 'message': str}
        """
        if ch != 1:
            return {"ok": False, "message": "EPC3004: only ch=1 supported"}
        with self._seq_lock:
            try:
                ok = self._write_word(self.R_AUTO_MAN, 0)   # ensure Auto / PID running
                msg = f"EPC3004 STANDBY (Auto mode confirmed) {'✓' if ok else '✗'}"
                log.info(msg)
                return {"ok": ok, "message": msg}
            except Exception as e:
                msg = f"EPC3004 go_standby error: {e}"
                log.warning(msg)
                return {"ok": False, "message": msg}

    def read_ramp_rate(self, ch: int) -> Optional[float]:
        """Read the SP ramp rate in °C/min.

        Returns None on read error.  0.0 means ramp is disabled (SP jumps instantly).
        Normalises to °C/min regardless of the unit register value.
        Register addresses confirmed readable by diagnostic (2026-05-11):
          R_SP_RATE   0x0020 read = 1000 (100.0 °C/min stored value)
          R_SP_RATE_U 0x0021 read = 0    (off — ramp currently disabled)
        Write access to these registers not yet confirmed; test with iTools if write fails.
        """
        if ch != 1:
            return None
        acquired = self._seq_lock.acquire(blocking=True, timeout=0.5)
        if not acquired:
            return None
        try:
            ramp_raw = self._read_words(self.R_SP_RATE,   1)[0]   # INT16, stored × 10
            unit_raw = self._read_uint16(self.R_SP_RATE_U)         # 0=off 1=°C/s 2=°C/min 3=°C/h
        except Exception:
            return None
        finally:
            self._seq_lock.release()

        if unit_raw == 0:
            return 0.0                   # off → instant
        rate = ramp_raw / 10.0           # undo ×10 encoding
        if unit_raw == 1:
            rate *= 60.0                 # °C/s → °C/min
        elif unit_raw == 3:
            rate /= 60.0                 # °C/h → °C/min
        # unit_raw == 2 → already °C/min
        return rate

    def write_ramp_rate(self, ch: int, rate_per_min: float) -> bool:
        """Write the SP ramp rate in °C/min.

        0 = instant (device-internal ramp disabled; SP jumps to target immediately).
        Encoding: stored_value = rate × 10; unit register written as 2 (per/min).
        R_SP_RATE (0x0020) and R_SP_RATE_U (0x0021) confirmed readable (2026-05-11).
        Write access not yet confirmed — if writes fail, use iTools to find correct addresses.
        Returns True if both register writes succeeded.
        """
        if ch != 1:
            return False
        raw = int(round(max(0.0, rate_per_min) * 10))
        raw = min(raw, 10000)   # cap at 1000.0 °C/min
        log.info(f"EPC3004 write_ramp_rate = {rate_per_min:.1f}°C/min (raw={raw})")
        with self._seq_lock:
            ok1 = self._write_word(self.R_SP_RATE,   raw)
            time.sleep(0.02)
            ok2 = self._write_word(self.R_SP_RATE_U, 2)   # 2 = per/min
        return ok1 and ok2

    # ── Per-channel read methods ──────────────────────────────────────────────
    # Called by the Poller and recipe engine; same API pattern as HEAT3.

    def ch1_read_process_value(self) -> Optional[float]:
        """Current measured temperature (°C)."""
        return self._safe_read(lambda: self._raw_to_c(self._read_int16(self.R_PV)))

    def ch1_read_setpoint(self) -> Optional[float]:
        """Target setpoint (°C) — reads R_SP (0x0018, writable target SP1)."""
        return self._safe_read(lambda: self._raw_to_c(self._read_int16(self.R_SP)))

    def ch1_read_output_pct(self) -> Optional[float]:
        """Working output power (%).
        Raw value ÷ 10 = % (e.g., raw 500 → 50.0 %).
        Verify output scaling with iTools if values appear 10× too large or small.
        """
        return self._safe_read(lambda: self._read_int16(self.R_OP) / 10.0)

    def ch1_is_operating(self) -> Optional[bool]:
        """True when controller is in Auto (PID) mode."""
        return self._safe_read(
            lambda: not bool(self._read_uint16(self.R_STATUS) & self._STATUS_MANUAL_BIT))

    def poll_all(self) -> dict:
        """Batch poll: 2 FC 0x03 reads for all live process values.

        Read 1 — process block regs 0x0001–0x0005 (5 words):
            offset 0 = R_PV   (0x0001) → in_pv  (process variable, read-only)
            offset 1 = R_SP_RO(0x0002) → (working/ramped SP, used for in_out CRC chain only)
            offset 2 = reg 3           → (unused)
            offset 3 = R_OP   (0x0004) → in_out (output %; raw ÷ 10 = %)
            offset 4 = R_WSP  (0x0005) → (not separately exposed)

        Read 2 — target setpoint reg 0x0018 (1 word):
            R_SP (0x0018) → in_sp  (target SP — updates immediately after apply_setpoint).
            Using R_SP instead of R_SP_RO (0x0002) so the GUI reflects the new target
            setpoint immediately rather than only after a ramp completes.

        Read 3 — status reg 0x0111 (1 word):
            R_STATUS → in_op  (True = Auto mode, False = Manual)

        Returns dict with keys:  in_pv, in_sp, in_op, in_out.
        """
        result = {"in_pv": None, "in_sp": None, "in_op": None, "in_out": None}
        acquired = self._seq_lock.acquire(blocking=True, timeout=0.5)
        if not acquired:
            return result
        try:
            # Read 1: process block regs 0x0001–0x0005 (5 words, one FC 0x03)
            try:
                words   = self._read_words(self.R_PV, 5)
                raw_pv  = words[0]
                raw_op  = words[3]   # R_OP (0x0004) — output % × 10
                result["in_pv"]  = self._raw_to_c(raw_pv - 0x10000 if raw_pv >= 0x8000 else raw_pv)
                result["in_out"] = (raw_op - 0x10000 if raw_op >= 0x8000 else raw_op) / 10.0
            except Exception:
                pass

            # Read 2: target setpoint R_SP (0x0018) — writable target; matches what
            #         apply_setpoint() writes so the GUI reflects the change immediately.
            try:
                raw_sp = self._read_int16(self.R_SP)
                result["in_sp"] = self._raw_to_c(raw_sp)
            except Exception:
                pass

            # Read 3: instrument status → Auto/Manual flag
            try:
                status = self._read_uint16(self.R_STATUS)
                result["in_op"] = not bool(status & self._STATUS_MANUAL_BIT)
            except Exception:
                pass

        finally:
            self._seq_lock.release()
        return result

    # ── Legacy / recipe engine compatibility aliases (matching HEAT3 API) ──────

    def ch1_go_operate(self)       -> bool:           return self.go_operate(1)["ok"]
    def ch1_go_standby(self)       -> bool:           return self.go_standby(1)["ok"]
    def ch1_set_setpoint(self, c)  -> bool:           return self.apply_setpoint(1, c)["ok"]
    def ch1_set_mode_auto(self)    -> bool:           return self._write_word(self.R_AUTO_MAN, 0)

    def read_process_value_celsius(self) -> Optional[float]: return self.ch1_read_process_value()
    def set_setpoint_celsius(self, c)    -> bool:            return self.ch1_set_setpoint(c)
    def is_operating(self)               -> Optional[bool]:  return self.ch1_is_operating()
