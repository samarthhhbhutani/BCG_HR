# HR — Multi-sensor Recording & Analysis

Three independent sensor pipelines, all targeting the M5StickC PLUS2:

| Pipeline | Sensor | What it records | Folder |
|---|---|---|---|
| **PPG** | MAX30102 (IR + Red, 200 Hz) | photoplethysmography → heart rate, with 4-detector benchmark | `Heart_Rate_PPG/` |
| **Temperature** | MLX90614 (1 Hz) | non-contact object temperature | `Temperature/` |
| **BCG/IMU** *(legacy)* | M5's onboard IMU (200 Hz) | ballistocardiography from chest motion | `firmware/`, `pc/` (top-level) |

Each pipeline has its own firmware sketch, its own Python venv, and its own
`data/session_<UTC>/` output folder. Pick whichever you need; you don't have
to set up the others.

> **Conventions in this README**
> - All commands work on **macOS / Linux / Windows** unchanged unless marked.
> - Where the venv's Python differs by OS:
>   - **macOS / Linux**: `.venv/bin/python`
>   - **Windows (PowerShell or cmd)**: `.venv\Scripts\python.exe`
> - In the snippets below, `python` means "Python 3.10+ from your PATH" before
>   the venv exists, and "venv Python" after activation. The activation step is
>   shown for each pipeline.

---

## 0. One-time prerequisites (any OS)

