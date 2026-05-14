# Vacuum Deposition Control System

A LabVIEW-style Python/PyQt5 GUI for controlling a vacuum deposition tool with:
- **BRAX 3400** pressure gauge controller (InstruTech RS485)
- **HEAT3** dual-loop heating power supply (Modbus RTU) — CH1=Selenium, CH2=Substrate
- **EPC3004** single-loop PID controller (Eurotherm, Modbus RTU) — Indium evaporation source (Type C TC via J-proxy)
- **Arduino** shutter controller (Serial, 2 sources: Indium + Selenium)

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Flash the Arduino
1. Flash `CovapDuty_v2.ino` to your Arduino Uno
2. Or use **File → Export Firmware…** in the app to save the sketch

### 3. Run the application
```bash
python main.py
```

### 4. Connect hardware
- Set COM ports in the top toolbar or go to **⚙ Settings**
- Click **Connect All**

### 5. (First time only) Configure the EPC3004 for Type C thermocouple

The EPC3004 does not natively support Type C (W5%Re/W26%Re). Run this **once** to
put it in Type J mode — the software handles the conversion automatically:

```bash
python diagnostics/configure_epc3004_cstm1.py --port COM13 --set-j-mode
```

Verify with:
```bash
python diagnostics/configure_epc3004_cstm1.py --port COM13 --check-regs
```

After this one-time setup the app reads and writes true Type C temperatures (0–2300°C)
transparently. See **Type C Thermocouple (J-Proxy)** section below for details.

---

## Hardware Configuration

| Device      | Interface  | Default Port | Baud  | Protocol               |
|-------------|------------|--------------|-------|------------------------|
| HEAT3       | RS485      | COM12        | 19200 | Modbus RTU, Slave 1    |
| BRAX 3400   | RS485      | COM11        | 19200 | InstruTech RS485       |
| Arduino     | USB Serial | COM7         | 9600  | Plain text commands    |
| EPC3004     | RS485      | COM13        | 19200 | Modbus RTU, Slave 1    |

> Ports and baud are configurable in **⚙ Settings** or the topbar COM dropdowns.

---

## Device-Internal Ramp Rates

Each controller has an internal ramp rate that controls how fast it transitions to a
newly-written setpoint.  The SP is always written instantly; the device itself ramps to it.

| Device  | Channel | Default |
|---------|---------|---------|
| HEAT3   | CH1 (Selenium)  | 4 °C/min  |
| HEAT3   | CH2 (Substrate) | 100 °C/min |
| EPC3004 | CH1 (Indium)    | 4 °C/min  |

**In the GUI:** The **Dev Ramp** spinbox on each channel panel shows the current device
rate.  Click **Set** to write it.  0 = "Off (instant)".

On connect (or when Test Mode is enabled), the spinboxes are auto-populated by reading
the device registers.

> **Note:** `RAMP_TEMP` in the recipe engine writes the device-internal ramp rate (one
> register write), not a software SP-stepping loop.  Typical pattern:
> `RAMP_TEMP` (set rate) → `SET_TEMP` (move to target) → `WAIT_TEMP` (stabilize).

---

## Data Update Rate

The poller defaults to **0.25 s** intervals. With `poll_all()` batching (3 Modbus reads
instead of 8), the hardware read cycle takes ~50–70 ms, giving **~4 Hz** data refresh.

| Timer | Interval | Purpose |
|---|---|---|
| Poller thread | 0.25 s (default) | Serial reads → Snap + DataLogger.feed() |
| Value labels (`_vt`) | 100 ms | Reads Snap, updates QLabel text |
| PlotWorker thread | 100 ms tick | Builds numpy arrays, emits signal |
| rec_log sync (`_pt`) | 100 ms | Pushes recipe rows into plot buffers |
| Plot update (`_on_plot_data`) | on signal | Calls pyqtgraph setData() |

Buffer→plot latency: **~50 ms** (PlotWorker picks up new data within 100 ms of arrival).

To adjust the poll rate: **⚙ Settings → Poll interval** (range 0.10–30 s).

> **Architecture note:** The Poller is the single serial-read path. After each cycle it
> calls `DataLogger.feed()` directly, so the plot and the CSV see data at the same rate.
> There is no separate DataLogger poll thread competing for the serial port.

---

## Recipe File Format

Recipes are Excel (`.xlsx`) files with the following columns:

| Column | Header  | Description                                                    |
|--------|---------|----------------------------------------------------------------|
| A      | Step    | Step number (integer, 1-based)                                 |
| B      | Action  | Action keyword (see below)                                     |
| C      | Param1  | First parameter (channel / source / value)                     |
| D      | Param2  | Second parameter (°C target, °C/min rate, tolerance, loop count) |
| E      | Notes   | Human-readable description                                     |

