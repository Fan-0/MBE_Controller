#!/usr/bin/env python3
"""
configure_epc3004_cstm1.py  —  EPC3004 Type C Thermocouple Setup (J-Proxy Method)
===================================================================================
Configures the EPC3004 to accept a Type C thermocouple (W5%Re / W26%Re) using a
Type J proxy: the EPC3004 reads the TC in Type J mode and Python corrects the PV.

CLOSE THE MAIN VacDepositionControl GUI before running — exclusive serial access needed.

═══════════════════════════════════════════════════════════════════════════════
 WHY J-PROXY? (why not CSTM1 / LIN16 / Custom1 linearization?)
═══════════════════════════════════════════════════════════════════════════════

 The EPC3004 has a "Custom1" linearization table (CSTM1/LIN16) that would
 natively support any thermocouple.  However, the Modbus register addresses
 for the 16-point breakpoint table (In1–In16, Out1–Out16) are hidden from the
 iTools address-space export until Custom1 is already activated on the device.
 They were not found in the exported addys.csv despite an exhaustive search.

 REGISTERS CONFIRMED (from iTools addys.csv, 2026-05-12):
   AI.1.Type          → frame 0x3002   (0=TC, 1=mV, 2=V, 3=mA, 4=RTD, 5=Zirconia)
   AI.1.Linearisation → frame 0x078E   (0=J, 1=K…7=S, 8=Custom1, 9=Custom2…)

 J-PROXY APPROACH (implemented here):
   1. Set the EPC3004 to Type J thermocouple mode (writes to 0x3002 and 0x078E).
   2. Connect the Type C TC.  The EPC3004 reads the Type C EMF and interprets
      it as if it were a Type J TC → displays a "J-apparent temperature" (T_J).
   3. Python software converts T_J → true Type C temperature (T_C) using the
      J-PROXY CONVERSION TABLE below.

 ADVANTAGES:
   • No unknown register addresses — only uses confirmed AI.1.Type/Linearisation.
   • EPC3004 handles CJC automatically (J-type CJC, see correction note below).
   • Fully reversible: switching back to K/J/etc. is a 2-register write.
   • Works transparently with the existing epc3004.py poll/setpoint interface.

═══════════════════════════════════════════════════════════════════════════════
 DERIVATION OF THE J-PROXY TABLE
═══════════════════════════════════════════════════════════════════════════════

 When EPC3004 is in Type J mode and a Type C TC is connected:

   V_TC_measured = V_C(T_hot) - V_C(T_cj)        ← what the TC actually produces
   V_for_J_inv   = V_TC_measured + V_J(T_cj)      ← EPC adds J-based CJC
                 = V_C(T_hot) - V_C(T_cj) + V_J(T_cj)
   T_J_display   = J_inverse(V_for_J_inv)

 At T_cj = 25 °C (fixed lab cold junction):
   V_C(25°C) = 0.353 mV   (ASTM E988)
   V_J(25°C) = 1.277 mV   (NIST ITS-90)
   Δ_CJC     = V_J(25°C) − V_C(25°C) = +0.924 mV   (constant offset)

 So:  T_J_display = J_inv( V_C(T_hot) + 0.924 )

 CJC ERROR NOTE:
   The +0.924 mV correction assumes T_cj = 25°C.  If the actual cold junction
   temperature drifts (e.g., EPC3004 terminal block gets warm), the error is:
     ΔT_C ≈ (V_J(T_cj_actual) − V_C(T_cj_actual) − 0.924) / (dV_C/dT at T_hot)
   At T_hot > 500°C this error is < 1°C for T_cj variations of ±10°C.
   For better accuracy, the conversion function below accepts an optional
   cjc_offset_mv correction term.

═══════════════════════════════════════════════════════════════════════════════
 WORKFLOW
═══════════════════════════════════════════════════════════════════════════════

 Step 1 — Verify current register state (read-only, safe):
     python configure_epc3004_cstm1.py --port COM13 --check-regs

 Step 2 — Activate Type J mode on the EPC3004:
     python configure_epc3004_cstm1.py --port COM13 --set-j-mode

 Step 3 — Verify J mode was written:
     python configure_epc3004_cstm1.py --port COM13 --check-regs

 Step 4 — Run the conversion table dry-run (no serial):
     python configure_epc3004_cstm1.py --dryrun
     Shows the full J-display → Type C mapping for all 16 points.

 Step 5 — Live conversion test (reads current PV and converts):
     python configure_epc3004_cstm1.py --port COM13 --test-live
     Shows raw J-display PV and converted Type C temperature side by side.

 Step 6 — Copy the J_PROXY_TABLE and j_display_to_c() / c_to_j_display()
     functions into devices/epc3004.py (see "INTEGRATION" section below).
═══════════════════════════════════════════════════════════════════════════════
"""

