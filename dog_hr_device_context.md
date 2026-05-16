# Canine Heart Rate Detection via Ballistocardiography — Project Context

> Self-contained context document for handing to any LLM. Covers project goals, hardware, design decisions, signal-processing pipeline, data format, firmware approach, and first-session plan. Stage: concept/planning. Date: 2026-05-12.

---

## 1. Project Overview

**Goal:** Build a non-invasive heart-rate detector for dogs using ballistocardiography (BCG) — detecting micro-vibrations produced by the mechanical action of the heart, captured by an accelerometer mounted on the dog.

**Why BCG (vs. alternatives):**
- vs. PPG (optical): fur attenuates the signal severely
- vs. ECG: requires shaved skin contact and conductive gel for reliable electrodes

**Hardware stack:**
- MCU + sensor: M5StickC PLUS2 (ESP32-PICO-V3-02 with built-in MPU6886 6-axis IMU)
- IMU sample rate: 200 Hz target (MPU6886 capable up to 1 kHz)
- Battery: 200 mAh — short for continuous BLE; fine for tethered USB sessions
- Toolchain: Arduino IDE or PlatformIO + M5Unified library

**Scope decision (important):** HR is computed **only during resting periods**. Active motion windows are detected and discarded. This dramatically simplifies the gating problem — a binary rest-vs-not-rest detector is sufficient. No need to track HR through movement.

**Relevant ranges:**
- Resting canine HR: 40–120 bpm (≈ 0.7–2.0 Hz)
- Active HR: up to ~220 bpm (out of scope for v1)
- BCG signal energy: dominantly 1–10 Hz, typically bandpassed 1–20 Hz

---

## 2. Mounting Decision

Mounting location dictates SNR, motion artifact characteristics, and reproducibility.

| Location | Pros | Cons |
|---|---|---|
| **Chest harness** (lateral thorax, behind elbow, over heart) | Strongest mechanical coupling; best SNR; reproducible; harness familiar to dogs | Requires harness; orientation depends on fit |
| **Collar** (neck) | Universal — every dog wears one | Much smaller BCG amplitude; heavy motion artifact from head movement (sniffing, drinking, panting); collar tightness varies hugely |

**Decision:** Prototype on a **chest harness mount**. Collar adaptation = v2 research problem, not a v1 requirement.

---

## 3. Signal Chain

```
MPU6886 @ 200 Hz → DC removal → bandpass 1–20 Hz
   → axis selection / fusion → motion-artifact gating (rest detection)
   → FFT or autocorrelation on resting windows → HR estimate
```

**Sample rate:** 200 Hz is sufficient — BCG energy is sub-20 Hz; 200 Hz gives comfortable margin above Nyquist. Higher rates just cost power and storage.

**Axis choice:**
- Dorsoventral axis (perpendicular to spine) typically carries strongest BCG
- For a moving/oriented-arbitrary mount, consider:
  - Accel magnitude (orientation-independent)
  - PCA on a sliding window, take PC1 (most BCG papers use this — adapts to mounting)

**Motion gating (central problem because project is rest-only):**
- Compute accel-magnitude std and gyro-magnitude std over a sliding window
- Resting window = both stds below tuned thresholds
- Emit HR estimates only for quiescent windows

**HR estimation:** Autocorrelation or FFT/Welch PSD on 5–30 s windows is more robust than time-domain peak picking — individual J-peaks are noisy, but periodicity is stable.

---

## 4. Architecture: On-Device DSP vs. Stream-and-Process

| | On-device DSP | Stream raw → PC |
|---|---|---|
| Battery | Worse (CPU active) | Worse (BLE/USB active) |
| Iteration speed | Slow — every change requires re-flashing | Fast — iterate in Python |
| Final form factor | Self-contained | Requires companion (laptop/phone) |

**Decision:** For prototype, stream raw IMU over USB serial to laptop, do all DSP in Python. Iterate 10× faster while algorithm is unsettled. Port to on-device only after pipeline is validated. Migrate transport to BLE only when free-moving recording is needed.

---

## 5. Ground Truth & Validation

You cannot validate canine HR estimates without a reference signal.

| Reference | Quality | Practicality |
|---|---|---|
| **Polar H10 + canine chest strap** | Gold standard, ~1 ms accuracy | Best — works on dogs, off-the-shelf |
| Vet ECG | Excellent | Limited access, brief sessions only |
| Stethoscope + manual count | OK for spot checks | Useless for continuous validation |

**Plan from day one** to budget for Polar H10 + dog-fitted strap.