### Python 3.10+
- **macOS**: `brew install python@3.12` (or download from python.org)
- **Windows**: install from [python.org](https://www.python.org/downloads/) — tick "Add Python to PATH"
- **Linux**: `sudo apt install python3.12 python3.12-venv` (or your distro equivalent)

Verify:
```sh
python --version            # should print 3.10 or higher
```
On macOS/Linux that command may be `python3` instead of `python` — use whichever
your system has. Below I just write `python`.

### Arduino IDE 2.x
Download from [arduino.cc/en/software](https://www.arduino.cc/en/software). Same
on every OS.

After install, in **Preferences → Additional Board Manager URLs** add:
```
https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json
```
Then **Boards Manager → install M5Stack (≥ 2.1.0)**.
Then **Library Manager → install** these libraries (depending on which sketch you flash):
- For PPG: `M5StickCPlus2`, `SparkFun MAX3010x Pulse and Proximity Sensor Library`
- For Temperature: `M5StickCPlus2`, `Adafruit MLX90614 Library`
- For BCG/IMU: `M5StickCPlus2`, `M5Unified`

### Identifying the M5's serial port

After plugging the M5StickC in:
- **macOS**: `ls /dev/cu.usbserial-* /dev/cu.usbmodem*`
- **Linux**: `ls /dev/ttyUSB* /dev/ttyACM*`
- **Windows**: open Device Manager → "Ports (COM & LPT)" → look for `COMn`. Use that as `--port COM5` (or whichever number).

The Python apps auto-detect on macOS / Linux. On Windows you usually need
`--port COMn`.

---

## 1. PPG pipeline (`Heart_Rate_PPG/`)

The flagship pipeline. Live HR via TERMA, plus an offline benchmark that runs
four peak-detection algorithms (TERMA, Elgendi, Bishop/MSPTD, Charlton/MSPTDfast)
on the same recording.

### 1.1 Wiring (M5StickC PLUS2 → MAX30102)
| MAX30102 | M5 |
|---|---|
| SDA | G0 |
| SCL | G26 |
| VIN | 5V |
| GND | GND |

### 1.2 Flash the firmware
Open `Heart_Rate_PPG/firmware/ppg_streamer/ppg_streamer.ino` in Arduino IDE,
**Board: M5StickC-PLUS2**, **Port:** the one you identified above, **Upload**,
then **unplug & replug USB**.

The screen should show `seq <n>` incrementing at ~200/s.

### 1.3 Set up the Python environment (one-time)

```sh
cd Heart_Rate_PPG/pc
python -m venv .venv
```

Activate the venv:
- **macOS / Linux**: `source .venv/bin/activate`
- **Windows PowerShell**: `.\.venv\Scripts\Activate.ps1`
- **Windows cmd**: `.venv\Scripts\activate.bat`

Then install:
```sh
pip install --upgrade pip
pip install -r requirements.txt
```

### 1.4 Run the live UI

From `Heart_Rate_PPG/pc/` with the venv active:
```sh
python app.py --species human
python app.py --species human --port COM5            # Windows / explicit port
python app.py --species human --multi-detector       # 4-color peak overlay pane
python app.py --species human --detector charlton    # only run one detector live
```

Press **Space** (or click the green Record button) to start a session, again to
stop. Each session writes:
```
Heart_Rate_PPG/data/session_<UTC>/
├─ ppg_NNNN.parquet      raw IR/Red @ 200 Hz, rolled every 5 min
├─ hr_30s.parquet        live HR averages (one row / 30 s)
└─ session.json          subject + species + start time + config
```

### 1.5 Run the offline benchmark

After recording at least one session:
```sh
cd Heart_Rate_PPG               # repo root, NOT pc/
python benchmark.py --latest                              # all 4 detectors, latest session
python benchmark.py --session data/session_<UTC>          # specific session
python benchmark.py --all                                 # every session
python benchmark.py --latest --detector charlton          # one detector
python benchmark.py --latest --detector elgendi,charlton  # subset
```

Outputs land in `data/session_<UTC>/analysis/`:
- `report.md` — human-readable: per-pipeline summary + pairwise F1 + 30 s chunk HR table
- `benchmark.json` — same data, scriptable
- `chunks_hr.parquet` — wide HR table for pandas
- `<detector>/peaks.parquet`, `ibis.parquet`, `chunks.parquet`, `meta.json`

To open the report:
- **macOS**: `open data/session_<UTC>/analysis/report.md`
- **Windows**: `start data\session_<UTC>\analysis\report.md`
- **Linux**: `xdg-open data/session_<UTC>/analysis/report.md`
- *Any OS*: open it in your editor (VS Code, etc).

Detailed docs: `Heart_Rate_PPG/BENCHMARK.md`.

---

## 2. Temperature pipeline (`Temperature/`)

Standalone non-contact temperature recording at 1 Hz. No HR logic, no benchmark
— just timestamped temperatures.

### 2.1 Wiring (M5StickC PLUS2 → MLX90614)
| MLX90614 | M5 |
|---|---|
| SDA | G0 |
| SCL | G26 |
| VIN | **3V3** *(NOT 5V — the bare GY-906 chip is 3.3 V only)* |
| GND | GND |

> **Heads-up**: bare GY-906 breakouts often lack on-board pull-up resistors. If
> the firmware shows `MLX90614 NOT FOUND`, run the I²C scanner sketch at
> `Temperature/firmware/i2c_scanner/i2c_scanner.ino`. If the scanner reports
> `NO DEVICES`, you need 4.7 kΩ pull-ups from SDA/SCL to 3V3 — or wire the
> MAX30102 in parallel (it has built-in pull-ups; both sensors coexist on the
> same bus at different addresses).

### 2.2 Flash the firmware
Open `Temperature/firmware/temp_streamer/temp_streamer.ino`, **Board:
M5StickC-PLUS2**, Upload, unplug-replug.

The screen should show `seq <n>` and `T <°C>` updating once per second.

### 2.3 Set up the Python environment (one-time)

```sh
cd Temperature/pc
python -m venv .venv
```
Activate (see PPG section 1.3 for OS-specific activation).
```sh
pip install --upgrade pip
pip install -r requirements.txt
```

### 2.4 Run the live UI

```sh
python app.py
python app.py --port COM5                # Windows / explicit port
python app.py --subject self --note "post-run"
```

Press Space to start/stop recording. Output:
```
Temperature/data/session_<UTC>/
├─ temp_1hz.parquet      seq, t_us, t_obj_c (one row / second)
└─ session.json
```

### 2.5 Headless mode (no UI, just record)

```sh
python receiver.py --subject self
```
Auto-records the whole session; **Ctrl+C** stops it.

---

## 3. BCG/IMU pipeline (top-level `firmware/`, `pc/`)

The original ballistocardiography pipeline that uses the M5's onboard IMU to
detect heartbeats from chest motion. No external sensor needed.

### 3.1 Flash the firmware
Open `firmware/hr_streamer/hr_streamer.ino`, **Board: M5StickCPlus2**, Upload,
unplug-replug.

### 3.2 Python environment

```sh
cd pc
python -m venv .venv
```
Activate (see section 1.3).
```sh
pip install --upgrade pip
pip install -r requirements.txt
```

### 3.3 Run

```sh
python app.py --subject self --mount sternum-finger-press --note "trial 1"
python app.py --port COM5                # Windows / explicit port
```

Press Space to start/stop recording. Output:
```
data/session_<UTC>/
├─ imu_NNNN.parquet      6-axis IMU rolled every 5 min
└─ session.json
```

Detailed setup notes: `SETUP.md`.

---

## 4. Sanity check (no device required)

For any of the three pipelines, with that pipeline's venv active:

```sh
python -c "import numpy, scipy, serial, pyarrow, pyqtgraph, PyQt6; print('ok')"
```

If it prints `ok`, your Python environment is set up correctly.

For the PPG pipeline specifically, also confirm benchmark deps:
```sh
python -c "import pandas, neurokit2; print('benchmark ok')"
```

---

## 5. Common issues

### `python: command not found`
- Try `python3` instead.
- Verify it's on PATH: `python --version` or `python3 --version`.
- On Windows, re-run the Python installer and tick **"Add Python to PATH"**.

### `ModuleNotFoundError: No module named 'pyqtgraph'`
You're running the system Python, not the venv. Either:
- Activate the venv first (see section 1.3), or
- Call the venv's Python directly:
  - macOS/Linux: `pc/.venv/bin/python pc/app.py …`
  - Windows: `pc\.venv\Scripts\python.exe pc\app.py …`

### `No serial port found`
- Make sure the M5 is plugged in and the firmware is flashed.
- On Windows, pass `--port COMn` explicitly.
- macOS / Linux: check `ls /dev/cu.*` (macOS) or `ls /dev/tty*` (Linux); the M5
  appears as `usbserial-*` (macOS) or `ttyUSB*` / `ttyACM*` (Linux).
- If it still fails: unplug, wait 3 s, replug, and close any Arduino Serial
  Monitor that might be holding the port.

### `rx=0 drop=0` in the live UI
The M5 is enumerated but no bytes are reaching Python. Almost always solved by
**unplug, wait 3 s, replug**. The ESP32 USB CDC stack occasionally gets stuck
half-enumerated.

### Firmware screen says "MAX30102 NOT FOUND" or "MLX90614 NOT FOUND"
The I²C bus can't see the sensor. Check wiring (G0=SDA, G26=SCL, correct power
voltage). For MLX90614 specifically, see the heads-up in section 2.1.

---

## 6. Repo layout

```
HR/
├─ README.md                    this file
├─ SETUP.md                     setup notes for the BCG/IMU pipeline
├─ PPG_Algos.md                 PPG algorithm research notes
├─ Heart_Rate_PPG/              MAX30102 PPG pipeline (live + benchmark)
│  ├─ firmware/ppg_streamer/
│  ├─ pc/                       live UI, receiver, hr worker
│  ├─ pipelines/                4 peak-detection pipelines
│  ├─ benchmark.py              offline benchmark CLI
│  ├─ data/                     session recordings + analysis/
│  └─ BENCHMARK.md              detailed benchmark docs
├─ Temperature/                 MLX90614 temperature pipeline
│  ├─ firmware/temp_streamer/
│  ├─ firmware/i2c_scanner/     diagnostic sketch
│  ├─ pc/                       live UI, receiver
│  └─ data/                     session recordings
├─ firmware/hr_streamer/        BCG/IMU firmware (legacy)
├─ pc/                          BCG/IMU live UI (legacy)
└─ data/                        BCG/IMU session recordings (legacy)
```

Each pipeline is fully independent — its own venv, its own data folder, its own
firmware sketch. You can run them in isolation or in parallel (different sensors
at the same time, with separate USB-C cables).

---

## 7. Cloning to a new machine

```sh
# From the OLD machine — exclude venvs and caches
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.DS_Store' \
  ~/Desktop/HR/ newbox:~/Desktop/HR/
```

Or USB / tar — see prior `README.md` versions in git history.

On the NEW machine, follow section 0, then for each pipeline you want to use,
follow section 1.3 / 2.3 / 3.2 to recreate its venv. Recordings are
machine-independent and will replay through the benchmark on any OS.
