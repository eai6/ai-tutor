# Simple-Tutor Eval Run — 2026-05-27

**Run id**: `b03997a3956d`
**Started**: 2026-05-27T01:40:10.919934+00:00
**Finished**: 2026-05-27T01:56:58.539508+00:00
**Engine**: simple_tutor (SIMPLE_TUTOR_ENGINE=on)
**DB**: Azure Postgres staging

## Overall

| Metric | Value |
|---|---|
| Total scenarios | 23 |
| **Pass** | **7 (30%)** |
| Fail | 16 (70%) |
| Error | 0 (0%) |

## By mode

| Mode | Pass / Total | Rate |
|---|---|---|
| multi_turn | 6 / 6 | **100%** |
| single_turn | 1 / 17 | **6%** |

## By persona

| Persona | Pass / Total | Rate |
|---|---|---|
| average | 2 / 8 | 25% |
| capable | 2 / 4 | 50% |
| non_responder | 1 / 2 | 50% |
| probe_resistant | 1 / 2 | 50% |
| struggler | 1 / 7 | 14% |

## Why scenarios failed (assertion frequency)

| Assertion | Failures |
|---|---|
| `max_paragraphs` | 16 |
| `rubric` | 4 |
| `must_end_with_question` | 4 |

## Rubric (LLM judge)

| Metric | Value |
|---|---|
| Rubric pass | 19 / 23 |
| Mean rubric score (passes) | 0.94 |
| Judge tokens in | 18433 |
| Judge tokens out | 6951 |

## Passing scenarios (7)

| Scenario | Mode | Persona | Tutor sample / outcome |
|---|---|---|---|
| `math_correct_advance_001` | single_turn | average | Got it — that's right. Here's the next one: |
| `average_session_completion_001` | multi_turn | average | exit_ticket @ 11 turns |
| `capable_full_session_001` | multi_turn | capable | exit_ticket @ 6 turns |
| `no_banned_opener_loop_capable_001` | multi_turn | capable | exit_ticket @ 8 turns |
| `non_responder_engagement_001` | multi_turn | non_responder | max_turns @ 12 turns |
| `probe_resistant_refusal_chain_001` | multi_turn | probe_resistant | exit_ticket @ 11 turns |
| `struggler_session_completion_001` | multi_turn | struggler | exit_ticket @ 6 turns |

## Failing scenarios (16)

| Scenario | Persona | Failed | Rubric |
|---|---|---|---|
| `off_topic_redirect_001` | average | `max_paragraphs` | 0.95 / 0.65 |
| `no_banned_opener_001` | struggler | `max_paragraphs`, `rubric(0.17<0.70)` | 0.17 / 0.70 |
| `no_unfounded_praise_001` | average | `max_paragraphs` | 1.00 / 0.70 |
| `single_paragraph_001` | average | `max_paragraphs` | 1.00 / 0.70 |
| `math_capable_pushback_001` | capable | `max_paragraphs`, `rubric(0.17<0.70)` | 0.17 / 0.70 |
| `math_false_accept_numeric_001` | struggler | `max_paragraphs` | 1.00 / 0.70 |
| `math_leaks_answer_guard_001` | struggler | `max_paragraphs` | 1.00 / 0.75 |
| `math_wrong_bare_diagnostic_001` | struggler | `max_paragraphs`, `must_end_with_question` | 1.00 / 0.70 |
| `math_wrong_mcq_no_praise_001` | average | `max_paragraphs` | 0.67 / 0.65 |
| `over_eager_working_001` | average | `max_paragraphs` | 0.97 / 0.70 |
| `wrong_answer_diagnostic_001` | struggler | `max_paragraphs`, `must_end_with_question`, `rubric(0.33<0.70)` | 0.33 / 0.70 |
| `average_clarifying_question_001` | average | `must_end_with_question`, `max_paragraphs` | 1.00 / 0.70 |
| `capable_pushback_001` | capable | `max_paragraphs` | 1.00 / 0.70 |
| `non_responder_monosyllabic_001` | non_responder | `max_paragraphs` | 0.97 / 0.70 |
| `probe_resistant_refusal_001` | probe_resistant | `max_paragraphs` | 1.00 / 0.70 |
| `struggler_idk_handling_001` | struggler | `must_end_with_question`, `max_paragraphs`, `rubric(0.57<0.70)` | 0.57 / 0.70 |

## Analysis

### Multi-turn end-to-end: 6/6 ✅

Every multi-turn scenario passed and 5 of 6 reached the exit ticket cleanly. The 6th (`non_responder_engagement_001`) hit `max_turns` by design — that persona gives monosyllabic answers and the scenario tests engagement under that condition. Simple_tutor drove full lesson → exit ticket flow across all 5 personas (struggler, average, capable, probe_resistant, non_responder).

### Single-turn: 1/17 ❌ — dominated by `max_paragraphs`

`max_paragraphs` is cited as a failure cause on **16 of 16 failing single-turn scenarios**. Of those, most also had rubric scores AT or ABOVE threshold (often 1.00/0.70) — the LLM judge thought the response was pedagogically good, the deterministic length cap vetoed it because the response was too long. Simple_tutor's prompt encourages explanation + worked example + question stem + options in one turn, which routinely runs 3-5 paragraphs.

### Real quality issues (rubric below threshold)

- **`no_banned_opener_001`** (rubric 0.17/0.70) — banned_opener, format
- **`math_capable_pushback_001`** (rubric 0.17/0.70) — student_input, math, persona_handling
- **`wrong_answer_diagnostic_001`** (rubric 0.33/0.70) — false_accept_guard, diagnostic, pedagogy
- **`struggler_idk_handling_001`** (rubric 0.57/0.70) — non_answer, scaffolding, persona_handling

## Recommended next steps

1. **Tighten the prompt against multi-paragraph responses** — the M12 prompt instructs "2-4 sentences for questions or clarifications; up to ~150 words for worked examples" but the LLM is exceeding the deterministic check. Add an explicit "max 2 paragraphs" rule. Estimated impact: 9-12 of 13 max_paragraphs failures flip to pass.
2. **Investigate the 4-5 rubric-failing scenarios individually** — those are genuine pedagogy misses (banned opener, false-accept-numeric, struggler IDK handling, math diagnostic).
3. **Re-run after fixes** to lock in the gains. Multi-turn baseline is healthy (6/6); single-turn pass rate should comfortably exceed 70% after prompt tightening.

