# BCG_HR — New-Machine Setup

Steps to clone yesterday's HR/SCG code onto a fresh machine and run it. Repo lives at `~/Desktop/HR`; adjust paths if you put it elsewhere.

## 1. Copy the code

`.venv/`, `__pycache__/`, and `.DS_Store` are machine-specific — exclude them. Pick one:

```sh
# Option A — over the network (run from OLD machine; NEW machine = hostname `newbox`)
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.DS_Store' \
  ~/Desktop/HR/ newbox:~/Desktop/HR/

# Option B — via USB drive
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.DS_Store' \
  ~/Desktop/HR/ /Volumes/USB/HR/

# Option C — tar + scp
tar --exclude='.venv' --exclude='__pycache__' --exclude='.DS_Store' \
  -czf ~/HR.tgz -C ~/Desktop HR
scp ~/HR.tgz newbox:~/
# On new machine:
cd ~/Desktop && tar -xzf ~/HR.tgz
```

## 2. PC-side setup (Python)

```sh
# prereqs (macOS) — skip if already installed
brew install python@3.12

# create venv and install deps
cd ~/Desktop/HR/pc
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` pulls:

- `pyserial>=3.5`
- `numpy>=1.26`
- `scipy>=1.11`
- `pyarrow>=15`
- `pyqtgraph>=0.13`
- `PyQt6>=6.6`

## 3. Firmware setup (Arduino + M5StickC PLUS2)

Sketch lives at `firmware/hr_streamer/hr_streamer.ino`.

1. Install Arduino IDE 2.x.
2. **Preferences → Additional Board Manager URLs**, add:
   ```
   https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json
   ```
3. **Boards Manager** → install **M5Stack** (≥ 2.1.0).
4. **Library Manager** → install **M5StickCPlus2** and **M5Unified**.
5. Open the sketch.
6. **Tools → Board** → `M5StickCPlus2`.
7. **Tools → Port** → the `cu.usbmodem*` that appears when the device is plugged in.
8. **Upload**.

The screen should show `seq <n>` ticking and ~200 samples/s.

## 4. Run

```sh
cd ~/Desktop/HR/pc
.venv/bin/python app.py --subject self --mount sternum-finger-press --note "trial 1"
```

The app opens in **live preview** — nothing is saved until you press **Space** (or click Record). Each start/stop pair writes a session under `~/Desktop/HR/data/session_<UTC>/`:

- `session.json` — subject, mount, note, sample rate, full-scale settings
- `imu_NNNN.parquet` — IMU rolled every 5 minutes (zstd, all 6 channels + seq + t_us)

## 5. Sanity check (no device required)

```sh
cd ~/Desktop/HR/pc
.venv/bin/python -c "import numpy, scipy, serial, pyarrow, pyqtgraph, PyQt6; print('ok')"
```

If that prints `ok`, the Python side is good. With the M5Stick plugged in, confirm the serial port:

```sh
ls /dev/cu.usbmodem*
```