> **Backward compatible:** Old 6-column files (with a separate Rate column) load correctly — the extra column is read and ignored.

### Supported Actions

| Action            | Param1                                                      | Param2           | Description                                     |
|-------------------|-------------------------------------------------------------|------------------|-------------------------------------------------|
| `SET_TEMP`        | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | °C target        | Jump SP to target immediately                   |
| `RAMP_TEMP`       | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | °C/min rate      | Set device-internal SP ramp rate                |
| `WAIT_TEMP`       | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | ±°C tolerance    | Wait until PV within ±Param2 of SP              |
| `WAIT_PRESSURE`   | Torr target                                                 | —                | Wait until IG ≤ Param1                          |
| `WAIT_TIME`       | seconds                                                     | —                | Wait for Param1 seconds                         |
| `OPEN_SHUTTER`    | `In`, `Se`, or `Both`                                       | —                | Open shutter(s)                                 |
| `CLOSE_SHUTTER`   | `In`, `Se`, or `Both`                                       | —                | Close shutter(s)                                |
| `HEATER_ON`       | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | —                | Device → OPERATE state                          |
| `HEATER_OFF`      | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | —                | Device → STANDBY state                          |
| `IG_ON`           | —                                                           | —                | Turn ion gauge ON                               |
| `IG_OFF`          | —                                                           | —                | Turn ion gauge OFF                              |
| `GOTO`            | step# to jump to                                            | loop count       | Loop: executes loop count+1 times total         |
| `END`             | —                                                           | —                | Terminate recipe cleanly                        |

### RAMP_TEMP Example
```
Step 1  HEATER_ON   Substrate (CH2)  —      Enable substrate heater
Step 2  RAMP_TEMP   Substrate (CH2)  5      Set sub device ramp rate to 5°C/min
Step 3  SET_TEMP    Substrate (CH2)  300    Move SP to 300°C (device ramps at 5°C/min)
Step 4  WAIT_TEMP   Substrate (CH2)  5      Wait until within ±5°C of SP
Step 5  RAMP_TEMP   Selenium (CH1)   10     Set Se device ramp rate to 10°C/min
Step 6  SET_TEMP    Selenium (CH1)   200    Move Se SP to 200°C
Step 7  RAMP_TEMP   Indium (EPC)     4      Set Indium device ramp rate to 4°C/min
Step 8  SET_TEMP    Indium (EPC)     800    Move Indium SP to 800°C
```

### GOTO / Loop Example
```
Step 5  OPEN_SHUTTER   Both  —    —    Open shutters
Step 6  WAIT_TIME      60    —    —    Wait 60s (deposition)
Step 7  CLOSE_SHUTTER  Both  —    —    Close shutters
Step 8  WAIT_TIME      30    —    —    Wait 30s (inter-step)
Step 9  GOTO           5     4    —    Loop back to step 5, run 5 times total (loops+1)
Step 10 HEATER_OFF     Selenium (CH1)  —  —    Cool down Se
```

To generate a sample recipe file:
```bash
python recipes/create_recipe_template.py
```

---

## Type C Thermocouple (J-Proxy)

The EPC3004 has no native Type C support. The **J-proxy method** works as follows:

1. **Hardware config (one-time):** EPC3004 is set to Type J TC mode via two Modbus register writes (see Quick Start step 5).
2. **Physics:** The Type C TC generates EMF V_C(T). The EPC3004, expecting Type J EMF, applies a constant CJC offset (+0.924 mV at 25°C cold junction) and inverts the Type J curve — producing a J-apparent temperature in the range ~18–679°C.
3. **Software correction:** `j_display_to_c()` and `c_to_j_display()` in `devices/epc3004.py` implement a 16-point piecewise-linear table (ASTM E988 + NIST ITS-90 Type J) that converts between the J-apparent display value and true Type C temperature (0–2300°C).
4. **Transparent integration:** `EPC3004` is instantiated with `j_proxy=True`. All PV reads and SP writes are automatically converted — the recipe engine, safety system, GUI labels, and BEP manager all see true °C.

| J-apparent display | True Type C temp |
|--------------------|-----------------|
| ~18°C | 0°C |
| ~284°C | 800°C |
| ~531°C | 1600°C |
| ~679°C | 2300°C |

The full 16-point table is in `manuals/type_c_via_j_proxy.csv`.

**Diagnostic tool:** `diagnostics/configure_epc3004_cstm1.py`
- `--check-regs` — read current EPC3004 input type registers
- `--set-j-mode` — activate J-proxy mode (write 2 registers)
- `--dryrun` — print the full conversion table (no serial)
- `--test-live` — read PV live and show J-apparent + true Type C side by side

---

## Building an EXE

```bash
pip install pyinstaller
pyinstaller build.spec
```

