"""
Arduino Shutter Controller
Serial communication with the Arduino to control the two shutter pins
(Indium source = "In", Selenium source = "Se").

The Arduino runs updated firmware (see CovapDuty_v2.ino) that listens
for text commands over Serial at 9600 baud.

Commands sent to Arduino:
    IN_ON       - Open In shutter  (pin LOW)
    IN_OFF      - Close In shutter (pin HIGH)
    SE_ON       - Open Se shutter  (pin LOW)
    SE_OFF      - Close Se shutter (pin HIGH)
    BOTH_ON     - Open both shutters
    BOTH_OFF    - Close both shutters
    STATUS      - Query status

Arduino responses:
    OK:IN_ON
    OK:IN_OFF
    OK:SE_ON
    OK:SE_OFF
    OK:BOTH_ON
    OK:BOTH_OFF
    STATUS:IN=ON,SE=OFF
"""

import serial
import threading
import time
from typing import Optional, Tuple


class ArduinoShutter:
    """Driver for the Arduino-controlled shutter system."""

    def __init__(self, port: str, baudrate: int = 9600):
        self.port = port
        self.baudrate = baudrate
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self.connected = False
        # Tracked states (updated on successful command)
        self._in_open = False
        self._se_open = False

    def connect(self) -> bool:
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=2.0,
            )
            # Arduino resets on serial connect; wait for it to boot
            time.sleep(2.0)
            self._serial.reset_input_buffer()
            self.connected = True
            self.query_status()  # Sync state
            return True
        except serial.SerialException as e:
            self.connected = False
            raise ConnectionError(f"Arduino connect failed on {self.port}: {e}")

    def disconnect(self):
        if self._serial and self._serial.is_open:
            self._serial.close()
        self.connected = False

    def _send(self, command: str) -> str:
        if not self._serial or not self._serial.is_open:
            raise IOError("Arduino serial port not open")
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write((command + "\n").encode("ascii"))
            time.sleep(0.1)
            raw = self._serial.readline()
        return raw.decode("ascii", errors="ignore").strip()

    def in_open(self) -> bool:
        resp = self._send("IN_ON")
        if "IN_ON" in resp or "OK" in resp:
            self._in_open = True
            return True
        return False

    def in_close(self) -> bool:
        resp = self._send("IN_OFF")
        if "IN_OFF" in resp or "OK" in resp:
            self._in_open = False
            return True
        return False

    def se_open(self) -> bool:
        resp = self._send("SE_ON")
        if "SE_ON" in resp or "OK" in resp:
            self._se_open = True
            return True
        return False

    def se_close(self) -> bool:
        resp = self._send("SE_OFF")
        if "SE_OFF" in resp or "OK" in resp:
            self._se_open = False
            return True
        return False

    def both_open(self) -> bool:
        resp = self._send("BOTH_ON")
        if "BOTH_ON" in resp or "OK" in resp:
            self._in_open = True
            self._se_open = True
            return True
        return False

    def both_close(self) -> bool:
        resp = self._send("BOTH_OFF")
        if "BOTH_OFF" in resp or "OK" in resp:
            self._in_open = False
            self._se_open = False
            return True
        return False

    def query_status(self) -> Tuple[Optional[bool], Optional[bool]]:
        """Query Arduino for current shutter states. Returns (in_open, se_open)."""
        try:
            resp = self._send("STATUS")
            # Parse STATUS:IN=ON,SE=OFF
            if "IN=ON" in resp:
                self._in_open = True
            elif "IN=OFF" in resp:
                self._in_open = False
            if "SE=ON" in resp:
                self._se_open = True
            elif "SE=OFF" in resp:
                self._se_open = False
        except Exception:
            pass
        return self._in_open, self._se_open

    @property
    def in_is_open(self) -> bool:
        return self._in_open

    @property
    def se_is_open(self) -> bool:
        return self._se_open


# ============================================================
# UPDATED ARDUINO FIRMWARE
# Save this as CovapDuty_v2.ino and flash to the Arduino
# ============================================================
ARDUINO_FIRMWARE = """
// CovapDuty_v2.ino
// Shutter controller - receives serial commands from Python GUI
// Commands: IN_ON, IN_OFF, SE_ON, SE_OFF, BOTH_ON, BOTH_OFF, STATUS

const int PIN_IN = 10;   // Indium source shutter
const int PIN_SE = 7;    // Selenium source shutter

bool inOpen = false;
bool seOpen = false;

void setup() {
  Serial.begin(9600);
  pinMode(PIN_IN, OUTPUT);
  pinMode(PIN_SE, OUTPUT);
  digitalWrite(PIN_IN, HIGH);  // HIGH = closed (active LOW relay)
  digitalWrite(PIN_SE, HIGH);
  Serial.println("READY");
}

void setIn(bool open) {
  inOpen = open;
  digitalWrite(PIN_IN, open ? LOW : HIGH);
}

void setSe(bool open) {
  seOpen = open;
  digitalWrite(PIN_SE, open ? LOW : HIGH);
}

void loop() {
  if (Serial.available() > 0) {
    String cmd = Serial.readStringUntil('\\n');
    cmd.trim();

    if (cmd == "IN_ON")        { setIn(true);  Serial.println("OK:IN_ON"); }
    else if (cmd == "IN_OFF")  { setIn(false); Serial.println("OK:IN_OFF"); }
    else if (cmd == "SE_ON")   { setSe(true);  Serial.println("OK:SE_ON"); }
    else if (cmd == "SE_OFF")  { setSe(false); Serial.println("OK:SE_OFF"); }
    else if (cmd == "BOTH_ON") {
      setIn(true); setSe(true);
      Serial.println("OK:BOTH_ON");
    }
    else if (cmd == "BOTH_OFF") {
      setIn(false); setSe(false);
      Serial.println("OK:BOTH_OFF");
    }
    else if (cmd == "STATUS") {
      Serial.print("STATUS:IN=");
      Serial.print(inOpen ? "ON" : "OFF");
      Serial.print(",SE=");
      Serial.println(seOpen ? "ON" : "OFF");
    }
    else {
      Serial.println("ERR:UNKNOWN_CMD");
    }
  }
}
"""
