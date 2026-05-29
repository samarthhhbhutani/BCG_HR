# Benchmark report — session_20260524T201533Z

- Duration: 114.6 s
- Samples: 22657
- fs (measured): 197.73 Hz
- Subject: human / mount: finger

## Per-pipeline summary

| Pipeline | Peaks | Mean HR (bpm) | HR CV | Runtime (ms) | Consensus F1 |
|---|---:|---:|---:|---:|---:|
| terma | 68 | 45.5 | 0.556 | 2.6 | 0.014 |
| elgendi | 91 | 64.1 | 0.128 | 1079.1 | 0.870 |
| bishop | 118 | 66.0 | 0.185 | 13686.1 | 0.745 |
| charlton | 134 | 66.8 | 0.184 | 22.2 | 0.686 |

## Pairwise agreement (F1, ±50 ms)

| | terma | elgendi | bishop | charlton |
|---|---:|---:|---:|---:|
| terma | — | 0.013 | 0.011 | 0.010 |
| elgendi | 0.013 | — | 0.699 | 0.720 |
| bishop | 0.011 | 0.699 | — | 0.905 |
| charlton | 0.010 | 0.720 | 0.905 | — |

## Chunk HR (30 s windows)

| chunk_idx | t_start_unix | finger_present_frac | hr_terma | hr_elgendi | hr_bishop | hr_charlton |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1779653733.0 | 0.97 | 16.4 | — | 80.0 | 80.8 |
| 1 | 1779653763.0 | 0.90 | 62.3 | 69.9 | 60.4 | 61.8 |
| 2 | 1779653793.0 | 1.00 | 57.8 | 58.3 | 57.5 | 57.8 |