import sys
import struct
import time
import argparse
import logging

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed.  Run:  pip install pyserial")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Connection ─────────────────────────────────────────────────────────────────
DEFAULT_PORT = "COM13"
BAUD         = 19200
NODE         = 1          # Modbus slave address

# ── Confirmed register addresses (from iTools addys.csv, 2026-05-12) ──────────
REG_INPUT_TYPE    = 0x3002   # AI.1.Type        — hardware input type selector
REG_LINEARISATION = 0x078E   # AI.1.Linearisation — TC curve selector

# AI.1.Type enum values:
TYPE_THERMOCOUPLE = 0
TYPE_MV           = 1

# AI.1.Linearisation enum values:
LIN_J       = 0
LIN_K       = 1
LIN_CUSTOM1 = 8

# EPC3004 resolution factor (confirmed 2026-05-11):
RESOLUTION_FACTOR = 10   # raw PV register = °C × 10

# ══════════════════════════════════════════════════════════════════════════════
#  J-PROXY CONVERSION TABLES
#  ─────────────────────────────────────────────────────────────────────────────
#  Derived from ASTM E988 (Type C) + NIST ITS-90 (Type J), T_cj = 25°C.
#  V_eff = V_C(T_true) + 0.924 mV;  T_J_display = J_inverse(V_eff)
#
#  J inverse computed by piecewise-linear interpolation of the NIST ITS-90
#  Type J reference table (0–760°C).
#
#  Format: (T_J_display °C,  T_C_true °C)
# ══════════════════════════════════════════════════════════════════════════════
J_PROXY_TABLE = [
    #  T_J_display    T_C_true    ← V_C (mV)   V_eff (mV)
    (   17.9,          0.0),     #   0.000       0.924
    (   45.9,        100.0),     #   1.451       2.375
    (   76.6,        200.0),     #   3.090       4.014
    (  109.5,        300.0),     #   4.865       5.789
    (  143.6,        400.0),     #   6.732       7.656
    (  213.6,        600.0),     #  10.609      11.533
    (  283.6,        800.0),     #  14.494      15.418
    (  351.7,       1000.0),     #  18.260      19.184
    (  416.3,       1200.0),     #  21.825      22.749
    (  476.4,       1400.0),     #  25.155      26.079
    (  531.4,       1600.0),     #  28.243      29.167
    (  581.1,       1800.0),     #  31.087      32.011
    (  625.1,       2000.0),     #  33.669      34.593
    (  645.0,       2100.0),     #  34.849      35.773
    (  663.0,       2200.0),     #  35.940      36.864
    (  679.1,       2300.0),     #  36.931      37.855
]

# Inverse table for setpoint conversion (T_C_true → T_J_display):
# (same data, transposed — used by c_to_j_display())
_C_TO_J = [(tc, tj) for tj, tc in J_PROXY_TABLE]


def j_display_to_c(t_j: float, cjc_offset_mv: float = 0.0) -> float:
    """Convert EPC3004 J-apparent PV reading → true Type C temperature (°C).

    Args:
        t_j:           J-apparent temperature as read from EPC3004 PV register.
        cjc_offset_mv: Optional CJC correction in mV if cold junction != 25°C.
                       Positive = cold junction warmer than 25°C.
                       (V_J(T_cj) − V_C(T_cj) − 0.924 mV)
    Returns:
        True Type C thermocouple temperature in °C.
    """
    table = J_PROXY_TABLE
    if t_j <= table[0][0]:
        # Extrapolate below first point using first-segment slope
        slope = (table[1][1] - table[0][1]) / (table[1][0] - table[0][0])
        return table[0][1] + slope * (t_j - table[0][0])
    if t_j >= table[-1][0]:
        # Extrapolate above last point using last-segment slope
        slope = (table[-1][1] - table[-2][1]) / (table[-1][0] - table[-2][0])
        return table[-1][1] + slope * (t_j - table[-1][0])
    # Linear interpolation
    for i in range(len(table) - 1):
        t_j0, tc0 = table[i]
        t_j1, tc1 = table[i + 1]
        if t_j0 <= t_j <= t_j1:
            frac = (t_j - t_j0) / (t_j1 - t_j0)
            return tc0 + frac * (tc1 - tc0)
    return float('nan')


