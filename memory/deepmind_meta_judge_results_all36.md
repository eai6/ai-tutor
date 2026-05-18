# Post-hoc meta-judge results — BEA 2025 rubric (all36)

Cross-vendor evaluation of every saved tutor turn across the 36 experiment sessions covered (9 models), using the BEA 2025 Shared Task on AI Tutor Evaluation rubric (https://sig-edu.org/sharedtask/2025).

Two non-Anthropic meta-judges rate each tutor turn on 4 dimensions, 3-point ordinal scale ("Yes" / "To some extent" / "No"):

- **Mistake_Identification** — does the tutor recognize the student made a mistake?
- **Mistake_Location** — does the tutor point to where the mistake is?
- **Providing_Guidance** — does the tutor offer correct, useful guidance?
- **Actionability** — is it clear what the student should do next?

Meta-judges: **Gemini 2.5 Flash** (Google) and **GPT-4o** (OpenAI). Chosen non-Anthropic to avoid same-vendor bias on Anthropic-tutored sessions. Both temperature=0.

Scoring convention: "Yes"=1.0, "To some extent"=0.5, "No"=0.0. Combined per-turn score is mean of the two judges; per-cell score is mean across mistake-response turns only (non-mistake turns excluded — e.g. warm-ups, introductions, transitions).

**Inter-judge agreement** (exact-label match, mistake-response turns): 69.5% across 128 comparisons.

## Per-model averages (across all sessions)

| model | mistake turns | non-mistake | Mistake_ID | Mistake_Loc | Guidance | Actionability | BEA mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| Claude Haiku 4.5 | 9 | 25 | 0.89 | 0.78 | 0.78 | 0.89 | **0.83** |
| Claude Opus 4.7 | 4 | 28 | 1.00 | 0.75 | 1.00 | 1.00 | **0.94** |
| Claude Sonnet 4 | 4 | 28 | 0.75 | 0.25 | 0.62 | 0.62 | **0.56** |
| GPT-4o | 9 | 20 | 0.64 | 0.17 | 0.72 | 0.72 | **0.56** |
| GPT-4o mini | 11 | 19 | 0.73 | 0.20 | 0.89 | 0.93 | **0.69** |
| GPT-5 | 8 | 10 | 0.62 | 0.25 | 0.66 | 0.69 | **0.55** |
| Gemini 2.5 Flash | 10 | 28 | 1.00 | 0.57 | 0.95 | 0.95 | **0.87** |
| Gemini 3.1 Flash | 8 | 11 | 0.50 | 0.06 | 0.50 | 0.50 | **0.39** |
| Gemini 3.1 Pro | 13 | 77 | 0.94 | 0.54 | 0.81 | 0.69 | **0.75** |

## Per (model × lesson × persona) cell

| model | lesson | persona | mistake turns | Mistake_ID | Mistake_Loc | Guidance | Actionability |
|---|---:|---|---:|---:|---:|---:|---:|
| Claude Haiku 4.5 | 540 | capable | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| Claude Haiku 4.5 | 540 | struggler | 2 | 1.00 | 1.00 | 1.00 | 1.00 |
| Claude Haiku 4.5 | 638 | capable | 4 | 1.00 | 0.75 | 0.75 | 0.75 |
| Claude Haiku 4.5 | 638 | struggler | 2 | 0.50 | 0.50 | 0.50 | 1.00 |
| Claude Opus 4.7 | 540 | capable | 0 | nan | nan | nan | nan |
| Claude Opus 4.7 | 540 | struggler | 0 | nan | nan | nan | nan |
| Claude Opus 4.7 | 638 | capable | 2 | 1.00 | 1.00 | 1.00 | 1.00 |
| Claude Opus 4.7 | 638 | struggler | 2 | 1.00 | 0.50 | 1.00 | 1.00 |
| Claude Sonnet 4 | 540 | capable | 0 | nan | nan | nan | nan |
| Claude Sonnet 4 | 540 | struggler | 0 | nan | nan | nan | nan |
| Claude Sonnet 4 | 638 | capable | 1 | 1.00 | 0.00 | 0.00 | 0.00 |
| Claude Sonnet 4 | 638 | struggler | 3 | 0.67 | 0.33 | 0.83 | 0.83 |
| GPT-4o | 540 | capable | 0 | nan | nan | nan | nan |
| GPT-4o | 540 | struggler | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| GPT-4o | 638 | capable | 4 | 0.62 | 0.00 | 0.75 | 0.75 |
| GPT-4o | 638 | struggler | 4 | 0.56 | 0.12 | 0.62 | 0.62 |
| GPT-4o mini | 540 | capable | 3 | 0.83 | 0.42 | 0.67 | 0.83 |
| GPT-4o mini | 540 | struggler | 1 | 1.00 | 0.50 | 1.00 | 1.00 |
| GPT-4o mini | 638 | capable | 5 | 0.60 | 0.10 | 1.00 | 1.00 |
| GPT-4o mini | 638 | struggler | 2 | 0.75 | 0.00 | 0.88 | 0.88 |
| GPT-5 | 540 | capable | 1 | 1.00 | 0.50 | 1.00 | 1.00 |
| GPT-5 | 540 | struggler | 4 | 0.69 | 0.12 | 0.75 | 0.75 |
| GPT-5 | 638 | capable | 2 | 0.12 | 0.00 | 0.12 | 0.25 |
| GPT-5 | 638 | struggler | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| Gemini 2.5 Flash | 540 | capable | 1 | 1.00 | 0.50 | 1.00 | 1.00 |
| Gemini 2.5 Flash | 540 | struggler | 5 | 1.00 | 0.80 | 1.00 | 1.00 |
| Gemini 2.5 Flash | 638 | capable | 2 | 1.00 | 0.50 | 0.75 | 0.75 |
| Gemini 2.5 Flash | 638 | struggler | 2 | 1.00 | 0.12 | 1.00 | 1.00 |
| Gemini 3.1 Flash | 540 | capable | 1 | 0.00 | 0.00 | 0.00 | 0.00 |
| Gemini 3.1 Flash | 540 | struggler | 1 | 0.00 | 0.00 | 0.00 | 0.00 |
| Gemini 3.1 Flash | 638 | capable | 2 | 0.88 | 0.25 | 0.75 | 0.75 |
| Gemini 3.1 Flash | 638 | struggler | 4 | 0.56 | 0.00 | 0.62 | 0.62 |
| Gemini 3.1 Pro | 540 | capable | 0 | nan | nan | nan | nan |
| Gemini 3.1 Pro | 540 | struggler | 5 | 0.90 | 0.60 | 1.00 | 0.70 |
| Gemini 3.1 Pro | 638 | capable | 1 | 1.00 | 0.50 | 1.00 | 1.00 |
| Gemini 3.1 Pro | 638 | struggler | 7 | 0.96 | 0.50 | 0.64 | 0.64 |

## Label distribution per model (raw judge votes)

Combined across both meta-judges. Each mistake-response turn contributes 2 votes per dimension (one per judge).

| model | dim | Yes | To some extent | No |
|---|---|---:|---:|---:|
| Claude Haiku 4.5 | Mistake_Identification | 11 (92%) | 0 (0%) | 1 (8%) |
| Claude Haiku 4.5 | Mistake_Location | 10 (83%) | 0 (0%) | 2 (17%) |
| Claude Haiku 4.5 | Providing_Guidance | 10 (83%) | 0 (0%) | 2 (17%) |
| Claude Haiku 4.5 | Actionability | 11 (92%) | 0 (0%) | 1 (8%) |
| Claude Opus 4.7 | Mistake_Identification | 5 (100%) | 0 (0%) | 0 (0%) |
| Claude Opus 4.7 | Mistake_Location | 4 (80%) | 0 (0%) | 1 (20%) |
| Claude Opus 4.7 | Providing_Guidance | 5 (100%) | 0 (0%) | 0 (0%) |
| Claude Opus 4.7 | Actionability | 5 (100%) | 0 (0%) | 0 (0%) |
| Claude Sonnet 4 | Mistake_Identification | 5 (83%) | 0 (0%) | 1 (17%) |
| Claude Sonnet 4 | Mistake_Location | 2 (33%) | 0 (0%) | 4 (67%) |
| Claude Sonnet 4 | Providing_Guidance | 4 (67%) | 0 (0%) | 2 (33%) |
| Claude Sonnet 4 | Actionability | 4 (67%) | 0 (0%) | 2 (33%) |
| GPT-4o | Mistake_Identification | 7 (54%) | 4 (31%) | 2 (15%) |
| GPT-4o | Mistake_Location | 3 (23%) | 0 (0%) | 10 (77%) |
| GPT-4o | Providing_Guidance | 9 (69%) | 2 (15%) | 2 (15%) |
| GPT-4o | Actionability | 9 (69%) | 2 (15%) | 2 (15%) |
| GPT-4o mini | Mistake_Identification | 7 (47%) | 8 (53%) | 0 (0%) |
| GPT-4o mini | Mistake_Location | 1 (7%) | 4 (27%) | 10 (67%) |
| GPT-4o mini | Providing_Guidance | 11 (73%) | 4 (27%) | 0 (0%) |
| GPT-4o mini | Actionability | 13 (87%) | 2 (13%) | 0 (0%) |
| GPT-5 | Mistake_Identification | 4 (40%) | 4 (40%) | 2 (20%) |
| GPT-5 | Mistake_Location | 1 (10%) | 2 (20%) | 7 (70%) |
| GPT-5 | Providing_Guidance | 6 (60%) | 1 (10%) | 3 (30%) |
| GPT-5 | Actionability | 6 (60%) | 2 (20%) | 2 (20%) |
| Gemini 2.5 Flash | Mistake_Identification | 15 (100%) | 0 (0%) | 0 (0%) |
| Gemini 2.5 Flash | Mistake_Location | 7 (47%) | 4 (27%) | 4 (27%) |
| Gemini 2.5 Flash | Providing_Guidance | 14 (93%) | 1 (7%) | 0 (0%) |
| Gemini 2.5 Flash | Actionability | 14 (93%) | 1 (7%) | 0 (0%) |
| Gemini 3.1 Flash | Mistake_Identification | 4 (36%) | 4 (36%) | 3 (27%) |
| Gemini 3.1 Flash | Mistake_Location | 0 (0%) | 1 (9%) | 10 (91%) |
| Gemini 3.1 Flash | Providing_Guidance | 4 (36%) | 4 (36%) | 3 (27%) |
| Gemini 3.1 Flash | Actionability | 4 (36%) | 4 (36%) | 3 (27%) |
| Gemini 3.1 Pro | Mistake_Identification | 19 (90%) | 2 (10%) | 0 (0%) |
| Gemini 3.1 Pro | Mistake_Location | 10 (48%) | 4 (19%) | 7 (33%) |
| Gemini 3.1 Pro | Providing_Guidance | 16 (76%) | 2 (10%) | 3 (14%) |
| Gemini 3.1 Pro | Actionability | 14 (67%) | 3 (14%) | 4 (19%) |

## Per-judge breakdown (sanity check)

| model | judge | Mistake_ID | Mistake_Loc | Guidance | Actionability |
|---|---|---:|---:|---:|---:|
| Claude Haiku 4.5 | Gemini 2.5 Flash | 0.80 | 0.80 | 0.80 | 1.00 |
| Claude Haiku 4.5 | GPT-4o | 1.00 | 0.86 | 0.86 | 0.86 |
| Claude Opus 4.7 | Gemini 2.5 Flash | 1.00 | 1.00 | 1.00 | 1.00 |
| Claude Opus 4.7 | GPT-4o | 1.00 | 0.75 | 1.00 | 1.00 |
| Claude Sonnet 4 | Gemini 2.5 Flash | 1.00 | 0.50 | 1.00 | 1.00 |
| Claude Sonnet 4 | GPT-4o | 0.75 | 0.25 | 0.50 | 0.50 |
| GPT-4o | Gemini 2.5 Flash | 0.62 | 0.25 | 0.75 | 0.75 |
| GPT-4o | GPT-4o | 0.80 | 0.20 | 0.80 | 0.80 |
| GPT-4o mini | Gemini 2.5 Flash | 0.72 | 0.22 | 0.94 | 1.00 |
| GPT-4o mini | GPT-4o | 0.75 | 0.17 | 0.75 | 0.83 |
| GPT-5 | Gemini 2.5 Flash | 0.62 | 0.25 | 0.62 | 0.69 |
| GPT-5 | GPT-4o | 0.50 | 0.00 | 0.75 | 0.75 |
| Gemini 2.5 Flash | Gemini 2.5 Flash | 1.00 | 0.88 | 1.00 | 1.00 |
| Gemini 2.5 Flash | GPT-4o | 1.00 | 0.29 | 0.93 | 0.93 |
| Gemini 3.1 Flash | Gemini 2.5 Flash | 0.57 | 0.07 | 0.64 | 0.64 |
| Gemini 3.1 Flash | GPT-4o | 0.50 | 0.00 | 0.38 | 0.38 |
| Gemini 3.1 Pro | Gemini 2.5 Flash | 0.96 | 0.71 | 1.00 | 0.88 |
| Gemini 3.1 Pro | GPT-4o | 0.94 | 0.39 | 0.56 | 0.56 |

## Methodology notes

- Rubric source: BEA 2025 Shared Task on AI Tutor Evaluation, https://sig-edu.org/sharedtask/2025.
- Judges first decide `is_mistake_response`: true iff the prior student input contains a mistake / confusion the tutor is addressing. Warm-ups, introductions, transitions, and responses to fully-correct student answers are excluded from BEA scoring.
- A turn is counted as mistake-response if EITHER judge flags it as such (liberal inclusion to maximize signal).
- Per-turn combined score = mean of the two judges; per-cell score = mean across mistake-response turns; BEA mean = mean of the 4 per-cell dimension scores.
- Errors / parse failures are silently dropped from per-axis averages. The 'mistake turns' count reflects judged turns; errored turns are excluded entirely.
- Same lesson context provided to both judges: lesson title, objective, unit, course, grade level; prior tutor turn; preceding student input; turn under evaluation; next student input.

Raw per-turn data: `memory/.deepmind_meta_judge_scores_all36.jsonl` (322 rows).