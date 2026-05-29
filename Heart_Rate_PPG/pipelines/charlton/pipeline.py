"""Charlton 2025 — MSPTDfast v2.

NK2 ppg_findpeaks(method='charlton'): downsamples internally to 20 Hz, splits
into windows with 20% overlap, and reduces the scalogram to scales above
30 bpm. ~10x faster than vanilla MSPTD with ~equal accuracy on benchmark
datasets.
"""
from __future__ import annotations

import numpy as np

from ..base import BasePipeline
from .._nk_helper import windowed_findpeaks


class CharltonPipeline(BasePipeline):
    name = "charlton"

    def detect(self, cleaned: np.ndarray, fs: float) -> np.ndarray:
        # NK2's charlton already windows internally; pass full signal.
        return windowed_findpeaks(cleaned, fs, method="charlton", window_s=120.0)
