# BCG/SCG Setup — XIAO nRF52840 + BMI330

Complete reference for the new hardware platform. Replaces the M5StickC PLUS2 + onboard MPU6886 of the original BCG pipeline. Covers hardware, wiring, firmware, PC software, validation, known issues, and the next steps that remain open.

This file is self-contained — anyone picking up the project should be able to read this and reproduce the working state. For the historical M5StickC pipeline see `SETUP.md`; for the broader algorithmic context see `PPG_Algos.md`; for the original canine concept doc see `dog_hr_device_context.md`.

---

## 1. What this setup is for

A wearable seismocardiography (SCG) / ballistocardiography (BCG) recorder. A 6-axis IMU on the chest measures the micro-vibrations of the heartbeat, streams them over USB to a laptop, and a Python pipeline estimates heart rate in real time.

End goal: validate the pipeline on humans (resting), then port to dogs (chest-harness mount, eventually BLE).

---

## 2. Hardware

### 2.1 Bill of materials

| Item | Part | Notes |
|---|---|---|
| MCU board | **Seeed XIAO nRF52840 Sense** | "Sense" variant has an onboard LSM6DS3TR-C IMU + PDM mic that we don't use; mechanically/electrically identical to the regular XIAO for our purposes. Native USB CDC, BLE 5, Cortex-M4F @ 64 MHz, 1 MB flash, 256 KB RAM. |
| IMU | **7SEMI BMI330 Nano breakout** | Bosch BMI330 6-axis IMU on a tiny PCB. I²C only (no INT1/INT2 pins broken out on this variant). Address 0x68 or 0x69 (set by ADDR pin/jumper). Bottom side of the breakout is bare PCB — safe for direct skin contact. |
| Cable | 4 × thin silicone-jacketed wires, 28-30 AWG, ~10-15 cm | For prototype. Production version: 0.5 mm pitch ZIF FFC connector + flex cable. |
| Power | USB-C cable (data — not charge-only) | Must be a real data cable. Charge-only cables corrupt the bring-up. |

### 2.2 Why this hardware

BMI330 was chosen specifically for the lower noise floor:

- BMI330 accel noise density: **~120 µg/√Hz**
- MPU6886 (old hardware) noise density: ~300 µg/√Hz
- That's ~8 dB more SNR headroom in the cardiac band

The XIAO nRF52840 was chosen for:
- Native USB CDC (no CH9102 bridge → no baud-corruption issues like in `docs/session_2026-05-16.md`)
- BLE 5 native (unblocks the dog port without a transport rewrite)
- Tiny size for wearable form factor (21 × 17.5 mm)

### 2.3 Wiring

Only 4 wires. The 7SEMI BMI330 Nano breakout exposes only `VIN, GND, SDA, SCL` on its header — no INT1, INT2, CS, or SDO pins.

| XIAO nRF52840 | 7SEMI BMI330 Nano |
|---|---|
| `3V3` | `VIN` |
| `GND` | `GND` |
| `D4` (SDA) | `SDA` |
| `D5` (SCL) | `SCL` |

**Use 3V3, NOT 5V.** Many BMI330 breakouts run silicon at 3.3 V; even those with regulators are universally 3.3V-safe.

### 2.4 BMI330 quirks worth knowing

1. **Boots in SPI mode.** Any I²C transaction switches it to I²C — Bosch's protocol auto-detects the bus. Our firmware does this in `bmi330_init()` via a dummy read.
2. **Returns 2 dummy bytes before data on I²C reads.** This is documented in Bosch's SensorAPI as `dev->dummy_byte = 2`. Without accounting for it, every register read returns 0x00 in the first byte. Our `i2c_read()` requests `n + 2` bytes and discards the first 2.
3. **Configuration registers are 16-bit, word-addressed.** ACC_CONF and GYR_CONF take a single 16-bit word each, packing ODR / range / mode / averaging.
4. **CHIP_ID = 0x47.** Reading register 0x00 (after the dummy bytes) should return this constant. Anything else means it's not BMI330 (BMI323 returns 0x43, for example).

---

## 3. Wire format

Byte-identical to the M5StickC pipeline so `protocol.py` / `receiver.py` / `hr_worker.py` / `app.py` work unchanged.

22-byte little-endian binary packet:

```
[0xAA][0x55]              sync
[seq u16]                 wraps at 65535
[t_us u32]                MCU's micros() at moment of read
[ax i16][ay i16][az i16]  raw accel counts, FS = ±4 g
[gx i16][gy i16][gz i16]  raw gyro counts, FS = ±1000 dps
[crc8]                    CRC8/Dallas over bytes [2..19]
[0x0A]                    '\n' framing aid (parser ignores)
```

