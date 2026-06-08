# Cost & latency breakdown

## Per-cell

| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |
|---|---|---|---:|---:|---:|---:|
| Claude Sonnet 4 | L1137 | error_prone | 7 | 117.8 | 16.8 | 12459/148 |
| Claude Sonnet 4 | L1425 | error_prone | 20 | 370.6 | 18.5 | 55136/840 |
| Gemini 3 Flash | L1137 | error_prone | 7 | 324.4 | 46.3 | 10876/79 |
| Gemini 3 Flash | L1425 | error_prone | 20 | 779.0 | 39.0 | 45885/696 |

## Aggregate per model

| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 2 | 13.5 | 244.2 | 17.68 |
| Gemini 3 Flash | 2 | 13.5 | 551.7 | 42.65 |

## Judge cost

- Judge model: Claude Opus
- Total cells scored: 4
- Total input tokens: 25,855
- Total output tokens: 31,712
