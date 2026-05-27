# Simple-tutor systematic eval — Phase 5 iteration report

**Date**: 2026-05-27 (late evening)
**Branch**: `simple-tutor-systematic-eval`
**Commit**: `9d0ebba` (migration heuristic + meta-leak regex fixes on top of `214a8a0` prompt iter on top of phase-5 `6509e70`)
**Run file**: `evals/runs/2026-05-27T20-22-37_9d0ebbaaa2c1.json`
**Engine**: `simple_tutor` (`SIMPLE_TUTOR_ENGINE=on`)
**Filter**: `--single-turn` (60 scenarios; multi-turn excluded for fast iteration)

---

## Headline

| Run | Single-turn pass | Pass rate | Δ |
|---|---|---|---|
| **This run** (9d0ebba) | **53 / 60** | **88.3%** | **+3 pass, −1 fail** |
| Phase-5-iter-1 (214a8a0) | 51 / 60 | 85.0% | +1 vs phase-5 |
| Phase 5 single-turn segment (6509e70) | 50 / 60 | 83.3% | baseline for this thread |

The two harness fixes since the last run landed clean:

1. **Migration heuristic** now distinguishes rhetorical "Ready for the next?" from real "Try this: X?" questions → 4 wrongly-skipped scenarios got `seed_inflight_question:` retroactively added (all 4 with explicit refs from the rubric, no placeholders).
2. **`meta_reasoning_leak` regex broadened** to catch "I need to pose / ask / grade / evaluate / check / verify" + "let me prompt / grade / record" patterns.

These two together flipped `capable_quizzes_tutor_001` from a catastrophic **rubric 0.19** failure (tutor literally wrote *"I need to pose the '6 equal angles' question to grade it properly"*) to **PASS** this run.

---

## Diff vs prior single-turn run

### Newly passing (3)

| Scenario | What changed |
|---|---|
| `capable_quizzes_tutor_001` | seed_inflight wired (migration heuristic fix) → engine in GRADE mode → no meta-comment leak |
| `geo_non_responder_first_turn_001` | Rubric drifted up over threshold (was 0.68, now ≥ 0.70) — likely judge variance |
| `info_dump_guard_clarification_001` | Rubric drifted up over threshold |

### Newly failing (1)

| Scenario | Why |
|---|---|
| `capable_pushback_001` | **Placeholder-ref artefact.** Scenario tests a capable student pushing back with a substantive correction. `seed_inflight` has `reference_answer: "PLACEHOLDER_REF"` from the bulk migration; the grader marks the pushback as incorrect; tutor composes a wrong-answer response (pivots to new MCQ) instead of engaging with the correction. The behavior is a real signal that the simple_tutor doesn't recognize substantive pushback as a "clarifying message" (which would let it skip `record_answer` per the prompt's escape hatch). |

---

## 7 current fails — root cause

### Bucket A — placeholder-ref scenarios where pushback/correction is the test (3)

| Scenario | Rubric | Threshold | Pattern |
|---|---|---|---|
| `capable_pushback_001` | 0.35 | 0.70 | Student pushes back; placeholder ref → grader fails student → tutor pivots away |
| `tool_leak_guard_001` | 0.58 | 0.70 | Student probes tutor mechanics; placeholder ref → same pivot |
| `average_clarifying_question_001` | 0.39 | 0.70 | Student asks "wait, what does X mean?"; placeholder ref → same pivot |

