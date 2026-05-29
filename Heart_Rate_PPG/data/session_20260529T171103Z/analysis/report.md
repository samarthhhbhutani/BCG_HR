# Benchmark report — session_20260529T171103Z

- Duration: 105.0 s
- Samples: 20771
- fs (measured): 197.80 Hz
- Subject: human / mount: finger

## Per-pipeline summary

| Pipeline | Peaks | Mean HR (bpm) | HR CV | Runtime (ms) | Consensus F1 |
|---|---:|---:|---:|---:|---:|
| terma | 129 | 87.6 | 0.029 | 2.8 | 0.008 |
| elgendi | 138 | 86.2 | 0.036 | 1082.5 | 0.985 |
| bishop | 135 | 86.6 | 0.031 | 12141.5 | 0.989 |
| charlton | 141 | 86.8 | 0.026 | 20.2 | 0.975 |

## Pairwise agreement (F1, ±50 ms)

| | terma | elgendi | bishop | charlton |
|---|---:|---:|---:|---:|
| terma | — | 0.015 | 0.000 | 0.007 |
| elgendi | 0.015 | — | 0.974 | 0.982 |
| bishop | 0.000 | 0.974 | — | 0.978 |
| charlton | 0.007 | 0.982 | 0.978 | — |

## Chunk HR (30 s windows)

| chunk_idx | t_start_unix | finger_present_frac | hr_terma | hr_elgendi | hr_bishop | hr_charlton |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1780074663.0 | 1.00 | 90.6 | 89.2 | 89.2 | 89.2 |
| 1 | 1780074693.0 | 1.00 | 86.6 | 83.0 | 83.9 | 84.8 |
| 2 | 1780074723.0 | 1.00 | 85.7 | 86.3 | 86.6 | 86.3 |