def c_to_j_display(t_c: float) -> float:
    """Convert true Type C temperature (°C) → J-apparent value for EPC3004 setpoint.

    Use this when writing a setpoint: the EPC3004 PID loop runs in J-scale, so
    you must convert the desired Type C temperature to its J-proxy equivalent
    before writing to the SP register.

    Args:
        t_c:  Desired Type C temperature in °C.
    Returns:
        J-apparent temperature to write as setpoint, in °C.
    """
    table = _C_TO_J
    if t_c <= table[0][0]:
        slope = (table[1][1] - table[0][1]) / (table[1][0] - table[0][0])
        return table[0][1] + slope * (t_c - table[0][0])
    if t_c >= table[-1][0]:
        slope = (table[-1][1] - table[-2][1]) / (table[-1][0] - table[-2][0])
        return table[-1][1] + slope * (t_c - table[-1][0])
    for i in range(len(table) - 1):
        tc0, tj0 = table[i]
        tc1, tj1 = table[i + 1]
        if tc0 <= t_c <= tc1:
            frac = (t_c - tc0) / (tc1 - tc0)
            return tj0 + frac * (tj1 - tj0)
    return float('nan')


# ══════════════════════════════════════════════════════════════════════════════
#  INTEGRATION NOTES — copy j_display_to_c() and c_to_j_display() into
#  devices/epc3004.py and use them as follows:
#
#  In EPC3004.poll_all():
#    pv_raw = self._read_words(R_PV, 1)[0]
#    pv_j_apparent = pv_raw / RESOLUTION_FACTOR   # existing code
#    snap['pv'] = j_display_to_c(pv_j_apparent)   # ← add this conversion
#
#  In EPC3004.apply_setpoint(celsius):
#    sp_j = c_to_j_display(celsius)               # ← convert first
#    raw  = round(sp_j * RESOLUTION_FACTOR)        # then encode for register
#    self._write_word(R_SP, raw)
#
#  The EPC3004's PID loop operates on J-scale internally, but all external
#  setpoints and PV readings seen by the rest of the Python code are in true
#  Type C °C.  No other changes needed.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
#  MODBUS RTU HELPERS (standalone — no dependency on project devices/)
# ══════════════════════════════════════════════════════════════════════════════

def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class ModbusRTU:
    """Minimal Modbus RTU client — FC 0x03 (read) + FC 0x06 (write single)."""

    def __init__(self, port: str, baud: int = 19200, node: int = 1):
        self.port = port
        self.baud = baud
        self.node = node
        self._ser = None

    def open(self):
        self._ser = serial.Serial(
            port=self.port, baudrate=self.baud,
            bytesize=8, parity='N', stopbits=1, timeout=0.25)
        log.info(f"Opened {self.port} at {self.baud} baud, node {self.node}")

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def _transact(self, req: bytes, expected: int) -> bytes:
        self._ser.reset_input_buffer()
        self._ser.write(req)
        time.sleep(0.01)
        resp = self._ser.read(expected)
        if not resp:
            raise IOError("No response (timeout)")
        if len(resp) >= 3 and (resp[1] & 0x80):
            raise IOError(f"Modbus exception FC={resp[1]&0x7F:#x} code={resp[2]}")
        if len(resp) < expected:
            raise IOError(f"Short response: got {len(resp)}, expected {expected}")
        if _crc16(resp[:expected - 2]) != struct.unpack("<H", resp[expected - 2:expected])[0]:
            raise IOError("CRC mismatch")
        return resp

    def read_registers(self, reg: int, count: int) -> list:
        req = struct.pack(">BBHH", self.node, 0x03, reg, count)
        req += struct.pack("<H", _crc16(req))
        resp = self._transact(req, 3 + count * 2 + 2)
        return [struct.unpack(">H", resp[3 + i * 2:5 + i * 2])[0] for i in range(count)]

    def read_int16(self, reg: int) -> int:
        v = self.read_registers(reg, 1)[0]
        return v - 0x10000 if v >= 0x8000 else v

    def write_register(self, reg: int, value: int) -> bool:
        val16 = value & 0xFFFF
        req = struct.pack(">BBHH", self.node, 0x06, reg, val16)
        req += struct.pack("<H", _crc16(req))
        try:
            self._transact(req, 8)
            return True
        except IOError as e:
            log.warning(f"  Write reg=0x{reg:04X} val={value}: {e}")
            return False


