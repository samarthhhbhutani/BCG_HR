# PPG Heart Rate Algorithms — Context & Pipeline Notes

Project context: M5StickC PLUS2 (ESP32-PICO-V3-02 + MPU6886) HR device. POC subject is human first, pivoting to pets (dogs, then potentially cats) later. Resting HR now, HRV later. PPG sensor is MAX30102 (red 660 nm + IR 880 nm, reflective). BCG path via MPU6886 retained as fallback for furred regions.

---

## Part 1 — Algorithm Survey & Trade-offs

The central problem in PPG HR estimation is **motion artifact**: the cardiac AC component is 0.1–2% of the DC level, and during motion the accelerometer-band signal can swamp the cardiac peak in 0.5–4 Hz. Almost every algorithm is judged primarily on how it handles this. Rest is easy; running is hard.

**Standard preprocessing (everyone does this):** bandpass 0.5–4 Hz (HR-mean) or 0.5–8 Hz (morphology), detrend DC, z-score normalize, optional downsample to ~25–125 Hz.

**Wavelength note:** green (~525 nm) is the wrist-wearable default. MAX30102 only has red and IR — use **IR for HR** (stronger pulsatile-to-DC ratio at finger), red is for SpO2.

### Algorithm Families

#### 1. Time-domain peak detection (Elgendi TERMA, slope-sum, derivative)
- **Pros:** Trivial compute (O(N), KB of memory). Gives beat-to-beat intervals → enables HRV. Easy to debug visually.
- **Cons:** Falls apart under motion. Threshold tuning is dataset-dependent.
- **Use when:** Resting/clinical PPG, or as the final stage after motion cleanup. Required for HRV.
- **Avoid when:** Subject is ambulatory and you have no upstream cleanup.
- **Canonical:** Elgendi 2013 PLoS ONE (DOI 10.1371/journal.pone.0076585).

#### 2. Frequency-domain (FFT / Welch sliding window)
- **Pros:** Cheap (one 256–512-pt FFT per 1–2 s, sub-ms on ESP32 with esp-dsp/CMSIS-DSP). Easy.
- **Cons:** Tracks motion when motion's spectral peak > cardiac peak. Window-averaged → **no HRV**. ~10 bpm MAE baseline under exercise.
- **Use when:** Baseline/sanity check, or back-end of a spectrum-cleanup pipeline.
- **Avoid when:** You need beat-to-beat data (HRV).

