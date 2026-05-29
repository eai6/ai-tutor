# Cost & latency breakdown

## Per-cell

| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |
|---|---|---|---:|---:|---:|---:|
| Claude Sonnet 4 | L1137 | error_prone | 7 | 123.4 | 17.6 | 12082/88 |
| Claude Sonnet 4 | L1425 | error_prone | 20 | 324.6 | 16.2 | 54268/751 |
| Gemini 3 Flash | L1137 | error_prone | 20 | 669.0 | 33.5 | 39977/312 |
| Gemini 3 Flash | L1425 | error_prone | 20 | 656.3 | 32.8 | 44443/879 |

## Aggregate per model

| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 2 | 13.5 | 224.0 | 16.93 |
| Gemini 3 Flash | 2 | 20.0 | 662.7 | 33.13 |

## Judge cost

- Judge model: Claude Opus
- Total cells scored: 4
- Total input tokens: 25,801
- Total output tokens: 34,180
