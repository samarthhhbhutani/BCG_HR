"""Analyze a recorded session and write Heart Rate per timestamp + overall average to Excel.

Usage:
    python3 analyze_session.py <session_dir> [--out OUTPUT.xlsx]

Examples:
    # explicit session
    python3 analyze_session.py ~/Desktop/HR/data/session_20260516T053721Z

    # auto-pick newest session
    python3 analyze_session.py --latest

The output .xlsx has two sheets:
  - "HR_per_timestamp": one row per 8 s sliding window (1 s hop). Columns:
        t_center_s, hr_acf_bpm, hr_env_bpm, hr_fused_bpm, sqi_db, confident
  - "Summary": single-row averages over the whole recording.

Heart-rate algorithm matches the live UI exactly: bandpass 5-25 Hz, PCA across
the 3 accel axes with hysteresis, then ACF + envelope-peak estimators fused.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

# Reuse the live worker's algorithm so this stays in sync with the running app.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hr_worker import (  # noqa: E402
    _project_pc1,
    _hr_autocorr,
    _hr_envelope,
    _design_bandpass,
    WINDOW_S,
    STEP_S,
    AGREE_BPM,
)


def find_latest_session(data_dir: Path) -> Path:
    sessions = sorted(p for p in data_dir.iterdir() if p.is_dir() and p.name.startswith("session_"))
    if not sessions:
        sys.exit(f"No sessions found under {data_dir}")
    return sessions[-1]


def load_session(session_dir: Path) -> tuple[np.ndarray, float]:
    """Load all imu_*.parquet files in the session, concatenate, return (accel Nx3, fs)."""
    parquets = sorted(session_dir.glob("imu_*.parquet"))
    if not parquets:
        sys.exit(f"No imu_*.parquet in {session_dir}")
    parts = []
    for p in parquets:
        t = pq.read_table(p)
        parts.append(np.column_stack([
            t["ax_g"].to_numpy().astype(np.float64),
            t["ay_g"].to_numpy().astype(np.float64),
            t["az_g"].to_numpy().astype(np.float64),
        ]))
    accel = np.concatenate(parts, axis=0)
    fs = 200.0  # firmware/protocol contract
    return accel, fs


def compute_per_window(accel: np.ndarray, fs: float):
    """Return list of dicts with HR estimates per sliding window."""
    sos = _design_bandpass(fs)
    win_n = int(WINDOW_S * fs)
    hop_n = int(STEP_S * fs)
    pc_cache = None
    rows = []
    for s in range(0, len(accel) - win_n + 1, hop_n):
        w = accel[s : s + win_n]
        pc, sqi, _, pc_cache = _project_pc1(w, sos, fs, pc_cache)
        h_acf = _hr_autocorr(pc, fs)
        h_env, _, _ = _hr_envelope(pc, fs)
        valid = [v for v in (h_acf, h_env) if not np.isnan(v)]
        fused = float(np.mean(valid)) if valid else float("nan")
        confident = (
            len(valid) == 2 and abs(h_acf - h_env) <= AGREE_BPM
        )
        rows.append({
            "t_center_s": (s + win_n / 2) / fs,
            "hr_acf_bpm": h_acf,
            "hr_env_bpm": h_env,
            "hr_fused_bpm": fused,
            "sqi_db": sqi,
            "confident": confident,
        })
    return rows


def write_excel(rows: list[dict], session_dir: Path, accel: np.ndarray, fs: float, out: Path) -> None:
    wb = Workbook()

    # Sheet 1 — per-timestamp HR
    ws1 = wb.active
    ws1.title = "HR_per_timestamp"
    headers = ["t_center_s", "hr_acf_bpm", "hr_env_bpm", "hr_fused_bpm", "sqi_db", "confident"]
    ws1.append(headers)
    bold = Font(bold=True)
    fill_conf = PatternFill("solid", fgColor="D8F0D8")  # light green
    for c in ws1[1]:
        c.font = bold
    for r in rows:
        ws1.append([
            round(r["t_center_s"], 2),
            None if np.isnan(r["hr_acf_bpm"]) else round(r["hr_acf_bpm"], 1),
            None if np.isnan(r["hr_env_bpm"]) else round(r["hr_env_bpm"], 1),
            None if np.isnan(r["hr_fused_bpm"]) else round(r["hr_fused_bpm"], 1),
            round(r["sqi_db"], 2),
            r["confident"],
        ])
        if r["confident"]:
            for c in ws1[ws1.max_row]:
                c.fill = fill_conf
    for col_letter, width in zip("ABCDEF", (12, 12, 12, 14, 10, 11)):
        ws1.column_dimensions[col_letter].width = width

    # Sheet 2 — summary
    ws2 = wb.create_sheet("Summary")

    fused_all = np.array([r["hr_fused_bpm"] for r in rows], dtype=float)
    fused_conf = np.array([r["hr_fused_bpm"] for r in rows if r["confident"]], dtype=float)
    acf_all = np.array([r["hr_acf_bpm"] for r in rows], dtype=float)
    env_all = np.array([r["hr_env_bpm"] for r in rows], dtype=float)
    acf_conf = np.array([r["hr_acf_bpm"] for r in rows if r["confident"]], dtype=float)
    env_conf = np.array([r["hr_env_bpm"] for r in rows if r["confident"]], dtype=float)
    sqi = np.array([r["sqi_db"] for r in rows], dtype=float)
    duration_s = len(accel) / fs

    def safe(fn, x):
        if x.size == 0 or np.all(np.isnan(x)):
            return None
        return float(fn(x))

    def r1(v):
        return None if v is None else round(v, 1)

    def r2(v):
        return None if v is None else round(v, 2)

    summary = [
        ("session_dir", str(session_dir)),
        ("duration_s", round(duration_s, 1)),
        ("n_windows", len(rows)),
        ("n_confident", int(sum(r["confident"] for r in rows))),
        ("pct_confident", round(100 * sum(r["confident"] for r in rows) / max(len(rows), 1), 1)),
        ("", ""),
        ("HR averages (bpm)", ""),
        ("avg_hr_fused_all_windows",         r1(safe(np.nanmean, fused_all))),
        ("median_hr_fused_all_windows",      r1(safe(np.nanmedian, fused_all))),
        ("avg_hr_fused_confident_only",      r1(safe(np.nanmean, fused_conf))),
        ("median_hr_fused_confident_only",   r1(safe(np.nanmedian, fused_conf))),
        ("", ""),
        ("Per-method averages", ""),
        ("avg_hr_acf",                       r1(safe(np.nanmean, acf_all))),
        ("avg_hr_env",                       r1(safe(np.nanmean, env_all))),
        ("median_hr_acf",                    r1(safe(np.nanmedian, acf_all))),
        ("median_hr_env",                    r1(safe(np.nanmedian, env_all))),
        ("avg_hr_acf_confident",             r1(safe(np.nanmean, acf_conf))),
        ("avg_hr_env_confident",             r1(safe(np.nanmean, env_conf))),
        ("", ""),
        ("Spread (bpm)", ""),
        ("hr_fused_p10_all", r1(safe(lambda x: np.nanpercentile(x, 10), fused_all))),
        ("hr_fused_p90_all", r1(safe(lambda x: np.nanpercentile(x, 90), fused_all))),
        ("hr_fused_min_all", r1(safe(np.nanmin, fused_all))),
        ("hr_fused_max_all", r1(safe(np.nanmax, fused_all))),
        ("", ""),
        ("Signal quality", ""),
        ("sqi_median_db", r2(safe(np.nanmedian, sqi))),
        ("sqi_min_db",    r2(safe(np.nanmin, sqi))),
        ("sqi_max_db",    r2(safe(np.nanmax, sqi))),
    ]
    ws2.append(["metric", "value"])
    for c in ws2[1]:
        c.font = bold
    section_fill = PatternFill("solid", fgColor="EEEEEE")
    for k, v in summary:
        ws2.append([k, v])
        if v == "" and k:
            for c in ws2[ws2.max_row]:
                c.font = bold
                c.fill = section_fill
    ws2.column_dimensions["A"].width = 32
    ws2.column_dimensions["B"].width = 60

    wb.save(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", nargs="?", help="Path to a session_<UTC> directory.")
    ap.add_argument("--latest", action="store_true",
                    help="Auto-pick newest session under ~/Desktop/HR/data/.")
    ap.add_argument("--out", help="Output xlsx path (default: <session_dir>/hr_analysis.xlsx)")
    args = ap.parse_args()

    if args.latest:
        session_dir = find_latest_session(Path.home() / "Desktop/HR/data")
    elif args.session_dir:
        session_dir = Path(args.session_dir).expanduser().resolve()
    else:
        ap.error("Provide a session_dir or --latest.")

    if not session_dir.is_dir():
        sys.exit(f"Not a directory: {session_dir}")

    accel, fs = load_session(session_dir)
    rows = compute_per_window(accel, fs)
    if not rows:
        sys.exit("Recording too short for any 8 s window.")

    out = Path(args.out).expanduser().resolve() if args.out else (session_dir / "hr_analysis.xlsx")
    write_excel(rows, session_dir, accel, fs, out)

    fused_all = np.array([r["hr_fused_bpm"] for r in rows], dtype=float)
    fused_conf = np.array([r["hr_fused_bpm"] for r in rows if r["confident"]], dtype=float)
    print(f"Session     : {session_dir.name}")
    print(f"Duration    : {len(accel)/fs:.1f} s   Windows: {len(rows)}")
    print(f"Avg HR (all confidence)        : {np.nanmean(fused_all):.1f} bpm")
    if fused_conf.size:
        print(f"Avg HR (confident windows only): {np.nanmean(fused_conf):.1f} bpm "
              f"({len(fused_conf)}/{len(rows)} windows)")
    print(f"Wrote       : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
