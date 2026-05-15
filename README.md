# BCG_HR — New Machine Setup

Steps to clone yesterday's setup onto a new machine.

---

## 1. Copy the Code

`.venv/`, `__pycache__/`, and `.DS_Store` are machine-specific — exclude them.

Choose one of the following methods:

```sh
# Option A — over the network
# Run from OLD machine; NEW machine hostname = `newbox`

rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.DS_Store' \
  ~/Desktop/HR/ newbox:~/Desktop/HR/
```

```sh
# Option B — via USB drive

rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.DS_Store' \
  ~/Desktop/HR/ /Volumes/USB/HR/
```

```sh
# Option C — tar + scp

tar --exclude='.venv' --exclude='__pycache__' --exclude='.DS_Store' \
  -czf ~/HR.tgz -C ~/Desktop HR

scp ~/HR.tgz newbox:~/

# On the new machine:
cd ~/Desktop && tar -xzf ~/HR.tgz
```

---

## 2. PC-side Setup (Python)

```sh
# Prerequisites (macOS) — skip if already installed
brew install python@3.12

# Create virtual environment and install dependencies
cd ~/Desktop/HR/pc

python3.12 -m venv .venv

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### `requirements.txt` includes

- `pyserial>=3.5`
- `numpy>=1.26`
- `scipy>=1.11`
- `pyarrow>=15`
- `pyqtgraph>=0.13`
- `PyQt6>=6.6`

---

## 3. Firmware Setup (Arduino + M5StickC PLUS2)

Sketch location:

```text
firmware/hr_streamer/hr_streamer.ino
```

### Steps

1. Install **Arduino IDE 2.x**
2. Go to:

   ```text
   Preferences → Additional Board Manager URLs
   ```

   Add:

   ```text
   https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json
   ```

3. Open **Boards Manager** and install:

   ```text
   M5Stack (>= 2.1.0)
   ```

4. Open **Library Manager** and install:

   - `M5StickCPlus2`
   - `M5Unified`

5. Open the sketch.
6. Set:

   ```text
   Tools → Board → M5StickCPlus2
   ```

7. Set:

   ```text
   Tools → Port → cu.usbmodem*
   ```

   (Choose the port that appears when the device is plugged in.)

8. Click **Upload**

### Expected Result

The screen should display:

```text
seq <n>
```

incrementing continuously at approximately:

```text
~200 samples/s
```

---

## 4. Run the App

```sh
cd ~/Desktop/HR/pc

.venv/bin/python app.py \
  --subject self \
  --mount sternum-finger-press \
  --note "trial 1"
```

### Recording Controls

- Press **Spacebar** to start logging
- Press **Spacebar again** to stop logging

You can also use the **Record** button in the UI.

### Output

Each start/stop recording session creates:

```text
~/Desktop/HR/data/session_<UTC>/
```

Contents:

- `session.json`
  - subject
  - mount
  - note
  - sample rate
  - full-scale settings

- `imu_NNNN.parquet`
  - IMU data rolled every 5 minutes
  - zstd compressed
  - includes:
    - all 6 IMU channels
    - sequence number
    - timestamps (`t_us`)

---

## 5. Sanity Check (No Device Required)

```sh
cd ~/Desktop/HR/pc

.venv/bin/python -c \
"import numpy, scipy, serial, pyarrow, pyqtgraph, PyQt6; print('ok')"
```

If this prints:

```text
ok
```

then the Python environment is configured correctly.

### Verify Device Connection

With the M5Stick plugged in:

```sh
ls /dev/cu.usbmodem*
```
