"""
sim_devices.py  –  Simulated hardware for Test Mode

Each simulated device mirrors the API of its real counterpart exactly.
Behaviour:
  - BRAX3400Sim   : random walk pressure in physically plausible ranges
  - HEAT3Sim      : temperature ramps toward setpoint with realistic lag
  - ArduinoShutter: already uses the real driver if connected; this class
                    is provided as a fallback if the Arduino is NOT present

Test-mode policy (enforced by the GUI):
  If a device IS connected → use the real driver.
  If a device is NOT connected → use the Sim equivalent.
  The GUI toggles this at runtime with the TEST MODE button.
"""

import math
import random
import threading
import time
from typing import Optional


# ============================================================
# BRAX 3400 Simulator  (Ion Gauge + CG1/CG2/AI)
# ============================================================

class BRAX3400Sim:
    """Simulates the B-RAX 3400 vacuum gauge controller."""

    connected = True

    def __init__(self):
        # Base pressures – random-walk from realistic starting points
        self._ig  = 5e-7      # ion gauge (high vacuum)
        self._cg1 = 5e-3      # convectron 1 (roughing range)
        self._cg2 = 8e-3
        self._ai  = 3e-3
        self._ig_on = True
        self._lock  = threading.Lock()

    def _walk(self, val: float, lo: float, hi: float, scale: float) -> float:
        """Multiplicative random walk within [lo, hi]."""
        factor = math.exp(random.gauss(0, scale))
        new = val * factor
        return max(lo, min(hi, new))

    def connect(self)    -> bool: return True
    def disconnect(self) -> None: pass

    def read_ion_gauge(self) -> Optional[float]:
        if not self._ig_on:
            return 1.1e3
        with self._lock:
            self._ig = self._walk(self._ig, 1e-9, 5e-5, 0.03)
            return self._ig

    def read_cg1(self) -> Optional[float]:
        with self._lock:
            self._cg1 = self._walk(self._cg1, 1e-4, 1e3, 0.02)
            return self._cg1

    def read_cg2(self) -> Optional[float]:
        with self._lock:
            self._cg2 = self._walk(self._cg2, 1e-4, 1e3, 0.02)
            return self._cg2

    def read_analog_input(self) -> Optional[float]:
        with self._lock:
            self._ai = self._walk(self._ai, 1e-4, 1e3, 0.02)
            return self._ai

    def ig_on(self)     -> bool:
        self._ig_on = True
        return True

    def ig_off(self)    -> bool:
        self._ig_on = False
        return True

    def ig_status(self) -> Optional[bool]:
        return self._ig_on

    def read_firmware_version(self) -> str:
        return "SIM-01306-11"


# ============================================================
# HEAT3 Channel Simulator  (single channel)
# ============================================================

class _ChannelSim:
    """Simulates one PID-controlled heating channel with thermal lag."""

    def __init__(self, ambient: float = 25.0):
        self._pv       = ambient     # process value (°C)
        self._sp       = ambient     # setpoint
        self._operating = False
        self._output_pct = 0.0
        # Thermal model constants
        self._tau      = 120.0       # time constant (seconds)
        self._max_rate = 5.0         # max °C/s at 100% output
        self._last_t   = time.time()
        self._lock     = threading.Lock()

    def tick(self):
        """Advance the thermal simulation. Call ~1/s."""
        now = time.time()
        with self._lock:
            dt = now - self._last_t
            self._last_t = now
            if self._operating:
                error = self._sp - self._pv
                # Simplified P-only sim with rate limiting
                out = max(0.0, min(100.0, error * 2.0))
                self._output_pct = out
                rate = (out / 100.0) * self._max_rate
                # Cool toward ambient when output low
                cooling = (self._pv - 25.0) / self._tau
                self._pv += (rate - cooling) * dt
                # Add a tiny noise
                self._pv += random.gauss(0, 0.05)
            else:
                # Cool down toward ambient
                self._output_pct = 0.0
                self._pv += -(self._pv - 25.0) / self._tau * dt

    @property
    def process_value(self) -> float:
        with self._lock:
            return self._pv

    @property
    def setpoint(self) -> float:
        with self._lock:
            return self._sp

    @setpoint.setter
    def setpoint(self, v: float):
        with self._lock:
            self._sp = v

    @property
    def operating(self) -> bool:
        with self._lock:
            return self._operating

    def go_operate(self):
        with self._lock:
            self._operating = True

    def go_standby(self):
        with self._lock:
            self._operating = False

    @property
    def output_pct(self) -> float:
        with self._lock:
            return self._output_pct


