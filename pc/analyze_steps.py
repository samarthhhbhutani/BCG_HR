"""Recompute steps from a recorded session by replaying its IMU parquet through
the live StepWorker. Use this to validate the step counter against a known walk.

Usage:
    python3 analyze_steps.py <session_dir>
    python3 analyze_steps.py --latest
    python3 analyze_steps.py --latest --truth 30      # compare to a counted walk
    python3 analyze_steps.py --latest --csv           # also write steps.csv

Because this drives the SAME StepWorker the live UI uses, the offline count
equals what the app produced in real time (no algorithm drift). The raw IMU
parquet is the source of truth, so any past or future session can be re-scored.

Output: a per-second table (time, cumulative steps, cadence, activity) printed
to stdout, plus a summary line. With --truth, prints absolute and percent error.
With --csv, writes <session_dir>/steps.csv.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

# Reuse the live worker so this stays in sync with the running app by construction.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import protocol  # noqa: E402
from ringbuffer import RingBuffer, N_COLS  # noqa: E402
from step_worker import StepWorker, StepUpdate, WINDOW_S, STEP_S  # noqa: E402

DATA_DIR = Path.home() / "Desktop/HR/data"


def find_latest_session(data_dir: Path) -> Path:
    sessions = sorted(
        p for p in data_dir.iterdir()
        if p.is_dir() and p.name.startswith("session_") and any(p.glob("imu_*.parquet"))
    )
    if not sessions:
        sys.exit(f"No sessions with imu_*.parquet under {data_dir}")
    return sessions[-1]


def load_session(session_dir: Path) -> tuple[np.ndarray, np.ndarray, float]:
    """Load all imu_*.parquet, concatenated. Returns (t_us Nx1, accel Nx3, fs)."""
    parquets = sorted(session_dir.glob("imu_*.parquet"))
    if not parquets:
        sys.exit(f"No imu_*.parquet in {session_dir}")
    t_parts, a_parts = [], []
    for p in parquets:
        t = pq.read_table(p)
        t_parts.append(t["t_us"].to_numpy().astype(np.float64))
        a_parts.append(np.column_stack([
            t["ax_g"].to_numpy().astype(np.float64),
            t["ay_g"].to_numpy().astype(np.float64),
            t["az_g"].to_numpy().astype(np.float64),
        ]))
    t_us = np.concatenate(t_parts)
    accel = np.concatenate(a_parts, axis=0)
    return t_us, accel, float(protocol.SAMPLE_RATE_HZ)


def replay(t_us: np.ndarray, accel: np.ndarray, fs: float) -> list[tuple[float, StepUpdate]]:
    """Feed samples into a fresh StepWorker exactly as the live run() loop does:
    append each sample to the ring, and call _process every STEP_S once the
    window is full. Returns (session_time_s, snapshot) per processed window.
    """
    ring = RingBuffer(capacity=int(fs * 120))
    sw = StepWorker(ring=ring, fs=fs)
    win_n = int(WINDOW_S * fs)
    hop_n = int(STEP_S * fs)
    t0 = t_us[0]
    row = np.zeros(N_COLS)
    out: list[tuple[float, StepUpdate]] = []
    for i in range(len(accel)):
        row[0] = t_us[i]
        row[1], row[2], row[3] = accel[i]
        ring.append(row.copy())
        if (i + 1) % hop_n == 0 and ring.total >= win_n:
            sw._process(ring.latest(win_n))
            snap = sw.latest
            if snap is not None:
                out.append(((t_us[i] - t0) / 1e6, snap))
    return out, sw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?", help="session dir; omit with --latest")
    ap.add_argument("--latest", action="store_true", help="use newest session with data")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--truth", type=int, help="known step count for error reporting")
    ap.add_argument("--csv", action="store_true", help="write <session>/steps.csv")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    if args.latest or not args.session:
        session_dir = find_latest_session(data_dir)
    else:
        session_dir = Path(args.session)
        if not session_dir.is_absolute() and not session_dir.exists():
            session_dir = data_dir / session_dir
    if not session_dir.exists():
        sys.exit(f"Session not found: {session_dir}")

    t_us, accel, fs = load_session(session_dir)
    duration_s = (t_us[-1] - t_us[0]) / 1e6
    timeline, sw = replay(t_us, accel, fs)

    print(f"Session : {session_dir.name}")
    print(f"Samples : {len(accel)}  ({duration_s:.1f} s @ {fs:.0f} Hz)")
    print()
    print("  t(s)   steps  cadence  activity")
    for t_s, su in timeline:
        cad = f"{su.cadence_spm:5.0f}" if not np.isnan(su.cadence_spm) else "    —"
        print(f"  {t_s:5.1f}   {su.total_steps:4d}   {cad}    {su.activity}")

    total = sw._total_steps
    print()
    print(f"TOTAL STEPS : {total}")
    if duration_s > 0:
        print(f"Avg cadence : {total / (duration_s / 60.0):.0f} spm over full session")
    if args.truth is not None:
        err = total - args.truth
        pct = 100.0 * abs(err) / args.truth if args.truth else float("nan")
        print(f"Ground truth: {args.truth}   error: {err:+d}  ({pct:.1f}%)")

    if args.csv:
        out = session_dir / "steps.csv"
        lines = ["t_s,total_steps,cadence_spm,activity"]
        for t_s, su in timeline:
            cad = "" if np.isnan(su.cadence_spm) else f"{su.cadence_spm:.1f}"
            lines.append(f"{t_s:.2f},{su.total_steps},{cad},{su.activity}")
        out.write_text("\n".join(lines) + "\n")
        print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