The output is in `dist/VacControl/VacControl.exe`.

---

## Project Structure

```
vac_control/
├── main.py                    # Entry point
├── gui_app.py                 # Main PyQt5 window (timers, poller, plots)
├── recipe_engine.py           # Recipe state machine
├── recipe_logger.py           # 1Hz run logger → .xlsx export
├── data_logger.py             # CSV logger + deque plot buffers (fed via feed())
├── bep_manager.py             # BEP measurement workflow (20Hz IG sampling)
├── CovapDuty_v2.ino           # Arduino firmware
├── CovapDuty_v2/              # Arduino sketch folder
├── devices/
│   ├── heat3.py               # HEAT3 Modbus RTU driver + poll_all()
│   ├── brax3400.py            # BRAX 3400 RS485 driver (read_until \r)
│   ├── arduino_shutter.py     # Arduino serial driver + firmware string
│   ├── epc3004.py             # Eurotherm EPC3004 PID driver (Indium source)
│   ├── sim_devices.py         # Sim devices for test mode (incl. EPC3004Sim)
│   └── __init__.py
├── diagnostics/
│   ├── timing_diag.py               # Poll-cycle timing benchmark ← run to verify hw perf
│   ├── plot_diag.py                 # GUI plot pipeline diagnostic ← run to verify plot perf
│   ├── heat3_diag.py                # CH2 setpoint investigation
│   ├── heat3_remote_test.py         # PREVAC V2 remote-enable diagnostic
│   ├── heat3_remote_watchdog.py     # Remote watchdog simulator
│   └── configure_epc3004_cstm1.py  # EPC3004 Type C TC setup (J-proxy, one-time)
├── logs/                      # Auto-created; all CSV + Excel output goes here
├── recipes/
│   ├── create_recipe_template.py  # Generates a sample recipe
│   ├── recipe.xlsx / *.xlsx   # Saved recipes
│   └── bep_log.csv            # Cumulative BEP history
├── manuals/
│   ├── Heat3_UserManual.pdf
│   ├── VacDepositionControl_TechnicalManual.pdf
│   ├── type_c_via_j_proxy.csv  # J-proxy lookup table (J-apparent ↔ Type C °C)
│   └── type_c_lin16_v2.csv     # ASTM E988 Type C reference table (16-point, 0–2300°C)
├── requirements.txt
├── build.spec                 # PyInstaller config
└── README.md
```

---

## Live Pressure & Temperature Logging

- Click **▶ Start Logging** in the Live Plot tab
- CSV logs save to `logs/pressure_log_YYYYMMDD_HHMMSS.csv` (folder auto-created)
- Columns: `Timestamp, IG_Pressure_Torr, CH1_Temp_C, CH2_Temp_C, In_Temp_C`
- The live plot shows a **rolling window** of the last 1 / 10 / 30 / 60 minutes
  — select with the buttons above the plots (default 10 min)
- The x-axis shows **real wall-clock time** (HH:MM:SS) via pyqtgraph DateAxisItem
- Up to 20 000 points are kept in RAM (~1.4 hr at 4 Hz); full history is in the CSV
- During a recipe run, logging is handed off to `RecipeLogger` (Excel export to `logs/`)

---

## Diagnostics

All diagnostic scripts require exclusive serial access — close the GUI first.
Run from the `vac_control/` directory using the Anaconda Python interpreter:

```
C:\Users\fmammo3\AppData\Local\anaconda3\python.exe diagnostics/<script>.py [args]
```

| Script | Purpose | Example |
|--------|---------|---------|
| `timing_diag.py` | Benchmark hardware poll-cycle timing (needs exclusive serial) | `... timing_diag.py COM12 COM11 19200` |
| `plot_diag.py` | Benchmark GUI plot pipeline end-to-end (no hardware needed) | `... plot_diag.py --duration 10 --inject-hz 4` |
| `heat3_sp_direct.py` | Verify SP write works on real hardware | `... heat3_sp_direct.py COM12 19200 1 100` |
| `heat3_diag.py` | Deep CH2 register dump | `... heat3_diag.py COM12 19200 1 100` |

**HEAT3 prerequisite:** Remote Enable must be ON via the device front panel:
`Setup → Device Settings → Communication → Remote Enable → ON`

The `timing_diag.py` benchmark shows per-call timing for each HEAT3 read, compares
old 8-read path vs `poll_all()` batch path, and projects effective data rates.

---

## Test Mode

Click **🧪 Test: OFF** in the toolbar to enable Test Mode. Simulated hardware
(HEAT3Sim, BRAX3400Sim, ArduinoShutterSim, **EPC3004Sim**) is used — no real serial
ports needed.  All sim devices use a P-only thermal model with realistic ramp behavior.
Logging starts automatically in test mode.