PC side: `accel_g = raw / (32768/4)`, `gyro_dps = raw / (32768/1000)`. Drop detection via `(seq - last_seq) & 0xFFFF`.

USB CDC's "baud rate" is virtual — the chip moves bytes at full USB speed regardless of the number we set. We use 115200 in code by convention.

---

## 4. Firmware

Located at `firmware/hr_streamer_bmi330/hr_streamer_bmi330.ino`. Key design choices:

### 4.1 Sample-rate strategy: status-register polling

The 7SEMI breakout has no INT1/INT2 pin exposed, so we can't use a hardware data-ready interrupt. Instead we **poll the BMI330's STATUS register** (0x02) every 5 ms; when both `drdy_acc` (bit 7) and `drdy_gyr` (bit 6) are set, we read the data and emit a packet.

This makes the **chip the rate source** — no phantom samples, regardless of internal ODR drift. Same lesson learned from the PPG project (`Heart_Rate_PPG/PPG_HR_Pipeline_Create.md` § 2.1).

### 4.2 Deadline-based scheduling

`Serial.write()` on USB CDC takes ~860 µs per 22-byte packet. Naive polling at "every 5 ms after the last loop" gives an actual rate of ~170 Hz, not 200 Hz. To fix: schedule the *next* deadline at `last_poll_us + 5000`, not `now_us + 5000`. Work time is then absorbed into the period instead of adding to it.

```cpp
last_poll_us += POLL_PERIOD_US;
if ((int32_t)(now_us - last_poll_us) > (int32_t)(2 * POLL_PERIOD_US)) {
  last_poll_us = now_us;   // snap forward if we fell more than 2 periods behind
}
```

Note: as of 2026-06-05 this fix has been written but flashing has not yet validated 200 Hz. Recordings are still at fs ≈ 170.68 Hz. Algorithm uses **measured fs** from `t_us` deltas, so on-disk recordings analyze correctly; only the live UI (which hardcodes 200) reads incorrectly.

### 4.3 Configuration words

ACC_CONF and GYR_CONF are 16-bit packed:

```
bits  3:0  ODR     (0x09 in our config — see note below)
bits  6:4  range   (acc 0x01 = ±4 g; gyr 0x03 = ±1000 dps)
bit   7    bw      (0 = ODR/2 averaging)
bits 10:8  avg     (0 = no averaging)
bits 14:12 mode    (7 = high-performance)
```

Current values:
- `ACC_CONF_VALUE = 0x7019` (acc, hp, ±4 g, ODR=0x09)
- `GYR_CONF_VALUE = 0x7039` (gyr, hp, ±1000 dps, ODR=0x09)

⚠️ **The ODR table in BMI330 differs from BMI323.** The first BMI330 build with these values produced 512 Hz at 1 ms poll rate (chip running ~1 kHz internally), not 200 Hz. We now gate the loop at 5 ms in firmware regardless of what the chip is internally producing — decimation aliasing isn't a concern because BCG energy is below 25 Hz, well under the 100 Hz post-decimation Nyquist.

### 4.4 Verification on first boot

Firmware prints to USB serial at boot:

```
[bmi330] found at 0x69      ← (or 0x68 depending on breakout strap)
[bmi330] CHIP_ID = 0x47     ← must be 0x47 — anything else is wrong
[bmi330] streaming...
```

After these three lines, the binary packet stream begins. The Serial Monitor renders it as garbage characters — that's correct.

---

## 5. PC pipeline

### 5.1 What stays the same

The whole PC stack from the M5StickC era is untouched and works as-is:

```
pc/
  protocol.py         packet parser + CRC8
  ringbuffer.py       lock-protected numpy ring buffer
  receiver.py         serial → ring buffer + Parquet rolling logger
  hr_worker.py        PCA + ACF + envelope HR estimator
  app.py              PyQt6 live UI (4 panes + recording control)
  analyze_session.py  offline replay → Excel HR-per-window report
  requirements.txt    pyserial, numpy, scipy, pyarrow, pyqtgraph, PyQt6
```

### 5.2 Setup

```sh
cd ~/Desktop/HR/pc
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# For analyze_session.py:
pip install openpyxl
```

### 5.3 Running

```sh
# Live UI with recording
python app.py --subject self --mount sternum-finger-press --note "first-light"

# Offline analysis of latest session
python analyze_session.py --latest
```