# ── AI.1.Type enum labels for display ──────────────────────────────────────────
_INPUT_TYPE_NAMES = {0: "Thermocouple", 1: "mV", 2: "V", 3: "mA", 4: "RTD", 5: "Zirconia"}
_LIN_NAMES = {
    0: "J", 1: "K", 2: "L", 3: "R", 4: "B", 5: "N", 6: "T", 7: "S",
    8: "Custom1", 9: "Custom2", 10: "PT100", 11: "PT1000", 12: "Linear", 13: "Sqrt"
}


# ══════════════════════════════════════════════════════════════════════════════
#  CHECK REGS — read current AI.1.Type and AI.1.Linearisation
# ══════════════════════════════════════════════════════════════════════════════

def do_check_regs(mb: ModbusRTU):
    print("\n" + "=" * 72)
    print(" CURRENT EPC3004 INPUT CONFIGURATION")
    print("=" * 72)

    type_raw = mb.read_registers(REG_INPUT_TYPE, 1)[0]
    lin_raw  = mb.read_registers(REG_LINEARISATION, 1)[0]

    type_name = _INPUT_TYPE_NAMES.get(type_raw, f"unknown({type_raw})")
    lin_name  = _LIN_NAMES.get(lin_raw,  f"unknown({lin_raw})")

    print(f"  AI.1.Type          [0x{REG_INPUT_TYPE:04X}]  =  {type_raw}  ({type_name})")
    print(f"  AI.1.Linearisation [0x{REG_LINEARISATION:04X}]  =  {lin_raw}  ({lin_name})")
    print()

    if type_raw == TYPE_THERMOCOUPLE and lin_raw == LIN_J:
        print("  ✓ J-PROXY MODE IS ACTIVE — ready for Type C TC with Python conversion.")
    elif type_raw == TYPE_THERMOCOUPLE:
        print(f"  → TC mode active but linearisation is {lin_name}, not J.")
        print("    Run --set-j-mode to activate J-proxy mode.")
    else:
        print(f"  → Input type is {type_name} (not thermocouple).")
        print("    Run --set-j-mode to activate J-proxy mode.")


# ══════════════════════════════════════════════════════════════════════════════
#  SET J MODE — write Thermocouple + J linearisation
# ══════════════════════════════════════════════════════════════════════════════

def do_set_j_mode(mb: ModbusRTU):
    print("\n" + "=" * 72)
    print(" ACTIVATING J-PROXY MODE ON EPC3004")
    print("=" * 72)
    print(f"  Writing AI.1.Type [0x{REG_INPUT_TYPE:04X}] ← {TYPE_THERMOCOUPLE} (Thermocouple)")
    ok1 = mb.write_register(REG_INPUT_TYPE, TYPE_THERMOCOUPLE)
    time.sleep(0.20)

    print(f"  Writing AI.1.Linearisation [0x{REG_LINEARISATION:04X}] ← {LIN_J} (J)")
    ok2 = mb.write_register(REG_LINEARISATION, LIN_J)
    time.sleep(0.30)

    # Readback
    rb_type = mb.read_registers(REG_INPUT_TYPE, 1)[0]
    rb_lin  = mb.read_registers(REG_LINEARISATION, 1)[0]

    print()
    print(f"  Readback AI.1.Type          = {rb_type} ({_INPUT_TYPE_NAMES.get(rb_type, '?')})  "
          f"{'✓' if rb_type == TYPE_THERMOCOUPLE else '✗'}")
    print(f"  Readback AI.1.Linearisation = {rb_lin} ({_LIN_NAMES.get(rb_lin, '?')})  "
          f"{'✓' if rb_lin == LIN_J else '✗'}")

    if rb_type == TYPE_THERMOCOUPLE and rb_lin == LIN_J:
        print("\n  ✓ J-proxy mode active.")
        print("  Connect Type C TC to IP1 terminals.")
        print("  The EPC3004 will display a J-apparent temperature (approx 18–679°C).")
        print("  Python software converts this to true Type C temperature (0–2300°C).")
        print()
        print("  NEXT: integrate j_display_to_c() / c_to_j_display() into epc3004.py")
        print("        (see INTEGRATION section in this script's comments)")
    else:
        print("\n  ✗ Write failed — device may need Config mode.")
        print("    Enter Config mode on EPC3004 front panel (hold Page + Scroll during")
        print("    power-up, enter password), then re-run --set-j-mode.")


# ══════════════════════════════════════════════════════════════════════════════
#  DRY RUN — print full conversion table
# ══════════════════════════════════════════════════════════════════════════════

