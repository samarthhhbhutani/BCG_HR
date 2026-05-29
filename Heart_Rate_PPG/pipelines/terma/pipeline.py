"""TERMA pipeline (this project's existing detector, ported offline).

Pulled from pc/hr_worker.py — same TERMA + onset refinement + Pan-Tompkins
slope-ratio gate, but operating on a whole array rather than ring-buffer
windows. The cleaner is whatever the caller passes in (benchmark uses BP 0.5-8
Hz to match the other detectors), so the BP cutoff is decoupled from this file.

References:
  - Elgendi 2013 PLoS ONE                      — TERMA candidate detector
  - Pan & Tompkins 1985 IEEE TBME              — slope-ratio (50 %) rejection
  - Peralta 2019 Physiol. Meas.                — onset (d/dt max) fiducial
"""
from __future__ import annotations

import numpy as np

from ..base import BasePipeline


TERMA_W1_MS = 111.0   # short MA — width of systolic peak (Elgendi 2013)
TERMA_W2_MS = 667.0   # long MA — width of one beat cycle
TERMA_BETA = 0.02


def _moving_average(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1:
        return x.copy()
    win = min(win, len(x))
    kernel = np.ones(win, dtype=np.float64) / win
    return np.convolve(x, kernel, mode="same")


def _terma_peaks(x: np.ndarray, fs: float) -> np.ndarray:
    w1 = max(3, int(fs * TERMA_W1_MS / 1000.0))
    w2 = max(w1 + 2, int(fs * TERMA_W2_MS / 1000.0))
    if len(x) < w2 + 2:
        return np.array([], dtype=int)

    z = np.clip(x, 0, None) ** 2
    ma_peak = _moving_average(z, w1)
    ma_beat = _moving_average(z, w2)
    threshold = ma_beat + TERMA_BETA * float(np.mean(ma_beat))

    blocks = ma_peak > threshold
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
    if len(peaks) < 2:
        return peaks
    accepted: list[int] = [int(peaks[0])]
    last_slope = _slope_at_peak(x, accepted[0], fs)
    rr_running_samples = 0.0
    for p in peaks[1:]:
        p = int(p)
        prev = accepted[-1]
        gap = p - prev
        if len(accepted) >= 2:
            rrs = np.diff(np.asarray(accepted, dtype=np.float64))
            rr_running_samples = float(np.mean(rrs))
        elif rr_running_samples == 0.0:
            rr_running_samples = 0.6 * fs
        slope = _slope_at_peak(x, p, fs)
        is_near = gap < near_window_factor * rr_running_samples
        is_low_slope = slope < slope_ratio * last_slope if last_slope > 0 else False
        if is_near and is_low_slope:
            continue
        accepted.append(p)
        last_slope = slope
    return np.asarray(accepted, dtype=int)


class TermaPipeline(BasePipeline):
    name = "terma"

    # TERMA + slope-ratio gate is tuned for a narrower 0.5–4 Hz band (see
    # subject_config.HUMAN). At 0.5–8 Hz the dicrotic notch leaks through and
    # the slope gate rejects too many real systolic peaks.
    bandpass_low_hz = 0.5
    bandpass_high_hz = 4.0

    # TERMA's adaptive threshold is mean(ma_beat) over the *entire* signal —
    # one finger-off transient (IR drops 100x) inflates the threshold and most
    # real peaks get missed. Live hr_worker.py avoids this by operating on 8 s
    # windows; here we window at 30 s (long enough for stable threshold,
    # short enough that one transient can't poison the rest).
    window_s = 30.0

    def detect(self, cleaned: np.ndarray, fs: float) -> np.ndarray:
        n = len(cleaned)
        if n == 0:
            return np.array([], dtype=int)
        win = max(int(self.window_s * fs), int(8 * fs))
        if n <= win:
            return self._detect_window(cleaned, fs)
        out: list[int] = []
        for start in range(0, n, win):
            end = min(start + win, n)
            if end - start < int(4 * fs):
                break
            seg = cleaned[start:end]
            seg_peaks = self._detect_window(seg, fs) + start
            out.extend(int(p) for p in seg_peaks)
        return np.unique(np.asarray(out, dtype=int))

    @staticmethod
    def _detect_window(x: np.ndarray, fs: float) -> np.ndarray:
        peaks = _terma_peaks(x, fs)
        peaks = _refine_to_onset(x, peaks, fs)
        peaks = _slope_ratio_gate(x, peaks, fs)
        return peaks
