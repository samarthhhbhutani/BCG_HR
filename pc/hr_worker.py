"""Real-time HR estimator: sliding-window band-pass + PCA fusion
+ autocorrelation + envelope-peak fusion.

Reads from a shared RingBuffer; emits HR updates via a callback.

Algorithm (PCA path, current):
  1. Pull last WINDOW_S of accel data.
  2. Detrend per-axis, band-pass 5-25 Hz (Butterworth 4, sosfiltfilt).
  3. SVD on the 3 band-passed axes; project onto PC1 (cardiac direction).
  4. Method A — autocorrelation on PC1: search lag for HR in [HR_MIN, HR_MAX].
  5. Method B — Hilbert envelope + adaptive peak detection (refractory 250 ms).
  6. Fuse: if methods agree within AGREE_BPM, emit mean; else emit best-effort with low-confidence flag.

Older single-axis path (per-axis SNR argmax) is preserved below as commented
code; see docs/session_2026-05-16.md for the rationale for the switch and the
revert recipe.

References:
  Brüser et al., IEEE TITB 15(5), 2011 — autocorrelation BCG HR.
  Brüser et al., Physiol. Meas. 34(2), 2013 — multi-estimator fusion.
  Inan et al., IEEE JBHI 19(4), 2015 — BCG/SCG review.
  Pandia et al., 2012; Di Rienzo et al., 2018 — PCA on tri-axis SCG.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.signal import butter, sosfiltfilt, hilbert, find_peaks, welch

import protocol
from ringbuffer import RingBuffer, COL_AX, COL_AY, COL_AZ

WINDOW_S = 8.0
STEP_S = 1.0
HR_MIN_BPM = 40.0
HR_MAX_BPM = 200.0
BAND_LOW_HZ = 5.0
BAND_HIGH_HZ = 25.0
AGREE_BPM = 8.0  # methods must agree within this for high-confidence

# Axis-direction hysteresis (per-window PCA selection is too jittery; once
# we lock onto a high-SQI cardiac direction, hold it unless a fresh candidate
# decisively beats it). Tuned on session_20260516T052121Z: 53% confident vs
# 38% without hysteresis.
PC_HYSTERESIS_MARGIN_DB = 5.0   # fresh PC must beat cached by this much to swap
PC_CACHE_MIN_SQI_DB = 5.0       # don't cache a direction unless its SQI exceeds this


@dataclass(slots=True)
class HRUpdate:
    t_unix: float
    hr_acf_bpm: float    # NaN if not estimable
    hr_env_bpm: float    # NaN if not estimable
    hr_bpm: float        # fused; NaN if both methods failed
    confident: bool
    axis: str            # "ax" / "ay" / "az"
    snr_db: float
    n_beats: int
    filtered: np.ndarray  # the band-passed selected axis (for plotting)
    envelope: np.ndarray
    peak_indices: np.ndarray


def _design_bandpass(fs: float):
    nyq = 0.5 * fs
    return butter(
        4,
        [BAND_LOW_HZ / nyq, BAND_HIGH_HZ / nyq],
        btype="band",
        output="sos",
    )


# --- LEGACY: single-axis selection by per-axis SNR (kept for switch-back) ---
# def _select_axis(window: np.ndarray, fs: float) -> tuple[int, float]:
#     """Return (col_index_in_window, snr_db). Picks axis with most 1-3 Hz energy."""
#     best_col, best_snr = 0, -np.inf
#     for col in range(3):
#         x = window[:, col] - np.mean(window[:, col])
#         f, p = welch(x, fs=fs, nperseg=min(len(x), 1024))
#         cardiac = (f >= 1.0) & (f <= 3.0)
#         noise = (f >= 10.0) & (f <= 30.0)
#         if not cardiac.any() or not noise.any():
#             continue
#         sig = float(np.mean(p[cardiac]))
#         nse = float(np.mean(p[noise])) + 1e-12
#         snr = 10.0 * np.log10(sig / nse)
#         if snr > best_snr:
#             best_snr = snr
#             best_col = col
#     return best_col, float(best_snr)


def _envelope_cardiac_score(x: np.ndarray, fs: float) -> float:
    """SQI for a band-passed candidate signal.

    Bandpass 5-25 Hz removes the cardiac fundamental, so spectral SNR has to be
    computed on the *envelope*: |hilbert(x)| then PSD peak in 0.7-1.8 Hz (resting
    HR fundamental, 42-108 bpm) divided by the local median power. Higher = more
    cardiac-like.
    """
    if np.std(x) < 1e-9:
        return float("-inf")
    env = np.abs(hilbert(x))
    env -= env.mean()
    f, p = welch(env, fs=fs, nperseg=min(len(env), 1024))
    band = (f >= 0.7) & (f <= 1.8)
    if not band.any() or p[band].max() <= 0:
        return float("-inf")
    return 10.0 * np.log10(p[band].max() / (np.median(p[band]) + 1e-18))


def _project_pc1(
    window: np.ndarray,
    sos,
    fs: float,
    cached_weights: np.ndarray | None = None,
) -> tuple[np.ndarray, float, np.ndarray, np.ndarray | None]:
    """Band-pass each axis, SVD, pick the PC with strongest cardiac envelope.

    Why not just PC1: PCA maximises *variance* in the 5-25 Hz band, not cardiac
    SNR. If a non-cardiac axis happens to carry more band energy (mount rumble,
    body creak), PC1 aligns with it. So we compute PC1/PC2/PC3 and pick the one
    whose Hilbert envelope has the strongest peak in the cardiac fundamental
    band (0.7-1.8 Hz on the envelope spectrum) — i.e. SVD provides a better
    *basis*, the SQI provides the *axis* selection within that basis.

    Hysteresis: once a cardiac direction is locked in (cached_weights), keep
    using it even if a fresh PCA candidate scores slightly higher. Only swap
    when a fresh candidate beats the cached one by PC_HYSTERESIS_MARGIN_DB.
    Without this, brief noise bursts on ax/ay flip the projection axis and
    crash confidence.

    Returns (pc, score, chosen_weights, new_cache).
    """
    Y = np.column_stack([
        sosfiltfilt(sos, window[:, c] - np.mean(window[:, c]))
        for c in range(3)
    ])
    # SVD: Y (N x 3) = U S V^T. Time-domain PC_k = Y @ V[:,k].
    _, _, Vt = np.linalg.svd(Y, full_matrices=False)

    # Best fresh candidate among the three SVD principal axes.
    fresh_w, fresh_score = Vt[0], float("-inf")
    for k in range(Vt.shape[0]):
        score = _envelope_cardiac_score(Y @ Vt[k], fs)
        if score > fresh_score:
            fresh_w, fresh_score = Vt[k], score

    # --- LEGACY: no hysteresis, always take fresh best (kept for switch-back) ---
    # chosen_w, chosen_score = fresh_w, fresh_score

    if cached_weights is None:
        chosen_w, chosen_score = fresh_w, fresh_score
    else:
        cached_score = _envelope_cardiac_score(Y @ cached_weights, fs)
        if fresh_score - cached_score >= PC_HYSTERESIS_MARGIN_DB:
            chosen_w, chosen_score = fresh_w, fresh_score
        else:
            chosen_w, chosen_score = cached_weights, cached_score

    # Only update the cache when the chosen direction is genuinely good.
    new_cache = chosen_w if chosen_score >= PC_CACHE_MIN_SQI_DB else cached_weights
    return Y @ chosen_w, chosen_score, chosen_w, new_cache


def _hr_autocorr(x: np.ndarray, fs: float) -> float:
    """Biased autocorr peak in the HR lag range."""
    x = x - np.mean(x)
    if np.std(x) < 1e-9:
        return float("nan")
    n = len(x)
    # FFT-based autocorrelation
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    f = np.fft.rfft(x, nfft)
    acf = np.fft.irfft(f * np.conj(f), nfft)[:n]
    acf /= acf[0]
    lag_min = int(fs * 60.0 / HR_MAX_BPM)
    lag_max = int(fs * 60.0 / HR_MIN_BPM)
    lag_max = min(lag_max, n - 1)
    if lag_max <= lag_min + 2:
        return float("nan")
    region = acf[lag_min : lag_max + 1]
    # require a local max, not just argmax of a monotonic tail
    peaks, _ = find_peaks(region)
    if len(peaks) == 0:
        return float("nan")
    best_local = peaks[np.argmax(region[peaks])]
    lag = lag_min + best_local
    return 60.0 * fs / lag


def _hr_envelope(x: np.ndarray, fs: float) -> tuple[float, np.ndarray, np.ndarray]:
    """Hilbert envelope → low-pass smooth → adaptive peak detect → median IBI HR.

    The envelope of a 5-25 Hz BCG band-passed signal contains two lobes per
    beat (S1 and S2, separated by ~300 ms). A short moving-average smoother
    leaves both lobes intact, so the peak counter doubles HR. The fix is a
    proper low-pass at 3 Hz: it merges S1+S2 into one per-beat envelope lobe
    while preserving the cardiac fundamental at ~1 Hz. Combined with a 500 ms
    refractory (> S1-S2 separation), this prevents harmonic doubling.

    Tuning verified by parameter sweep on session_20260516T050032Z (recorded
    against an oximeter ground truth of 63-75 bpm); see docs/session_2026-05-16.md.
    """
    if np.std(x) < 1e-9:
        return float("nan"), np.zeros_like(x), np.array([], dtype=int)
    env = np.abs(hilbert(x))

    # --- LEGACY: 100 ms moving-average smoother (kept for switch-back) ---
    # win = max(1, int(0.1 * fs))
    # kernel = np.ones(win) / win
    # env_s = np.convolve(env, kernel, mode="same")

    # 3 Hz Butterworth low-pass merges S1/S2 into one per-beat lobe.
    sos_env = butter(4, 3.0 / (0.5 * fs), btype="low", output="sos")
    env_s = sosfiltfilt(sos_env, env)

    # --- LEGACY: thr = mean + 0.5σ, refractory 250 ms (kept for switch-back) ---
    # thr = float(np.mean(env_s) + 0.5 * np.std(env_s))
    # distance = max(1, int(0.25 * fs))  # 240 bpm ceiling

    thr = float(np.mean(env_s) + 0.7 * np.std(env_s))
    distance = max(1, int(0.5 * fs))   # 120 bpm ceiling, > S1-S2 separation
    peaks, _ = find_peaks(env_s, height=thr, distance=distance)
    if len(peaks) < 3:
        return float("nan"), env_s, peaks
    ibis = np.diff(peaks) / fs
    if len(ibis) == 0:
        return float("nan"), env_s, peaks
    hr = 60.0 / float(np.median(ibis))
    if hr < HR_MIN_BPM or hr > HR_MAX_BPM:
        return float("nan"), env_s, peaks
    return hr, env_s, peaks


class HRWorker:
    def __init__(
        self,
        ring: RingBuffer,
        on_update: Callable[[HRUpdate], None] | None = None,
        fs: float = float(protocol.SAMPLE_RATE_HZ),
    ):
        self.ring = ring
        self.on_update = on_update
        self.fs = fs
        self._stop = threading.Event()
        self._sos = _design_bandpass(fs)
        self._latest: HRUpdate | None = None
        self._lock = threading.Lock()
        self._pc_cache: np.ndarray | None = None  # cached cardiac projection direction

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
            accel = data[:, [COL_AX, COL_AY, COL_AZ]]
            update = self._process(accel)
            with self._lock:
                self._latest = update
            if self.on_update:
                try:
                    self.on_update(update)
                except Exception as e:
                    print(f"on_update error: {e!r}")

    def _process(self, accel_window: np.ndarray) -> HRUpdate:
        # --- LEGACY single-axis path (kept for switch-back) ---
        # col, snr = _select_axis(accel_window, self.fs)
        # x = accel_window[:, col]
        # x = x - np.mean(x)
        # filt = sosfiltfilt(self._sos, x)

        filt, snr, weights, self._pc_cache = _project_pc1(
            accel_window, self._sos, self.fs, self._pc_cache
        )
        col = int(np.argmax(np.abs(weights)))   # dominant axis label only
        hr_acf = _hr_autocorr(filt, self.fs)
        hr_env, env, peaks = _hr_envelope(filt, self.fs)

        valid = [v for v in (hr_acf, hr_env) if not np.isnan(v)]
        if len(valid) == 2:
            confident = abs(hr_acf - hr_env) <= AGREE_BPM
            hr = float(np.mean(valid))
        elif len(valid) == 1:
            confident = False
            hr = float(valid[0])
        else:
            confident = False
            hr = float("nan")

        return HRUpdate(
            t_unix=time.time(),
            hr_acf_bpm=hr_acf,
            hr_env_bpm=hr_env,
            hr_bpm=hr,
            confident=confident,
            axis=("ax", "ay", "az")[col],
            snr_db=snr,
            n_beats=int(len(peaks)),
            filtered=filt,
            envelope=env,
            peak_indices=peaks,
        )
