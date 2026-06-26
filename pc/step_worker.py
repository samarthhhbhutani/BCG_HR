"""Real-time step counter from the same ring buffer feeding the HR worker.

Algorithm follows Brajdic & Harle, "Walk detection and step counting on
unconstrained smartphones" (UbiComp 2013), which compared WD/SC methods over
27 subjects x 6 device placements and found the most accurate, robust combo
(<3% error) to be standard-deviation thresholding for Walk Detection plus
Windowed Peak Detection (WPD) for Step Counting. Their published optimal
parameters: WPD MovAvr_win = 0.31 s, Peak_win = 0.59 s.

Per-window pipeline (window = WINDOW_S, hop = STEP_S):
  1. Magnitude  |a| = sqrt(ax² + ay² + az²). Gravity has constant magnitude
     (1 g) regardless of orientation, so a simple mean-subtract removes it —
     no orientation tracking needed.
  2. Walk Detection: if the window's std-dev < STD_WALK_THRESH_G, there is not
     enough motion to be gait → emit zero steps ("still"). This is the std
     threshold the paper validates; it is the principled replacement for the
     fixed energy gate (a fixed *amplitude* threshold is known to fail across
     subjects/placements, but a std/energy WD gate generalises well).
  3. Step Counting (WPD): moving-average smooth at MovAvr_win (this IS the
     low-pass — cutoff ≈ 1/MovAvr_win ≈ 3 Hz, matching the paper's walk band),
     then peak-detect with min separation Peak_win. The 0.59 s separation
     structurally rejects the secondary toe-off/arm-swing peak (~0.3 s after
     heel strike) that previously caused 2x overcounting.
  4. Cross-window de-dup + cadence/anchor tracking (see _process) so a step is
     counted once even though windows overlap, using the ring's monotonic
     sample index as a jitter-free clock.

NOTE: Peak_win = 0.59 s caps detection at ~100 steps/min — tuned for WALKING.
Running (150-180 spm) will be undercounted; lower PEAK_WIN_S to ~0.33 s if you
need to count running.

This reuses the already-running RingBuffer — no firmware change, no extra
sensor traffic. Cardiac (5-25 Hz) and gait (≲3 Hz) live in disjoint bands.

Outputs an `on_update` callback per step plus a `latest` snapshot containing
total steps, instantaneous cadence (steps/min), and activity state.
"""
from __future__ import annotations
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import find_peaks

import protocol
from ringbuffer import RingBuffer, COL_AX, COL_AY, COL_AZ

WINDOW_S = 4.0          # 4 s window — long enough for ~6-12 steps at typical cadence
STEP_S = 1.0            # update every 1 s

# --- Windowed Peak Detection (WPD), Brajdic & Harle optimal parameters ---
MOVAVG_WIN_S = 0.31     # moving-average smoothing window (acts as ~3 Hz LPF)
PEAK_WIN_S = 0.59       # minimum separation between step peaks (~100 spm ceiling)
PROMINENCE_G = 0.05     # min peak prominence on the smoothed signal (rejects ripple)

# --- Walk Detection (WD): std-dev thresholding (Brajdic & Harle "STD TH") ---
# Paper optimum: σthresh = 0.6, stdwin = 0.8 s. The paper's accelerometer is in
# m/s² (cf. its MAGN TH magnthresh = 10.5, just above gravity 9.81), so the
# threshold converts to our units (g) as 0.6 / 9.80665 = 0.0612 g. Std is
# computed over a trailing stdwin (0.8 s) — the region where this hop's new,
# not-yet-counted peaks live — so counting stops promptly when motion ceases.
# (Independent validation on this rig: walking std ~0.08-0.32 g, still ~0.02-0.05 g.)
STD_WALK_THRESH_G = 0.6 / 9.80665   # = 0.0612 g
STD_WIN_S = 0.8