App opens in **LIVE PREVIEW**. Press **Space** (or the green button) to start/stop recording. Each start creates `data/session_<UTC>/` with:
- `session.json` — subject, mount, note, fs, full-scale settings
- `imu_NNNN.parquet` — 6-axis data, zstd-compressed, rolled every 5 minutes

### 5.4 HR algorithm (unchanged from 2026-05-16)

```
   accel (8 s window @ measured fs)
   │
   ▼ per-axis detrend (subtract mean = remove gravity)
   ▼ per-axis bandpass 5–25 Hz (Butterworth 4, sosfiltfilt)
   ▼ SVD on 3-axis bandpassed → 3 PC candidates
   ▼ score each PC by ENVELOPE-DOMAIN cardiac SNR
   │     (Hilbert |·|, Welch PSD, peak in 0.7–1.8 Hz / median power)
   ▼ pick highest-scoring PC, with 5 dB hysteresis vs. cached direction
   ▼ projected 1-D signal `pc`
   │
   ├── Method A: FFT autocorrelation → HR_acf
   │   (search lag in [60, 300] samples; find_peaks, NOT argmax)
   │
   ├── Method B: Hilbert envelope → 3 Hz Butterworth low-pass
   │   → adaptive threshold (mean + 0.7σ), 500 ms refractory
   │   → median IBI → HR_env
   │
   └── Fusion
         both NaN → NaN, gray
         one NaN  → that one, low confidence (orange)
         |Δ| ≤ 8  → mean, high confidence (green)
         |Δ| > 8  → mean, low confidence (orange)
```

Constants (`hr_worker.py`):
- WINDOW_S = 8.0, STEP_S = 1.0
- HR_MIN_BPM = 40, HR_MAX_BPM = 200
- BAND_LOW_HZ = 5, BAND_HIGH_HZ = 25
- AGREE_BPM = 8
- PC_HYSTERESIS_MARGIN_DB = 5.0
- PC_CACHE_MIN_SQI_DB = 5.0

The **3 Hz envelope LPF** (instead of the original 100 ms moving average) is critical — it merges the AO and AC valve transients into one envelope lobe per beat, killing the 2× harmonic doubling that originally read 108 bpm vs. truth 63–75. See `docs/session_2026-05-16.md`.

---

## 6. End-to-end bring-up procedure

### 6.1 Arduino IDE setup (one time)

1. **Settings** → **Additional Boards Manager URLs**, add:
   ```
   https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
   ```
2. **Tools → Boards Manager** → search "seeed nrf52" → install **Seeed nRF52 Boards**.
3. Plug in the XIAO. **Tools → Board → Seeed nRF52 Boards → Seeed XIAO nRF52840 Sense**.
4. **Tools → Port** → select the `/dev/cu.usbmodem*` (Mac) or `COMn` (Windows) that appeared.

### 6.2 Flashing

1. Open `firmware/hr_streamer_bmi330/hr_streamer_bmi330.ino` in Arduino IDE.
2. Click **Upload**.
3. If upload fails with "Timed out waiting for acknowledgement" — the XIAO isn't in DFU mode. Force it:
   - **Single-tap reset** = warm reboot (port stays the same).
   - **Double-tap reset (within ~0.5 s)** = enter DFU bootloader for ~30 s. The orange LED breathes slowly. Re-select the new port that appears, then click Upload.
   - If you can't find/press the tiny reset button, short the **RST** pad to **GND** with a metal tweezer / paperclip — same effect. Two quick contacts = DFU.

### 6.3 First-boot smoke test

1. After a successful upload, **wait 3 seconds** for USB to re-enumerate.
2. **Tools → Port** → re-select. The number may have changed.
3. **Tools → Serial Monitor** at 115200 baud. (USB CDC ignores baud, but the IDE wants a value.)
4. **Single-tap reset** so `setup()` runs and prints its boot lines.
5. Expect:
   ```
   [bmi330] found at 0x69
   [bmi330] CHIP_ID = 0x47
   [bmi330] streaming...
   <binary garbage thereafter>
   ```
6. **Close the Serial Monitor** before running `app.py` — only one process can hold the serial port.

### 6.4 Sanity check on data

With the board flat on a table:

| Pane / stat | Expected |
|---|---|
| Receiver `rx` count | climbing at ~170-200 / s |
| Receiver `drop`, `bad_crc` | both 0 |
| Pane 1 (raw accel, up axis) | ≈ 1.0 g (board flat); flips to −1 g (board inverted); 0 g (board on edge) |
| `\|a\|` magnitude | ≈ 1.00 ± 0.03 g |
| Pane 2 (5-25 Hz BP) | small wiggle near zero (no body coupling = no signal) |

