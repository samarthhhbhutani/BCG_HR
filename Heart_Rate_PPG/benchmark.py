"""Benchmark all (or a subset of) PPG peak-detection pipelines on a session.

Reads `Heart_Rate_PPG/data/session_<UTC>/ppg_*.parquet` (the firmware-recorded
data — record once, analyse with all pipelines), runs each requested detector
on the same cleaned signal, and writes per-pipeline + combined outputs into
`session_<UTC>/analysis/`.

Usage:
    python benchmark.py --latest
    python benchmark.py --session data/session_<UTC>
    python benchmark.py --all
    python benchmark.py --latest --detector terma
    python benchmark.py --latest --detector elgendi,charlton
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipelines as pl
from pipelines.base import PipelineResult
from pipelines.common import (
    load_session,
    clean_ppg,
    finger_present_mask,
    Session,
)


CHUNK_S = 30.0
MATCH_TOLERANCE_S = 0.05  # ±50 ms peak match window
MIN_IBIS_PER_CHUNK = 5
MIN_FINGER_FRAC = 0.7
ROLLING_HR_S = 10.0


# ---------- chunking ----------

def chunk_hr(
    res: PipelineResult,
    t_start_unix: float,
    t_end_unix: float,
    finger_mask: np.ndarray,
    chunk_s: float = CHUNK_S,
) -> pd.DataFrame:
    """Per-chunk median-IBI HR for one pipeline.

    finger_mask is per-sample (over the original cleaned signal). A chunk is
    valid iff finger_present_frac >= MIN_FINGER_FRAC AND it contains at least
    MIN_IBIS_PER_CHUNK plausible IBIs.
    """
    if t_end_unix <= t_start_unix:
        return pd.DataFrame(columns=["chunk_idx", "t_start_unix", "t_end_unix",
                                      "hr_bpm", "n_ibis", "finger_present_frac"])
    n_chunks = int(np.floor((t_end_unix - t_start_unix) / chunk_s))
    if n_chunks <= 0:
        n_chunks = 1
    fs = res.fs
    rows = []
    for i in range(n_chunks):
        c_start = t_start_unix + i * chunk_s
        c_end = c_start + chunk_s
        # Peak times falling in this chunk.
        if len(res.t_unix_per_peak) >= 2:
            in_chunk = (res.t_unix_per_peak >= c_start) & (res.t_unix_per_peak < c_end)
            peak_t = res.t_unix_per_peak[in_chunk]
            ibis = np.diff(peak_t) * 1000.0  # ms
        else:
            ibis = np.array([], dtype=np.float64)
        # Finger fraction across this chunk.
        s_idx0 = int(round((c_start - t_start_unix) * fs))
        s_idx1 = int(round((c_end - t_start_unix) * fs))
        s_idx0 = max(0, min(s_idx0, len(finger_mask)))
        s_idx1 = max(0, min(s_idx1, len(finger_mask)))
        seg_mask = finger_mask[s_idx0:s_idx1]
        finger_frac = float(np.mean(seg_mask)) if len(seg_mask) else 0.0
        ok = (finger_frac >= MIN_FINGER_FRAC) and (len(ibis) >= MIN_IBIS_PER_CHUNK)
        hr = 60_000.0 / float(np.median(ibis)) if ok else float("nan")
        rows.append({
            "chunk_idx": i,
            "t_start_unix": c_start,
            "t_end_unix": c_end,
            "hr_bpm": hr,
            "n_ibis": int(len(ibis)),
            "finger_present_frac": finger_frac,
        })
    return pd.DataFrame(rows)


# ---------- agreement ----------

def f1_match(t_a: np.ndarray, t_b: np.ndarray, tol_s: float = MATCH_TOLERANCE_S) -> dict:
    """F1 between two peak-time arrays; greedy nearest-neighbor with `tol_s`."""
    if len(t_a) == 0 or len(t_b) == 0:
        return {"tp": 0, "fp": int(len(t_a)), "fn": int(len(t_b)),
                "precision": 0.0, "recall": 0.0, "f1": 0.0}
    a = np.sort(t_a.astype(np.float64))
    b = np.sort(t_b.astype(np.float64))
    j = 0
    tp = 0
    used_b = np.zeros(len(b), dtype=bool)
    for ta in a:
        while j < len(b) and b[j] < ta - tol_s:
            j += 1
        # Find nearest unmatched b within tol.
        best = -1
        best_d = tol_s + 1e9
        k = j
        while k < len(b) and b[k] <= ta + tol_s:
            if not used_b[k]:
                d = abs(b[k] - ta)
                if d < best_d:
                    best_d = d
                    best = k
            k += 1
        if best >= 0:
            used_b[best] = True
            tp += 1
    fp = len(a) - tp
    fn = len(b) - tp
    p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1}


def consensus_score(results: dict[str, PipelineResult],
                    tol_s: float = MATCH_TOLERANCE_S) -> dict[str, float]:
    """Each detector's F1 against the consensus set.

    Consensus = peaks supported by a majority of detectors. Build by clustering
    all peaks within `tol_s` and keeping clusters of size >= ceil(n/2).
    """
    names = list(results.keys())
    if len(names) < 2:
        return {n: float("nan") for n in names}
    all_t = []
    for n in names:
        for t in results[n].t_unix_per_peak:
            all_t.append((float(t), n))
    all_t.sort()
    clusters: list[list[tuple[float, str]]] = []
    for t, n in all_t:
        if clusters and (t - clusters[-1][-1][0]) <= tol_s:
            clusters[-1].append((t, n))
        else:
            clusters.append([(t, n)])
    quorum = (len(names) + 1) // 2 + (1 if len(names) > 2 else 0)
    quorum = max(2, min(quorum, len(names)))
    consensus_t = np.array([
        float(np.median([x[0] for x in c]))
        for c in clusters
        if len({x[1] for x in c}) >= quorum
    ])
    out = {}
    for n in names:
        m = f1_match(results[n].t_unix_per_peak, consensus_t, tol_s=tol_s)
        out[n] = float(m["f1"])
    return out


# ---------- runner ----------

def run_pipeline(name: str, sess: Session) -> PipelineResult:
    pipeline = pl.get(name)
    return pipeline.run(sess.ir, sess.fs_measured, sess.t_unix)


def write_outputs(
    session: Session,
    results: dict[str, PipelineResult],
    finger_mask: np.ndarray,
    detectors_run: list[str],
    detectors_skipped: list[str],
    chunks_by_pipe: dict[str, pd.DataFrame],
    pairwise: dict[tuple[str, str], dict],
    consensus_f1: dict[str, float],
) -> Path:
    out_dir = session.session_dir / "analysis"
    out_dir.mkdir(exist_ok=True)

    # Per-pipeline subfolders.
    for n, res in results.items():
        sub = out_dir / n
        sub.mkdir(exist_ok=True)

        peaks_df = pd.DataFrame({
            "sample_idx": res.peak_indices.astype(np.int64),
            "t_us": session.t_us[res.peak_indices].astype(np.uint64) if len(res.peak_indices) else np.array([], dtype=np.uint64),
            "t_unix": res.t_unix_per_peak.astype(np.float64),
        })
        pq.write_table(pa.Table.from_pandas(peaks_df, preserve_index=False),
                       sub / "peaks.parquet", compression="zstd")

        ibi_t = res.t_unix_per_peak[1:] if len(res.t_unix_per_peak) >= 2 else np.array([])
        ibi_df = pd.DataFrame({"t_unix": ibi_t, "ibi_ms": res.ibis_ms})
        pq.write_table(pa.Table.from_pandas(ibi_df, preserve_index=False),
                       sub / "ibis.parquet", compression="zstd")

        chunks = chunks_by_pipe[n]
        pq.write_table(pa.Table.from_pandas(chunks, preserve_index=False),
                       sub / "chunks.parquet", compression="zstd")

        valid = chunks["hr_bpm"].dropna()
        meta = {
            "name": n,
            "n_peaks": int(len(res.peak_indices)),
            "mean_hr_bpm": float(valid.mean()) if len(valid) else None,
            "hr_cv": float(valid.std() / valid.mean()) if len(valid) and valid.mean() > 0 else None,
            "runtime_ms": float(res.runtime_ms),
            "fs": float(res.fs),
            "n_samples": int(res.n_samples),
            "consensus_f1": consensus_f1.get(n),
        }
        (sub / "meta.json").write_text(json.dumps(meta, indent=2))

    # Combined chunks_hr.parquet (wide).
    base = next(iter(chunks_by_pipe.values()))[
        ["chunk_idx", "t_start_unix", "t_end_unix", "finger_present_frac"]
    ].copy()
    for n, df in chunks_by_pipe.items():
        base[f"hr_{n}"] = df["hr_bpm"].to_numpy()
    pq.write_table(pa.Table.from_pandas(base, preserve_index=False),
                   out_dir / "chunks_hr.parquet", compression="zstd")

    # Top-level benchmark.json.
    benchmark = {
        "session_dir": str(session.session_dir),
        "fs_measured": session.fs_measured,
        "n_samples": int(len(session.ir)),
        "duration_s": float((session.t_unix[-1] - session.t_unix[0])) if len(session.t_unix) else 0.0,
        "detectors_run": detectors_run,
        "detectors_skipped": detectors_skipped,
        "per_pipeline": {
            n: {
                "n_peaks": int(len(results[n].peak_indices)),
                "runtime_ms": float(results[n].runtime_ms),
                "mean_hr_bpm": float(chunks_by_pipe[n]["hr_bpm"].dropna().mean())
                    if len(chunks_by_pipe[n]["hr_bpm"].dropna()) else None,
                "consensus_f1": consensus_f1.get(n),
            }
            for n in detectors_run
        },
        "pairwise_f1": {f"{a}__vs__{b}": v for (a, b), v in pairwise.items()},
        "consensus_f1": consensus_f1,
    }
    (out_dir / "benchmark.json").write_text(json.dumps(benchmark, indent=2, default=str))

    # Markdown report.
    report = render_report(session, results, detectors_run, detectors_skipped,
                           chunks_by_pipe, pairwise, consensus_f1)
    (out_dir / "report.md").write_text(report)
    return out_dir


def render_report(
    session: Session,
    results: dict[str, PipelineResult],
    detectors_run: list[str],
    detectors_skipped: list[str],
    chunks_by_pipe: dict[str, pd.DataFrame],
    pairwise: dict[tuple[str, str], dict],
    consensus_f1: dict[str, float],
) -> str:
    lines: list[str] = []
    lines.append(f"# Benchmark report — {session.session_dir.name}")
    lines.append("")
    lines.append(f"- Duration: {(session.t_unix[-1] - session.t_unix[0]):.1f} s")
    lines.append(f"- Samples: {len(session.ir)}")
    lines.append(f"- fs (measured): {session.fs_measured:.2f} Hz")
    lines.append(f"- Subject: {session.meta.get('species', '?')} / mount: {session.meta.get('mount', '?')}")
    if detectors_skipped:
        lines.append(f"- Detectors skipped this run: {', '.join(detectors_skipped)}")
    lines.append("")

    lines.append("## Per-pipeline summary")
    lines.append("")
    lines.append("| Pipeline | Peaks | Mean HR (bpm) | HR CV | Runtime (ms) | Consensus F1 |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for n in detectors_run:
        valid = chunks_by_pipe[n]["hr_bpm"].dropna()
        mean_hr = f"{valid.mean():.1f}" if len(valid) else "—"
        cv = f"{valid.std() / valid.mean():.3f}" if len(valid) and valid.mean() > 0 else "—"
        rt = f"{results[n].runtime_ms:.1f}"
        f1 = f"{consensus_f1.get(n, float('nan')):.3f}" if not np.isnan(consensus_f1.get(n, float('nan'))) else "—"
        lines.append(f"| {n} | {len(results[n].peak_indices)} | {mean_hr} | {cv} | {rt} | {f1} |")
    lines.append("")

    if len(detectors_run) >= 2:
        lines.append("## Pairwise agreement (F1, ±50 ms)")
        lines.append("")
        header = "| | " + " | ".join(detectors_run) + " |"
        sep = "|---|" + "|".join(["---:"] * len(detectors_run)) + "|"
        lines.append(header)
        lines.append(sep)
        for a in detectors_run:
            row = [a]
            for b in detectors_run:
                if a == b:
                    row.append("—")
                else:
                    key = (a, b) if (a, b) in pairwise else (b, a)
                    v = pairwise.get(key, {}).get("f1", float("nan"))
                    row.append(f"{v:.3f}" if not np.isnan(v) else "—")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    # Chunk HR table.
    base = next(iter(chunks_by_pipe.values()))[
        ["chunk_idx", "t_start_unix", "finger_present_frac"]
    ].copy()
    for n, df in chunks_by_pipe.items():
        base[f"hr_{n}"] = df["hr_bpm"].to_numpy()
    lines.append(f"## Chunk HR ({int(CHUNK_S)} s windows)")
    lines.append("")
    cols = ["chunk_idx", "t_start_unix", "finger_present_frac"] + [f"hr_{n}" for n in detectors_run]
    header = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join(["---:"] * len(cols)) + "|"
    lines.append(header)
    lines.append(sep)
    for _, row in base.iterrows():
        cells = [
            str(int(row["chunk_idx"])),
            f"{row['t_start_unix']:.1f}",
            f"{row['finger_present_frac']:.2f}",
        ]
        for n in detectors_run:
            v = row[f"hr_{n}"]
            cells.append(f"{v:.1f}" if not np.isnan(v) else "—")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    return "\n".join(lines)


# ---------- session selection ----------

def latest_session(data_dir: Path) -> Path:
    sessions = [p for p in data_dir.glob("session_*") if p.is_dir()]
    if not sessions:
        raise FileNotFoundError(f"no session_* under {data_dir}")
    return max(sessions, key=lambda p: p.name)


def all_sessions(data_dir: Path) -> list[Path]:
    return sorted(p for p in data_dir.glob("session_*") if p.is_dir())


# ---------- main ----------

def parse_detectors(arg: str | None) -> list[str]:
    if not arg:
        return list(pl.ALL_NAMES)
    names = [s.strip().lower() for s in arg.split(",") if s.strip()]
    for n in names:
        if n not in pl.ALL_NAMES:
            raise SystemExit(f"unknown detector {n!r}; valid: {', '.join(pl.ALL_NAMES)}")
    return names


def benchmark_session(session_dir: Path, detectors: list[str]) -> Path:
    print(f"[{session_dir.name}] loading…")
    sess = load_session(session_dir)
    print(f"  {len(sess.ir)} samples, fs={sess.fs_measured:.1f} Hz, "
          f"duration={sess.t_unix[-1]-sess.t_unix[0]:.1f} s")

    # Finger-presence mask uses the shared 0.5-8 Hz cleaner (not any one
    # pipeline's). The mask only gates which chunks count, it doesn't feed
    # the detectors.
    print("  finger-presence mask (BP 0.5–8 Hz)…")
    cleaned_for_mask = clean_ppg(sess.ir, sess.fs_measured)
    finger_mask = finger_present_mask(sess.ir, cleaned_for_mask, sess.fs_measured)
    finger_pct = float(np.mean(finger_mask)) * 100.0
    print(f"  finger-present: {finger_pct:.1f}% of samples")

    results: dict[str, PipelineResult] = {}
    for n in detectors:
        print(f"  running {n}…", end="", flush=True)
        t0 = time.perf_counter()
        results[n] = run_pipeline(n, sess)
        print(f" {results[n].runtime_ms:.0f} ms, {len(results[n].peak_indices)} peaks "
              f"({(time.perf_counter()-t0)*1000:.0f} ms wall)")

    chunks_by_pipe: dict[str, pd.DataFrame] = {}
    t_start = float(sess.t_unix[0])
    t_end = float(sess.t_unix[-1])
    for n, res in results.items():
        chunks_by_pipe[n] = chunk_hr(res, t_start, t_end, finger_mask)

    pairwise: dict[tuple[str, str], dict] = {}
    if len(results) >= 2:
        names = list(results.keys())
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pairwise[(a, b)] = f1_match(
                    results[a].t_unix_per_peak,
                    results[b].t_unix_per_peak,
                )

    consensus = consensus_score(results)

    skipped = [n for n in pl.ALL_NAMES if n not in detectors]
    out_dir = write_outputs(
        sess, results, finger_mask, detectors, skipped,
        chunks_by_pipe, pairwise, consensus,
    )
    print(f"  → {out_dir}")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--latest", action="store_true", help="benchmark the most recent session")
    g.add_argument("--all", action="store_true", help="benchmark every session under --data-dir")
    g.add_argument("--session", type=Path, help="path to a single session_<UTC>/ directory")
    ap.add_argument("--data-dir", type=Path,
                    default=Path(__file__).resolve().parent / "data",
                    help="root directory containing session_*/")
    ap.add_argument("--detector", default=None,
                    help=f"comma-separated subset of {','.join(pl.ALL_NAMES)} (default: all)")
    args = ap.parse_args()

    detectors = parse_detectors(args.detector)
    print(f"detectors: {', '.join(detectors)}")

    if args.session:
        benchmark_session(args.session, detectors)
    elif args.latest:
        benchmark_session(latest_session(args.data_dir), detectors)
    else:  # --all
        sess_dirs = all_sessions(args.data_dir)
        if not sess_dirs:
            print(f"no sessions under {args.data_dir}", file=sys.stderr)
            return 1
        for d in sess_dirs:
            try:
                benchmark_session(d, detectors)
            except Exception as e:
                print(f"  ! failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
