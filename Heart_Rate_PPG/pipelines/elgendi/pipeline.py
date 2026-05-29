"""Elgendi 2013 TERMA — NeuroKit2 reference implementation.

Different from the project's `terma` pipeline: this is the canonical NK2
implementation (no slope-ratio gate, no onset refinement). Useful as a
sanity check that our custom pipeline isn't drifting from the published
algorithm.
"""
from __future__ import annotations

import numpy as np

from ..base import BasePipeline
from .._nk_helper import windowed_findpeaks


class ElgendiPipeline(BasePipeline):
    name = "elgendi"

    def detect(self, cleaned: np.ndarray, fs: float) -> np.ndarray:
        return windowed_findpeaks(cleaned, fs, method="elgendi")
