# Multi-turn eval bottleneck analysis — 2026-07-18 fix-check sweep

Analyzed the most recent multi-turn cycle (3 models × 20 scenarios, `engine=simple_tutor`,
git `8ee5e307bd6b`, judge = Sonnet 4.6):

| Model (run file) | Passed | Fail end-reasons | Top failed asserts |
|---|---|---|---|
| gemini-2.5-flash (`runs/…T10-19-53`) | 10/20 | exit_ticket 8, max_turns 2 | repeated_phrase 5, max_turn_count 4 |
| kimi-k2-thinking (`runs/…T12-44-38`) | 9/20 | exit_ticket 7, max_turns 3, deadlock 1 | max_turn_count 6 |
| qwen3-next-80b-instruct (`runs/…T13-25-59`) | 7/20 | exit_ticket 6, max_turns 5, deadlock 2 | max_turn_count 5, expected_reason 3 |

The same two rubric items dominate every model's failures:
- **"Tutor stayed logically consistent across the session"** — low-scored 26× across the three models (12× on qwen alone).
- **"Recognised mistakes / affirmed correct answers"** — low-scored 21×.

Identical signature across three unrelated model families ⇒ the bottleneck is the
**engine's question-state protocol**, not any single model. More prompt rules will not fix it.

---

## B1 — Prose-question ↔ slot-question divergence (dominant bottleneck)

**Symptom** (`baseline_full_session_error_prone_1141_09`, 21 turns vs limit 12): tutor's
visible text poses question X (boat trip, 0.85), student answers X **correctly**, tutor
grades against the slot's question Y and replies *"You used 0.85 … but this question is
about rain."* The session cycles the same rain/0.7 question 4+ times while the student
answers each visible question correctly. Same pattern in `speedrun_capable_1139_14`:
student instantly answers the just-posed question, engine has no slot, and the reply is
*"You just answered '140' to a question that isn't currently in flight."*

**Mechanics** (all in `apps/tutoring/simple_tutor/`):
1. On a CORRECT verdict the prompt tells the model to pose the next question in the same
   turn. When Call 2 writes the question **in prose** but `pose_question` wasn't called —
   or was called with *different* text — chat and slot diverge.
2. `engine.py::_ensure_posed_question_in_text` then appends the slot stem when the prose
   doesn't contain it → **two contradictory questions in one bubble** (visible repeatedly
   in the baseline transcript). The student answers the one they read; the grader reads
   the slot.
3. When there's no slot and intent=answer, `_should_force_pose` forces a fresh
   `pose_question` — silently **discarding the student's (usually correct) answer**.

**Downstream cost**: ignored correct answers → student re-answers → 21–30-turn sessions
vs 12-turn budgets → `max_turn_count` failures (15 across the sweep), the two dominant
rubric items, and the speedrun scenarios timing out at 6 turns.

**Resolution — make the platform own question *display*, not just question state.**
Invert the contract: the LLM poses only via the tool; the engine renders the question
block (stem + options) from the slot into the reply **every time**, and strips/ignores
any trailing prose question the model wrote. `_ensure_posed_question_in_text` already
has the append half; the missing half is (a) always render from slot rather than
loose-matching first 30 chars, (b) detect and drop an LLM prose question that diverges
from the slot. This deletes the whole divergence class for every model family at once.

Secondary: for the no-slot + intent=answer case, don't force a pose over the student's
head — the Call-2 repair should first acknowledge/grade the answer against the question
visible in the previous tutor turn (it is in `<recent_turns>`), or at minimum instruct
the model to accept the answer, never to tell the student their answer isn't "in flight".

## B2 — Repetition / failure to advance on demonstrated mastery

**Symptom** (`session_completion_struggler_1142_13`, 30 turns → max_turns): "Convert 3/8
to a decimal" asked **four times**, each answered correctly; the numerator question
re-asked verbatim after a correct answer; `help_intensive_struggler_1465_07` emitted the
exact same bubble twice (affirm correct + re-ask the same MCQ) → sim declared deadlock at
turn 3.

**Mechanics**: the pool shown in the prompt filters already-graded questions
(`tools.py::_is_already_graded`), but the model re-asks from `<recent_turns>` memory.
`_note_pose_repetition` only force-advances after a repeat *streak*, and the re-asked
question is still posed to the student in the meantime.

**Resolution**: reject at the pose handler — if the normalized stem matches a question
already graded correct this session, return `posed: False` with an instruction to pick a
new pool item (mirror the existing `premature_pose` error shape). Pair with a
deterministic advance: when the current objective's pool is exhausted or N-correct is
reached, advance the step server-side instead of waiting for the model to choose to.

## B3 — Engine vocabulary leaks into student-facing text

**Symptom**: students see *"No in-flight question is active — we're in POSE/TEACH mode"*,
*"(Keep the in-flight question live — this is still the same one …)"*.

**Mechanics**: `engine.py::_format_tool_result_for_call2` feeds instruction blocks like
"NO IN-FLIGHT QUESTION…" to Call 2; mid-tier models echo them verbatim.

