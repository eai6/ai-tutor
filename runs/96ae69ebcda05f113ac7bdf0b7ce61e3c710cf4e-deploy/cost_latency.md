# Cost & latency breakdown

## Per-cell

| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |
|---|---|---|---:|---:|---:|---:|
| Claude Sonnet 4 | L1137 | error_prone | 20 | 327.8 | 16.4 | 44074/536 |
| Claude Sonnet 4 | L1425 | error_prone | 20 | 310.3 | 15.5 | 54774/695 |
| Gemini 3 Flash | L1137 | error_prone | 7 | 227.5 | 32.5 | 11000/35 |
| Gemini 3 Flash | L1425 | error_prone | 7 | 242.4 | 34.6 | 12039/175 |

## Aggregate per model

| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 2 | 20.0 | 319.1 | 15.95 |
| Gemini 3 Flash | 2 | 7.0 | 234.9 | 33.56 |

## Judge cost

- Judge model: Claude Opus
- Total cells scored: 4
- Total input tokens: 22,827
- Total output tokens: 28,157
