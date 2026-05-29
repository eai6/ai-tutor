# GEO-S5 Evaluation — 2026-05-29 run13 (consolidated move prompts)

**Scenario:** GEO-S5 — struggling S5 Geography, mostly-correct.
**Lesson:** 1426 — Compass Directions and Bearings. **Engine:** v2, local.
**Session:** 127, student `geoeval2`.
**Purpose:** validate the move-prompt consolidation (commits `1a52591`,
`fe20276`, `8328cdc` — move_prompts.py 1313 → 761 lines: hoist universals
to preamble, unify the affirmation bound, ≤3-item checklists, de-shout,
strip repeated attributions, remove dead refs). Hypothesis: sharpening
signal-to-noise improves instruction-following (the run-12 failures).

---

## Session

| Turn | Student | Verdict | Move | Notes |
|---|---|---|---|---|
| T1 | "used a compass but bearings are new" | — | (pose) | posed bank idx3 (bare stem) |
| T2 | "i think its 45 degrees" | **correct** ✓ | confirm_and_advance | **bare MCQ, no affirmation** (gate_failures []) |
| T3 | "maybe B?" | **wrong** ✓ | worked_example | revealed the answer in Step 3 + ended on a colon |
| T4 | "oh ok then A" | **correct** ✓ | close_topic | proper affirmation + exit ticket |

## P1 errors: 0
Correct→correct (T2, T4), wrong→wrong (T3). No correct-marked-wrong, no
wrong-marked-correct, no incomplete bank questions. No regression vs run12.

## The consolidation hypothesis — partially DISCONFIRMED

The cleaner prompt did **not** fix the two run-12 quality issues:

1. **Affirmation-on-correct still missing (T2).** Identical failure to
   run-12 T2: a correct "45" produced a bare ferry MCQ with no
   acknowledgment, `gate_failures: []`. confirm_and_advance's affirmation
   rule is now stated cleanly and un-buried, yet the LLM still called
   `pose_question` and emitted **no lead-in text**. This is **structural**,
   not a clarity problem: a pose-dominant move forces the tool
   (`tool_choice="any"`), and the model reliably omits the text block when
   forced. Sharpening the instruction can't fix a dropped output channel.

2. **worked_example revealed the answer + ended on a colon (T3).** Despite
   the preserved open-question canonical guard ("stop one step short; the
   last step POSES, doesn't state"), Step 3 stated "150° is South-East and
   more southerly than the first" (= option A). It then authored a trailing
   verifiable prose question — the `curriculum_fidelity` gate caught it
   (attempt 1) and stripped it, leaving "Try the question again with that
   in mind:" (a trailing colon, MCQ not re-shown). Same shape as run-12 T3.

What the consolidation DID achieve: a 42%-smaller, internally-consistent,
dead-noise-free prompt with **zero P1s and no behavioral regression** —
worth doing as hygiene, and it removes the noise that was a plausible
*contributing* factor. But it is not the fix for these two failures.

## Conclusion — the real fixes are structural (not more/cleaner prompt)

This run is the empirical answer to "are more prompt changes the real
fix?": **no, for the affirmation + reveal failures.** They survive a
clean, consistent prompt. The fixes are structural, and notably they do
NOT require moving pedagogy/selection off the LLM (rejected option A):

- **Affirmation-on-correct → engine-level floor (option B).** When a
  CORRECT-verdict confirm_and_advance ships an empty lead-in, synthesize a
  one-line affirmation from the grader's `student_safe_feedback.what_right`
  + `student_value` before appending the bank stem. (Or: don't force the
  tool — `tool_choice="auto"` + required lead-in — so the model keeps the
  text channel.) Deterministic backstop, LLM keeps authoring when it does.

- **worked_example reveal → make the canonical guard structural.** The
  prose-instruction guard ("stop one step short") is not reliably
  followed. Detect a stated-answer in the walkthrough (semantic
  answer-leak: the prose contains the open question's canonical /
  winning-option text, which the engine holds) and retry/degrade — the
  same belt-and-suspenders pattern as the one_question gate.

- **Trailing-colon after a stripped prose re-pose** is a degrade-quality
  issue: when the curriculum_fidelity gate strips a worked_example's prose
  re-pose, the move should re-surface the open question, not leave a colon.

Recommend: implement the engine-level affirmation floor next (highest
user-visible value; the run-12 + run-13 repeat failure), then the semantic
answer-leak detection.
