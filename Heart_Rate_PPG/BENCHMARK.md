# PPG Multi-Detector Benchmark

How to run four PPG peak-detection pipelines on the same recorded data,
plus what each piece does under the hood.

---

## 1. Quick start

### 1.1 Record a session (existing flow — unchanged)

```sh
cd Heart_Rate_PPG/pc
source .venv/bin/activate
python app.py --species human          # press Space to start/stop recording
```

This writes `Heart_Rate_PPG/data/session_<UTC>/`:
- `ppg_NNNN.parquet` — raw IR + Red samples @ 200 Hz (rolled every 5 minutes)
- `hr_30s.parquet`   — TERMA live HR, one row per 30 s
- `session.json`     — subject + species + start time + config snapshot

### 1.2 Install benchmark dependencies (one-time)

`pandas` and `neurokit2` are added on top of the existing requirements. From
the same venv as the live UI:

```sh
cd Heart_Rate_PPG/pc
source .venv/bin/activate
pip install -r requirements.txt
```

### 1.3 Run the benchmark on a recorded session

From `Heart_Rate_PPG/`:

```sh
# Default: run all 4 detectors on the most-recent session
python benchmark.py --latest

# Specific session
python benchmark.py --session data/session_<UTC>

# Every session under data/
python benchmark.py --all

# One detector only
python benchmark.py --latest --detector charlton

# Subset
python benchmark.py --latest --detector elgendi,charlton
```

Detector names: `terma`, `elgendi`, `bishop`, `charlton`.

### 1.4 View the results

Each session gets a fresh `analysis/` folder:

```
data/session_<UTC>/analysis/
├─ report.md                 ← open this first
├─ benchmark.json            ← same data, machine-readable
├─ chunks_hr.parquet         ← wide table: 30 s × {hr_terma, hr_elgendi, hr_bishop, hr_charlton}
├─ terma/
│  ├─ peaks.parquet          (sample_idx, t_us, t_unix)
│  ├─ ibis.parquet           (t_unix, ibi_ms)
│  ├─ chunks.parquet         (chunk_idx, t_start, t_end, hr_bpm, n_ibis, finger_present_frac)
│  └─ meta.json              (n_peaks, mean_hr, hr_cv, runtime_ms, consensus_f1)
├─ elgendi/                  (same files)
├─ bishop/                   (same files)
└─ charlton/                 (same files)
```

`report.md` contains:
1. Per-pipeline summary (peaks, mean HR, HR CV, runtime, consensus F1)
2. Pairwise agreement matrix (F1 with ±50 ms tolerance)
3. Chunk HR table — each row is one 30 s window, columns are the four detectors

Re-running with a different `--detector` subset overwrites only those
detectors' subfolders; others stay as they were.

### 1.5 Live multi-detector pane (optional)

Adds a 5th plot pane to `app.py` showing peaks from all 4 detectors in
different colors, plus per-detector instant HR up top:

```sh
python pc/app.py --species human --multi-detector              # all 4
python pc/app.py --species human --detector elgendi,charlton   # subset
```

Colors: TERMA red, Elgendi blue, Bishop green, Charlton orange.

The big HR number and `hr_30s.parquet` are still driven by the existing
TERMA `HRWorker`. The pane is visual sanity check only — for end-of-session
comparison use `benchmark.py`.

---

## 2. Implementation details

### 2.1 Folder layout

```
Heart_Rate_PPG/
├─ firmware/ppg_streamer/ppg_streamer.ino    (unchanged)
├─ pc/
│  ├─ app.py                  + --multi-detector / --detector flags
│  ├─ multi_worker.py         NEW — runs all detectors live on the ring buffer
│  ├─ hr_worker.py, receiver.py, ringbuffer.py, …  (unchanged)
│  └─ requirements.txt        + pandas, neurokit2
├─ pipelines/                 NEW
│  ├─ __init__.py             REGISTRY + get(name)
│  ├─ base.py                 BasePipeline ABC, PipelineResult dataclass
│  ├─ common.py               session loader + shared cleaner + finger-presence mask
│  ├─ _nk_helper.py           lazy NK2 import + windowed runner
│  ├─ terma/pipeline.py       project's TERMA + slope-ratio gate (offline)
│  ├─ elgendi/pipeline.py     NK2 ppg_findpeaks(method='elgendi')
│  ├─ bishop/pipeline.py      NK2 ppg_findpeaks(method='bishop')
│  └─ charlton/pipeline.py    NK2 ppg_findpeaks(method='charlton')
├─ benchmark.py               NEW — runner + chunking + agreement + report writer
├─ data/session_<UTC>/        (firmware-recorded; record once, analyse with all)
│  └─ analysis/               NEW — written by benchmark.py
├─ README.md
└─ BENCHMARK.md               (this file)
```

### 2.2 Shared contract

`pipelines/base.py`:

