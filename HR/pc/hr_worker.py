"""Real-time HR estimator: sliding-window band-pass + axis selection
+ autocorrelation + envelope-peak fusion.

Reads from a shared RingBuffer; emits HR updates via a callback.

Algorithm:
  1. Pull last WINDOW_S of accel data.
  2. Detrend per-axis, band-pass 5-25 Hz (Butterworth 4, sosfiltfilt).
  3. Pick the axis with highest spectral power in 1-3 Hz (cardiac fundamental).
  4. Method A — autocorrelation: search lag for HR in [HR_MIN, HR_MAX] bpm.
  5. Method B — Hilbert envelope + adaptive peak detection (refractory 250 ms).
  6. Fuse: if methods agree within AGREE_BPM, emit mean; else emit best-effort with low-confidence flag.

References:
  Brüser et al., IEEE TITB 15(5), 2011 — autocorrelation BCG HR.
  Brüser et al., Physiol. Meas. 34(2), 2013 — multi-estimator fusion.
  Inan et al., IEEE JBHI 19(4), 2015 — BCG/SCG review.
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


def _select_axis(window: np.ndarray, fs: float) -> tuple[int, float]:
    """Return (col_index_in_window, snr_db). Picks axis with most 1-3 Hz energy."""
    best_col, best_snr = 0, -np.inf
    for col in range(3):
        x = window[:, col] - np.mean(window[:, col])
        f, p = welch(x, fs=fs, nperseg=min(len(x), 1024))
        cardiac = (f >= 1.0) & (f <= 3.0)
        noise = (f >= 10.0) & (f <= 30.0)
        if not cardiac.any() or not noise.any():
            continue
        sig = float(np.mean(p[cardiac]))
        nse = float(np.mean(p[noise])) + 1e-12
        snr = 10.0 * np.log10(sig / nse)
        if snr > best_snr:
            best_snr = snr
            best_col = col
    return best_col, float(best_snr)


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
    """Hilbert envelope → adaptive peak detect → median IBI HR."""
    if np.std(x) < 1e-9:
        return float("nan"), np.zeros_like(x), np.array([], dtype=int)
    env = np.abs(hilbert(x))
    # smooth with ~100 ms moving avg
    win = max(1, int(0.1 * fs))
    kernel = np.ones(win) / win
    env_s = np.convolve(env, kernel, mode="same")
    # adaptive threshold + 250 ms refractory
    thr = float(np.mean(env_s) + 0.5 * np.std(env_s))
    distance = max(1, int(0.25 * fs))  # 240 bpm ceiling
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
        col, snr = _select_axis(accel_window, self.fs)
        x = accel_window[:, col]
        x = x - np.mean(x)
        filt = sosfiltfilt(self._sos, x)
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
