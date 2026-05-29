# Heart_Rate_PPG

PPG-based heart rate pipeline for M5StickC PLUS2 + MAX30102.

## Layout
```
Heart_Rate_PPG/
  firmware/ppg_streamer/ppg_streamer.ino    flash to M5StickC PLUS2
  pc/                                        Python pipeline
    protocol.py        wire format
    ringbuffer.py      shared sample buffer
    receiver.py        serial → ring buffer → parquet
    hr_worker.py       TERMA peak detector + IBI HR
    subject_config.py  per-species params (human/dog/cat)
    app.py             PyQt6 live UI
    subjects/*.json    species presets
    requirements.txt
  data/                                      session_<UTC>/ output goes here
```

## Hardware
- M5StickC PLUS2 (ESP32-PICO-V3-02)
- MAX30102 breakout
- Wiring (M5StickC PLUS2 → MAX30102):
  - `G0`  → SDA
  - `G26` → SCL
  - 5V → VIN
  - GND → GND

## Firmware
Open `firmware/ppg_streamer/ppg_streamer.ino` in the Arduino IDE.

Required libraries:
- `M5StickCPlus2` (M5Stack)
- `SparkFun MAX3010x Pulse and Proximity Sensor Library`

Board: `M5StickC-PLUS2`. Flash, then unplug-replug USB.

## PC pipeline

```sh
cd Heart_Rate_PPG/pc
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python app.py --species human          # human finger probe
python app.py --species dog            # dog ear-clip
python app.py --species cat
python app.py --species subjects/human.json   # or path to a custom config
```

Press **Space** (or click *Start Recording*) to begin a session. A new
`data/session_<UTC>/` directory is created with:
- `ppg_NNNN.parquet` — raw IR/Red samples (rolled every 5 minutes)
- `hr_30s.parquet` — one row per 30 s (mean HR, beats counted, finger-present fraction)
- `session.json` — subject + species + mount + start time + config snapshot

## UI panes
1. **Raw IR** — DC level. Drops sharply when finger is removed.
2. **Band-passed IR + beats** — cleaned signal with detected beat markers.
3. **Instant HR** — per worker update (~1 Hz) when finger present.
4. **30 s mean HR** — one orange dot per 30 s averaging window.

Status row: big HR readout, finger-present indicator (green/grey), live
metadata (IR_DC, AC amplitude, beat count, confidence).

## Cross-species pivot
The pipeline is species-agnostic. Only `subject_config.py` (or a JSON
under `subjects/`) changes — HR range, IBI bounds, bandpass cutoffs,
finger-presence thresholds. No code changes needed for dog/cat.

## Multi-detector benchmark
Each PPG peak detector lives in its own folder under `pipelines/`:

- `pipelines/terma/`      — this project's TERMA + slope-ratio gate
- `pipelines/elgendi/`    — NeuroKit2 reference Elgendi 2013 TERMA
- `pipelines/bishop/`     — NeuroKit2 Bishop 2018 MSPTD
- `pipelines/charlton/`   — NeuroKit2 Charlton 2025 MSPTDfast v2

Recordings already created with `app.py` (under `data/session_<UTC>/`) are the
input — record once, analyse with all detectors:

```sh
python benchmark.py --latest                              # all 4 detectors
python benchmark.py --session data/session_<UTC>          # a specific session
python benchmark.py --all                                 # every session
python benchmark.py --latest --detector terma             # one detector only
python benchmark.py --latest --detector elgendi,charlton  # subset
```

Outputs land in `data/session_<UTC>/analysis/`:
- `report.md`            — human-readable summary, per-pipeline table, pairwise F1, 30 s chunk HR
- `benchmark.json`       — same data, machine-readable
- `chunks_hr.parquet`    — wide table: chunk × {hr_terma, hr_elgendi, hr_bishop, hr_charlton}
- `<detector>/peaks.parquet`, `ibis.parquet`, `chunks.parquet`, `meta.json`

Each pipeline runs end-to-end (its own cleaner + detector). The chunk size is
30 s; HR per chunk = 60 000 / median(IBIs in chunk), null if finger-present
< 70 % or fewer than 5 valid IBIs.

## Live multi-detector pane
Add a 5th plot pane to `app.py` that overlays peaks from all 4 detectors in
different colors and shows each detector's instant HR:

```sh
python pc/app.py --species human --multi-detector              # all 4
python pc/app.py --species human --detector elgendi,charlton   # subset
```

The main HR readout continues to be driven by the existing TERMA `HRWorker`;
the multi-detector pane is a visual sanity check only and is not persisted.
For comparing HRs over a whole session, use `benchmark.py` after the recording.