```python
@dataclass(slots=True)
class PipelineResult:
    name: str
    peak_indices: np.ndarray           # sample indices into the cleaned signal
    ibis_ms: np.ndarray                # filtered, plausible IBIs (300–1500 ms)
    t_unix_per_peak: np.ndarray        # absolute UTC time per peak
    runtime_ms: float                  # detect() wall time, excludes cleaning
    fs: float
    n_samples: int

class BasePipeline(ABC):
    name: str
    bandpass_low_hz: float = 0.5
    bandpass_high_hz: float = 8.0

    def clean(ir, fs) -> np.ndarray: ...      # default uses common.clean_ppg
    @abstractmethod
    def detect(cleaned, fs) -> np.ndarray: ...
    def run(ir, fs, t_unix) -> PipelineResult:
        # cleaner -> detect -> IBI sanity filter (300–1500 ms) -> result
```

Each pipeline runs **end-to-end** (its own cleaner + its own detector). This
matters because TERMA is tuned for BP 0.5–4 Hz — feeding it BP 0.5–8 Hz lets
dicrotic-notch energy through and trips the slope-ratio gate.

### 2.3 Cleaner

`pipelines/common.py:clean_ppg(ir, fs, low_hz, high_hz)`:

1. **DC removal**: subtract a 1 s moving-average baseline (matches `hr_worker.py`).
2. **Bandpass**: scipy `butter(order=2)` + `sosfiltfilt` between `low_hz` and `high_hz`.
3. **Sign flip**: MAX30102 IR convention is *more blood → less light → smaller
   IR*, so after BP+DC the systolic peaks point down. Negating makes them
   point up, which all four published detectors expect.

Defaults: `low_hz=0.5`, `high_hz=8.0`. Each detector subclass can override its
own cutoffs:

| Detector | low | high | why |
|---|---|---|---|
| terma    | 0.5 | 4.0 | slope-ratio gate needs the dicrotic notch suppressed |
| elgendi  | 0.5 | 8.0 | NK2 default |
| bishop   | 0.5 | 8.0 | NK2 default |
| charlton | 0.5 | 8.0 | NK2 default; charlton internally downsamples to 20 Hz |

### 2.4 Per-detector pipelines

#### TERMA (`pipelines/terma/`)
Direct port of `pc/hr_worker.py`'s detection logic to offline batch:
1. Square the (sign-flipped) cleaned signal.
2. Two boxcar moving averages — short `W1=111 ms` (peak width), long `W2=667 ms` (beat cycle).
3. Threshold = `MA_long + 0.02 * mean(MA_long)`.
4. Each contiguous block where `MA_short > threshold` and length ≥ `W1` becomes one peak (argmax of `x` in the block).
5. Refine each peak to the local d/dt max within the previous 200 ms (Peralta 2019 — onset is the stable HRV fiducial).
6. Pan-Tompkins slope-ratio gate: reject any candidate within `1.5 × running mean RR` of the previous accepted peak whose upstroke slope is `< 0.5 ×` the previous accepted peak's slope (rejects dicrotic notches).

**Windowed at 30 s.** TERMA's threshold is global over the whole signal; one
finger-off transient (IR drops ~100×) inflates `mean(MA_long)` and starves
real peaks. Live `hr_worker.py` avoids this by operating on 8 s rolling
windows; the offline pipeline reproduces that by slicing into 30 s windows
(long enough for a stable threshold, short enough that one transient can't
poison neighbours).

#### Elgendi (`pipelines/elgendi/`)
NeuroKit2's `ppg_findpeaks(method='elgendi')` — same TERMA approach as above,
*without* onset refinement or the slope-ratio gate. Useful as a sanity check
that the project's TERMA isn't drifting from the published algorithm.

#### Bishop (`pipelines/bishop/`)
NK2's MSPTD (Bishop & Ercole 2018) — multi-scale peak/trough scalogram.
Detrend signal, build `m_max[k][i] = (x[i] > x[i-k] AND x[i] > x[i+k])`,
pick scales with the most marks, peaks are columns where every retained scale
agrees. **O(N²)** in window length, so the pipeline windows at 30 s.

#### Charlton (`pipelines/charlton/`)
NK2's MSPTDfast v2 (Charlton 2025) — windowed/downsampled MSPTD. Internally:
splits the signal into windows with 20 % overlap, downsamples to 20 Hz, drops
scales below 30 bpm. ~10× faster than vanilla MSPTD with comparable accuracy
on benchmark datasets. The pipeline calls it on 120 s slices.

#### Why the helper at `pipelines/_nk_helper.py`?
- **Lazy import.** NK2 pulls in matplotlib + sklearn at import; TERMA-only
  runs shouldn't pay that cost.
- **Windowed runner.** Slices the signal, calls `nk.ppg_findpeaks`, offsets
  per-window peak indices back to absolute, and unique-merges them. Robust
  to NK2 throwing on a flat / corrupt window.

### 2.5 Session loader

