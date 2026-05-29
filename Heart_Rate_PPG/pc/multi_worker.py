"""Live multi-detector worker for app.py.

Runs all four pipelines (or any subset) on the same 8 s rolling window every
~1 s. Returns a dict of per-detector results so the UI can overlay the peaks
in different colors and display each detector's instant HR.

Heavy detectors (Bishop) take ~150 ms per 8 s window — fine at 1 Hz refresh.
Charlton internally downsamples to 20 Hz before its scalogram and is much
faster (~5 ms).
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

# Allow the live UI to import from the project's pipelines/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipelines as pl  # noqa: E402

import protocol  # noqa: E402
from ringbuffer import RingBuffer, COL_IR  # noqa: E402

WINDOW_S = 8.0
STEP_S = 1.0


@dataclass(slots=True)
class DetectorSnapshot:
    name: str
    peaks: np.ndarray            # in-window sample indices (cleaned signal length)
    cleaned: np.ndarray          # the detector's own cleaner output, sign-flipped
    hr_bpm: float                # NaN if not estimable
    n_peaks: int
    runtime_ms: float


@dataclass(slots=True)
class MultiUpdate:
    t_unix: float
    fs: float
    raw_ir: np.ndarray           # raw IR for the window
    snapshots: dict[str, DetectorSnapshot] = field(default_factory=dict)


class MultiDetectorWorker:
    """One thread runs all configured detectors on the same window."""

    def __init__(
        self,
        ring: RingBuffer,
        detector_names: list[str],
        on_update: Callable[[MultiUpdate], None] | None = None,
        fs: float = float(protocol.SAMPLE_RATE_HZ),
    ):
        self.ring = ring
        self.detector_names = detector_names
        self.detectors = {n: pl.get(n) for n in detector_names}
        self.on_update = on_update
        self.fs = fs
        self._stop = threading.Event()
        self._latest: MultiUpdate | None = None
        self._lock = threading.Lock()

    @property
    def latest(self) -> MultiUpdate | None:
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
            ir = data[:, COL_IR].astype(np.float64)
            update = self._process(ir)
            with self._lock:
                self._latest = update
            if self.on_update:
                try:
                    self.on_update(update)
                except Exception as e:
                    print(f"multi on_update error: {e!r}")

    def _process(self, ir: np.ndarray) -> MultiUpdate:
        update = MultiUpdate(t_unix=time.time(), fs=self.fs, raw_ir=ir)
        for name, pipeline in self.detectors.items():
            t0 = time.perf_counter()
            try:
                cleaned = pipeline.clean(ir, self.fs)
                peaks = pipeline.detect(cleaned, self.fs)
                peaks = np.asarray(peaks, dtype=np.int64)
                peaks = peaks[(peaks >= 0) & (peaks < len(cleaned))]
                # Live HR from peaks in the window: filter to plausible IBIs first.
                if len(peaks) >= 2:
                    ibi_ms = np.diff(peaks) / self.fs * 1000.0
                    ibi_ms = ibi_ms[(ibi_ms >= 300.0) & (ibi_ms <= 1500.0)]
                    hr = 60_000.0 / float(np.median(ibi_ms)) if len(ibi_ms) >= 2 else float("nan")
                else:
                    hr = float("nan")
            except Exception as e:
                print(f"[{name}] live error: {e!r}")
                cleaned = np.zeros_like(ir)
                peaks = np.array([], dtype=np.int64)
                hr = float("nan")
            update.snapshots[name] = DetectorSnapshot(
                name=name,
                peaks=peaks,
                cleaned=cleaned,
                hr_bpm=hr,
                n_peaks=int(len(peaks)),
                runtime_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return update
