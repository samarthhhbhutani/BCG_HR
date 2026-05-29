"""Common types for PPG peak-detection pipelines."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class PipelineResult:
    name: str
    peak_indices: np.ndarray          # int sample indices into the cleaned signal
    ibis_ms: np.ndarray               # filtered, plausible IBIs (ms)
    t_unix_per_peak: np.ndarray       # absolute UTC time per peak (seconds)
    runtime_ms: float
    fs: float
    n_samples: int
    cleaned: np.ndarray = field(default_factory=lambda: np.array([]))
    extra: dict = field(default_factory=dict)


class BasePipeline(ABC):
    name: str = "base"

    # Each detector can override its own bandpass. Defaults match NK2's
    # `elgendi` cleaner, which is the canonical pre-step for the NK2 detectors.
    bandpass_low_hz: float = 0.5
    bandpass_high_hz: float = 8.0

    def clean(self, ir: np.ndarray, fs: float) -> np.ndarray:
        """Override in a subclass if the detector needs a different cleaner."""
        from .common import clean_ppg
        return clean_ppg(ir, fs, low_hz=self.bandpass_low_hz, high_hz=self.bandpass_high_hz)

    @abstractmethod
    def detect(self, cleaned: np.ndarray, fs: float) -> np.ndarray:
        """Return integer peak indices into the cleaned (sign-flipped) signal."""

    def run(
        self,
        ir: np.ndarray,
        fs: float,
        t_unix: np.ndarray,
        ibi_min_ms: float = 300.0,
        ibi_max_ms: float = 1500.0,
    ) -> PipelineResult:
        """Run the detector end-to-end (cleaner + detect) and return a result.

        `t_unix` must be the same length as `ir`, mapping each sample to
        absolute time in seconds.
        """
        import time
        cleaned = self.clean(ir, fs)
        t0 = time.perf_counter()
        peaks = self.detect(cleaned, fs)
        runtime_ms = (time.perf_counter() - t0) * 1000.0

        peaks = np.asarray(peaks, dtype=np.int64)
        peaks = peaks[(peaks >= 0) & (peaks < len(cleaned))]
        peaks.sort()

        if len(peaks) >= 2:
            ibis_ms_raw = np.diff(peaks).astype(np.float64) / fs * 1000.0
            keep = (ibis_ms_raw >= ibi_min_ms) & (ibis_ms_raw <= ibi_max_ms)
            # Drop peaks whose succeeding IBI is implausible: keep peak[0], then
            # only those peaks where the *preceding* IBI was kept.
            kept_peaks = [int(peaks[0])]
            for i, ok in enumerate(keep):
                if ok:
                    kept_peaks.append(int(peaks[i + 1]))
            peaks = np.asarray(kept_peaks, dtype=np.int64)
            ibis_ms = np.diff(peaks).astype(np.float64) / fs * 1000.0
        else:
            ibis_ms = np.array([], dtype=np.float64)

        t_per_peak = t_unix[peaks] if len(peaks) > 0 else np.array([], dtype=np.float64)

        return PipelineResult(
            name=self.name,
            peak_indices=peaks,
            ibis_ms=ibis_ms,
            t_unix_per_peak=t_per_peak,
            runtime_ms=runtime_ms,
            fs=fs,
            n_samples=len(cleaned),
        )