`pipelines/common.py:load_session(session_dir) -> Session`:
1. Reads all `ppg_*.parquet` shards, concatenates, sorts by `seq`.
2. Estimates fs from t_us deltas (firmware uses a wrapped `u32` µs counter;
   walking diffs as unsigned 32-bit handles the 71-minute wrap).
3. Reconstructs absolute UTC time per sample from `session.json`'s
   `started_utc` plus cumulative t_us deltas.
4. Returns `Session(meta, fs, fs_measured, t_us, t_unix, ir, red, seq)`.

### 2.6 Benchmark pipeline (`benchmark.py`)

For each session, in order:

1. **Load** via `load_session()`.
2. **Finger-presence mask** — runs the shared 0.5–8 Hz cleaner *once*
   (separate from the per-pipeline cleaners) and computes a per-sample
   bool: `min(ir) >= 50_000 AND max(ir) <= 260_000 AND p2p(cleaned) >= 200`
   over 1 s blocks. The mask only gates which chunks count, it does not feed
   any detector.
3. **Run each detector** end-to-end (`pipeline.run(ir, fs, t_unix)`).
4. **Chunk HR** per detector: 30 s windows, `hr = 60000 / median(IBIs in chunk)`,
   null if `finger_present_frac < 0.7` or `n_ibis < 5`.
5. **Pairwise F1** (±50 ms tolerance, greedy nearest-neighbour matching).
6. **Consensus F1**: cluster all peaks within ±50 ms across detectors, keep
   clusters of quorum size, score each detector against the consensus
   timestamps. Quorum is `ceil(N/2) + 1` for `N > 2` else 2.
7. **Write outputs** under `analysis/`:
   - `analysis/<detector>/peaks.parquet`, `ibis.parquet`, `chunks.parquet`, `meta.json`
   - `analysis/chunks_hr.parquet` — wide table joining all detectors
   - `analysis/benchmark.json` — top-level metrics
   - `analysis/report.md` — markdown with the three tables

### 2.7 Chunk-HR table format

`chunks_hr.parquet`:

| col | type | meaning |
|---|---|---|
| `chunk_idx` | int64 | 0-based, monotonic |
| `t_start_unix` | float64 | UTC seconds, chunk start |
| `t_end_unix` | float64 | UTC seconds, chunk end (always `start + 30`) |
| `finger_present_frac` | float32 | fraction of samples in chunk with finger present |
| `hr_terma` | float32 | bpm, NaN if invalid |
| `hr_elgendi` | float32 | bpm, NaN if invalid |
| `hr_bishop` | float32 | bpm, NaN if invalid |
| `hr_charlton` | float32 | bpm, NaN if invalid |

If a detector wasn't run, its column is omitted from `report.md`'s table but
the wide parquet still has the column present from the prior run (subfolders
are independent).

### 2.8 Live multi-detector worker (`pc/multi_worker.py`)

One thread runs each requested detector on the same 8 s rolling window every
1 s. Per detector: `clean(ir) → detect(cleaned) → median IBI → instant HR`,
filtering IBIs to 300–1500 ms before the median.

The UI's `_update_multi_pane` plots one cleaned signal underneath (TERMA's by
default) and overlays each detector's peaks at the same x-coordinate using
the *reference* cleaner's y value, so the four colored markers align
visually even though each detector saw its own cleaner's amplitude.

Heavy detectors: Bishop ≈ 150 ms per 8 s window, Charlton ≈ 5 ms,
Elgendi (NK2) ≈ 50 ms after first call (cold matplotlib font cache adds
~2 s the very first time). All comfortably fit in the 1 Hz refresh budget.

### 2.9 Adding a new detector

1. Create `pipelines/<name>/__init__.py` and `pipeline.py`.
2. Subclass `BasePipeline`, set `name`, set `bandpass_low_hz` / `bandpass_high_hz`
   if the detector needs different cutoffs, implement `detect(cleaned, fs)`.
3. Register it in `pipelines/__init__.py`'s `REGISTRY`.
4. It immediately works with `--detector <name>`, the live multi-detector
   pane, and the benchmark report.

---

## 3. Known behaviours

- **TERMA scores low consensus F1 on offline benchmarks.** The slope-ratio
  gate is conservative on 30 s windows; live operation on 8 s rolling windows
  is more forgiving. This is a genuine finding the report surfaces, not a
  bug. To loosen the gate, edit `pipelines/terma/pipeline.py:_slope_ratio_gate`
  (`slope_ratio` parameter).
- **Bishop is slow.** O(N²) in window length. We slice at 30 s; longer
  windows are exponentially slower. If you raise it, expect minutes.
- **First Elgendi run is slow.** NK2 imports matplotlib, which builds a font
  cache on cold boot. Subsequent runs are ~50 ms.
- **`t_us` wraps every ~71 min.** The session loader walks diffs as unsigned
  32-bit and accumulates, so multi-hour sessions reconstruct correctly.
