# Overnight run report

Generated 2026-08-24T09:56:57Z.

## Arms

| arm | scenarios | passed | failed | end reasons |
|---|---|---|---|---|
| geo_4b_v2 | 34 | 34 | 0 | exit_ticket 32, max_turns 2 |
| math_4b_v2 | 34 | 24 | 10 | exit_ticket 20, max_turns 14 |
| geo_27b_v2 | 34 | 34 | 0 | exit_ticket 33, max_turns 1 |
| math_27b_v2 | INCOMPLETE 19/34 | 14 | 5 | checkpoint survives — resume with --resume |

## Run health (from the per-turn traces)

| arm | turns | hosts | placeholders | retries | picker % |
|---|---|---|---|---|---|
| geo_4b_v2 | 313 | 1 | 0 | 3 | 89% |
| math_4b_v2 | 635 | 1 | 0 | 0 | 64% |
| geo_27b_v2 | 294 | 1 | 0 | 0 | 0% |
| math_27b_v2 | 407 | 1 | 0 | 1 | 0% |

## Teardown

- instance `48486859` destroyed after 16.8h (~$3.01)
- instances remaining: 0 (expect 0)
- tunnel supervisor stopped
- SSH tunnel closed

## Next

- Nothing is committed. Results are untracked under `offline_eval/multi_turn_results/`.
- Cloud leg is prepared but NOT started: `offline_eval/cloud_models_3arm.txt`
  (opus-4-7, gemini-3.5-flash, gpt-5.4-mini — all free-text). Needs no GPU.
- Rebuild the viewer to grade: `./venv/bin/python offline_eval/build_viewer.py`
  (register the new run dirs in `FLAT_RUNS` first).