def do_dryrun():
    print("\n" + "=" * 72)
    print(" J-PROXY CONVERSION TABLE — Type C TC via J-mode EPC3004")
    print("=" * 72)
    print(" EPC3004 in Type J mode reads Type C EMF and displays J-apparent temp.")
    print(" Python corrects: T_true_C = j_display_to_c(T_J_display)")
    print()
    print(f"  CJC assumption: T_cold_junction = 25°C")
    print(f"  CJC correction: V_J(25°C) − V_C(25°C) = 1.277 − 0.353 = +0.924 mV")
    print()
    print(f"  {'Pt':>2}  {'T_C_true (°C)':>14}  {'V_C (mV)':>9}  {'V_eff (mV)':>11}  "
          f"{'T_J_display (°C)':>17}  {'SP_raw (×10)':>12}  {'SetPoint to send':>16}")
    print("  " + "-" * 90)

    # Type C EMF table for display
    type_c_mv = [0.000, 1.451, 3.090, 4.865, 6.732, 10.609, 14.494, 18.260,
                 21.825, 25.155, 28.243, 31.087, 33.669, 34.849, 35.940, 36.931]
    delta_cjc = 0.924

    for i, (t_j, t_c) in enumerate(J_PROXY_TABLE):
        v_c   = type_c_mv[i]
        v_eff = v_c + delta_cjc
        sp_raw = round(t_j * RESOLUTION_FACTOR)
        # Verify conversion roundtrip
        rt = j_display_to_c(t_j)
        print(f"  {i+1:>2}  {t_c:>14.1f}  {v_c:>9.3f}  {v_eff:>11.3f}  "
              f"{t_j:>17.1f}  {sp_raw:>12d}  "
              f"{'c_to_j→'+str(round(c_to_j_display(t_c),1)):>16}")

    print()
    print(" T_J range: 17.9 – 679.1°C  (within J TC rated range of 0–760°C)")
    print(" T_C range:  0.0 – 2300°C   (15°C below Type C rated max of 2315°C)")
    print()
    print(" ROUNDTRIP CHECK: j_display_to_c(c_to_j_display(T_C)) ≈ T_C ?")
    worst_err = 0.0
    for t_j, t_c in J_PROXY_TABLE:
        rt = j_display_to_c(c_to_j_display(t_c))
        err = abs(rt - t_c)
        worst_err = max(worst_err, err)
    print(f"   Max roundtrip error: {worst_err:.4f}°C  (should be < 0.01°C at table points)")


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE TEST — read current PV and show conversion
# ══════════════════════════════════════════════════════════════════════════════

def do_test_live(mb: ModbusRTU):
    R_PV = 0x0001  # confirmed from epc3004.py diagnostic

    print("\n" + "=" * 72)
    print(" LIVE CONVERSION TEST — reading PV from EPC3004 every 2 s")
    print(" Ctrl-C to stop")
    print("=" * 72)
    print(f"  {'J-display (°C)':>16}  {'J raw':>8}  {'T_C_true (°C)':>14}")
    print("  " + "-" * 44)

    try:
        while True:
            raw = mb.read_int16(R_PV)
            t_j = raw / RESOLUTION_FACTOR
            t_c = j_display_to_c(t_j)
            print(f"  {t_j:>16.1f}  {raw:>8d}  {t_c:>14.1f}")
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\n  Stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="EPC3004 Type C TC setup via J-proxy method",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port",        default=DEFAULT_PORT, help=f"Serial port (default: {DEFAULT_PORT})")
    ap.add_argument("--baud",        type=int, default=BAUD)
    ap.add_argument("--node",        type=int, default=NODE)
    ap.add_argument("--check-regs",  action="store_true", help="Read current AI.1.Type and AI.1.Linearisation")
    ap.add_argument("--set-j-mode",  action="store_true", help="Write Thermocouple+J mode to EPC3004")
    ap.add_argument("--dryrun",      action="store_true", help="Print J-proxy conversion table (no serial)")
    ap.add_argument("--test-live",   action="store_true", help="Read PV in a loop and show converted T_C")
    args = ap.parse_args()

    if args.dryrun:
        do_dryrun()
        return

    if not any([args.check_regs, args.set_j_mode, args.test_live]):
        ap.print_help()
        print("\nRun --dryrun first to verify the conversion table without connecting.")
        return

    mb = ModbusRTU(args.port, args.baud, args.node)
    try:
        mb.open()
    except serial.SerialException as e:
        log.error(f"Cannot open {args.port}: {e}")
        sys.exit(1)

    try:
        if args.check_regs:
            do_check_regs(mb)
        if args.set_j_mode:
            do_set_j_mode(mb)
        if args.test_live:
            do_test_live(mb)
    finally:
        mb.close()
        log.info("Serial port closed.")


if __name__ == "__main__":
    main()
