# Step Counter — Design, Debugging & Context

Real-time step counting from the **same BMI330 accelerometer stream** that drives
heart rate. A parallel thread (`StepWorker`) reads the shared `RingBuffer`, so
there is **no firmware change and no extra sensor traffic** — cardiac energy
(5–25 Hz) and gait energy (≲3 Hz) live in disjoint bands and coexist on one stream.

This doc captures the full context: how it's wired in, the bugs we hit, the
research that drove the fix, the final parameters, and how to validate it.

---

## 1. Architecture — how it's wired in

Three daemon threads share one lock-protected ring buffer:

```
BMI330 → USB CDC → receiver thread ──┐
                                      ├──> RingBuffer (60 s @ 200 Hz, lock-protected)
                  HR worker thread ──┤        │  .latest(n) returns a COPY
                  Step worker thread ─┘        ▼
                              UI QTimer @ 10 Hz reads worker.latest snapshots
```

Key files:
- `pc/step_worker.py` — the `StepWorker` class and algorithm.
- `pc/app.py` — starts the `step-worker` daemon thread (`StepWorker(ring=ring)`),
  shows `Steps: N` + activity/cadence labels (color: gray=still, green=walking,
  red=running), and calls `step_worker.reset()` when a recording is armed so the
  count starts fresh per session.
- `pc/analyze_steps.py` — **offline** re-scoring of any recorded session.

Sharing is **read-only**: `StepWorker` only calls `ring.latest()`, which returns a
locked copy, so there is no data race with the receiver's `append()`.

Per-window pipeline: window = `WINDOW_S` (4 s), hop = `STEP_S` (1 s).

---

## 2. The algorithm (Brajdic & Harle, UbiComp 2013)

We follow **Brajdic & Harle, "Walk detection and step counting on unconstrained
smartphones" (UbiComp 2013)** — a comparison of WD/SC methods over **27 subjects
× 130 walks × 6 device placements**. Their most accurate, robust combination
(**<3 % error**) is:

- **Walk Detection (WD):** standard-deviation thresholding.
- **Step Counting (SC):** Windowed Peak Detection (WPD).

Crucially the paper found **"only the back trouser pocket degrades step counting
significantly."** Hand-held is *not* a problem placement — so our in-hand case
should reach <3 % error with the right parameters. Our original bug was the
**algorithm**, not the mount.

### Per-window steps
1. **Magnitude** `|a| = √(ax² + ay² + az²)`. Gravity contributes a constant ~1 g
   to the magnitude regardless of orientation, so a **mean-subtract** removes it —
   no orientation tracking needed.
2. **Walk Detection (std-dev threshold):** if `std(|a|` over the trailing
   `STD_WIN_S`) `< STD_WALK_THRESH_G`, there is not enough motion to be gait →
   emit **zero** steps and mark `"still"`.
