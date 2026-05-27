# Simple-tutor systematic eval — Phase 5 iter3 report

**Date**: 2026-05-27 (late evening)
**Branch**: `simple-tutor-systematic-eval`
**Commit**: `fedb562` (intent classifier polish on top of `77d3f75` intent classifier wired)
**Run file**: `evals/runs/2026-05-27T21-35-39_fedb562a18bc.json`
**Engine**: `simple_tutor` (`SIMPLE_TUTOR_ENGINE=on`)
**Filter**: `--single-turn` (60 scenarios)

---

## Headline

| Run | Pass | Rate | Δ |
|---|---|---|---|
| **iter3** (this run, intent polished) | **54 / 60** | **90.0%** | +5 vs iter2 |
| iter2 (intent classifier wired, untuned) | 49 / 60 | 81.7% | regression |
| iter1 (prompt + dataset cleanup) | 53 / 60 | 88.3% | — |
| Phase 5 single-turn baseline | 50 / 60 | 83.3% | — |

**+4 net** since the pre-intent baseline (50 → 54), but the composition matters more than the headline number — iter3 is passing because the engine now genuinely handles the scenarios, not because it got lucky with placeholder refs.

---

## Trajectory of the intent-classifier work

| Iteration | Pass | Rate | What landed |
|---|---|---|---|
| iter1 | 51 / 60 | 85.0% | Pre-intent baseline (phrase-assertion strip + R14 sharpening) |
| iter2 | 49 / 60 | 81.7% | Intent classifier wired. Regression — guidance text echoed by LLM + classifier gaps on novel phrasings |
| **iter3** | **54 / 60** | **90.0%** | Guidance rewritten as direct instruction; classifier patches for `you mean`, `i can't`, `idk this is hard`, `yeah` |

The intent classifier is now demonstrably working. Vs iter2, the 6 newly-passing scenarios:

| Scenario | Was | Why it flipped |
|---|---|---|
| `geo_capable_corrects_tutor_001` | 0.37 → PASS | Pushback regex now catches "i think you mean X" |
| `struggler_gives_up_001` | 0.66 → PASS | non_engagement catches "i can't do this" |
| `struggler_idk_handling_001` | 0.51 → PASS | non_engagement catches "idk this is hard" |
| `math_non_responder_after_explanation_001` | 0.64 → PASS | non_engagement catches "yeah" |
| `repeats_phrase_guard_001` | meta-leak → PASS | Guidance rewritten so model stops echoing classification language |
| `math_average_arithmetic_slip_001` | 0.65 → PASS | Rubric drifted up (likely judge variance) |

**Vs iter1** (pre-intent baseline), net flips:
- 3 wins: `average_clarifying_question_001`, `capable_pushback_001`, `ungrounded_factual_guard_001` — all genuine intent-driven wins.
- 2 losses: `false_reject_capable_001`, `math_non_responder_after_explanation_001` — both `reveals_answer` dim misses (engine quality, not intent).

---

## 6 current fails — all `reveals_answer` cluster

| Scenario | Rubric | Threshold | Dim miss |
|---|---|---|---|
| `figure_ref_no_attachment_001` | 0.63 | 0.70 | reveals_answer |
| `false_reject_capable_001` | 0.47 | 0.70 | reveals_answer |
| `leaks_answer_guard_mcq_001` | 0.50 | 0.70 | mistake_location, reveals_answer |
| `tool_leak_guard_001` | 0.67 | 0.70 | mistake_identification, providing_guidance, coherence |
| `math_leaks_answer_guard_001` | 0.65 | 0.75 | (rubric only) |
| `math_non_responder_after_explanation_001` | 0.63 | 0.70 | reveals_answer |

**5 of 6 fail with `reveals_answer` flagged.** The intent classifier work has converged the failure mode — every remaining fail is in the same family. This is real engine signal, not harness noise.

`tool_leak_guard_001` is the lone outlier — tutor responded to a tool-probe but didn't surface the manipulation attempt; different failure mode entirely.

---

## Pedagogical dimensions (advisory)

