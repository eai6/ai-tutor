# Cost & latency breakdown

## Per-cell

| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |
|---|---|---|---:|---:|---:|---:|
| Claude Sonnet 4 | L1137 | error_prone | 20 | 277.2 | 13.9 | 47424/384 |
| Claude Sonnet 4 | L1425 | error_prone | 20 | 300.8 | 15.0 | 54063/817 |
| Gemini 3 Flash | L1137 | error_prone | 20 | 783.3 | 39.2 | 40556/390 |
| Gemini 3 Flash | L1425 | error_prone | 20 | 622.3 | 31.1 | 46152/430 |

## Aggregate per model

| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 2 | 20.0 | 289.0 | 14.45 |
| Gemini 3 Flash | 2 | 20.0 | 702.8 | 35.14 |

## Judge cost

- Judge model: Claude Opus
- Total cells scored: 4
- Total input tokens: 29,862
- Total output tokens: 38,504
