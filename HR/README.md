# HR — sternum SCG via M5StickC PLUS2

Real-time heart-rate estimation from MPU6886 accelerometer data, sternum-mounted (SCG).

## Layout

```
HR/
  firmware/hr_streamer/hr_streamer.ino   Arduino sketch: 200 Hz IMU → binary USB serial
  pc/
    protocol.py                          Wire format + CRC8
    ringbuffer.py                        Lock-protected numpy ring buffer
    receiver.py                          Serial reader + Parquet rolling logger
    hr_worker.py                         Band-pass + ACF + envelope-peak HR fusion
    app.py                               pyqtgraph live UI (run this)
    requirements.txt
    .venv/                               Python 3.12 venv
  data/                                  session_<UTC>/ created per run
  docs/
```

## Wire format

22-byte little-endian packets at 921600 baud:

| Bytes | Field |
|---|---|
| 0–1 | sync `0xAA 0x55` |
| 2–3 | seq u16 |
| 4–7 | t_us u32 (ESP timer, monotonic) |
| 8–13 | ax, ay, az i16 (FS = ±4 g) |
| 14–19 | gx, gy, gz i16 (FS = ±1000 dps) |
| 20 | CRC8/Dallas over bytes 2–19 |
| 21 | `\n` (eyeball-only, ignored by parser) |

Convert in Python: `g = raw / (32768 / 4)`, `dps = raw / (32768 / 1000)`.

## Firmware setup (one-time)

1. Arduino IDE → Boards Manager → install **M5Stack** (≥ 2.1.0).
2. Library Manager → install **M5StickCPlus2** and **M5Unified**.
3. Open `firmware/hr_streamer/hr_streamer.ino`.
4. Tools → Board → `M5StickCPlus2`; Tools → Port → the `cu.usbmodem*` that appears when plugged in.
5. Upload.

The screen should show `seq <n>` updating every second and a per-second sample count near 200.

## PC setup (one-time)

```sh
cd ~/Desktop/HR/pc
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Already done in this checkout.

## Running a session

```sh
cd ~/Desktop/HR/pc
.venv/bin/python app.py --subject self --mount sternum-finger-press --note "trial 1"
```

The UI opens with four panes (raw axis, filtered SCG, envelope+beats, HR trend) plus a Record button. **The app starts in LIVE PREVIEW mode — nothing is saved.** Position the device, wait for the green light, then press **Space** (or click Record) to start writing to disk. Press again to stop. Each start/stop pair creates one `data/session_<UTC>/` containing:

- `session.json` — metadata (subject, mount, note, sample rate, full-scale settings)
- `imu_NNNN.parquet` — IMU rolled every 5 minutes (zstd-compressed, all 6 IMU channels + seq + t_us)

Compare the on-screen fused HR readout to your pulse oximeter directly — no logging in the app.

## Session protocol (sternum SCG, finger-pressed)

1. Sit upright at a desk, elbows braced on the table.
2. Press the device's display face firmly against the lower sternum, ~3 cm below the suprasternal notch.
3. Wait ~10 s for the SNR readout to climb above ~10 dB and the fused HR to turn green (high confidence).
4. **Trial A — quiet breathing** (60 s): note pulse-ox HR every 15 s into the GT input.
5. **Trial B — breath-hold** (30 s after a normal exhale): SCG is cleanest here. Log GT before and after.
6. **Trial C — quiet breathing again** (60 s): repeat A.

Stop with the window's close button or Ctrl+C in the terminal — both flush Parquet and exit cleanly.

## Algorithm notes

- 8 s sliding window, stepped every 1 s.
- Band-pass 5–25 Hz (Butterworth 4th, `sosfiltfilt`).
- Axis selected per window by 1–3 Hz spectral power vs. 10–30 Hz noise floor.
- Method A: biased autocorrelation, peak in lag range corresponding to 40–200 bpm.
- Method B: Hilbert envelope, 100 ms moving avg, adaptive threshold (mean + 0.5σ), 250 ms refractory peaks → median IBI.
- Fusion: average if methods agree within 8 bpm (high confidence, green); else pass through best-effort (low confidence, orange).

## References

- Inan et al., *BCG and SCG: A review of recent advances*, IEEE JBHI 19(4), 2015.
- Brüser et al., *Adaptive beat-to-beat HR estimation in BCG*, IEEE TITB 15(5), 2011.
- Brüser et al., *Robust IBI estimation in cardiac vibration signals*, Physiol. Meas. 34(2), 2013.
- Hernandez, McDuff, Picard, *BioPhone: Physiology Monitoring from Peripheral Smartphone Motions*, IEEE EMBC, 2015.
- Pandia et al., *Extracting respiratory information from SCG*, Physiol. Meas. 33(10), 2012.
- Landreani et al., *Smartphone Accelerometers for the Detection of Heart Rate*, IEEE TBME, 2017.
- Sadek, Biswas, Abdulrazak, *BCG signal processing: a review*, Health Inf. Sci. Syst., 2019.
- Bland & Altman, *Statistical methods for assessing agreement…*, Lancet, 1986. (Validation methodology.)
