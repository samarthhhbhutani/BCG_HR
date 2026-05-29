"""Lazy NeuroKit2 import + windowed runner for slow detectors.

NK2 pulls in matplotlib + sklearn at import time and is ~20 MB. Importing
inside the function means TERMA-only runs don't pay that cost.

Bishop's MSPTD is O(N^2) on the array length, so for long sessions we slice
into 60 s windows (no overlap) and concatenate peaks. NK2's `charlton` already
windows internally; calling _windowed on it is a no-op aside from a tiny
boundary effect, but we still slice for consistency in benchmark timing.
"""
from __future__ import annotations

from typing import Callable
import numpy as np


def lazy_nk():
    import importlib
    return importlib.import_module("neurokit2")


def windowed_findpeaks(
    cleaned: np.ndarray,
    fs: float,
    method: str,
    window_s: float = 60.0,
) -> np.ndarray:
    """Run nk.ppg_findpeaks on `window_s`-second slices and concat results."""
    nk = lazy_nk()
    n = len(cleaned)
    if n == 0:
        return np.array([], dtype=int)
    win = max(int(window_s * fs), int(8 * fs))  # min 8 s window
    if n <= win:
        info = nk.ppg_findpeaks(cleaned, sampling_rate=int(round(fs)), method=method)
        return np.asarray(info.get("PPG_Peaks", []), dtype=int)

    peaks: list[int] = []
    for start in range(0, n, win):
        end = min(start + win, n)
        if end - start < int(4 * fs):
            break  # too short for meaningful detection
        seg = cleaned[start:end]
        try:
            info = nk.ppg_findpeaks(seg, sampling_rate=int(round(fs)), method=method)
            seg_peaks = np.asarray(info.get("PPG_Peaks", []), dtype=int) + start
            peaks.extend(int(p) for p in seg_peaks)
        except Exception:
            # Robust to NK2 throwing on a flat / corrupt window.
            continue
    out = np.unique(np.asarray(peaks, dtype=int))
    return out
