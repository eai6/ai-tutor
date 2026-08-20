# Cost & latency breakdown

## Per-cell

| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |
|---|---|---|---:|---:|---:|---:|
| Claude Sonnet 4 | L1137 | error_prone | 1 | 2.6 | 2.6 | 1268/23 |
| Claude Sonnet 4 | L1425 | error_prone | 1 | 1.7 | 1.7 | 1268/22 |
| Gemini 3 Flash | L1137 | error_prone | 1 | 1.5 | 1.5 | 1268/21 |
| Gemini 3 Flash | L1425 | error_prone | 1 | 10.5 | 10.5 | 1268/12 |

## Aggregate per model

| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 2 | 1.0 | 2.1 | 2.12 |
| Gemini 3 Flash | 2 | 1.0 | 6.0 | 6.00 |

## Judge cost

- Judge model: Claude Opus
- Total cells scored: 4
- Total input tokens: 12,216
- Total output tokens: 9,877