#### 3. Autocorrelation / Wavelet / Hilbert
- **Pros:** Autocorrelation is FFT-cheap and noise-robust. CWT gives time-frequency localization → ridge tracking under mild motion.
- **Cons:** CWT is heavy (probably won't fit on ESP32 in real time). Mild improvement only.
- **Use when:** Rest-to-light-motion, or cheap back-end alternative to FFT.
- **Avoid when:** Heavy exercise or compute-constrained for CWT.

#### 4. Adaptive filtering with accelerometer reference (LMS/NLMS/RLS)
- **Pros:** Cheap (10–30 taps per axis). ESP32-friendly. Directly addresses motion using the IMU you already have.
- **Cons:** Assumes motion-to-PPG is approximately linear — fails on nonlinear skin–sensor compression. RLS heavier (O(L²)/sample).
- **Use when:** Default first try for ambulatory PPG.
- **Avoid when:** Resting subject (overkill) or skin-coupling artifacts dominate.
- **Canonical:** Ram 2011, Tanweer 2017.

#### 5. Spectral subtraction / SPECMAR
- **Pros:** Sweet spot of accuracy vs. cost. ~1.2–2 bpm MAE on IEEE SPC. Two FFTs (PPG + accel) per window — trivial on ESP32.
- **Cons:** Window-averaged → **no HRV**. Needs accel reference.
- **Use when:** Strong motion robustness with embedded compute, mean-HR only.
- **Avoid when:** You need IBI-level data.
- **Canonical:** Islam et al. 2019 (Med. Biol. Eng. Comput.).

#### 6. Sparse signal reconstruction (TROIKA / JOSS)
- **Pros:** State-of-the-art on IEEE SPC for years (~1.3–2.3 bpm MAE).
- **Cons:** **Not real-time on ESP32.** SVD on Hankel matrices + iterative l1 reconstruction (FOCUSS).
- **Use when:** Offline analysis, or compute is on a phone.
- **Avoid when:** Embedded real-time.
- **Canonical:** Zhang TROIKA 2014 (arXiv:1409.5181), Zhang JOSS 2015 (arXiv:1503.00688).

#### 7. EMD / EEMD / VMD
- **Pros:** Data-driven decomposition, can isolate respiration vs. cardiac IMFs.
- **Cons:** Heavy compute, mode-mixing, marginal gains. Iterative and unbounded sifting.
- **Use when:** Research, or as a denoising front end with short windows.
- **Avoid when:** Real-time on ESP32.

#### 8. Kalman filter / state-space tracking
- **Pros:** Negligible compute. Smooths spurious frame-to-frame jumps from any front end.
- **Cons:** Standalone useless — needs a peak detector underneath.
- **Use when:** Always pair with FFT / spectral subtraction / peak detection.
- **Avoid when:** Standalone.

#### 9. Deep learning (DeepPPG, CorNET, Q-PPG, transformers, SSL)
- **Pros:** Best published accuracy on PPG-DaLiA. Q-PPG is MCU-deployable (INT8 TCN, MAE ~4.4 bpm at <1 mW on Cortex-M4).
- **Cons:** DeepPPG/CorNET as published need MB of weights + smartphone compute. Out-of-distribution motion still hurts. **Trained on human-only datasets — no labeled canine PPG data exists.** Kills DL as a cross-species path. Most output mean HR, not IBIs.
- **Use when:** Best-in-class motion robustness, willing to invest in training/quantization tooling, single species.
- **Avoid when:** Cross-species pivot planned, time-boxed prototype, or no training data.
- **Canonical:** Reiss DeepPPG 2019 (Sensors 19:3079), Burrello Q-PPG 2021 (arXiv:2203.14907).

#### 10. Multi-wavelength / multi-sensor fusion
- **Pros:** Physically independent measurements of the same pulse → strong motion suppression.
- **Cons:** Hardware complexity. MAX30102 has red+IR but reading both for fusion (not just SpO2) is uncommon.
- **Use when:** Custom optical front end.
- **Avoid when:** Stock module.

---

## Part 2 — Re-Ranked for Resting HR + HRV + Cross-Species

### Scoring axes
| Axis | Weight | Why |
|---|---|---|
| Provides IBIs (HRV-capable) | **Very High** | HRV is on the roadmap |
| Cross-species portable | **Very High** | Pet pivot planned |
| Resting HR accuracy | High | Primary deliverable now |
| ESP32 compute fit | High | Real-time on the stick |
| Implementation effort | Medium | Prototype, not production |
| Resists baseline wander / breathing | Medium | Dominant noise at rest |
| Resists motion artifact | **Low** | Resting only |

### Tier 1 — Recommended
- **Time-domain peak detection (Elgendi TERMA + onset fiducial)** — only family that natively produces IBIs. Resting eliminates its weakness. Onset-based fiducial points (Peralta 2019) are stable across morphology variation and HR ranges. **Default pick.**
- **Bandpass + 2nd-derivative onset detection** — robust fiducial point, well-documented for finger PPG. Pair with TERMA.

### Tier 2 — Useful as auxiliary
- **Kalman filter on IBI series** — outlier rejection layer on top of peak detection.
- **FFT/Welch** — sanity-check display only. Never source of truth (no IBIs → HRV dead end).

### Tier 3 — Wrong tool for this job
- **Adaptive filter (LMS/NLMS/RLS)** — solves motion you don't have.
- **SPECMAR** — no IBIs.
- **TROIKA/JOSS** — not real-time on ESP32.
- **EMD/EEMD/VMD** — heavy, marginal gains.
- **Wavelet/CWT** — too heavy, no rest-time win.
- **Deep learning (Q-PPG etc.)** — locked to human training data; cross-species pivot kills it.
- **Multi-wavelength fusion** — overkill at rest, hardware complexity.

---

## Part 3 — Recommended Pipeline

```
[Sensor abstraction]
  MAX30102 (IR) ──┐
  MPU6886 (Z)  ───┤── select per session (PPG primary, BCG fallback for furred regions)
                  ↓
[Preprocessing — parameterized by Subject config]
  DC removal (moving avg, window from config)
  Bandpass 0.5–8 Hz (cutoffs from config)
  Normalize
                  ↓
[Peak detection — adaptive, morphology-agnostic]
  Elgendi TERMA (window sizes from config)
  Onset-based fiducial (1st-derivative max) for HRV-grade timing
                  ↓
[IBI cleanup — parameterized]
  Plausibility filter (range from config)
  Kalman / median smoother
                  ↓
[Output]
  HR (mean over window)
  IBI series (for HRV)
```

### MAX30102 specifics
- Use **IR (channel 2)** for HR. Red is for SpO2.
- Sample at 100–200 Hz. Cardiac band is <8 Hz; oversampling beyond ~250 Hz hurts I2C bandwidth without HRV benefit (Peláez-Coca 2021).

---

## Part 4 — Cross-Species Extensibility

### Layer 1 — Algorithm (free if parameterized)
The pipeline is species-agnostic. Only numbers change. Build a `Subject` config struct.

| Parameter | Human (rest) | Dog (rest) | Cat (rest) |
|---|---|---|---|
| HR range (bpm) | 50–100 | 60–140 | 120–220 |
| HR search bound | 40–200 | 40–220 | 60–250 |
| IBI plausibility (ms) | 300–1500 | 270–1000 | 240–500 |
| Bandpass low (Hz) | 0.5 | 0.5 | 0.7 |
| Bandpass high (Hz) | 5 | 6 | 8 |
| Sample rate (Hz) | 100 | 200 | 250 |

### Layer 2 — Hardware (the harder problem)
MAX30102 is reflective optical and assumes contact with hairless skin. For pets, viable PPG sites:
1. **Ear pinna (clip mount)** — sparsely furred, vet-standard PPG site. Works on dogs and cats. Requires mechanical clip, not stick face.
2. **Paw pad** — hairless but motion-prone, pets won't tolerate sustained contact.
3. **Shaved chest/inner thigh patch** — works but invasive for non-clinical product.
4. **Tongue** — clinical only (anesthetized).

**Architectural rule:** put the MAX30102 on a **flexible cable / detachable probe**, not rigid to the M5StickC body. Finger probe (human) ↔ ear clip (pet) without re-firmwaring.

### Layer 3 — Don't abandon BCG
For furred regions, BCG via MPU6886 remains the right answer (original reason BCG was chosen for dogs). Keep both pipelines:
- **PPG path** (MAX30102) — primary when probe is on hairless skin (human finger, dog ear pinna).
- **BCG path** (MPU6886) — fallback for fur, or chest-strap mounting.
- Pick the higher-SNR path per session, or fuse IBIs from both.

### Extensibility traps to avoid
- Deep learning trained on human-only data — kills cross-species.
- Hardcoded HR ranges in firmware.
- Hardcoded fiducial windows (e.g., "search 100 ms after onset") — these scale with HR.
- Peak-detection thresholds tuned to human pulse amplitude — pet PPG amplitudes differ. Use adaptive thresholding (TERMA).

---

## Part 5 — ELI15 Walkthrough

Imagine you're at a noisy party trying to count how often your friend taps their foot. You can't see the tap, but you can feel the floor vibrate. Music, walking, the fridge — all that noise is mixed in with the tap. Your job is to filter it out and count the taps. The "tap" is your heartbeat. The "floor" is your finger. The MAX30102 is the sensor feeling the vibrations.

### Step 0 — The sensor sees light bouncing back
The MAX30102 shines an infrared LED into your finger. Every heartbeat pumps blood through the fingertip arteries. More blood = more light absorbed = less light bouncing back. The sensor reads the light level **200 times per second**, giving a stream like `1832, 1834, 1841, 1855, 1872, 1889...`. Plotted, you'd see a wave going up and down — but with slow drift and jitter. We want **just the heartbeat wave**.

### Step 1 — Remove the slow drift (DC removal)
The signal has a baseline that wanders slowly (finger movement, breathing, LED warming up). Imagine the ocean: waves are heartbeats, the tide is the drift. We don't care about the tide.

**Trick:** for each sample, calculate the average of the last 1 second and subtract it. Slow drift averages out, fast wiggles survive. Now the signal hovers around zero with neat up-down bumps for each beat.

### Step 2 — Throw away frequencies we don't care about (bandpass filter)
Heartbeats happen at ~1–3 Hz. The sensor also picks up breathing (~0.2 Hz, slower) and electrical noise (50/60 Hz, faster).

**Trick:** A bandpass filter is like a bouncer who only lets in frequencies between ~0.5 Hz and ~8 Hz. Slower stuff (breathing) and faster stuff (electrical noise) get rejected. What survives is mostly the heartbeat.

### Step 3 — Find each heartbeat (peak detection)
Now we have clean bumps and need to mark each one. Sounds easy — "find the highest points!" — but some bumps are smaller than others, and there's a tiny secondary bump (dicrotic notch — a heart valve closing) you don't want to miscount.

**Elgendi TERMA trick:** calculate two moving averages:
- A **short** one (~100 ms) that tracks the heartbeat bumps closely
- A **long** one (~600 ms) that smooths over a whole beat cycle

Whenever short > long, you're inside a beat — mark the peak. When short < long, you're between beats. It self-adjusts to the person's signal strength, so it works on different fingers, different people, different species.

### Step 4 — Pick the exact moment of the beat (fiducial point)
For just measuring HR, the peak is fine. For HRV (future), you need very precise timestamps — small errors mess up HRV.

The **steepest upstroke** (where the signal rises fastest) is more reliable than the peak. Peaks can be flat or jittery; the steep climb is sharp every beat.

**Trick:** take the derivative (rate of change) and find where *that* is maximum. That's the official moment of the beat.

### Step 5 — Reject impossible beats (sanity check)
A noise spike might fool the detector into a "beat" 100 ms after the previous (= 600 bpm, impossible).

**Trick:** Real beats are 300–1500 ms apart for humans (40–200 bpm). Anything outside — throw out. For dogs: 270–1000 ms. Cats: 240–500 ms. (This is why we keep these in a config file, not hardcoded.)

### Step 6 — Compute heart rate
Beat timestamps: `t = 0.81 s, 1.62 s, 2.45 s, 3.28 s, 4.10 s, ...`
Gaps (IBIs): `0.81, 0.83, 0.83, 0.82, ...`
Average ≈ 0.82 s → **HR = 60 / 0.82 ≈ 73 bpm.**

### Step 7 — (Future) HRV
HRV is the *variation* in those gaps. A healthy heart isn't a metronome — gaps slightly vary (0.81, 0.83, 0.79, 0.85, 0.82). That variation tells you about stress, fitness, recovery.

Statistics over a few minutes:
- **RMSSD** — how much each gap differs from the next (high = relaxed)
- **SDNN** — overall standard deviation
- **pNN50** — % of gaps differing from the previous by > 50 ms

Pipeline above already gives raw beat timestamps. HRV is just math on those.

### Why this is extensible
Swap human finger for dog ear:
- Steps 0, 3, 4, 6 — same code
- Steps 1, 2, 5 — same algorithm, different *numbers* (config file)

**Code stays the same. Only a config file changes.** Like difficulty settings in a video game.

### TL;DR
Smooth → keep only heartbeat-frequency wiggles → mark each bump → check bumps make sense → count them. Same recipe for human, dog, or cat — just turn the dials differently.

---

## Citations (verified)

- Elgendi 2013 — *Systolic peak detection in acceleration photoplethysmograms.* PLoS ONE 8(10): e76585. DOI: 10.1371/journal.pone.0076585.
- Zhang TROIKA 2014/2015 — IEEE Trans. Biomed. Eng. 62(2): 522–531. arXiv:1409.5181.
- Zhang JOSS 2015 — IEEE Trans. Biomed. Eng. 62(8): 1902–1910. arXiv:1503.00688.
- Reiss DeepPPG 2019 — Sensors 19(14): 3079. DOI: 10.3390/s19143079.
- Burrello Q-PPG 2021 — IEEE Trans. Biomed. Circuits Syst. arXiv:2203.14907.
- Islam SPECMAR 2019 — Med. Biol. Eng. Comput. (Springer).
- Temko WFPV 2017 — IEEE Trans. Biomed. Eng. (MAE 1.97 bpm online on IEEE SPC).
- Peralta et al. 2019 — *Optimal fiducial points for pulse rate variability analysis.* Physiological Measurement.
- Peláez-Coca et al. 2021 — *Impact of PPG sampling rate on PRV indices.* IEEE J. Biomed. Health Inform.
- Schmidt et al. 2018 — *Introducing WESAD.* ACM ICMI.
- Pimentel et al. 2016 — *Robust estimation of respiratory rate from pulse oximeters.* IEEE TBME.
