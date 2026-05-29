"""Bishop & Ercole 2018 — Multi-Scale Peak & Trough Detection (MSPTD).

NK2 reference. O(N^2) memory in the window length, so we slice the signal
into 60 s windows.
"""
from __future__ import annotations

import numpy as np

from ..base import BasePipeline
from .._nk_helper import windowed_findpeaks


class BishopPipeline(BasePipeline):
    name = "bishop"

    def detect(self, cleaned: np.ndarray, fs: float) -> np.ndarray:
        return windowed_findpeaks(cleaned, fs, method="bishop", window_s=30.0)
