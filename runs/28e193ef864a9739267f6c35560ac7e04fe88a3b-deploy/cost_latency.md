# Cost & latency breakdown

## Per-cell

| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |
|---|---|---|---:|---:|---:|---:|
| Claude Sonnet 4 | L1137 | error_prone | 7 | 119.9 | 17.1 | 13454/318 |
| Claude Sonnet 4 | L1425 | error_prone | 20 | 267.5 | 13.4 | 52828/1004 |
| Gemini 3 Flash | L1137 | error_prone | 20 | 771.2 | 38.6 | 41924/270 |
| Gemini 3 Flash | L1425 | error_prone | 20 | 586.9 | 29.3 | 48056/311 |

## Aggregate per model

| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 2 | 13.5 | 193.7 | 15.26 |
| Gemini 3 Flash | 2 | 20.0 | 679.1 | 33.95 |

## Judge cost

- Judge model: Claude Opus
- Total cells scored: 4
- Total input tokens: 26,783
- Total output tokens: 32,223
