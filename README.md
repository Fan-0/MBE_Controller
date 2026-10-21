# Vacuum Deposition Control System

A LabVIEW-style Python/PyQt5 GUI for controlling a vacuum deposition tool with:
- **BRAX 3400** pressure gauge controller (InstruTech RS485)
- **HEAT3** dual-loop heating power supply (Modbus RTU) — CH1=Selenium, CH2=Substrate
- **EPC3004** single-loop PID controller (Eurotherm, Modbus RTU) — Indium evaporation source (Type C TC via J-proxy)
- **Arduino** shutter controller (Serial, 2 sources: Indium + Selenium)
- **LN2 dewar level tracker** — logs into Air Products APDirect twice daily (or on demand) and shows a live fill graphic + trend plot

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

### 6. (First time only, optional) Set up the LN2 dewar monitor

Run this **yourself, in your own terminal** — it asks for your Air Products APDirect
password with hidden input, so it must be interactive:

```bash
python tools\setup_ln2_credentials.py
```

See **LN2 Dewar Monitor** section below for what this does and how the panel behaves.
Skip this step if you don't need LN2 tracking — the rest of the app works fine without it.

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
| `INCREASE_TEMP`   | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | Δ°C step         | SP += Param2 (negative steps down). Relative to the **setpoint**, not the live PV, so repeated passes land on exact temperatures |
| `WAIT_TEMP`       | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | ±°C tolerance    | Wait until PV within ±Param2 of SP              |
| `WAIT_PRESSURE`   | Torr target                                                 | —                | Wait until IG ≤ Param1                          |
| `WAIT_TIME`       | seconds                                                     | —                | Wait for Param1 seconds                         |
| `OPEN_SHUTTER`    | `In`, `Se`, or `Both`                                       | —                | Open shutter(s)                                 |
| `CLOSE_SHUTTER`   | `In`, `Se`, or `Both`                                       | —                | Close shutter(s)                                |
| `HEATER_ON`       | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | —                | Device → OPERATE state                          |
| `HEATER_OFF`      | `Selenium (CH1)`, `Substrate (CH2)`, or `Indium (EPC)`      | —                | Device → STANDBY state                          |
| `IG_ON`           | —                                                           | —                | Turn ion gauge ON                               |
| `IG_OFF`          | —                                                           | —                | Turn ion gauge OFF                              |
| `MEASURE_BEP`     | `In`, `Se`, or `Both`                                       | —                | Run a BEP measurement inline and **block until it finishes** (~10 s per source, ~50 s for `Both` incl. settle). Each result is saved to its own sheet in the run log |
| `GOTO`            | step# to jump to                                            | loop count       | Loop: executes loop count+1 times total         |
| `END`             | —                                                           | —                | Terminate recipe cleanly                        |

### Flux Calibration Example (temperature vs BEP)

`INCREASE_TEMP` + `MEASURE_BEP` + `GOTO` produce a temperature-vs-flux sweep unattended:

```
Step 1  RAMP_TEMP      Selenium (CH1)  30    Se ramp rate 30°C/min
Step 2  SET_TEMP       Selenium (CH1)  200   Start of sweep
Step 3  WAIT_TEMP      Selenium (CH1)  3     Settle within ±3°C
Step 4  MEASURE_BEP    Se              —     Flux point at this temperature
Step 5  INCREASE_TEMP  Selenium (CH1)  20    Step +20°C
Step 6  GOTO           3               7     8 points total (200…340°C)
Step 7  HEATER_OFF     Selenium (CH1)  —     Cool down
```

Each `MEASURE_BEP` **step** writes one **`bep_measure_rec_#`** sheet (`_1`, `_2`, … in
execution order) into the run's `.xlsx`, holding that point's source, temperature,
baseline, peak, BEP, run-elapsed time and the full 20 Hz pressure trace plus a chart.
A `Both` sweep is a single measurement, so In and Se share one sheet — In in columns
A/B, Se in D/E, with a chart for each. The same records are
also appended to the cumulative `bep_log.csv`, which is the quickest place to pull a
temperature-vs-flux curve across runs.

> **Note:** `MEASURE_BEP` opens the shutter for its measurement, so don't place one
> inside a window where you need the shutter held in a particular state.

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

## Standalone Recipe Editor

`recipe_editor.py` is a **separate, lightweight app** for building and editing
recipe `.xlsx` files. It has the same recipe table, action dropdowns, drag-drop
reordering, and estimated-timeline preview plot as the main app's Recipe tab,
but has **no dependency on hardware, pyserial, or minimalmodbus** — it only
needs PyQt5, pyqtgraph, and openpyxl. This makes it safe to hand to lab
members so they can write and validate recipes ahead of time on their own
laptop, with no vacuum system attached, then bring the `.xlsx` file to the lab
PC and load it into the main app via **File > Load Recipe**.

Run it directly:
```bash
python recipe_editor.py
```

