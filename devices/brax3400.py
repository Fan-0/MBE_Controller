"""
BRAX 3400 Pressure Gauge Controller
InstruTech RS485 Protocol (19200 baud, 8N1)
"""

import serial
import time
import threading
from typing import Optional


class BRAX3400:
    OVER_RANGE = 1.1e3

    def __init__(self, port: str, address: int = 1, baudrate: int = 19200):
        self.port     = port
        self.address  = f"{address:02X}"
        self.baudrate = baudrate
        self._serial: Optional[serial.Serial] = None
        self._lock    = threading.Lock()
        self.connected = False

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self._serial = serial.Serial(
                port=self.port, baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=0.25,
            )
            self.connected = self._serial.is_open
            return self.connected
        except serial.SerialException as e:
            self.connected = False
            raise ConnectionError(f"BRAX connect failed on {self.port}: {e}")

    def disconnect(self):
        self.connected = False
        try:
            if self._serial and self._serial.is_open:
                self._serial.close()
        except Exception:
            pass
        self._serial = None

    def _is_open(self) -> bool:
        """Safe check – port can disappear at runtime."""
        try:
            return self._serial is not None and self._serial.is_open
        except Exception:
            return False

    # ── Low-level comms ─────────────────────────────────────────────────

    def _send_command(self, cmd: str) -> str:
        if not self._is_open():
            raise IOError("BRAX serial port not open")
        full_cmd = f"#{self.address}{cmd}\r"
        try:
            with self._lock:
                self._serial.reset_input_buffer()
                self._serial.write(full_cmd.encode("ascii"))
                time.sleep(0.02)   # 20 ms: RS-485 turnaround headroom
                raw = self._serial.read_until(b'\r')  # BRAX responses end in \r (not \n)
            return raw.decode("ascii", errors="ignore").strip()
        except serial.SerialException as e:
            self.connected = False
            raise IOError(f"BRAX comms error: {e}")

    def _parse_pressure(self, response: str) -> Optional[float]:
        """Parse *01_1.53E-06 style response."""
        if not response or response.startswith("?"):
            return None
        try:
            return float(response[4:].strip())
        except (ValueError, IndexError):
            return None

    # ── Pressure reads ───────────────────────────────────────────────────

    def read_ion_gauge(self) -> Optional[float]:
        try:
            return self._parse_pressure(self._send_command("RDIG"))
        except Exception:
            return None

    def read_cg1(self) -> Optional[float]:
        try:
            return self._parse_pressure(self._send_command("RDCG1"))
        except Exception:
            return None

    def read_cg2(self) -> Optional[float]:
        try:
            return self._parse_pressure(self._send_command("RDCG2"))
        except Exception:
            return None

    def read_analog_input(self) -> Optional[float]:
        try:
            return self._parse_pressure(self._send_command("RDAI"))
        except Exception:
            return None

    # ── IG control ───────────────────────────────────────────────────────

    def ig_on(self) -> bool:
        try:
            return "PROGM_OK" in self._send_command("IG1")
        except Exception:
            return False

    def ig_off(self) -> bool:
        try:
            return "PROGM_OK" in self._send_command("IG0")
        except Exception:
            return False

    def ig_status(self) -> Optional[bool]:
        try:
            resp = self._send_command("IGS")
            if "IG_ON"  in resp: return True
            if "IG_OFF" in resp: return False
            return None
        except Exception:
            return None

    def read_firmware_version(self) -> str:
        try:
            resp = self._send_command("VER")
            return resp[4:].strip() if len(resp) > 4 else "?"
        except Exception:
            return "?"
