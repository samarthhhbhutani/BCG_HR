# PPG HR Pipeline — Implementation, Validation & Improvements

Context file for the Heart_Rate_PPG project. Captures (a) the pipeline as built, (b) the bugs we hit during validation, (c) the time-domain vs FFT comparison, and (d) candidate improvements ranked by expected impact.

Project context: M5StickC PLUS2 + MAX30102 finger PPG. Human first (resting / sedentary), pet pivot later. HRV is on the roadmap but currently de-scoped. POC subject is the developer.

---

## 1. Pipeline as built

### 1.1 Hardware & wire format

- **Sensor:** MAX30102 reflective PPG (red 660 nm + IR 880 nm)
- **MCU:** M5StickC PLUS2 (ESP32-PICO-V3-02)
- **Wiring:** `G0` → SDA, `G26` → SCL, `5V` → VIN, `GND` → GND
- **I2C:** 400 kHz
- **Sensor config:** `sampleRate=400 Hz` × `sampleAverage=2` → effective output **200 Hz**, ledMode=2 (Red+IR), pulseWidth=411 µs (18-bit ADC), adcRange=4096 nA, LED current 0x1F (~6.4 mA)
- **Sampling design:** sensor is the rate source. Firmware drains FIFO one entry per packet. No `esp_timer` (an earlier design used a 200 Hz timer that emitted phantom samples while the sensor only delivered ~58 Hz; PC side measured `fs ≈ 58 Hz` and HR was off by 25–40 %).
- **Wire packet** (18 bytes, little-endian): `0xAA 0x55 | seq u16 | t_us u32 | ir u32 | red u32 | crc8 | 0x0A`

### 1.2 PC pipeline file map

```
pc/
  protocol.py         packet parser
  ringbuffer.py       shared sample buffer (t_us, ir, red)
  receiver.py         serial → ring buffer + parquet writer
  hr_worker.py        TERMA + slope gate + HeartPy filter → HR
  subject_config.py   per-species params (human/dog/cat)
  app.py              PyQt6 live UI
  subjects/*.json     species presets
```

### 1.3 Signal-processing pipeline (final state)

```
MAX30102 IR @ 200 Hz
   │
   ▼  Ring buffer (60 s capacity)
   │
   ▼  Worker tick (every 1 s, on last 8 s window)
   │
   ▼  Finger-presence gate
        ir_min ≥ contact_ir_min   (50 000 default for human)
        ir_max ≤ contact_ir_max   (260 000)
        AC amp  ≥ contact_ac_min  (200)
   │
   ▼  DC removal (subtract 1 s moving average)
   │
   ▼  Bandpass 0.5–4 Hz (Butterworth 4, sosfiltfilt, zero-phase)
   │
   ▼  Sign flip (MAX30102 IR drops at systole)
   │
   ▼  Elgendi TERMA peak detection
        MA_short = 111 ms
        MA_long  = 667 ms
        block-width gate ≥ 111 ms
        emits candidate beats
   │
   ▼  Onset refinement (move each peak to local d/dt max in 200 ms back-window)
   │     — Peralta 2019 fiducial point for HRV grade timing
   │
   ▼  Pan-Tompkins slope-ratio gate
        if candidate within 1.5 × running mean RR of previous accepted peak
        AND its upstroke slope < 50 % of previous slope
        → reject (dicrotic notch)
   │
   ▼  IBI plausibility filter (subject_config bounds, generous)
   │
   ▼  HeartPy RR-band filter
        reject IBIs outside mean ± max(30 % × mean, 300 ms)
        iterate up to 3 passes
   │
   ▼  HR = 60 000 / median(IBI)
   │
   ▼  Confidence:  ≥ 4 IBIs AND IBI CV < 25 %
```

### 1.4 What gets recorded per session

`data/session_<UTC>/`
- `ppg_NNNN.parquet` — every raw IR/Red sample (rolled every 5 minutes)
- `hr_30s.parquet` — one row per 30 s averaging window (mean HR, beats, finger-present fraction)
- `session.json` — subject, species, mount, start time, full subject_config snapshot

### 1.5 Cross-species design

Every subject-specific value lives in `subject_config.py` / `subjects/*.json`:
- `hr_min_bpm` / `hr_max_bpm`
- `ibi_min_ms` / `ibi_max_ms`
- `bandpass_low_hz` / `bandpass_high_hz`
- `contact_ir_min` / `contact_ir_max` / `contact_ac_min`

Pipeline code has zero hardcoded subject values. Switch via `--species human|dog|cat` or path to a custom JSON.

---

## 2. Validation — what we found during testing

### 2.1 Firmware sample rate bug (FIXED)