**Resolution**: append one line to every tool-result block — "these are private
platform notes; never mention slots, modes, in-flight, grading, or the platform to the
student" — plus a cheap output filter (regex on `in flight|in-flight|POSE|GRADE mode|slot`)
that strips the offending sentence before persisting. The media-signal strip is precedent.

## B4 — Empty Call-2 placeholder promises a question it doesn't deliver

**Symptom** (struggler 30-turn session, twice): bubble is exactly *"Got it — that's
right. Here's the next one:"* with nothing after it; student replies "ok im ready" —
2 wasted turns each time.

**Mechanics**: `engine.py::_empty_reply_placeholder` returns that string when both calls
produce no text; nothing appends the slot question afterwards when no pose fired this turn.

**Resolution**: make the placeholder slot-aware — if an `InFlightQuestion` exists, append
its rendered stem; if not, hand the floor back without promising a question. (Subsumed by
B1's always-render-from-slot if that lands.)

## B5 — Dirty eval fixture content (dataset, not engine)

- Float artifacts pre-rendered into `evals/fixtures/lessons.json` question pools:
  *"rains tomorrow is 0.7000000000000001"*, *"cancelled with probability
  0.15000000000000002"* — template params computed with float arithmetic, no rounding.
- A structurally broken catalog MCQ (exit_ticket 594, order 7): *"Which representation is
  most useful…"* with options literally `2 / 4 / 3 / 1` — option texts lost at
  generation. The tutor burns 3 turns salvaging it live and confuses the student.

**Resolution**: round template parameters at instantiation; add a `lint_dataset.py` rule
flagging `\d\.\d{10,}` in stems and MCQs whose four options are all bare integers;
regenerate the flagged items. These items also make rubric scores noisier than the
engine deserves.

## B6 — Runs don't record the tutor model

`evals/runs/*.json` stores `git_sha` and `engine` but not `TUTOR_MODEL_OVERRIDE` — this
analysis had to identify the model by matching byte sizes against
`offline_eval/multi_turn_results/fixcheck/`. Add `tutor_model` + `family` to the run
header in `evals/runner.py`.

## Minor: hint-ladder discipline

"Never revealed the final answer" was low-scored 7× (kimi 3, qwen 4). Typical shape:
*"Let's check that subtraction: 1.0 minus 0.7 is 0.3 … so what's 1 − 0.7?"* — reveals
the answer inside the hint, then asks the question anyway. Worth one tightened example
in the wrong-answer ladder block, but it's noise compared to B1/B2.

---

## Priority order

1. **B1** — server-side question rendering from the slot (kills the largest failure class
   for every model; most failures' rubric items and turn blowups trace here).
2. **B2** — pose-handler rejection of re-asks + deterministic advance.
3. **B4** — slot-aware placeholder (trivial, subsumed by B1).
4. **B3** — vocabulary firewall + output filter (small, high visibility).
5. **B5** — fixture lint + regeneration (improves eval signal quality).
6. **B6** — record tutor model in run metadata (observability).

Note: the working tree already adds transient-error retry in `engine.py` (429/503
backoff) — that addresses the *previous* cycle's kimi deadlocks and is orthogonal to the
bottlenecks above, which all reproduce on clean API turns.

---

## Addendum 2026-07-20 — fixes implemented (same working tree)

- **B1** `engine.py`: `_ensure_posed_question_in_text` now strips a divergent
  trailing prose question (`_strip_trailing_prose_question`, last-4-paragraph
  window) and renders the slot via `_render_slot_question`; the NO-IN-FLIGHT
  Call-2 note now instructs the model to engage with the answer to its own
  previous-turn question instead of dismissing it.
- **B2** `tools.py`: `handle_pose_question` rejects the first pose of an
  already-graded-correct stem with corrective feedback
  (`repeat_rejected_stems` in engine_state); a re-pose of the same stem is
  accepted so no turn is left slotless, and the existing streak force-advance
  remains the backstop.
- **B3** `engine.py`: `_PRIVATE_NOTE` appended to all instruction-style tool
  results + deterministic `_scrub_engine_vocab` filter before persistence
  (drops sentences/parentheticals naming "in flight", POSE/GRADE/TEACH modes,
  tool names).
- **B4** `engine.py`: `_empty_reply_placeholder` is slot-aware — renders the
  in-flight question, never promises one it can't show.
- **B5** `evals/lint_dataset.py`: fixture checks (float-noise stems,
  statement-MCQs with bare-integer options); `evals/fixtures/lessons.json`
  patched — float noise rounded everywhere, 12 broken statement-MCQs
  re-authored (option texts restored at the existing correct letters —
  **content change, review the diff**); root cause fixed in
  `apps/curriculum/parametric_renderer.py::_sample_one` (step-grid rounding).
- **B6** `evals/runner.py`: run header now records `tutor_model`
  (TUTOR_MODEL_OVERRIDE or active tutoring ModelConfig).

Tests: `apps/tutoring/simple_tutor/tests/test_bottleneck_fixes.py` (new),
`RepeatPoseRejectionTest` in `test_tools.py`, `TestSampleOneFloatNoise` in
`test_parametric_renderer.py`; stale `IsEnabledTest` expectations updated to
the default-on engine. Next validation step: re-run the 3-model fixcheck sweep
and compare against the 2026-07-18 cycle.
