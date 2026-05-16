"""Lock-protected numpy ring buffer for IMU samples shared by receiver, HR worker, UI."""
from __future__ import annotations
import threading
import numpy as np

# columns: t_us, ax, ay, az, gx, gy, gz
N_COLS = 7
COL_T = 0
COL_AX, COL_AY, COL_AZ = 1, 2, 3
COL_GX, COL_GY, COL_GZ = 4, 5, 6


class RingBuffer:
    def __init__(self, capacity: int):
        self._buf = np.zeros((capacity, N_COLS), dtype=np.float64)
        self._capacity = capacity
        self._write = 0       # next write index
        self._total = 0       # total samples ever written
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
