"""Shared session loader, cleaner, and finger-presence gate for all pipelines.

A single cleaner is used so that benchmark differences are attributable to
the detector, not the preprocessing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy.signal import butter, sosfiltfilt


# Same cleaner across all pipelines: NK2 elgendi default (Butterworth BP 0.5-8 Hz, order 2).
BANDPASS_LOW_HZ = 0.5
BANDPASS_HIGH_HZ = 8.0
BANDPASS_ORDER = 2

# Finger-presence gate (mirrors hr_worker, but applied chunk-by-chunk offline).
DEFAULT_CONTACT_IR_MIN = 50_000.0
DEFAULT_CONTACT_IR_MAX = 260_000.0
DEFAULT_CONTACT_AC_MIN = 200.0


@dataclass(slots=True)
class Session:
    session_dir: Path
    meta: dict
    fs: float                 # nominal sample rate from session.json
    fs_measured: float        # estimated from t_us (more accurate)
    t_us: np.ndarray          # microsecond timestamps from firmware
    t_unix: np.ndarray        # absolute UTC seconds (built from session start + t_us deltas)
    ir: np.ndarray            # raw IR
    red: np.ndarray           # raw Red
    seq: np.ndarray           # firmware sequence numbers


def load_session(session_dir: Path) -> Session:
    """Load all ppg_*.parquet shards and session.json into a single Session."""
    session_dir = Path(session_dir)
    meta_path = session_dir / "session.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"no session.json in {session_dir}")
    meta = json.loads(meta_path.read_text())
    fs = float(meta.get("sample_rate_hz", 200))

    shards = sorted(session_dir.glob("ppg_*.parquet"))
    if not shards:
        raise FileNotFoundError(f"no ppg_*.parquet in {session_dir}")

    frames = [pq.read_table(p).to_pandas() for p in shards]
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("seq").reset_index(drop=True)

    t_us = df["t_us"].to_numpy(dtype=np.uint64)
    ir = df["ir"].to_numpy(dtype=np.float64)
    red = df["red"].to_numpy(dtype=np.float64)
    seq = df["seq"].to_numpy(dtype=np.uint32)

    # Reconstruct UTC time per sample. The firmware t_us wraps every ~71 min,
    # so we walk diffs as unsigned 32-bit and accumulate. But if the M5StickC
    # is unplugged/replugged mid-session, esp_timer_get_time() resets to 0
    # and the *unsigned* diff produces a fake ~4295 s gap (one full u32 wrap)
    # that the wrap heuristic can't distinguish from a real overflow. Clip
    # any diff > MAX_GAP_S to the nominal sample period so a device reset
    # doesn't poison the entire timeline. Real wraps are always exactly
    # ~71 min minus the actual sample period — never anywhere near 1 s.
    MAX_GAP_S = 1.0
    diffs_us = np.diff(t_us.astype(np.int64))
    diffs_us = (diffs_us & 0xFFFFFFFF).astype(np.float64)  # unwrap natural u32 overflow
    nominal_dt_us = 1e6 / fs
    bad = diffs_us > (MAX_GAP_S * 1e6)
    n_bad = int(np.count_nonzero(bad))
    if n_bad:
        diffs_us[bad] = nominal_dt_us
    rel_us = np.concatenate(([0.0], np.cumsum(diffs_us)))
    rel_s = rel_us / 1e6

    started = meta.get("started_utc")
    if started:
        t0 = datetime.strptime(started, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        t0_unix = t0.timestamp()
    else:
        t0_unix = 0.0
    t_unix = t0_unix + rel_s

    duration_s = float(rel_s[-1] - rel_s[0]) if len(rel_s) > 1 else 0.0
    fs_measured = (len(df) / duration_s) if duration_s > 0 else fs
    if n_bad:
        print(f"  ⚠ patched {n_bad} t_us discontinuities (likely device reset/replug)")

    return Session(
        session_dir=session_dir,
        meta=meta,
        fs=fs,
        fs_measured=float(fs_measured),
        t_us=t_us,
        t_unix=t_unix,
        ir=ir,
        red=red,
        seq=seq,
    )


def design_bandpass(fs: float, low_hz: float = BANDPASS_LOW_HZ,
                    high_hz: float = BANDPASS_HIGH_HZ, order: int = BANDPASS_ORDER):
    nyq = 0.5 * fs
    return butter(order, [low_hz / nyq, min(high_hz, nyq * 0.99) / nyq],
                  btype="band", output="sos")


def clean_ppg(
    ir: np.ndarray,
    fs: float,
    low_hz: float = BANDPASS_LOW_HZ,
    high_hz: float = BANDPASS_HIGH_HZ,
    order: int = BANDPASS_ORDER,
) -> np.ndarray:
    """DC removal + Butterworth bandpass + sign flip.

    Returns the *sign-flipped* band-pass: MAX30102 IR convention is more
    blood -> less light -> smaller IR, so after cleaning the systolic peaks
    point downward. Flipping makes them point up, which is what every detector
    in the literature (Elgendi, Bishop, Charlton, TERMA) expects.

    Each pipeline can override the cutoffs via BasePipeline.bandpass_*.
    """
    if len(ir) < 16:
        return np.zeros_like(ir)
    # 1 s moving-average baseline removal — matches hr_worker.
    win = max(1, int(fs))
    win = min(win, len(ir))
    kernel = np.ones(win) / win
    baseline = np.convolve(ir, kernel, mode="same")
    ac = ir - baseline
    sos = design_bandpass(fs, low_hz=low_hz, high_hz=high_hz, order=order)
    filt = sosfiltfilt(sos, ac)
    return -filt  # peaks now point up


def finger_present_mask(
    ir: np.ndarray,
    cleaned: np.ndarray,
    fs: float,
    chunk_s: float = 1.0,
    ir_min: float = DEFAULT_CONTACT_IR_MIN,
    ir_max: float = DEFAULT_CONTACT_IR_MAX,
    ac_min: float = DEFAULT_CONTACT_AC_MIN,
) -> np.ndarray:
    """Return a per-sample bool mask. A sample is finger-present if the 1 s
    block containing it has min(ir) >= ir_min, max(ir) <= ir_max, and
    peak-to-peak(cleaned) >= ac_min.
    """
    n = len(ir)
    if n == 0:
        return np.zeros(0, dtype=bool)
    block = max(1, int(chunk_s * fs))
    out = np.zeros(n, dtype=bool)
    for start in range(0, n, block):
        end = min(start + block, n)
        seg_ir = ir[start:end]
        seg_cl = cleaned[start:end]
        ok = (
            float(np.min(seg_ir)) >= ir_min
            and float(np.max(seg_ir)) <= ir_max
            and float(np.max(seg_cl) - np.min(seg_cl)) >= ac_min
        )
        out[start:end] = ok
    return out
