# Cost & latency breakdown

## Per-cell

| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |
|---|---|---|---:|---:|---:|---:|
| Claude Sonnet 4 | L1137 | error_prone | 20 | 309.7 | 15.5 | 48226/515 |
| Claude Sonnet 4 | L1425 | error_prone | 20 | 334.9 | 16.7 | 55916/607 |
| Gemini 3 Flash | L1137 | error_prone | 20 | 641.7 | 32.1 | 37834/239 |
| Gemini 3 Flash | L1425 | error_prone | 20 | 616.2 | 30.8 | 46516/1080 |

## Aggregate per model

| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 2 | 20.0 | 322.3 | 16.12 |
| Gemini 3 Flash | 2 | 20.0 | 628.9 | 31.45 |

## Judge cost

- Judge model: Claude Opus
- Total cells scored: 4
- Total input tokens: 30,242
- Total output tokens: 38,075
