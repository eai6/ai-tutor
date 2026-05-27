# Phase 4 Redesign — Verification Report

**Date**: 2026-05-26
**Plan**: `memory/v2_unverified_trap_redesign.md`
**Scope**: 4 architectural fixes + Active Learning invariant + move-prompt restructure addressing the 6-run pattern of recurring P1 errors.

---

## Test suite — 342 v2 tests passing

```
apps/tutoring/v2/tests/  →  342 passed in 12.39s
```

New tests added (covering the Phase 4 contract changes):
- `test_grader.py`: 5 new tests for the answer-consistency verifier
  - Low-confidence + verifier unavailable → UNVERIFIED (fail-soft preserved)
  - Low-confidence + verifier=yes → CORRECT (the run-6 MATHS T6+ fix)
  - Low-confidence + verifier=no → WRONG (verifier overrides down)
  - Low-confidence + verifier=cant_tell → UNVERIFIED
  - High-confidence → skips verifier (it's a tiebreaker, not a re-grader)
- `test_grader.py`: 3 new tests for the math-path fallthrough
  - DSL extraction failure → falls through to grounded/verifier
  - DSL validation failure → falls through
  - DSL fail + verifier=yes → CORRECT (math proof unverified-trap break)
- `test_question_extractor.py` (new file): 6 tests for the post-render extractor
  - Empty text short-circuits without LLM call
  - Single action prompt returns count=1
  - Stacked questions detected (run-6 GEO T16 pattern)
  - Missing active-end flagged
  - LLM outage fails soft (action_count=1 default)
  - No client returns unavailable
- `test_mcq_options_threading.py` (new file): 9 tests for Fix 4a/4b
  - MCQ choices appended to stem
  - Non-MCQ steps return bare question
  - MCQ without choices falls back gracefully
  - MCQ-without-options detected at Phase A
  - Inline options pass Phase A
  - Non-MCQ stem not falsely flagged
- `test_doing_rate_bias.py` (new file): 8 tests for Active Learning invariant
  - Doing-rate window math
  - Window capped at 5 entries
  - CORRECT+early-mastery: extend ↓ to advance on low doing-rate
  - PARTIAL+attempts≥3: worked_example ↓ to scaffold_hint on low doing-rate
  - Wrong-branch escalation NOT affected by doing-rate

---

## Live verification (the run-6 P1 inputs replayed)

### MATHS-S1 — unverified-trap break

**Original run-6 failure**: T6 (and 7+ subsequent turns) on session 90:
> Student: "c=13, a=5, b=12. a²+b² = 25+144 = 169. c²=169. Since 169=169, the triangle IS right-angled."
> **Engine**: verdict=`unverified`, scaffold_hint fallback, looped indefinitely.

**Post-fix replay** (session 93, same input, fresh state):
> Student: same correct proof.
> **Engine**: **verdict=`correct`** ✅

The verifier upgrade fired:
- Math DSL extraction couldn't parse the prose proof → fell through to grounded path (Phase 4 fallthrough).
- Grounded returned `verdict=correct, confidence=0.4` (low — no KB anchor).
- Pre-Phase-4: this would have been blanket-downgraded to UNVERIFIED.
- Post-Phase-4: the answer-consistency verifier (Haiku 4.5) ran, confirmed `yes — student asserts the same answer as canonical`, final verdict = **CORRECT**.

`v2_trace` confirms:
```json
{
  "verdict": "correct",
  "doing_rate_5turn": {"rate": 0.5, "total": 2, "attempted": 1},
  "question_extractor": {"available": true, "action_count": 1, "has_active_end": false}
}
```

The unverified-trap is broken on the canonical math-proof case.

### GEO-S5 — MCQ options threading

**Original run-6 failure**: T16/T18/T20 posed *"which of the following best describes what happens?"* with no MCQ options rendered. Student saw 3 consecutive unanswerable questions and flagged it explicitly at T19.

**Post-fix verification** (direct render of L1428 step 4):
```
A coastguard needs to locate a person in distress near Port Louis.
Using a two-figure grid reference (e.g., '68 72'), the search area
is 1 km × 1 km. Using a six-figure grid reference for the same
person, which of the following best describes what happens?

A) The search area shrinks to approximately 100 m × 100 m, making
   the rescue faster and more efficient.
B) The search area expands to 2 km × 2 km, requiring a larger
   rescue team.
C) The search area remains 1 km × 1 km because grid squares are
   fixed.
D) The search area becomes 10 km × 10 km, making it easier to spot
   from the air.
```

Options now render. The Phase A safety floor (Fix 4b) ALSO refuses a pose where the stem reads as MCQ but the renderer dropped the choices — belt-and-braces.

---

## What changed (commit-shaped summary)

| Fix | Files |
|---|---|
| **Fix 1** — Answer-consistency verifier (Haiku 4.5) replaces blanket confidence-threshold downgrade | `services/student_grader.py`, `services/grader_prompts.py`, `apps/llm/models.py`, `apps/llm/migrations/0035_*.py` |
| **Fix 1b** — Math path falls through to grounded/verifier on DSL failure (covers prose proofs, definitions, multi-step justifications) | `services/student_grader.py` |
| **Fix 2a** — `open_question` cleared on CORRECT verdict BEFORE move selection runs (prevents stickiness on the in-turn new pose) | `services/tutor_engine.py` |
| **Fix 2b** — `/difficulty-signal/` endpoint routes v2 sessions through `tutor_engine.respond()` instead of bypassing it via `ConversationalTutor`. New `runtime_state.difficulty_level` + `last_system_event` fields | `views.py`, `contracts/runtime_state.py` |
| **Fix 2c** — Post-render question extractor (Haiku) counts action prompts; surfaces `one_question_per_turn` + `active_end_required` violations to the existing conformance retry path. Fail-soft to preserve deterministic gates as safety floors | `services/question_extractor.py` (new), `services/grader_prompts.py`, `services/tutor_engine.py` |
| **Fix 3** — Move-escalation table replaces verdict-keyed safe-template prose. When retry fails, engine escalates to the principled neighbour move (`pose_question → explain`, `scaffold_hint → worked_example`, etc.) before falling to the safe template at the escalated move | `services/move_escalation.py` (new), `services/tutor_engine.py`, `services/templates.py` (LO leak stripped) |
| **Fix 4a/4b** — MCQ options threaded through `pose_question` tool + bank-stem renderer. Phase A safety floor refuses MCQ-shaped stem with no options | `services/student_tutor.py`, `tools/pose_question.py` |
| **Active Learning invariant** — `runtime_state.student_doing_rate_window` rolling 5-turn flag; move-selection biases toward lighter cognitive lift when doing-rate ≤ 2/5. Doing-rate context line in SHARED_PREAMBLE | `services/move_selection.py`, `contracts/runtime_state.py`, `services/tutor_engine.py`, `services/move_prompts.py` |
| **Move-prompts** — Each move gets a `PRINCIPLE / INTENT` header citing the science-of-learning chapter. SCAFFOLD_HINT gains the "credit-the-partial" SHAPE imperative. WORKED_EXAMPLE gains the "exactly one practice prompt" CRITICAL rule. EXPLAIN gains the defensive help-request → method rule. NAME_MISCONCEPTION gains the GUARD that escalates to worked_example when no specific misconception can be named. SHARED_PREAMBLE gains LO-quotation prohibition and doing-rate context | `services/move_prompts.py` |

---

## Architecture-level changes that generalize

These are the load-bearing design moves the redesign rests on. They apply to all subjects, all lessons, all personas:

1. **Verifier as tiebreaker, not flow controller**: a cheap-LLM yes/no answer-consistency check replaces a numeric confidence threshold. Subject-agnostic.
2. **`open_question` single writer**: `tutor_engine.respond()` is now the only writer. The difficulty-signal endpoint goes through it. The math-DSL fallthrough goes through it. The clear-on-CORRECT happens at one consistent point. No side paths.
3. **Move-escalation ladder**: the safe-template fallback now reads as `test → teach the method → re-frame → hand off`, each step still ending with an action the student takes (Active Learning preserved at every step).
4. **Post-render invariants (Haiku-backed)**: one action prompt per turn + active-end on every turn — enforced by an LLM that handles every phrasing, not a regex enumerating each.
5. **Tools render fully or not at all**: MCQ stems can never reach the student missing their options (deterministic Phase A floor + Haiku extractor catch).
6. **Active Learning as runtime invariant**: doing-rate window mutates move-selection's choice in real time; surfaces as a context line to the LLM; appears in the trace for observability.

---

## What was NOT done (deliberately)

Per the user's guideline ("don't optimize prompts to specific lessons or specific test runs"):
- No lesson-specific prompt rules.
- No keyword regexes around specific run-6 phrasings.
- No backwards-compat shims (the math-path UNVERIFIED return is replaced wholesale by the fallthrough; older callers go through the same path).
- The 9-move table is unchanged. The conformance gates are unchanged (they remain safety floors, per the user's guideline).

---

## Trace observability

Every Phase 4 step emits a span. The trace now includes:
- `grader.answer_consistency_verifier` — verifier decision, why
- `tutor.question_extractor` — action_count, has_active_end
- `tutor.escalation` — escalated_move
- `tutor.clear_open_question_on_correct` — prior open question id
- `doing_rate_5turn` (in v2_trace rollup) — attempted/total/rate

These are visible in `SessionTurn.judge_outputs.v2_trace` and via the `/dashboard/v2-observability/` dashboard.