# ============================================================
# HEAT3 Simulator
# ============================================================

class HEAT3Sim:
    """Simulates the HEAT3 dual-channel heating power supply."""

    connected = True

    def __init__(self):
        self._ch1 = _ChannelSim(ambient=25.0)
        self._ch2 = _ChannelSim(ambient=25.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()

    def _tick_loop(self):
        while not self._stop.is_set():
            self._ch1.tick()
            self._ch2.tick()
            self._stop.wait(1.0)

    def connect(self)    -> bool: return True
    def disconnect(self) -> None:
        self._stop.set()

    # CH1
    def ch1_read_process_value(self) -> Optional[float]: return self._ch1.process_value
    def ch1_read_setpoint(self)      -> Optional[float]: return self._ch1.setpoint
    def ch1_set_setpoint(self, c: float) -> bool:
        self._ch1.setpoint = c
        return True
    def ch1_read_tc1(self) -> Optional[float]:
        return self._ch1.process_value + random.gauss(0, 0.3)
    def ch1_read_tc2(self) -> Optional[float]:
        return self._ch1.process_value + random.gauss(0, 0.5)
    def ch1_read_output_pct(self)   -> Optional[float]: return self._ch1.output_pct
    def ch1_read_output_watts(self) -> Optional[float]: return self._ch1.output_pct * 5.0
    def ch1_is_operating(self)      -> Optional[bool]:  return self._ch1.operating
    def ch1_pid_state(self)         -> Optional[int]:   return 1 if self._ch1.operating else 0
    def ch1_go_operate(self) -> bool: self._ch1.go_operate(); return True
    def ch1_go_standby(self) -> bool: self._ch1.go_standby(); return True
    def ch1_set_mode_auto(self)   -> bool: return True
    def ch1_set_mode_manual(self) -> bool: return True

    # CH2
    def ch2_read_process_value(self) -> Optional[float]: return self._ch2.process_value
    def ch2_read_setpoint(self)      -> Optional[float]: return self._ch2.setpoint
    def ch2_set_setpoint(self, c: float) -> bool:
        self._ch2.setpoint = c
        return True
    def ch2_read_output_pct(self)   -> Optional[float]: return self._ch2.output_pct
    def ch2_read_output_watts(self) -> Optional[float]: return self._ch2.output_pct * 3.0
    def ch2_is_operating(self)      -> Optional[bool]:  return self._ch2.operating
    def ch2_pid_state(self)         -> Optional[int]:   return 1 if self._ch2.operating else 0
    def ch2_go_operate(self) -> bool: self._ch2.go_operate(); return True
    def ch2_go_standby(self) -> bool: self._ch2.go_standby(); return True
    def ch2_set_mode_auto(self)   -> bool: return True
    def ch2_set_mode_manual(self) -> bool: return True

    # ── Unified interface matching real HEAT3 ──────────────────────────

    def apply_setpoint(self, ch: int, celsius: float) -> dict:
        ch_name = "CH1(Se)" if ch == 1 else "CH2(Sub)"
        (self._ch1 if ch == 1 else self._ch2).setpoint = celsius
        return {"ok": True, "readback": celsius,
                "message": f"{ch_name} SIM SP → {celsius:.1f}°C ✓"}

    def go_operate(self, ch: int = 1) -> dict:
        ch_name = "CH1(Se)" if ch == 1 else "CH2(Sub)"
        (self._ch1 if ch == 1 else self._ch2).go_operate()
        return {"ok": True, "message": f"{ch_name} SIM → OPERATE"}

    def go_standby(self, ch: int = 1) -> dict:
        ch_name = "CH1(Se)" if ch == 1 else "CH2(Sub)"
        (self._ch1 if ch == 1 else self._ch2).go_standby()
        return {"ok": True, "message": f"{ch_name} SIM → STANDBY"}

    # Legacy aliases kept for compatibility
    def read_process_value_celsius(self) -> Optional[float]: return self.ch1_read_process_value()
    def read_setpoint_celsius(self)      -> Optional[float]: return self.ch1_read_setpoint()
    def set_setpoint_celsius(self, c)    -> bool:            return self.ch1_set_setpoint(c)
    def is_operating(self)               -> Optional[bool]:  return self.ch1_is_operating()

    def enable_remote(self)  -> bool: return True
    def disable_remote(self) -> bool: return True
    debug_callback = None  # mirrors real HEAT3 attr so GUI can safely set it

    # ── Internal ramp rate (device-level) ─────────────────────────────
    # Mirrors real HEAT3 API; defaults match hardware factory defaults.
    _ramp_rate = {1: 4.0, 2: 100.0}   # °C/min per channel

    def read_ramp_rate(self, ch: int):
        return self._ramp_rate.get(ch, 0.0)

    def write_ramp_rate(self, ch: int, rate_per_min: float) -> bool:
        self._ramp_rate[ch] = max(0.0, rate_per_min)
        return True


# ============================================================
# Arduino Shutter Simulator  (used only if Arduino not connected)
# ============================================================

class ArduinoShutterSim:
    """Simulates the Arduino shutter controller."""

    connected = True

    def __init__(self):
        self._in_open = False
        self._se_open = False

    def connect(self)    -> bool: return True
    def disconnect(self) -> None: pass

    def in_open(self)    -> bool: self._in_open = True;  return True
    def in_close(self)   -> bool: self._in_open = False; return True
    def se_open(self)    -> bool: self._se_open = True;  return True
    def se_close(self)   -> bool: self._se_open = False; return True
    def both_open(self)  -> bool: self._in_open = True;  self._se_open = True;  return True
    def both_close(self) -> bool: self._in_open = False; self._se_open = False; return True
    def query_status(self): return self._in_open, self._se_open

    @property
    def in_is_open(self) -> bool:  return self._in_open
    @property
    def se_is_open(self) -> bool:  return self._se_open


# ============================================================
# EPC3004 Simulator  (Indium source single-loop temperature controller)
# ============================================================

class EPC3004Sim:
    """Simulates the Eurotherm EPC3004 single-loop temperature controller.

    Used as the Indium evaporation source controller in VacDepositionControl.
    Mirrors the real EPC3004 API exactly so the GUI and recipe engine can use
    the sim transparently in Test Mode.
    """

    connected    = True
    debug_callback = None   # real EPC3004 has this attr; GUI may set it

    def __init__(self):
        self._ch   = _ChannelSim(ambient=25.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()
        self._ramp_rate: float = 4.0   # °C/min default (can be written via write_ramp_rate)

    def _tick_loop(self):
        while not self._stop.is_set():
            self._ch.tick()
            self._stop.wait(1.0)

    def connect(self)    -> bool: return True
    def disconnect(self) -> None: self._stop.set()

    # ── Single-channel reads ───────────────────────────────────────────
    def ch1_read_process_value(self) -> Optional[float]: return self._ch.process_value
    def ch1_read_setpoint(self)      -> Optional[float]: return self._ch.setpoint
    def ch1_read_output_pct(self)    -> Optional[float]: return self._ch.output_pct
    def ch1_is_operating(self)       -> Optional[bool]:  return self._ch.operating

    # ── Command sequences (matching real EPC3004 return types) ─────────
    def apply_setpoint(self, ch: int, celsius: float) -> dict:
        self._ch.setpoint = celsius
        return {"ok": True, "readback": celsius,
                "message": f"In SIM SP → {celsius:.1f}°C ✓"}

    def go_operate(self, ch: int = 1) -> dict:
        self._ch.go_operate()
        return {"ok": True, "message": "In SIM → OPERATE"}

    def go_standby(self, ch: int = 1) -> dict:
        self._ch.go_standby()
        return {"ok": True, "message": "In SIM → STANDBY"}

    # ── Ramp rate ──────────────────────────────────────────────────────
    def read_ramp_rate(self, ch: int) -> float:
        return self._ramp_rate

    def write_ramp_rate(self, ch: int, rate_per_min: float) -> bool:
        self._ramp_rate = max(0.0, rate_per_min)
        return True

    # ── Batch poll (same key namespace as real EPC3004) ────────────────
    def poll_all(self) -> dict:
        return {
            "in_pv":  self._ch.process_value,
            "in_sp":  self._ch.setpoint,
            "in_op":  self._ch.operating,
            "in_out": self._ch.output_pct,
        }

    # ── Legacy aliases ─────────────────────────────────────────────────
    def ch1_set_setpoint(self, c: float)  -> bool: self._ch.setpoint = c; return True
    def ch1_go_operate(self)              -> bool: self._ch.go_operate(); return True
    def ch1_go_standby(self)              -> bool: self._ch.go_standby(); return True
    def ch1_set_mode_auto(self)           -> bool: return True
    def is_operating(self)       -> Optional[bool]:  return self._ch.operating
    def set_setpoint_celsius(self, c)     -> bool: self._ch.setpoint = c; return True
    def read_process_value_celsius(self)  -> Optional[float]: return self._ch.process_value
