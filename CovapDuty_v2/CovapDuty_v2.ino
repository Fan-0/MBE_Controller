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