**Validation metrics:**
- Per-window IMU HR vs. Polar HR averaged over same window
- Scatter plot + Bland–Altman (mean vs. difference)
- Target: MAE 3–8 bpm on resting dog

**Sync mechanism:** Sharp tap on the device at recording start — visible spike in IMU, easy to mark in Polar Flow timeline.

---

## 6. Known Pitfalls

- **Panting:** 1–3 Hz, overlaps HR band, much higher amplitude. Need to detect-and-exclude or do source separation.
- **Fur and harness slack:** introduces low-frequency rumble and intermittent decoupling. Strap tightness matters.
- **Battery life:** 200 mAh is small. Continuous IMU + BLE drains in a few hours. Plan duty cycling or external battery.
- **Breed variance:** Chihuahua chest mechanics ≠ Great Dane. Algorithm parameters likely scale with body mass / chest dimensions.
- **Timing jitter:** sampling with `delay()` in `loop()` jitters under BLE/USB load and corrupts the spectrum. Use a hardware timer interrupt.

---

## 7. End-to-End Offline Analysis Flow

```
M5StickC PLUS2 ──USB serial──► PC ──► CSV file ──► Python notebook ──► HR estimate
   (sample IMU)   (transport)   (logger)        (analysis)
```

Five stages: **acquire → transport → log → process → validate**.

### Stage 1 — Acquire (firmware on M5StickC)

- Configure MPU6886: ±2 g full scale (BCG is sub-1 g), DLPF on, 200 Hz
- Read accel + gyro on a hardware timer interrupt (not `delay()`)
- Push samples through a small ring buffer
- Transmit timestamped samples

### Stage 2 — Transport

| Option | Latency | Setup | Best for |
|---|---|---|---|
| **USB serial** | Low | Trivial — `Serial.printf` | Bench testing, tethered |
| BLE notify | Low–med | GATT + Python `bleak` | Wearable, moving dog |
| WiFi UDP | Low | Easy — one socket | Dedicated router, longer range |
| SD card on HAT | Offline | Hardware add-on | Long unattended recordings |

**Start with USB serial.** Zero infrastructure. Switch to BLE once DSP is validated.

### Stage 3 — Log to disk on PC

What the logger does:
1. Open serial port (or BLE characteristic)
2. Parse each incoming line/packet
3. Validate sample counter — warn on drops
4. Write to CSV (e.g. `bcg_2026-05-12_dogname_resting.csv`)
5. Optionally: live plot to confirm coupling/orientation before walking away

**Sidecar JSON metadata** (alongside each CSV):
- Dog name, breed, weight, age
- Mounting location and orientation
- Polar H10 reference filename
- Wall-clock start/stop timestamps
- IMU full-scale settings, DLPF cutoff, sample rate
- Free-text notes ("dog lying on left side, calm")

### Stage 4 — Offline processing (Python / Jupyter)

**4a. Load & sanity-check**
- Read CSV with pandas
- Plot raw `ax/ay/az`; eyeball for breathing (slow) and cardiac (faster, smaller)
- `np.diff(timestamp_us)` — confirm sample interval is constant; gaps indicate drops

**4b. Convert & detrend**
- Convert raw int16 to g (e.g. divide by 16384 for ±2 g)
- Subtract per-axis mean to remove gravity DC offset

**4c. Find resting windows**
- `accel_mag = sqrt(ax² + ay² + az²)`, similar for gyro
- Slide a 10 s / 50% overlap window
- Resting window = both accel-std and gyro-std below tuned thresholds
- Output: list of `(start, end)` resting segments; discard the rest

**4d. Bandpass**
- Butterworth bandpass 1–20 Hz (try 1–10 Hz also)
- `scipy.signal.butter` 4th order + `filtfilt` for zero-phase filtering

**4e. Axis selection / fusion**
- Use dorsoventral axis directly, OR
- Use filtered accel magnitude (orientation-robust), OR
- PCA on the three filtered axes; take PC1 (most BCG papers)

**4f. HR estimation (cross-check both methods)**
- **Frequency domain:** Welch PSD on 10–30 s window, find peak in 0.7–2.5 Hz band; HR = peak × 60
- **Autocorrelation:** first peak after lag 0 in expected lag range; HR = (Fs / lag) × 60
- Agreement between methods → trust estimate; disagreement → window probably contaminated

**4g. Aggregate**
- One HR estimate per resting window
- Plot HR vs. time
- Time-align with Polar H10 reference and compare

### Stage 5 — Validate

- Time-align IMU and Polar streams using sharp tap at recording start
- Compare per-window IMU HR vs. Polar HR averaged over same window
- Scatter + Bland–Altman; expect MAE 3–8 bpm

---

