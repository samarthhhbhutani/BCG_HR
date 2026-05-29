"""Real-time HR estimator for PPG (MAX30102 IR channel).

Pipeline:
  1. Pull last WINDOW_S of IR samples from the ring buffer.
  2. Finger-presence check (raw IR DC + AC amplitude).
  3. DC removal (subtract 1 s moving average).
  4. Band-pass (cutoffs from SubjectConfig; Butterworth 4, sosfiltfilt).
  5. Elgendi TERMA peak detection — candidate beats.
  6. Onset-based fiducial refinement — move each peak to local d/dt max.
  7. Pan-Tompkins slope-ratio gate — reject dicrotic notch candidates whose
     upstroke slope is < 50 % of the previous accepted peak (and which fall
     within 1.5 × running mean RR of it). Adapts to any HR.
  8. Subject-config IBI sanity bounds — generous floor/ceiling.
  9. HeartPy RR-band filter — reject IBIs outside mean ± max(30 %, 300 ms).
     Catches dicrotic doubles the slope gate missed.
 10. HR = 60 000 / median(IBI).

References:
  - Elgendi 2013 PLoS ONE — TERMA candidate detector.
  - Pan & Tompkins 1985 IEEE TBME — T-wave slope-ratio rejection (50 % rule),
    ported from ECG to PPG since the dicrotic upstroke is consistently
    shallower than the systolic upstroke.
  - Peralta 2019 Physiol. Meas. — onset (1st-derivative max) is the stable
    fiducial point for PRV/HRV.
  - van Gent et al. 2019 (HeartPy) — RR-band filter mean ± max(30 %, 300 ms).
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.signal import butter, sosfiltfilt

import protocol
from ringbuffer import RingBuffer, COL_IR
from subject_config import SubjectConfig

WINDOW_S = 8.0
STEP_S = 1.0
DC_WINDOW_S = 1.0       # moving-average window for DC removal
TERMA_W1_MS = 111.0     # short MA — width of systolic peak (Elgendi 2013)
TERMA_W2_MS = 667.0     # long MA — width of one beat cycle
TERMA_BETA = 0.02       # offset for adaptive threshold


@dataclass(slots=True)
class HRUpdate:
    t_unix: float
    hr_bpm: float                 # NaN if not estimable
    confident: bool
    finger_present: bool
    ir_dc: float
    ac_amplitude: float
    n_beats: int
    ibis_ms: np.ndarray           # filtered, plausible IBIs in ms
    raw_ir: np.ndarray            # last window, for plot pane 1
    filtered: np.ndarray          # band-passed IR, for plot pane 2
    peak_indices: np.ndarray      # in-window indices of detected beats


def _design_bandpass(fs: float, low_hz: float, high_hz: float):
    nyq = 0.5 * fs
    return butter(4, [low_hz / nyq, high_hz / nyq], btype="band", output="sos")


def _moving_average(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1:
        return x.copy()
    win = min(win, len(x))
    kernel = np.ones(win, dtype=np.float64) / win
    # 'same' length, edges are smoothed against zero-pad — ok for visualization
    # and TERMA threshold purposes; the active region is the interior.
    return np.convolve(x, kernel, mode="same")


def _terma_peaks(x: np.ndarray, fs: float) -> np.ndarray:
    """Elgendi 2013 TERMA. Returns peak indices into x.

    Two moving averages: short (peak-width) and long (beat-cycle). Inside
    each window where MA_short > MA_long + threshold, take the argmax of x.
    """
    w1 = max(3, int(fs * TERMA_W1_MS / 1000.0))
    w2 = max(w1 + 2, int(fs * TERMA_W2_MS / 1000.0))
    if len(x) < w2 + 2:
        return np.array([], dtype=int)

    # Square the signal to emphasise systolic upstroke energy (Elgendi step).
    z = np.clip(x, 0, None) ** 2
    ma_peak = _moving_average(z, w1)
    ma_beat = _moving_average(z, w2)
    threshold = ma_beat + TERMA_BETA * float(np.mean(ma_beat))

    blocks = ma_peak > threshold
    # Reject blocks shorter than w1 (likely noise, not real beats).
    peaks = []
    i, n = 0, len(blocks)
    while i < n:
        if not blocks[i]:
            i += 1
            continue
        j = i
        while j < n and blocks[j]:
            j += 1
        if (j - i) >= w1:
            seg = x[i:j]
            peaks.append(i + int(np.argmax(seg)))
        i = j
    return np.array(peaks, dtype=int)


def _refine_to_onset(x: np.ndarray, peaks: np.ndarray, fs: float) -> np.ndarray:
    """Move each peak back to the local 1st-derivative max (steepest upstroke).

    More stable fiducial point for HRV than the peak itself (Peralta 2019).
    Searches in a window of 200 ms preceding each peak.
    """
    if len(peaks) == 0:
        return peaks
    dx = np.gradient(x)
    look_back = max(2, int(0.2 * fs))
    out = np.empty_like(peaks)
    for i, p in enumerate(peaks):
        a = max(0, p - look_back)
        seg = dx[a:p + 1]
        if len(seg) == 0:
            out[i] = p
            continue
        out[i] = a + int(np.argmax(seg))
    return out


def _slope_at_peak(x: np.ndarray, peak_idx: int, fs: float) -> float:
    """Max first-derivative in the 200 ms window ending at peak_idx.

    The systolic upstroke has consistently larger d/dt than the dicrotic
    upstroke (Pan & Tompkins 1985 — ported from ECG T-wave logic).
    """
    look_back = max(2, int(0.2 * fs))
    a = max(0, peak_idx - look_back)
    seg = x[a:peak_idx + 1]
    if len(seg) < 2:
        return 0.0
    return float(np.max(np.diff(seg)))


def _slope_ratio_gate(
    x: np.ndarray, peaks: np.ndarray, fs: float,
    slope_ratio: float = 0.5, near_window_factor: float = 1.5,
) -> np.ndarray:
    """Pan-Tompkins T-wave check, ported to PPG.

    For each candidate, if it falls within near_window_factor * running mean
    RR of the previous accepted peak AND its upstroke slope is less than
    slope_ratio of the previous accepted peak's slope, reject as a dicrotic
    notch. The 0.5 ratio is verbatim from Pan & Tompkins 1985.

    Rationale: dicrotic-notch upstrokes are consistently <50 % as steep as
    systolic upstrokes, regardless of HR. Adapts to any rate.
    """
    if len(peaks) < 2:
        return peaks
    accepted: list[int] = [int(peaks[0])]
    last_slope = _slope_at_peak(x, accepted[0], fs)
    rr_running_samples = 0.0  # in sample counts
    for p in peaks[1:]:
        p = int(p)
        prev = accepted[-1]
        gap = p - prev
        # Build running mean RR from accepted peaks.
        if len(accepted) >= 2:
            rrs = np.diff(np.asarray(accepted, dtype=np.float64))
            rr_running_samples = float(np.mean(rrs))
        elif rr_running_samples == 0.0:
            # No history yet — fall back to "near" = 0.6 s window.
            rr_running_samples = 0.6 * fs

        slope = _slope_at_peak(x, p, fs)
        is_near = gap < near_window_factor * rr_running_samples
        is_low_slope = slope < slope_ratio * last_slope if last_slope > 0 else False
        if is_near and is_low_slope:
            continue  # dicrotic notch — reject
        accepted.append(p)
        last_slope = slope
    return np.asarray(accepted, dtype=int)


def _filter_ibis(ibis_ms: np.ndarray, cfg: SubjectConfig) -> np.ndarray:
    """Subject-config IBI sanity bounds. Generous floor/ceiling — the real
    work of rejecting dicrotic doubles is done by _slope_ratio_gate and
    _heartpy_rr_filter."""
    return ibis_ms[(ibis_ms >= cfg.ibi_min_ms) & (ibis_ms <= cfg.ibi_max_ms)]


def _heartpy_rr_filter(
    ibis_ms: np.ndarray, frac: float = 0.30, abs_floor_ms: float = 300.0,
    max_passes: int = 3,
) -> np.ndarray:
    """HeartPy-style RR-band filter (van Gent 2019).

    Reject any IBI outside mean(IBI) ± max(frac * mean(IBI), abs_floor_ms).
    Iterate up to max_passes times so a few bad RRs cannot inflate the mean
    enough to mask others.
    """
    if len(ibis_ms) == 0:
        return ibis_ms
    cur = ibis_ms
    for _ in range(max_passes):
        if len(cur) < 2:
            return cur
        m = float(np.mean(cur))
        tol = max(frac * m, abs_floor_ms)
        ok = (cur >= m - tol) & (cur <= m + tol)
        nxt = cur[ok]
        if len(nxt) == len(cur):
            return nxt
        cur = nxt
    return cur


class HRWorker:
    def __init__(
        self,
        ring: RingBuffer,
        cfg: SubjectConfig,
        on_update: Callable[[HRUpdate], None] | None = None,
        fs: float = float(protocol.SAMPLE_RATE_HZ),
    ):
        self.ring = ring
        self.cfg = cfg
        self.on_update = on_update
        self.fs = fs
        self._stop = threading.Event()
        self._sos = _design_bandpass(fs, cfg.bandpass_low_hz, cfg.bandpass_high_hz)
        self._latest: HRUpdate | None = None
        self._lock = threading.Lock()

    @property
    def latest(self) -> HRUpdate | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        win_n = int(WINDOW_S * self.fs)
        while not self._stop.is_set():
            time.sleep(STEP_S)
            data = self.ring.latest(win_n)
            if data.shape[0] < win_n:
                continue
            ir = data[:, COL_IR].astype(np.float64)
            update = self._process(ir)
            with self._lock:
                self._latest = update
            if self.on_update:
                try:
                    self.on_update(update)
                except Exception as e:
                    print(f"on_update error: {e!r}")

    def _process(self, ir: np.ndarray) -> HRUpdate:
        ir_dc = float(np.mean(ir))
        ir_min = float(np.min(ir))
        ir_max = float(np.max(ir))

        # DC-removed signal for the rest of the pipeline.
        dc_win = max(1, int(DC_WINDOW_S * self.fs))
        baseline = _moving_average(ir, dc_win)
        ac = ir - baseline

        # Band-pass.
        filt = sosfiltfilt(self._sos, ac)
        ac_amp = float(np.max(filt) - np.min(filt))

        # Finger-presence: stricter than mean(IR). Any sample below contact_ir_min
        # within the window means the finger came off mid-window, which leaves
        # huge contact transients in the band-pass that fool any peak detector.
        # We require the *minimum* IR (not just the mean) to clear the threshold.
        finger_present = (
            ir_min >= self.cfg.contact_ir_min
            and ir_max <= self.cfg.contact_ir_max
            and ac_amp >= self.cfg.contact_ac_min
        )

        if not finger_present:
            return HRUpdate(
                t_unix=time.time(),
                hr_bpm=float("nan"),
                confident=False,
                finger_present=False,
                ir_dc=ir_dc,
                ac_amplitude=ac_amp,
                n_beats=0,
                ibis_ms=np.array([]),
                raw_ir=ir,
                filtered=filt,
                peak_indices=np.array([], dtype=int),
            )

        # Peak detection on the band-passed signal.
        # MAX30102 IR convention: more blood → less light → smaller IR. After
        # band-pass + DC removal, systolic peaks point DOWN. Flip so TERMA
        # works on positive systolic peaks.
        x = -filt
        peaks = _terma_peaks(x, self.fs)
        peaks = _refine_to_onset(x, peaks, self.fs)
        # Pan-Tompkins slope-ratio gate: rejects dicrotic notches whose
        # upstroke slope is < 50 % of the previous accepted peak's slope.
        peaks = _slope_ratio_gate(x, peaks, self.fs)

        if len(peaks) < 3:
            return HRUpdate(
                t_unix=time.time(),
                hr_bpm=float("nan"),
                confident=False,
                finger_present=True,
                ir_dc=ir_dc,
                ac_amplitude=ac_amp,
                n_beats=int(len(peaks)),
                ibis_ms=np.array([]),
                raw_ir=ir,
                filtered=filt,
                peak_indices=peaks,
            )

        ibis_ms = np.diff(peaks) / self.fs * 1000.0
        ibis_ok = _filter_ibis(ibis_ms, self.cfg)
        # HeartPy RR-band filter: catches dicrotic doubles the slope gate
        # missed (single short RR followed by single long RR fall outside the
        # mean ± 30 % band; both get dropped).
        ibis_ok = _heartpy_rr_filter(ibis_ok)

        if len(ibis_ok) < 2:
            return HRUpdate(
                t_unix=time.time(),
                hr_bpm=float("nan"),
                confident=False,
                finger_present=True,
                ir_dc=ir_dc,
                ac_amplitude=ac_amp,
                n_beats=int(len(peaks)),
                ibis_ms=ibis_ok,
                raw_ir=ir,
                filtered=filt,
                peak_indices=peaks,
            )

        hr_bpm = 60_000.0 / float(np.median(ibis_ok))
        # Confidence: at least 4 plausible IBIs AND CV < 25 % (rest sinus rhythm
        # is fairly steady; high CV usually means missed/extra peaks).
        cv = float(np.std(ibis_ok) / np.mean(ibis_ok)) if np.mean(ibis_ok) > 0 else 1.0
        confident = (len(ibis_ok) >= 4) and (cv < 0.25)

        return HRUpdate(
            t_unix=time.time(),
            hr_bpm=hr_bpm,
            confident=confident,
            finger_present=True,
            ir_dc=ir_dc,
            ac_amplitude=ac_amp,
            n_beats=int(len(peaks)),
            ibis_ms=ibis_ok,
            raw_ir=ir,
            filtered=filt,
            peak_indices=peaks,
        )