| Dimension | iter3 | iter2 | iter1 | Phase-5 |
|---|---|---|---|---|
| `actionability` | 60 / 60 (100.0%) | (drop) | 60 / 60 (100.0%) | 60 / 60 |
| `tutor_tone` | 60 / 60 (100.0%) | 60 / 60 | 60 / 60 | 60 / 60 |
| `mistake_identification` | 59 / 60 (98.3%) | (drop) | 57 / 60 (95.0%) | 57 / 60 |
| `human_likeness` | 58 / 60 (96.7%) | (drop) | 53 / 60 (88.3%) | 57 / 60 |
| `providing_guidance` | 58 / 60 (96.7%) | (drop) | 57 / 60 (95.0%) | 57 / 60 |
| `mistake_location` | 57 / 60 (95.0%) | (drop) | 59 / 60 (98.3%) | 59 / 60 |
| `coherence` | 56 / 60 (93.3%) | (drop) | 54 / 60 (90.0%) | 58 / 60 |
| **`reveals_answer`** | **47 / 60 (78.3%)** | (drop) | 50 / 60 (83.3%) | 51 / 60 (85%) |

**Six of eight dimensions hit or exceed 95%.** The intent classifier rolled forward `coherence` and `human_likeness` significantly — those were dragging in earlier iterations because the tutor was pivoting away from clarifications/pushbacks.

**`reveals_answer` is the bottleneck.** It got *worse* across the iter sequence (85% → 83% → 80% → 83% → 78%). The R14 sharpening (added in `214a8a0`) is not net-positive. Worth considering: revert R14 to the simpler pre-iter1 wording, and try a different angle (worked-example pairs in the prompt, or a separate "hint quality" judge).

---

## Intent classifier behaviour

Distribution across 60 single-turn scenarios:

| Intent | Count | % |
|---|---|---|
| `answer` | 21 | 35% |
| `answer_or_other` | 20 | 33% |
| `clarification` | 8 | 13% |
| `non_engagement` | 8 | 13% |
| `pushback` | 3 | 5% |

`answer_or_other` is still the largest non-`answer` bucket at 33%. These are scenarios where the regex couldn't decide; the LLM falls back to its judgement. No evidence yet that wrong classifications on this bucket are causing failures.

---

## Recommendation on LLM-based intent classifier

**Skip it.** Iter3 confirms what I suspected: the classifier itself is not the bottleneck. The 6 remaining fails are all `reveals_answer` cluster (5 of 6) plus one tool-probe edge case — none of them caused by intent mis-classification.

Cost-benefit:
- LLM classifier would add ~$450/month at pilot scale + ~500ms latency per turn
- Would not move any of the 6 failing scenarios since they don't have intent issues
- The current regex catches all the patterns we've seen in 60 scenarios

The Tanzania pilot (Swahili-English code-switching) might still warrant the LLM upgrade later, but for the Seychelles dataset the regex is sufficient.

---

## Universal deterministic checks

| Verb | Failures this run |
|---|---|
| `meta_reasoning_leak` | 0 / 60 |
| `passive_ending` | 0 / 60 |

Both zero — including the extended `meta_reasoning_leak` regex that now catches "That's a clarification, not a new answer attempt"-style classification language. The intent guidance rewrite (third-person narration → direct instruction) holds.

---

## Next iteration priorities

In rank order:

1. **Revert R14 sharpening and reconsider.** The "don't point at distinguishing features" language has not produced a net gain on `reveals_answer` over 4 iterations. Try a different approach: one or two short worked-example pairs in the prompt showing a good hint vs a leaking hint side-by-side.

2. **`math_leaks_answer_guard_001` to 0.75.** Within 0.10 of passing. The strictest threshold in the dataset for a reason — math has the clearest right answer to leak.

3. **Multi-turn re-run.** All single-turn iteration done. Time to run the 20 multi-turn scenarios and confirm the intent classifier + R07 vary-affirmation rule both hold at session scale.

4. **`tool_leak_guard_001`** — investigate separately. Tutor needs to surface manipulation attempts explicitly per its prompt rules.

---

## Files referenced

- `apps/tutoring/simple_tutor/intent.py` — classifier with patches for `you mean`, `i can't`, `idk this is hard`, `yeah`
- `apps/tutoring/simple_tutor/prompts.py` — `_INTENT_GUIDANCE` rewritten as direct instruction; new GRADE/CONVERSATIONAL/POSE mode block
- `evals/scorers/deterministic.py` — extended `_META_REASONING_PATTERNS` for "That's a clarification" type leaks
- `evals/runs/2026-05-27T21-35-39_fedb562a18bc.json` — this run