## 8. Streaming / CSV Format

Four sub-decisions wrapped in "what format":
1. What values to send (raw int16 vs. converted floats)
2. How to encode them on the wire (text/CSV vs. binary)
3. What metadata to attach per sample
4. What goes in the file vs. on the wire

### 8.1 Raw counts vs. physical units

| | Raw int16 | Converted float (g, dps) |
|---|---|---|
| Bytes/sample | 12 (6×int16) | 24 (6×float32) |
| Precision | Exact sensor output | Lossy if float32; locked at capture |
| Reprocessability | Can rescale later if FS range changes | Locked in at capture |
| Firmware CPU | Trivial | Trivial |

**Decision:** Log raw int16 + record full-scale setting in metadata. Half the bandwidth, fully reprocessable. Convert in Python: `accel_g = raw * (2.0 / 32768.0)`.

### 8.2 Text CSV vs. binary

- **Text CSV:** human-readable, debuggable with `cat`/`tail -f`, ~20–40 bytes/sample (~4–8 KB/s at 200 Hz), easy to recover from logger crash.
- **Binary:** ~14 bytes/sample, faster to parse, no locale issues — but opaque, frame-sync becomes a problem if a byte drops.

**Decision:** CSV text on the wire and in the file. 200 Hz is nowhere near bandwidth limit. Switch to binary only if a real constraint shows up (≥1 kHz, BLE saturation, long unattended SD recording).

### 8.3 Per-sample metadata — include all of these

- `n` — sample counter (u32, +1 each sample). Detects dropped samples exactly.
- `t_us` — `micros()` timestamp. Detects timer jitter independent of counter.
- `ax, ay, az` — accel raw int16
- `gx, gy, gz` — gyro raw int16 (used for rest detection — free to include)

**Skip per-sample:** battery voltage / RSSI (log once per second elsewhere), wall-clock time (set in metadata), temperature unless drift correction is needed.

### 8.4 Wire vs. file format

- **Wire:** minimal CSV, no header, just numbers. Firmware stays dumb.
- **File:** same data + header row, written by PC logger, plus sidecar JSON metadata.

### Concrete recommendation

**On the wire (one line per sample, USB serial):**
```
n,t_us,ax,ay,az,gx,gy,gz
```

**In the CSV file:** header row identical to above + same data.

**Sidecar JSON** (`recording.meta.json`): wall-clock start, accel/gyro FS, DLPF, sample rate, dog metadata, mounting notes, Polar reference filename, total samples received vs. expected.

**Filename:** `bcg_<date>_<dog>_<condition>.csv`

### Rules for "which columns do I need?"

1. **Include anything hard to recover later.** Sample counter and µs timestamp — always.
2. **Include anything that costs nothing.** Gyro is one read away; ~3 bytes/sample. Free.
3. **Skip anything that can live in metadata.** Battery, dog name, sample rate setting → JSON sidecar, not per-row.

For this project: 8 columns above. Nothing else needed for v1.

---

## 9. Firmware: Streaming Real Data over USB

### Mental model

The M5StickC is a tiny computer. It does whatever you program it to do — out of the box, it does **not** automatically stream data. You must write firmware, flash it, and only then will plugging in USB produce real data.

The "columns" in your CSV are **whatever your firmware prints**. The firmware is the configuration — there is no menu.

### Process

**Step 1 — Set up dev environment**
- Install Arduino IDE (easiest) or PlatformIO (better long-term)
- Install M5StickCPlus2 board support + M5Unified library

**Step 2 — Write firmware that does three things**

```
setup() {
  start the IMU
  start the serial connection
  print header: "n,t_us,ax,ay,az,gx,gy,gz"
}

loop() {
  every 5 ms (200 Hz):
    read accel (ax, ay, az)
    read gyro  (gx, gy, gz)
    increment counter n
    read micros() into t_us
    print: "n,t_us,ax,ay,az,gx,gy,gz" + newline
}
```

~30 lines of C++. The line your firmware prints **is** what shows up on USB and becomes the columns of your CSV.

**Step 3 — Flash**
- Plug M5StickC into laptop via USB-C
- Arduino IDE: pick board `M5StickCPlus2`, pick port (e.g. `/dev/cu.usbserial-XXX`), click Upload
- ~30 seconds; device reboots and starts running the code

**Step 4 — Confirm streaming**

Open the Arduino Serial Monitor (baud must match firmware, e.g. 921600). You should see:

```
n,t_us,ax,ay,az,gx,gy,gz
1,1234,123,-45,16234,12,3,-8
2,6234,119,-41,16240,11,4,-7
3,11234,124,-42,16238,13,3,-9
...
```

