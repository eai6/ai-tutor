# Post-hoc meta-judge results — Opus vs Haiku (BEA 2025 rubric)

Cross-vendor evaluation of every saved tutor turn from the 8 Opus + Haiku experiment sessions, using the BEA 2025 Shared Task on AI Tutor Evaluation rubric (https://sig-edu.org/sharedtask/2025).

Two non-Anthropic meta-judges rate each tutor turn on 4 dimensions, 3-point ordinal scale ("Yes" / "To some extent" / "No"):

- **Mistake_Identification** — does the tutor recognize the student made a mistake?
- **Mistake_Location** — does the tutor point to where the mistake is?
- **Providing_Guidance** — does the tutor offer correct, useful guidance?
- **Actionability** — is it clear what the student should do next?

Meta-judges: **Gemini 2.5 Flash** (Google) and **GPT-4o** (OpenAI). Chosen non-Anthropic to avoid same-vendor bias on Anthropic-tutored sessions. Both temperature=0.

Scoring convention: "Yes"=1.0, "To some extent"=0.5, "No"=0.0. Combined per-turn score is mean of the two judges; per-cell score is mean across mistake-response turns only (non-mistake turns excluded — e.g. warm-ups, introductions, transitions).

**Inter-judge agreement** (exact-label match, mistake-response turns): 93.8% across 16 comparisons.

## Per-model averages (across all sessions)

| model | mistake turns | non-mistake | Mistake_ID | Mistake_Loc | Guidance | Actionability | BEA mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| Claude Haiku 4.5 | 9 | 25 | 1.00 | 0.75 | 0.89 | 0.89 | **0.88** |
| Claude Opus 4.7 | 4 | 28 | 1.00 | 0.75 | 0.88 | 0.88 | **0.88** |

## Per (model × lesson × persona) cell

| model | lesson | persona | mistake turns | Mistake_ID | Mistake_Loc | Guidance | Actionability |
|---|---:|---|---:|---:|---:|---:|---:|
| Claude Haiku 4.5 | 540 | capable | 2 | 1.00 | 0.50 | 1.00 | 1.00 |
| Claude Haiku 4.5 | 540 | struggler | 2 | 1.00 | 1.00 | 1.00 | 1.00 |
| Claude Haiku 4.5 | 638 | capable | 4 | 1.00 | 0.69 | 0.75 | 0.75 |
| Claude Haiku 4.5 | 638 | struggler | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| Claude Opus 4.7 | 540 | capable | 0 | nan | nan | nan | nan |
| Claude Opus 4.7 | 540 | struggler | 0 | nan | nan | nan | nan |
| Claude Opus 4.7 | 638 | capable | 2 | 1.00 | 1.00 | 1.00 | 1.00 |
| Claude Opus 4.7 | 638 | struggler | 2 | 1.00 | 0.50 | 0.75 | 0.75 |

## Label distribution per model (raw judge votes)

Combined across both meta-judges. Each mistake-response turn contributes 2 votes per dimension (one per judge).

| model | dim | Yes | To some extent | No |
|---|---|---:|---:|---:|
| Claude Haiku 4.5 | Mistake_Identification | 12 (100%) | 0 (0%) | 0 (0%) |
| Claude Haiku 4.5 | Mistake_Location | 8 (67%) | 3 (25%) | 1 (8%) |
| Claude Haiku 4.5 | Providing_Guidance | 11 (92%) | 0 (0%) | 1 (8%) |
| Claude Haiku 4.5 | Actionability | 11 (92%) | 0 (0%) | 1 (8%) |
| Claude Opus 4.7 | Mistake_Identification | 5 (100%) | 0 (0%) | 0 (0%) |
| Claude Opus 4.7 | Mistake_Location | 4 (80%) | 0 (0%) | 1 (20%) |
| Claude Opus 4.7 | Providing_Guidance | 4 (80%) | 1 (20%) | 0 (0%) |
| Claude Opus 4.7 | Actionability | 4 (80%) | 1 (20%) | 0 (0%) |

## Per-judge breakdown (sanity check)

| model | judge | Mistake_ID | Mistake_Loc | Guidance | Actionability |
|---|---|---:|---:|---:|---:|
| Claude Haiku 4.5 | Gemini 2.5 Flash | 1.00 | 0.70 | 1.00 | 1.00 |
| Claude Haiku 4.5 | GPT-4o | 1.00 | 0.86 | 0.86 | 0.86 |
| Claude Opus 4.7 | Gemini 2.5 Flash | 1.00 | 1.00 | 1.00 | 1.00 |
| Claude Opus 4.7 | GPT-4o | 1.00 | 0.75 | 0.88 | 0.88 |

## Methodology notes

- Rubric source: BEA 2025 Shared Task on AI Tutor Evaluation, https://sig-edu.org/sharedtask/2025.
- Judges first decide `is_mistake_response`: true iff the prior student input contains a mistake / confusion the tutor is addressing. Warm-ups, introductions, transitions, and responses to fully-correct student answers are excluded from BEA scoring.
- A turn is counted as mistake-response if EITHER judge flags it as such (liberal inclusion to maximize signal).
- Per-turn combined score = mean of the two judges; per-cell score = mean across mistake-response turns; BEA mean = mean of the 4 per-cell dimension scores.
- Errors / parse failures are silently dropped from per-axis averages. The 'mistake turns' count reflects judged turns; errored turns are excluded entirely.
- Same lesson context provided to both judges: lesson title, objective, unit, course, grade level; prior tutor turn; preceding student input; turn under evaluation; next student input.

Raw per-turn data: `memory/.deepmind_meta_judge_scores.jsonl` (66 rows).