3. **Step Counting (WPD):** moving-average smooth at `MOVAVG_WIN_S` (this *is* the
   low-pass — cutoff ≈ `1/MOVAVG_WIN_S` ≈ 3 Hz, matching the paper's walk band),
   then `find_peaks` with minimum separation `PEAK_WIN_S`. The 0.59 s separation
   structurally admits **at most one peak per step**.
4. **Cross-window de-dup + cadence tracking** (our addition — the paper counts
   offline over a whole trace; we count cumulatively across overlapping live
   windows). Uses the ring's **monotonic sample index** as a jitter-free clock.

---

## 3. The bugs we hit (and root causes)

All reproduced against real recordings, then fixed.

### Bug A — counter latched at "1 step", activity stuck on "still"
**Symptom:** walking with the accel in hand; counter froze at 1, never left "still".

**Three interacting defects (original code):**
1. **Anchor-freeze (core):** when a peak's gap exceeded `MAX_STEP_GAP_S`, the code
   did `continue` *without advancing* `_last_step_t_unix`. The anchor stayed
   frozen, so every later peak measured an ever-growing gap → permanently rejected.
   The counter welded at 1.
2. **"still" lock-in:** cadence needed `len(_recent_gaps) >= 2`, but the gap list
   only grew on a *surviving* peak — which Defect 1 prevented. So cadence stayed
   `NaN` → activity stayed `"still"` forever. (Same bug, two symptoms.)
3. **Wall-clock `t_peak`:** gaps were computed from `time.time()` at processing
   time, not the sample's own time. Thread jitter / skipped windows inflated gaps
   past 2 s even during clean walking — which is what *triggered* Defect 1.

**Why hand-held guaranteed it:** arm-swing peaks are ~1–2 s apart and irregular,
so the first inter-step gap easily exceeded `MAX_STEP_GAP_S`, springing the trap.

### Bug B — overcounting while walking + phantom steps while standing still
Reproduced on `session_20260607T125446Z` (walk 0–22 s, stop 22–36 s): the counter
climbed to **57**, falsely read "running" (150+ spm) while walking, and **kept
counting (+16) while standing still.**

**Root cause — a purely *relative* threshold with no absolute basis:**
- *Overcounting:* real peak gaps were `[0.39, 0.30, 0.41, 0.30, …]` s. The 0.30 s
  gaps are the **secondary** toe-off / arm-counterswing bump ~0.3 s after each heel
  strike. `MIN_STEP_GAP_S = 0.25 s` let both through → ~2 counts per step.
- *Counting while still:* the height threshold was `0.5 × RMS` with **no floor**.
  When you stop, RMS collapses (`0.14 g → 0.04 g`) and the threshold adapts *down*
  into the noise floor → noise peaks counted as steps.

**Literature confirms the cause:** a fixed *amplitude* threshold is explicitly
discredited (Brajdic & Harle; also `danielmurray/adaptiv`: *"a static threshold is
not a reliable method"*). The robust answer is a **std-dev WD gate** (energy/
variance, generalises across subjects) + **WPD timing** (cadence is a human
constant), which is what we adopted.

---

## 4. Final parameters — all paper-defined

Every constant in the step counter is now a **published optimum** (no hand-tuning).

| Parameter (code) | Paper value | In our units (g, s) | Source |
|---|---|---|---|
| `MOVAVG_WIN_S` | `MovAvr_win = 0.31 s` | 0.31 s | SC table (WPD) |
| `PEAK_WIN_S` | `Peak_win = 0.59 s` | 0.59 s | SC table (WPD) |
| `STD_WALK_THRESH_G` | `σthresh = 0.6` (m/s²) | **0.0612 g** | WD table (STD TH) |
| `STD_WIN_S` | `stdwin = 0.8 s` | 0.8 s | WD table (STD TH) |

**Units conversion (important):** the paper's accelerometers report in **m/s²** —
verifiable from the same table, where `MAGN TH magnthresh = 10.5` only makes sense
as 10.5 m/s² (just above gravity, 9.81). So `σthresh = 0.6 m/s²` → `0.6 / 9.80665
= 0.0612 g`. (A hand-tuned guess of 0.06 g had landed within 2 % by coincidence;
it's now the *derived* value.) The WPD **time** windows transfer cleanly because
gait cadence is a human constant, not a device property — only the amplitude-unit
threshold needed conversion.

Cadence/real-time-tracking params (our addition, not in the paper):
`MIN_STEP_GAP_S = 0.40 s`, `MAX_STEP_GAP_S = 2.0 s`, `CADENCE_HISTORY = 6`.

### Known limitation
`PEAK_WIN_S = 0.59 s` caps detection at **~100 steps/min** — tuned for **walking**.
**Running (150–180 spm) will be undercounted.** Lower `PEAK_WIN_S` to ~0.33 s if
you need to count running (this diverges from the paper's walking optimum).

---

## 5. Validation status

Replaying `session_20260607T125446Z` (walk-then-stop) through the final algorithm:
- Walking 0–22 s: cadence 53–80 spm, "walking" (was falsely 150+ "running").
- Still 22–36 s: count frozen, "still" (was climbing +16 phantom steps).
- **Total: 26 steps.**

**Open item:** 26 is *grounded in the paper* but **not yet verified against a known
count** for this rig/gait. Nothing left to tune — only to verify. WPD with the
0.59 s window biases toward *undercounting* a brisk walk, so if a counted walk
shows consistent undercount, that points specifically at `PEAK_WIN_S` clipping
cadence (do not touch it until ground truth says so).

**To validate:** walk a counted number of steps, record (press Space, close the
window cleanly — do **not** Ctrl-C, or the final Parquet chunk won't flush), then:
```bash
python3 analyze_steps.py --latest --truth <your count>
```
If error ≤ ~3 %, the counter is validated for your gait.

---

## 6. Offline analyzer — `pc/analyze_steps.py`

Re-scores any session by replaying its IMU Parquet through the **same StepWorker**
the live UI uses, so the offline count equals what the app produced (no drift, by
construction). The raw IMU Parquet is the source of truth — any past or future
session can be re-scored.

```bash
python3 analyze_steps.py --latest                 # newest session with data
python3 analyze_steps.py session_20260607T125446Z # specific session (name or path)
python3 analyze_steps.py --latest --truth 30      # compare to a counted walk
python3 analyze_steps.py --latest --csv           # also write <session>/steps.csv
```

Prints a per-second table (time, cumulative steps, cadence, activity), total
steps, average cadence, and — with `--truth` — signed + percent error. Avoids the
`openpyxl` dependency that `analyze_session.py` (HR) needs.

---

## 7. Gotchas worth remembering

- **Empty sessions:** a session dir with only `session.json` and no `imu_*.parquet`
  means recording was armed but no samples were written while armed — either
  Record was never pressed during the activity (live preview ≠ recording), or the
  app was hard-killed (Ctrl-C) so the writer's buffered chunk never flushed. Close
  the window cleanly to flush.
- **200 Hz assumption:** the worker uses `fs = protocol.SAMPLE_RATE_HZ` (200). The
  current `hr_streamer_bmi330.ino` emits exactly 200 Hz via a 5 ms gate; an older
  firmware ran at 170.68 Hz, which would scale all gaps/cadence ~17 % — a silent
  failure mode if firmware regresses.
- **`reset()` on arm:** step count restarts per recording session (wired in
  `app.py::_toggle_recording`), not per app run.

## Reference
Brajdic, A. & Harle, R. *Walk detection and step counting on unconstrained
smartphones.* UbiComp 2013. https://doi.org/10.1145/2493432.2493449