- See lines streaming → device is working
- Garbage → wrong baud
- Nothing → wrong port or firmware did not flash

**Step 5 — Save the stream to a CSV**

Option A — terminal one-liner (Mac/Linux):
```
cat /dev/cu.usbserial-XXX > recording.csv
```
Ctrl+C to stop.

Option B — Python script (more flexible):
```python
import serial
with serial.Serial('/dev/cu.usbserial-XXX', 921600) as s, \
     open('recording.csv', 'w') as f:
    while True:
        f.write(s.readline().decode())
```

Extend later for sync markers, live plot, sidecar JSON.

### Selecting columns

Change **one line** in firmware — the print statement:

```cpp
Serial.printf("%lu,%lu,%d,%d,%d,%d,%d,%d\n",
              n, t_us, ax, ay, az, gx, gy, gz);
```

- Drop gyro? Remove `gx, gy, gz` and shrink format string → 5 columns
- Add temperature? Read IMU temp register, append to printf, add `%d` → 9 columns
- Add battery? Read M5's API, append

The header line printed in `setup()` must match. **The firmware is the configuration.**

### The full picture

```
┌──────────────────────────────┐
│  M5StickC PLUS2              │
│  ┌────────────────────────┐  │
│  │ Your firmware:         │  │
│  │  read IMU              │  │      USB cable
│  │  Serial.printf(...)    │  │ ──────────────►  PC
│  └────────────────────────┘  │      (text, 200/sec)
└──────────────────────────────┘
                                          │
                                          ▼
                                  cat > recording.csv
                                          │
                                          ▼
                                   Jupyter notebook
                                  (load, filter, FFT, HR)
```

Three things you control:
- **Firmware:** what gets sent (columns) and how often (sample rate)
- **PC logger:** where it gets saved (filename, location)
- **Notebook:** what to do with it (filter, find rest, estimate HR)

---

## 10. First-Session Plan & Milestones

1. Order Polar H10 + canine chest strap for ground truth
2. Build a chest-harness mount (3D-printed clip → harness webbing) for the M5StickC
3. Firmware: stream MPU6886 @ 200 Hz over USB serial, no on-device DSP, 8-column CSV format
4. Capture 30 minutes of synchronized IMU + Polar HR on a calm dog (sleeping/resting is ideal first dataset)
5. Offline analysis in Jupyter: load → bandpass → find resting windows → FFT/autocorrelation → HR per window → compare to Polar
6. Iterate on bandpass cutoffs, rest-detection thresholds, axis fusion strategy
7. Once tethered version validated: port transport to BLE for free-moving recording
8. Once algorithm stable: port DSP to on-device for self-contained operation

**Deferred to v2:** collar mounting, motion-tolerant HR tracking through activity, breed-specific parameter calibration, on-device alerts.

---

## 11. Quick Reference / Cheat Sheet

| Item | Value |
|---|---|
| MCU | ESP32-PICO-V3-02 (M5StickC PLUS2) |
| IMU | MPU6886 (6-axis: 3 accel + 3 gyro) |
| Sample rate | 200 Hz |
| Accel FS | ±2 g |
| Bandpass | 1–20 Hz (try 1–10 Hz) |
| Window length (rest detection) | 10 s, 50% overlap |
| Window length (HR estimate) | 10–30 s |
| HR search band | 0.7–2.5 Hz (42–150 bpm) |
| Wire format | CSV text, raw int16, 8 cols: `n,t_us,ax,ay,az,gx,gy,gz` |
| File format | Same CSV + sidecar `.meta.json` |
| Transport | USB serial (v1) → BLE (v2) |
| Reference | Polar H10 + canine chest strap |
| Target accuracy | MAE 3–8 bpm on resting dog |
| Filter | Butterworth 4th order, `filtfilt` (zero-phase) |
| Axis fusion | PCA → PC1 (or accel magnitude) |
| HR algorithm | Welch PSD peak + autocorrelation, cross-check |

---

## 12. Open Questions / Decisions Pending First Capture

- Exact rest-detection thresholds (accel-std, gyro-std) — tune from first recording
- Optimal bandpass: 1–20 Hz vs. 1–10 Hz
- Whether breed-mass scaling of parameters is needed (likely yes; collect data across sizes)
- Whether panting requires an explicit suppression stage or rest-gating already excludes panting episodes
- Whether 200 Hz is enough or 500 Hz adds value (almost certainly 200 is fine)
- Mounting clip / harness attachment mechanism — TBD physical design

---

*End of context document. Hand this to any LLM as the project's complete state at concept/planning stage, 2026-05-12.*
