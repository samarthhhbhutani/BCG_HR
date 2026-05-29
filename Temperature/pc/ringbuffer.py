"""Simple lock-protected ring buffer for temperature samples.

Temp arrives at 1 Hz so a small (1-hour) buffer is plenty.
"""
from __future__ import annotations
import threading
import numpy as np

# columns: t_us, t_obj_c
N_COLS = 2
COL_T = 0
COL_TEMP = 1


class RingBuffer:
    def __init__(self, capacity: int):
        self._buf = np.zeros((capacity, N_COLS), dtype=np.float64)
        self._capacity = capacity
        self._write = 0
        self._total = 0
        self._lock = threading.Lock()

    @property
    def total(self) -> int:
        return self._total

    def append(self, row: np.ndarray) -> None:
        with self._lock:
            self._buf[self._write] = row
            self._write = (self._write + 1) % self._capacity
            self._total += 1

    def latest(self, n: int) -> np.ndarray:
        """Return a copy of the last n rows in chronological order."""
        with self._lock:
            n = min(n, self._total, self._capacity)
            if n == 0:
                return np.empty((0, N_COLS), dtype=np.float64)
            end = self._write
            start = (end - n) % self._capacity
            if start < end:
                return self._buf[start:end].copy()
            return np.concatenate((self._buf[start:], self._buf[:end]), axis=0)