**Symptom:** HR readings ~25 % too high on early sessions.
**Root cause:** Original firmware used `esp_timer` at 200 Hz to drive packet emission, but the sensor only delivered ~58 Hz of real data. Timer emitted phantom packets between real readings, with bursty timestamps. PC pipeline used `fs = 200` everywhere — wrong filter cutoffs, wrong TERMA window widths, wrong sample-to-time conversion.
**Fix:** Killed the timer. Sensor is now the rate source: drain FIFO one entry per packet, `t_us` stamped at moment of read. Receiver computes measured fs from `t_us` deltas; UI shows ⚠ if measured fs deviates >10 % from declared 200 Hz.
**Verification:** Post-fix sessions show measured fs = 197.5 Hz, p10/p50/p90 of dt = 4789/4834/5243 µs (clean 5 ms spacing).

### 2.2 Dicrotic notch false positives (PARTIALLY FIXED)

**Symptom:** TERMA over-counted beats. Truth 55 bpm, raw IBIs included 263, 319, 415, 511, 587 ms entries — these are dicrotic notches caught as beats.
**Diagnosis:** TERMA's squaring + block-width gate suppresses *some* dicrotic energy but not all of it on this hardware.
**Fix:** Added Pan-Tompkins slope-ratio gate (rejects candidates whose upstroke slope < 50 % of previous accepted peak's slope, when within 1.5 × running RR). Adapts to any HR. Plus HeartPy 30 % RR-band filter as second-line cleanup.
**Verification:** Cleaner sessions now produce sub-2-bpm error vs. truth (55.9 / 54.8 / vs. 55 truth).

### 2.3 Movement / contact pressure artifacts (OPEN)

**Symptom:** Some 30 s windows still read 70+ bpm when truth is 55. Diagnosis showed these windows had IR DC drifts (74k → 95k swings) and the band-pass filter rang on those drifts, creating spurious peaks.
**Status:** Not algorithmic — this is measurement quality. Stricter min-IR gating helped a bit (rejected 16 of 138 windows on a noisy session) but doesn't catch slow pressure drifts that stay within `contact_ir_min`.
**Candidate fixes:** see § 4.

### 2.4 Confirmed working

- Wire protocol: 0 dropped, 0 bad CRC across all sessions
- Finger-presence gate: catches discrete finger-off events
- 30 s averaging: emits correctly, logs to parquet write-through
- Cross-species architecture: not yet tested on pets but design is parameterized

---

## 3. Time-domain vs FFT — comparison

Tested empirically on session_20260524T201533Z (truth = 55 bpm).

### 3.1 Side-by-side properties

| Property | Time-domain (TERMA + slope + HeartPy) | FFT (Welch peak in 0.5–3.3 Hz) |
|---|---|---|
| Best-case accuracy (clean rest) | sub-1 bpm (Elgendi 2013: 99.89 % SE) | 0.5–1 bpm |
| Window needed | 8 s for ~8 IBIs at 60 bpm | 30 s for 2 bpm bin width |
| HR resolution | set by fs (~0.3 bpm at 200 Hz) | set by window length (30 s → 2 bpm) |
| Latency to first reading | ~8 s | ~30 s |
| Failure under motion | spurious peaks → wild instant HR | tracks motion peak — wrong but stable |
| Failure under harmonics | robust (peak count unambiguous) | locks 2× (dicrotic) or 0.5× (breathing) |
| Failure at low SNR | misses beats, IBI lengthens, HR low | cardiac peak below noise → garbage |
| Confidence available | yes — IBI count + CV + slope-gate rejection rate | weak — peak prominence only |
| HRV-capable | yes ✅ | no ❌ |
| Adapts to HR step changes | 1–2 windows (~2 s) | 1 window (~30 s) — slow |
| ESP32 cost | O(N) — moving averages + find_peaks | O(N log N) — 4096-pt FFT, ~1 ms with esp-dsp |
| Sensitive to fs miscalibration | yes (TERMA window widths) | yes (frequency axis) |
| MAE on PPG-DaLiA rest | ~1–2 bpm (TERMA + cleanup) | ~2–4 bpm (naive FFT) |
| MAE on IEEE SPC motion | ~5–10 bpm | ~1.2–2 bpm with SPECMAR |

### 3.2 Empirical results on session_20260524T201533Z

```
window         truth    time-domain         FFT (Welch peak)
0–30 s         55       55.9 ✅              57.9 ✅
30–60 s        55       54.8 ✅              69.5 ❌  (1.26× truth)
60–90 s        55       74.0 ❌              57.9 ✅
80–110 s       55       —                   86.9 ❌  (1.58× truth)
```

Time-domain: 2/3 hits, error clustered in motion window.
FFT: 2/4 hits, errors are harmonic confusion (1.26× and 1.58× truth).

**They fail in different windows.** Not a rank ordering — they are complementary failure modes.

### 3.3 Conclusion for finger-PPG-at-rest

- **Pure FFT would not be more accurate** on this hardware. Dicrotic harmonics and breathing-band leakage make Welch peak unreliable.
- **Pure time-domain is closer to truth on clean windows** but has motion failures with bigger absolute errors.
- **Literature-standard answer is hybrid:** time-domain primary, FFT as cross-check. If they disagree by > tolerance (e.g. 8 bpm), mark window low-confidence.
- **Going FFT-only would** lose accuracy on this data, close the door on HRV, and *increase* compute cost.

### 3.4 When FFT would actually win

Only if the use case changes to:
- Wrist-worn PPG (motion-dominated)
- Exercise / ambulatory monitoring
- TROIKA / SPECMAR-style spectral subtraction with IMU as motion reference

For finger-pressed PPG on a stationary subject, FFT is strictly worse.

---

## 4. Possible improvements — ranked by expected impact

### Tier 1 — high ROI, low risk

#### 4.1 FFT cross-check (NOT a replacement)
Run a Welch FFT in parallel with the time-domain pipeline. If `|HR_time − HR_fft| > 8 bpm`, mark the window low-confidence and exclude from the 30 s mean. This catches the "window 3 reads 74 bpm when truth is 55" failure (FFT in that window read 58 bpm correctly).

**Why:** literature-standard practice (TROIKA, Schäfer 2013). Costs one extra FFT per worker tick. Doesn't change the source-of-truth pipeline.

#### 4.2 Tighten finger-presence gate using *baseline drift* not just IR min/max
Current gate checks IR levels but not pressure-induced drifts within range. Add: reject window if `std(moving_average_1s(IR)) > threshold`. This rejects windows where the user is squeezing/relaxing the sensor mid-window, which causes the band-pass ringing we saw.

**Why:** the remaining errors are these drifts, not finger-off events. ~15 lines of code.

#### 4.3 Long-window output for final session HR
Once instant HR is per-1-s and 30 s windows exist, add a trailing 90–120 s **median** for the final session number. Rejects single bad windows by construction. Median, not mean, because one motion artifact shouldn't move the final.

**Why:** the user explicitly asked for a 1–2 min average as the final output. HeartPy session summaries do exactly this.

### Tier 2 — moderate ROI, moderate risk

#### 4.4 Adaptive bandpass cutoffs based on running HR
Currently 0.5–4 Hz fixed. At HR 50 bpm, fundamental is 0.83 Hz, dicrotic ≈ 1.6–2.5 Hz, all in band. At HR 150 bpm, fundamental is 2.5 Hz, dicrotic ≈ 5–7 Hz, mostly OUT of band → much cleaner. If we *track* HR and lower the high cutoff to ~`2.0 × HR_Hz + 0.5` we attenuate the dicrotic at every HR.

**Why:** the static 4 Hz cutoff is a compromise. Adaptive is what wrist wearables do internally.
**Risk:** requires a stable HR estimate to set the cutoff → bootstrapping problem. Usually solved with two-pass: pass 1 gives coarse HR, pass 2 with adapted cutoff gives final.

#### 4.5 SQI (Signal Quality Index) per window
Compute a 0–1 quality score per window: function of (a) AC/DC ratio, (b) IBI CV, (c) slope-gate rejection rate, (d) bandpass spectral centroid alignment with detected HR. Display in UI; gate the 30 s mean to only include SQI > threshold.

**Why:** Orphanidou 2015 (PPG SQI paper) shows this dramatically improves continuous-monitoring accuracy. Current "confident" boolean is binary; SQI is graded.

#### 4.6 Template matching as third-line filter
Build a beat template from the first N high-confidence beats. Reject any subsequent peak whose surrounding morphology has correlation < 0.6 with the template. Catches what slope-gate misses (e.g. a notch happening to look like a sharp peak in low-SNR).

**Why:** Orphanidou 2015 uses 0.86 correlation threshold for SQI. ~30 lines of code. Adapts as the template updates.

### Tier 3 — exploratory, higher cost

#### 4.7 Slope-Sum Function (Zong 2003) as alternative front-end
Replace TERMA with SSF + adaptive threshold (`0.6 × max SSF of previous beat`). SSF is amplitude-independent by construction; the dicrotic upstroke produces consistently smaller SSF magnitude than systolic.

**Why:** Zong 2003 reports 99.31 % / 99.74 % on 368 364 beats — comparable to TERMA but built around the upstroke specifically. Could replace TERMA + slope-gate with one mechanism.
**Risk:** rewrite of detector core; benchmark needed before swap.

#### 4.8 Multi-wavelength fusion (red + IR + ICA)
MAX30102 has both red and IR. Currently we use IR only. ICA on the two channels can separate cardiac from motion in some scenarios. Modest win for stationary subjects (red is mostly redundant) but valuable when adding pet ear-clip mounts (different tissue optics).

**Why:** Lee 2020, Ray 2021 show meaningful gains under motion. For stationary humans, gain is small — ~5–10 % MAE reduction at best.
**Cost:** ICA on two channels is cheap (Cardoso JADE), but decision logic about which IC to keep is fiddly.

#### 4.9 Lightweight TCN (Q-PPG style)
Burrello 2021 demonstrated INT8-quantized TCN deployable on Cortex-M4-class MCUs (~ESP32 class), MAE ~4.4 bpm on PPG-DaLiA. Trains on PPG + accelerometer.

**Why:** best-in-class motion robustness. **Why not:** trained on human-only datasets, no canine PPG data exists. Closes the door on cross-species pivot. Major training/quantization tooling investment.
**Verdict:** explicitly avoided in [[ppg-hr-algorithms]] for this project.

### Tier 4 — hardware / measurement-quality

#### 4.10 Mechanical probe redesign
The single largest source of remaining error is **finger-pressure drift**. Building a spring-loaded clip (or transmissive fingertip clip rather than reflective on the back of the sensor) eliminates this entirely. Cheap commercial finger probes (~$10) demonstrably outperform any algorithmic improvement on the same chip.

**Why:** signal quality dominates algorithm choice in PPG. Pulse oximeter clips work because they fix the geometry.
**Cost:** mechanical, not software.

#### 4.11 Higher LED current for thicker / darker skin
Current `0x1F` (~6.4 mA) is mid-range. Skin tone, finger thickness, and ambient temperature change effective optical path. A simple AGC loop (raise LED current until IR DC reaches target ~80–100 k) gives consistent SNR across users.

**Why:** clinical pulse oximeters do this. ~20 lines in firmware.

#### 4.12 Move sensor to ear clip / earlobe
For both humans and pets, the earlobe / pinna has thinner tissue, less motion sensitivity, and no fur (on dogs/cats). Vet-clinical PPG uses pinna for exactly this reason.

**Why:** measurement-quality win, not algorithm win. Required eventually for the pet pivot.
**Cost:** mechanical only.

---

## 5. Open questions / known gaps

- **`n_beats` column in `hr_30s.parquet` is misnamed.** It currently counts UI update events, not distinct beats. Should be either renamed `n_updates` or recomputed to count unique beats in the 30 s window.
- **Confidence is binary.** Should be a graded SQI (see § 4.5).
- **No long-window (1–2 min) HR yet.** User asked for this; not implemented.
- **Recording control is good but no live SQI plot.** The UI shows beat markers but not why the algorithm rejected things — hard to debug from the UI alone.
- **Cross-species not validated.** Architecture supports it, but no dog / cat session has been recorded. Finger-presence thresholds for pets are guesses until first real session.

---

## 6. Verified citations (used in implementation)

- Elgendi M. et al. (2013). *Systolic peak detection in acceleration photoplethysmograms.* PLoS ONE 8(10): e76585. DOI: 10.1371/journal.pone.0076585. — TERMA candidate detector.
- Pan J., Tompkins W.J. (1985). *A real-time QRS detection algorithm.* IEEE TBME. DOI: 10.1109/TBME.1985.325532. — slope-ratio T-wave check, 0.5 ratio verbatim.
- van Gent P. et al. (2019). *HeartPy: A novel heart rate algorithm for the analysis of noisy signals.* Transp. Res. F. DOI: 10.1016/j.trf.2019.09.015. — RR-band 30 % filter.
- Peralta E. et al. (2019). *Optimal fiducial points for pulse rate variability analysis.* Physiological Measurement. — onset (1st-derivative max) is the stable HRV fiducial.
- Zong W. et al. (2003). *An open-source algorithm to detect onset of arterial blood pressure pulses.* CinC. — SSF reference for § 4.7.
- Schäfer A., Vagedes J. (2013). *How accurate is pulse rate variability...* Int. J. Cardiol. DOI: 10.1016/j.ijcard.2012.03.119. — spectral cross-confirmation reference.
- Orphanidou C. et al. (2015). *Signal-quality indices for ECG and PPG.* IEEE JBHI. DOI: 10.1109/JBHI.2014.2338351. — template-matching SQI at 0.86 threshold.
- Charlton P.H. et al. (2022). PPG beat-detection benchmark. — qppg, ABD, IMS, PWD comparison.

Related project files: see [[ppg-hr-algorithms]] and `PPG_Algos.md` (in repo root) for the broader algorithm survey context.
