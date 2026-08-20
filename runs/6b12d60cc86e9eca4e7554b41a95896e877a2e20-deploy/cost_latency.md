# Cost & latency breakdown

## Per-cell

| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |
|---|---|---|---:|---:|---:|---:|
| Claude Sonnet 4 | L1137 | error_prone | 1 | 2.8 | 2.8 | 1268/8 |
| Claude Sonnet 4 | L1425 | error_prone | 1 | 1.3 | 1.3 | 1268/17 |
| Gemini 3 Flash | L1137 | error_prone | 1 | 1.3 | 1.3 | 1268/15 |
| Gemini 3 Flash | L1425 | error_prone | 1 | 1.4 | 1.4 | 1268/22 |

## Aggregate per model

| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 2 | 1.0 | 2.0 | 2.02 |
| Gemini 3 Flash | 2 | 1.0 | 1.4 | 1.36 |

## Judge cost

- Judge model: Claude Opus
- Total cells scored: 4
- Total input tokens: 12,195
- Total output tokens: 10,367