# --- Cadence tracking (real-time cumulative count across overlapping windows;
# not part of the paper, which counts offline over a whole trace) ---
MIN_STEP_GAP_S = 0.40   # gap below this between accepted peaks = same step's
                        # sub-peak; drop without re-anchoring.
MAX_STEP_GAP_S = 2.0    # gap above this = rhythm lost; re-anchor as a fresh stride.
# A fresh peak is rejected if its gap is < 0.5x or > 2x the running median gap,
# computed over the last CADENCE_HISTORY accepted gaps.
CADENCE_HISTORY = 6


@dataclass(slots=True)
class StepUpdate:
    t_unix: float
    total_steps: int
    cadence_spm: float          # steps per minute, NaN if not estimable
    activity: str               # "still" / "walking" / "running"


def _classify(cadence_spm: float) -> str:
    if np.isnan(cadence_spm):
        return "still"
    if cadence_spm < 40:
        return "still"
    if cadence_spm < 130:
        return "walking"
    return "running"


class StepWorker:
    def __init__(
        self,
        ring: RingBuffer,
        on_update: Callable[[StepUpdate], None] | None = None,
        fs: float = float(protocol.SAMPLE_RATE_HZ),
    ):
        self.ring = ring
        self.on_update = on_update
        self.fs = fs
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: StepUpdate | None = None

        # Persistent state across windows
        self._total_steps = 0
        # Absolute sample-index of the last accepted step. We use the ring's
        # monotonic sample counter as our clock (gap_s = Δidx / fs) instead of
        # time.time(), so inter-step gaps are immune to thread-scheduling jitter
        # and skipped windows. None until the first step is accepted.
        self._last_step_idx: int | None = None
        self._recent_gaps: deque[float] = deque(maxlen=CADENCE_HISTORY)
        # Track the latest sample-index in the ring at which we last accepted
        # a peak. Prevents double-counting when windows overlap.
        self._last_accepted_total_idx: int = 0

    @property
    def latest(self) -> StepUpdate | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        with self._lock:
            self._total_steps = 0
            self._last_step_idx = None
            self._recent_gaps.clear()
            self._last_accepted_total_idx = self.ring.total

    def run(self) -> None:
        win_n = int(WINDOW_S * self.fs)
        while not self._stop.is_set():
            time.sleep(STEP_S)
            data = self.ring.latest(win_n)
            if data.shape[0] < win_n:
                continue
            self._process(data)

    def _emit_idle(self) -> None:
        """Publish a 'still' snapshot: keep the step total, zero the cadence.
        Called when Walk Detection finds too little motion to be gait.
        Also fades the gap history so cadence starts clean when walking resumes.
        """
        self._recent_gaps.clear()
        update = StepUpdate(
            t_unix=time.time(),
            total_steps=self._total_steps,
            cadence_spm=float("nan"),
            activity="still",
        )
        with self._lock:
            self._latest = update
        if self.on_update:
            try:
                self.on_update(update)
            except Exception as e:
                print(f"step on_update error: {e!r}")

    def _process(self, accel_window: np.ndarray) -> None:
        # Magnitude. Gravity contributes a constant ~1 g to |a| regardless of
        # device orientation, so a mean-subtract (below) removes it without any
        # orientation tracking.
        mag = np.sqrt(
            accel_window[:, COL_AX] ** 2
            + accel_window[:, COL_AY] ** 2
            + accel_window[:, COL_AZ] ** 2
        )

        # --- Walk Detection (std-dev threshold, Brajdic & Harle "STD TH") ---
        # Std-dev measures gait-band energy independent of the DC gravity term.
        # Computed over the trailing STD_WIN_S (paper's stdwin) so that when you
        # stop, the gate trips as soon as the most-recent 0.8 s goes quiet,
        # rather than lagging until the whole 4 s window drains. Below threshold
        # = not enough motion to be walking → emit zero steps.
        std_n = max(1, int(STD_WIN_S * self.fs))
        if float(np.std(mag[-std_n:])) < STD_WALK_THRESH_G:
            self._emit_idle()
            return

        # --- Step Counting (Windowed Peak Detection) ---
        # Moving-average smoothing IS the low-pass (cutoff ≈ 1/MovAvr_win ≈ 3 Hz),
        # removing the high-frequency content (incl. the secondary toe-off peak)
        # that caused doubling. Detrend the gravity offset with the window mean.
        movavg_n = max(1, int(MOVAVG_WIN_S * self.fs))
        smooth = uniform_filter1d(mag, size=movavg_n)
        smooth = smooth - smooth.mean()

        # Peak separation = Peak_win (paper optimum) → at most one peak per step.
        peaks, props = find_peaks(
            smooth,
            distance=max(1, int(PEAK_WIN_S * self.fs)),
            prominence=PROMINENCE_G,
        )

        # Convert peak sample-indices into absolute "total samples" indices.
        # The ring buffer's most-recent sample is at index ring.total - 1.
        # We measure inter-step gaps in *samples* (gap_s = Δidx / fs) using this
        # monotonic counter as the clock — NOT time.time() — so the gaps reflect
        # true signal timing and aren't corrupted by thread jitter / skipped
        # windows. `now` is used only as a display timestamp on the snapshot.
        ring_total = self.ring.total
        win_start_total = ring_total - len(accel_window)
        now = time.time()
        for p in peaks:
            abs_total_idx = win_start_total + int(p)
            # Skip peaks we've already accepted in a previous overlapping window
            if abs_total_idx <= self._last_accepted_total_idx:
                continue

            # Cadence consistency check (all gaps in seconds, derived from the
            # monotonic sample index so they're jitter-free).
            if self._last_step_idx is not None:
                gap = (abs_total_idx - self._last_step_idx) / self.fs
                if gap < MIN_STEP_GAP_S:
                    # Too close to count as a separate step — drop it but do NOT
                    # advance the anchor (it's a sub-peak of the current step).
                    continue
                if gap > MAX_STEP_GAP_S:
                    # Rhythm was lost (e.g. a pause). Treat this peak as the
                    # start of a fresh stride: re-anchor and reset cadence
                    # history rather than freezing the old anchor forever.
                    self._recent_gaps.clear()
                    self._total_steps += 1
                    self._last_step_idx = abs_total_idx
                    self._last_accepted_total_idx = abs_total_idx
                    continue
                if self._recent_gaps:
                    median_gap = float(np.median(self._recent_gaps))
                    if gap < 0.5 * median_gap or gap > 2.0 * median_gap:
                        # Inconsistent with recent rhythm — likely false positive.
                        # Allow it to pass anyway if we have very few samples.
                        if len(self._recent_gaps) >= 3:
                            continue
                self._recent_gaps.append(gap)

            self._total_steps += 1
            self._last_step_idx = abs_total_idx
            self._last_accepted_total_idx = abs_total_idx

        # Compute instantaneous cadence from recent gaps. One gap (=2 steps) is
        # enough to show a cadence; we don't need to wait for a third step.
        if len(self._recent_gaps) >= 1:
            cadence = 60.0 / float(np.median(self._recent_gaps))
            # Decay: if no new step for a while, drop cadence toward "still".
            # Idle time is measured in samples from the last accepted step.
            if self._last_step_idx is not None:
                idle_s = (ring_total - self._last_step_idx) / self.fs
                if idle_s > 2.0:
                    cadence = float("nan")
                    # Fade out gap history so a future restart begins clean.
                    if idle_s > 5.0:
                        self._recent_gaps.clear()
        else:
            cadence = float("nan")

        update = StepUpdate(
            t_unix=now,
            total_steps=self._total_steps,
            cadence_spm=cadence,
            activity=_classify(cadence),
        )
        with self._lock:
            self._latest = update
        if self.on_update:
            try:
                self.on_update(update)
            except Exception as e:
                print(f"step on_update error: {e!r}")
