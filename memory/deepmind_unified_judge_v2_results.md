# Unified multi-axis judge — v2 (improved prompt + history context)

Sample: **100 tutor turns**, same seed=42 as v1 → direct comparison.

## What's different from v1

- **Definitions + examples per dimension** lifted from the production individual judges (`apps/tutoring/judges/*.py`). v1 used a 1-line description per dim.
- **Full conversation history**: last 8 turns with role labels, not just the immediately-preceding student input.
- **Per-dimension reasoning field**: each dimension requires a ≤30-word `reasoning` BEFORE the verdict (chain-of-thought-per-axis; per Tam et al. 2025 — strict JSON during generation hurts multi-aspect recall).
- **Step context**: bank_offered, bank_will_render, step_phase, subject_is_math threaded into prompt.
- **XML tag structure**: `<role>`, `<lesson_context>`, `<conversation_history>`, `<current_turn>`, `<step_context>`, `<dimensions>`, `<output_format>`.
- **max_tokens bumped to 3500** (v1 had truncation at 1500 on Gemini).

## Headline — per-judge agreement vs production baseline

| judge | dim | agreement | recall (flag→flag) | specificity (clean→clean) |
|---|---|---:|---:|---:|
| anthropic/claude-haiku-4-5-20251001 | factual_flagged | 92.0% (92/100) | 0.0% (0/8) | 100.0% (92/92) |
| anthropic/claude-haiku-4-5-20251001 | rule_flagged | 77.0% (77/100) | 41.9% (13/31) | 92.8% (64/69) |
| anthropic/claude-haiku-4-5-20251001 | coherence_flagged | 72.0% (72/100) | 20.8% (5/24) | 88.2% (67/76) |
| anthropic/claude-haiku-4-5-20251001 | figure_ref_flagged | 92.0% (92/100) | 62.5% (5/8) | 94.6% (87/92) |
| anthropic/claude-haiku-4-5-20251001 | safety_flagged | 100.0% (100/100) | nan% (0/0) | 100.0% (100/100) |
| anthropic/claude-haiku-4-5-20251001 | step_complete | 53.8% (7/13) | 66.7% (4/6) | 42.9% (3/7) |
| anthropic/claude-haiku-4-5-20251001 | answer_correct | 75.0% (6/8) | 100.0% (6/6) | 0.0% (0/2) |
| google/gemini-2.5-flash | factual_flagged | 90.8% (89/98) | 0.0% (0/8) | 98.9% (89/90) |
| google/gemini-2.5-flash | rule_flagged | 73.5% (72/98) | 35.5% (11/31) | 91.0% (61/67) |
| google/gemini-2.5-flash | coherence_flagged | 74.5% (73/98) | 20.8% (5/24) | 91.9% (68/74) |
| google/gemini-2.5-flash | figure_ref_flagged | 89.8% (88/98) | 37.5% (3/8) | 94.4% (85/90) |
| google/gemini-2.5-flash | safety_flagged | 100.0% (98/98) | nan% (0/0) | 100.0% (98/98) |
| google/gemini-2.5-flash | step_complete | 38.5% (5/13) | 50.0% (3/6) | 28.6% (2/7) |
| google/gemini-2.5-flash | answer_correct | 75.0% (6/8) | 100.0% (6/6) | 0.0% (0/2) |

## Cost + latency per call

| judge | avg input tokens | avg output tokens | avg latency | errors |
|---|---:|---:|---:|---:|
| anthropic/claude-haiku-4-5-20251001 | 3153 | 521 | 6.33s | 0 |
| google/gemini-2.5-flash | 2996 | 375 | 6.29s | 2 |

## v1 → v2 recall delta (this is the headline)

| judge | dim | v1 recall | v2 recall | delta |
|---|---|---:|---:|---:|
| anthropic/claude-haiku-4-5-20251001 | factual_flagged | 12.5% | 0.0% | -12.5pp |
| anthropic/claude-haiku-4-5-20251001 | rule_flagged | 67.7% | 41.9% | -25.8pp |
| anthropic/claude-haiku-4-5-20251001 | coherence_flagged | 25.0% | 20.8% | -4.2pp |
| anthropic/claude-haiku-4-5-20251001 | figure_ref_flagged | 75.0% | 62.5% | -12.5pp |
| anthropic/claude-haiku-4-5-20251001 | safety_flagged | nan% | nan% | n/a |
| anthropic/claude-haiku-4-5-20251001 | step_complete | 83.3% | 66.7% | -16.6pp |
| anthropic/claude-haiku-4-5-20251001 | answer_correct | 100.0% | 100.0% | +0.0pp |
| google/gemini-2.5-flash | factual_flagged | 0.0% | 0.0% | +0.0pp |
| google/gemini-2.5-flash | rule_flagged | 46.7% | 35.5% | -11.2pp |
| google/gemini-2.5-flash | coherence_flagged | 20.0% | 20.8% | +0.8pp |
| google/gemini-2.5-flash | figure_ref_flagged | 28.6% | 37.5% | +8.9pp |
| google/gemini-2.5-flash | safety_flagged | nan% | nan% | n/a |
| google/gemini-2.5-flash | step_complete | 100.0% | 50.0% | -50.0pp |
| google/gemini-2.5-flash | answer_correct | 100.0% | 100.0% | +0.0pp |

## Methodology

- Same 100 turns as v1 (random seed=42) for direct comparability.
- Baseline = saved production judge_outputs (mostly Opus 4.7 specialist judges).
- Recall = of the turns the production judge flagged, what fraction did the unified judge also flag?
- Specificity = of the turns the production judge cleared, what fraction did the unified judge also clear?
- Both unified judges run with temperature=0, max_tokens=3500.
- v1 results: `memory/deepmind_unified_judge_results.md`

Raw per-turn JSONL: `memory/.deepmind_unified_judge_v2_scores.jsonl`