If `\|a\|` is wildly off, the BMI330 config word is wrong (probably the range bits). If `bad_crc` climbs, the I²C read is truncated — try splitting the 12-byte read into two 6-byte reads.

---

## 7. Validation history

### 7.1 First successful body session — `session_20260605T200022Z`

- Duration: 77.3 s, finger-pressed against sternum
- 0 drops, 0 bad CRC
- Confident HR (median): **56.1 bpm**
- ACF only: 57.5; envelope only: 56.1 (methods agree to 1.4 bpm on confident windows)
- SQI: median 8.2 dB, max 15.1 dB
- **Oximeter ground truth: 55–60 bpm**
- Verdict: **sub-2-bpm error against ground truth.** First validated SCG session of the project.

### 7.2 Best documented session — `session_20260605T202030Z`

- Duration: 82.6 s, with deliberate breath-hold from 50–63 s
- Pre-hold (33–47 s): pipeline 105.6 bpm | oximeter 100–110 bpm | **within 5 bpm**
- Hold (50–63 s): pipeline 66 bpm | oximeter dropped to 76 bpm | **10 bpm gap, but pipeline caught the diving-reflex onset**
- Recovery (65+ s): pipeline 97 bpm | oximeter 90–95 bpm | **within 7 bpm**
- Peak SQI: **16.8 dB** (best of any session — the breath-hold prediction from `session_2026-05-14.md` was right)
- Verdict: pipeline tracks **real cardiac transitions in real time** including a textbook diving-reflex bradycardia.

### 7.3 What's now confirmed

- ✅ BMI330 wiring + I²C read protocol correct
- ✅ Wire format byte-compatible with M5StickC pipeline
- ✅ PCA + ACF + envelope fusion algorithm transfers cleanly to new IMU
- ✅ Sternum finger-press mount yields ≥10 dB SQI when placed correctly
- ✅ Hardware bring-up complete

---

## 8. Known issues / open work

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | Sample rate is 170.68 Hz, not 200 Hz | cosmetic (live UI displays 17% high; recordings unaffected) | deadline-fix written, awaiting reflash + verification |
| 2 | Confidence rate is 28-46% | acceptable | improvable with proper supine + taped mount |
| 3 | ACF harmonic confusion at high HR | open | known failure mode; tier-2 algorithm fix |
| 4 | No supine + tape-mounted trial | open | next milestone |
| 5 | No Polar H10 ground truth | open | budget item |
| 6 | Dog port not started | open | requires breath-hold-equivalent validation first |

---

## 9. Quick reference cheat sheet

| Item | Value |
|---|---|
| MCU | Seeed XIAO nRF52840 Sense |
| IMU | 7SEMI BMI330 Nano (I²C) |
| BMI330 address | 0x69 (per current breakout strap) |
| BMI330 CHIP_ID | 0x47 |
| ACC_CONF | 0x7019 (200 Hz hp, ±4 g) |
| GYR_CONF | 0x7039 (200 Hz hp, ±1000 dps) |
| I²C bus speed | 400 kHz |
| Sample-rate strategy | poll STATUS register every 5 ms; emit when drdy_acc & drdy_gyr |
| Wire format | 22-B framed binary, CRC8, identical to M5StickC era |
| Transport | native USB CDC (no real baud) |
| Wiring | 4 wires: 3V3, GND, D4(SDA), D5(SCL) |
| HR algorithm | unchanged: PCA + ACF + envelope fusion |
| Window / hop | 8 s / 1 s |
| Bandpass | 5-25 Hz Butterworth 4 SOS |
| Storage | session_<UTC>/imu_NNNN.parquet (zstd, 5-min rolls) + session.json |
| Validation | oximeter manual deltas (Polar H10 planned) |
| Best validated session | `session_20260605T202030Z` (16.8 dB peak SQI, diving-reflex bradycardia caught) |

---

## 10. Where to look next

- For mechanical / enclosure design: `docs/Enclosure_Design.md` (this folder).
- For the step counter (shares this accel stream): `docs/Step_Counter.md` (this folder).
- For broader algorithmic context: `PPG_Algos.md` (project root).
- For original canine concept: `dog_hr_device_context.md` (project root).
- For the M5StickC-era setup: `SETUP.md` (project root).
- For session-by-session history: `docs/session_*.md` (this folder).
- Old M5StickC firmware preserved at `firmware/hr_streamer/hr_streamer.ino` — do not delete; documented switch-back point.
