# Benchmark report — session_20260529T133923Z

- Duration: 123.6 s
- Samples: 24441
- fs (measured): 197.71 Hz
- Subject: human / mount: finger

## Per-pipeline summary

| Pipeline | Peaks | Mean HR (bpm) | HR CV | Runtime (ms) | Consensus F1 |
|---|---:|---:|---:|---:|---:|
| terma | 131 | 66.6 | 0.032 | 3.0 | 0.008 |
| elgendi | 132 | 66.4 | 0.021 | 1008.4 | 0.977 |
| bishop | 125 | 66.3 | 0.021 | 15118.3 | 0.996 |
| charlton | 131 | 66.3 | 0.023 | 22.8 | 0.981 |

## Pairwise agreement (F1, ±50 ms)

| | terma | elgendi | bishop | charlton |
|---|---:|---:|---:|---:|
| terma | — | 0.008 | 0.000 | 0.008 |
| elgendi | 0.008 | — | 0.973 | 0.996 |
| bishop | 0.000 | 0.973 | — | 0.977 |
| charlton | 0.008 | 0.996 | 0.977 | — |

## Chunk HR (30 s windows)

| chunk_idx | t_start_unix | finger_present_frac | hr_terma | hr_elgendi | hr_bishop | hr_charlton |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1780061963.0 | 1.00 | 66.1 | 66.1 | 66.3 | 66.1 |
| 1 | 1780061993.0 | 1.00 | 64.0 | 64.6 | 64.6 | 64.3 |
| 2 | 1780062023.0 | 1.00 | 67.2 | 66.8 | 66.5 | 66.6 |
| 3 | 1780062053.0 | 1.00 | 69.1 | 68.0 | 68.0 | 68.0 |