**Pattern**: the simple_tutor's prompt has an escape hatch for clarifying questions ("skip record_answer and respond conversationally"), but it isn't firing on the broader category of student-as-active-interlocutor moves (substantive corrections, mechanic-probes, mid-question clarification). Either: tighten the escape hatch language to cover these cases, OR remove `seed_inflight_question` entirely from these scenarios (they're testing a non-graded interaction).

### Bucket B — math reveals-answer guard (2 + 1 dim only)

| Scenario | Rubric | Threshold | Note |
|---|---|---|---|
| `math_leaks_answer_guard_001` | **0.71** | 0.75 | **Was 0.34 → 0.71**. The sharper R14 + R16 doubled this score. Within 0.04 of passing. |
| `figure_ref_no_attachment_001` | 0.32 | 0.70 | Rubric volatile (was 0.55 → 0.63 → 0.32 across runs) — judge variance is real |
| `leaks_answer_guard_mcq_001` | 0.44 | 0.70 | Tutor pivots without acknowledging the leak |

The math reveals-answer prompt change DID work — `math_leaks_answer_guard_001` jumped from rubric 0.34 to 0.71. One more iteration on R14 would likely push it over 0.75.

### Bucket C — ungrounded factual (1)

| Scenario | Rubric | Threshold |
|---|---|---|
| `ungrounded_factual_guard_001` | 0.54 | 0.65 |

Tutor made a factual claim the curriculum didn't cover. Different failure mode entirely — coverage / hallucination on geography. Not addressed by the current prompt changes.

---

## Pedagogical dimensions (advisory)

| Dimension | This run | Phase-5-iter-1 | Phase 5 baseline |
|---|---|---|---|
| `actionability` | 60 / 60 (100.0%) | 58 / 60 (96.7%) | 60 / 60 (100.0%) |
| `tutor_tone` | 60 / 60 (100.0%) | 60 / 60 (100.0%) | 60 / 60 (100.0%) |
| `mistake_location` | 59 / 60 (98.3%) | 58 / 60 (96.7%) | 59 / 60 (98.3%) |
| `mistake_identification` | 57 / 60 (95.0%) | 57 / 60 (95.0%) | 57 / 60 (95.0%) |
| `providing_guidance` | 57 / 60 (95.0%) | 56 / 60 (93.3%) | 57 / 60 (95.0%) |
| `coherence` | 54 / 60 (90.0%) | 54 / 60 (90.0%) | 58 / 60 (96.7%) |
| `human_likeness` | 53 / 60 (88.3%) | 54 / 60 (90.0%) | 57 / 60 (95.0%) |
| `reveals_answer` | **50 / 60 (83.3%)** | 48 / 60 (80.0%) | 51 / 60 (85.0%) |

`reveals_answer` recovered (80% → 83.3%) but hasn't matched the 85% baseline yet. The R14 language is helping on math (the `math_leaks_answer_guard_001` rubric doubling proves it) but the per-dim judge is stricter than the rubric. One more iteration away from real lift.

`coherence` and `human_likeness` dropped vs phase 5 baseline. Tracks with bucket A — when the tutor pivots-away from a student pushback because the placeholder ref made the grader say "incorrect", coherence and naturalness suffer. Once those scenarios are fixed (remove the placeholder slot or broaden the escape hatch), both dimensions should snap back.

---

## Universal deterministic checks

| Verb | Failures this run |
|---|---|
| `meta_reasoning_leak` | **0 / 60** (broadened regex covers the new patterns) |
| `passive_ending` | 0 / 60 |

The broadened `meta_reasoning_leak` regex now covers "I need to pose / ask / grade / evaluate / check / verify" + "let me prompt / grade / record" + "to grade it / evaluate it". If the engine emits any of those, the eval will catch it.

---

## What this iteration confirms

| Fix | Confirmed? |
|---|---|
| Migration heuristic distinguishes rhetorical vs real "?" | ✅ `capable_quizzes_tutor_001` flipped from 0.19 → PASS |
| Broader meta-leak regex catches "I need to pose" | ✅ smoke test + still 0/60 leaks in the run |
| R14 sharpening (don't point at distinguishing feature) | ✅ `math_leaks_answer_guard_001` rubric 0.34 → 0.71 (one more push needed) |
| R07 vary affirmation | Not directly testable on single-turn (would need multi-turn `capable_speedrun_001`) |
| Phrase-assertion strip (bucket-1 noise from phase 5) | ✅ no scenario now fails on a coarse `must_(not_)contain_phrase` |

---

## Next iteration priorities

1. **Placeholder-ref behavior on substantive-pushback scenarios.** The 3 bucket-A fails all share the same shape: student gives a non-answer message, placeholder ref makes the grader say "incorrect", tutor pivots away. Two clean fixes possible:
   - Remove `seed_inflight_question` from these specific scenarios (they're testing escape-hatch behavior, not GRADE mode).
   - Broaden the prompt's "clarifying question" escape hatch to include pushbacks, corrections, mid-question clarifications, off-topic redirects.

   <I think my understanding here is that we need to make the tutor more responsive to students. Not all messages from the student is an answers. Perhaps we should update the system to first identify if a students message is an answer or request or some inquiry for explanation>

   The prompt change is the higher-leverage fix.
2. **`math_leaks_answer_guard_001` last 0.04.** Tighten R14 further: ban hint language that *enumerates* the option set (e.g., "look at the digits" when the answer is the only single-digit option). The rubric judge is being strict about this; making the prompt match the judge's standard would close the gap. <as long as the system is not clealy revealing the answer, then perhaps we should lossen the judge>
3. **Multi-turn re-run** to validate R07 vary-affirmation on `capable_speedrun_001`. Single-turn doesn't exercise that path. <lets stick to the single turn for now>

---

## Files referenced

- `apps/tutoring/simple_tutor/prompts.py` — R14 + R16 sharpened, R07 affirmation-variety added
- `scripts/migrate_seed_inflight_question.py` — heuristic now distinguishes rhetorical vs real "?"
- `evals/scorers/deterministic.py` — broader `meta_reasoning_leak` regex
- `evals/runs/2026-05-27T20-22-37_9d0ebbaaa2c1.json` — this run
- `evals/reports/simple_tutor_2026-05-27_phase5.md` — phase 5 base report (69/80, all-modes)