Or build it into its own standalone executable:
```bash
pip install pyinstaller PyQt5 pyqtgraph openpyxl
pyinstaller recipe_editor.spec
```
The output is `dist/RecipeEditor/RecipeEditor.exe` — zip up the `RecipeEditor`
folder and share it; recipients don't need Python installed at all.

Files it produces are byte-for-byte the same 5-column format described above,
so anything saved here loads straight into the main VacDepositionControl app
(and vice versa).

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

## LN2 Dewar Monitor

Tracks the liquid nitrogen level for tank **52228** on the Air Products **APDirect**
portal (`apdirect.airproducts.com`). Shown as a panel in the left status column
("LN2 Supply"): a vertical fill graphic for the current level next to a time-series
plot of recent history, a **Last read:** timestamp, and a manual **⟳** refresh button.

### Setup (one-time, optional)

```bash
python tools\setup_ln2_credentials.py
```

Run this **yourself, directly, in your own terminal** — never through an assistant or
script — because the password prompt uses hidden input (`getpass`), which is what keeps
the plaintext password from ever appearing anywhere outside your own terminal session.
It encrypts your APDirect username/password with **Windows DPAPI**, tied to your Windows
account on this PC, and writes `secrets/ln2_creds.dat` (gitignored — never committed).
Nothing in the app ever logs, prints, or displays the decrypted credentials. Re-run the
script any time to rotate/replace them.

### How it updates

- **Automatic:** checks twice daily (default 08:00 / 20:00 local time) while the app is
  running, plus one catch-up check on startup if the last known reading is already stale.
  This is in-app only (no Windows Scheduled Task) — if the app is closed at a scheduled
  time, that check is simply skipped until the next one.
- **Manual:** click **⟳** on the panel at any time.

### Panel states

| State | Trigger | Visual |
|-------|---------|--------|
| OK | Reading is recent and above the low threshold | Sky-blue border/fill |
| Stale | No successful reading in the last 18 hours | Yellow border — "reading not recent" |
| Low | Last known level ≤ 20 in | Red border/fill (takes priority over stale) |

The fill bar's 100% reference is the tank's real full-scale value, read live from the
APDirect API per fetch (confirmed 46 in for tank 52228) — not a hardcoded guess.

### Data source

`devices/ln2_monitor.py` logs into `account.airproducts.com`, then reads
`Tanks/API/v1/ShipToList` (current reading + full-scale value) and
`Tanks/API/v1/GetEquipmentReadings` (history backfill, deduplicated on merge) from
`apdirect.airproducts.com`. History is cached to `logs/ln2_history.csv` so the trend
plot survives an app restart. Any failure (bad credentials, network issue, site layout
change) fails safely — the panel shows its stale/yellow state rather than crashing or
guessing a value.

> **HAR captures:** if you ever need to re-capture a browser DevTools HAR to debug this
> (e.g. after a site change), delete the `.har` file as soon as you're done with it — it
> contains your real password in plaintext (HAR records raw POST bodies) and is
> gitignored (`*.har`) but not encrypted at rest.

---

## Building an EXE

```bash
pip install pyinstaller
pyinstaller build.spec
```

The output is in `dist/VacControl/VacControl.exe`.

For the hardware-free recipe builder, see [Standalone Recipe Editor](#standalone-recipe-editor) above (`recipe_editor.spec`).

---

## Project Structure

```
vac_control/
├── main.py                    # Entry point
├── gui_app.py                 # Main PyQt5 window (timers, poller, plots)
├── recipe_engine.py           # Recipe state machine
├── recipe_editor.py           # Standalone recipe builder (no hardware deps)
├── recipe_editor.spec         # PyInstaller config for RecipeEditor.exe
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
│   ├── ln2_monitor.py         # LN2 dewar level tracker (Air Products APDirect)
│   ├── sim_devices.py         # Sim devices for test mode (incl. EPC3004Sim)
│   └── __init__.py
├── tools/
│   └── setup_ln2_credentials.py  # One-time LN2 credential setup (run manually, not via app)
├── secrets/                   # Gitignored; DPAPI-encrypted credentials only, never plaintext
│   └── ln2_creds.dat          # Created by tools/setup_ln2_credentials.py
├── diagnostics/
│   ├── timing_diag.py               # Poll-cycle timing benchmark ← run to verify hw perf
│   ├── plot_diag.py                 # GUI plot pipeline diagnostic ← run to verify plot perf
│   ├── heat3_diag.py                # CH2 setpoint investigation
│   ├── heat3_remote_test.py         # PREVAC V2 remote-enable diagnostic
│   ├── heat3_remote_watchdog.py     # Remote watchdog simulator
│   └── configure_epc3004_cstm1.py  # EPC3004 Type C TC setup (J-proxy, one-time)
├── logs/                      # Auto-created; all CSV + Excel output goes here (incl. ln2_history.csv)
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
├── .gitignore                 # secrets/, logs/, *.har, build artifacts
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
