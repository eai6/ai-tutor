# Tutor state drift + leak simplification — plan (2026-05-17)

## Problem

Prod session 265 (Opus 4.7 @ 0.0, "Map Scale and Map Types") exposed two
coupled defects in the hint/remediation path. Chat transcript captured
in `memory/chat_ht.md`; Azure Log Analytics evidence ranged
2026-05-17T16:32–16:52Z.

### Defect 1 — smuggled question + state pointer drift

Mechanism observed from `[Regen]` + `[TurnSummary]` logs:

1. Tutor's reveal/hint turn ends without a question → validator fires `no_question`.
2. Regen kicks in with goal "add a question".
3. The tutor authors a NEW question inline as prose.
4. `NO_AUTHORING` rule fires (we forbid authored questions when a bank Q is active per #200/#194).
5. Regen retries; same authored-prose pattern keeps emerging.
6. After 3 cycles all dirty, regen ships "least-violation candidate" — the dirty turn with the smuggled prose question.
7. `bank_question_ref` was NEVER updated (correct per the dry-run safety in #200), so when the student answers the smuggled prose question, the bank grader matches their answer against the original bank Q and marks it wrong.

Smoking gun in logs (turn 6, session 265):
```
[Regen] cycle=1 model=claude-opus-4-7 temp=0.20 score=-17.50 clean=False
  issues=rules=['NO_AUTHORING', 'NO_AUTHORING'],coh=1
[Regen] cycle=2 model=claude-opus-4-7 temp=0.15 score=-6.50 clean=False issues=coh=1
[Regen] cycle=3 model=claude-opus-4-7 temp=0.10 score=-6.00 clean=False issues=coh=1
[Regen] cycles exhausted — picking least-violation candidate from cycle 3
```

User experience: student answers a question they see on screen, tutor
says "that isn't one of the answer choices" — disowning its own question.

### Defect 2 — leak detection too lenient via arbiter

Three-layer leak system today (`apps/tutoring/answer_leak.py`, 525 lines):
- deterministic regex/jaccard check (`_deterministic_check_*`)
- LLM judge via `run_grading_batch(JUDGE_LEAK)` (`_llm_check`)
- arbiter LLM call when the two disagree (`_arbiter_call`)

Session 265 turn 8 log:
```
[LeakDetect] DISAGREE det=False llm=True — calling arbiter
[LeakDetect] ARBITER said leak=False reason='Tutor explains the concept
  (larger scale = smaller denominator = more detail) without stating the
  specific correct answer'
```

But the prior tutor turn literally said: *"The answer is actually C) The
street map shows more detail because it uses a larger scale."* The
arbiter rationalised "explains the concept" and overruled the LLM judge
that correctly said `leak=True`.

## Target design

### Phase A — restate the active question (anti-smuggle)

When the engine prepares a tutor turn AND `awaiting_answer` is set
(i.e., the student is mid-question), inject an `<active_question>` block
into the system/user prompt and an explicit instruction: *"DO NOT author
a new question. End your turn by restating the active question below
verbatim so the student can answer it."*

The active question is one of:
- bank Q (`engine_state.awaiting_answer.question_id` → render stem + options)
- chat-authored inline MCQ (rebuild from the prior `pose_inline_question`
  tool's args stored in turn metadata)

After this change:
- Hint turns end with the original question, not a new one.
- `no_question` validator stops false-positive firing on remediation
  (the restated Q is a question).
- `NO_AUTHORING` rule stops firing in regen cycles because there's no
  authored Q to flag.
- `bank_question_ref` stays aligned with what the student sees — bank
  grader hits the right Q.

Implementation:
- `apps/tutoring/conversational_tutor.py::_build_system_prompt()` (or a
  per-turn user-prompt extension): add an `<active_question>` block when
  `self.awaiting_answer` is truthy.
- Validator: in `apps/tutoring/validator.py`, when computing
  `no_question`, treat a verbatim restatement of the active question as
  satisfying the requirement (or just suppress the flag when
  `awaiting_answer` is set on this turn).
- Regen prompt (`apps/tutoring/regen/prompts.py` or similar): when the
  regen reason includes `no_question` AND `awaiting_answer` is set, the
  fix instruction is "restate the active question", not "add a question".

### Phase B — single LLM leak judge

Gut `apps/tutoring/answer_leak.py`:
- Delete `_deterministic_check_mcq`, `_deterministic_check_text`,
  `_deterministic_check` (lines 124–279).
- Delete `_arbiter_call` (lines 454–525) and the disagreement branch in
  `detect_answer_leak`.
- Delete `_tokenize`, `_ngrams`, `_jaccard` (only used by deterministic).
- Rewrite `detect_answer_leak()` to be a thin wrapper around
  `_llm_check()`. Returns a `LeakVerdict` populated from the LLM result.
- Net: ~525 → ~150 LOC.

Strengthen the LLM judge prompt in
`apps/tutoring/exit_ticket_grader.py::_LEAK_SYSTEM`:
- Add explicit positive examples for MCQ:
  *"If the tutor names the correct option letter (e.g. 'the answer is
  C', 'C is correct', 'choose C') OR repeats the exact text of the
  correct option as 'the answer', that IS a leak — regardless of how
  conceptually the surrounding text is framed."*
- Add: *"Conceptual explanation of WHY the answer is correct is NOT a
  leak as long as the canonical letter / exact option text is NOT named."*

Integration unchanged — the single LLM leak judge keeps running
concurrently inside `run_all_judges` (via `_run_leak_inline`,
`apps/tutoring/judges/__init__.py:299`) alongside every other judge in
the `_submit` fan-out. Its verdict surfaces as `result.answer_leaked` on
`CombinedJudgeResult` and feeds the regen scorer at
`apps/tutoring/regen/score.py` like any other judge signal. The
simplification just removes the deterministic pre-check + arbiter
inside `detect_answer_leak`; the concurrent dispatch + regen-routing is
untouched.

## Files to change

| File | Change |
|---|---|
| `apps/tutoring/conversational_tutor.py` | `_build_system_prompt`: inject `<active_question>` when `awaiting_answer` set; instruction "restate, don't author" |
| `apps/tutoring/validator.py` | Suppress `no_question` when `awaiting_answer` set AND active Q text appears in response |
| `apps/tutoring/regen/prompts.py` | Adjust `no_question` regen instruction conditionally on `awaiting_answer` |
| `apps/tutoring/answer_leak.py` | Drop deterministic + arbiter; `detect_answer_leak` becomes a thin `_llm_check` wrapper |
| `apps/tutoring/exit_ticket_grader.py` | Strengthen `_LEAK_SYSTEM` prompt with MCQ-letter-naming examples |
| `apps/tutoring/judges/__init__.py` | `_run_leak_inline` keeps same signature; just no longer pays arbiter cost |

## Out of scope

- The 2-strike reveal threshold is still firing per the existing policy
  (#170/#178); revisit only if the leak prompt rewrite changes its
  effective sensitivity.
- The "tutor confused its own follow-up Q with the prior bank Q" symptom
  is downstream of Phase A — once the tutor stops smuggling, that
  symptom should disappear. If it persists, file follow-up.

## Risks

- **Restating long bank Qs bloats the turn.** Mitigation: render in
  collapsed/compact form for the prompt context; the *student* already
  sees the original question on screen, the restatement is for the
  tutor's awareness, not necessarily verbatim on screen. Actually the
  point IS to put it on screen — accept the verbosity, it helps the
  student too.
- **LLM-only leak judge is one point of failure.** Mitigation: the
  current deterministic check already had a high false-negative rate
  (session 265 turn 8 showed `det=False` on an obvious leak). Removing
  it doesn't lose ground.

## Test plan

Local:
1. Reproduce session 265-like scenario on lesson with explicit reveal
   policy (lesson 540 or 638 with deliberate-wrong walkthrough).
2. After wrong answer, confirm:
   - Tutor's hint turn ends with the original Q text restated, no smuggled Q.
   - `bank_question_ref` stays the same across the hint turn.
   - Validator flags: no `no_question`, no `NO_AUTHORING`.
   - Regen audit: cycle 1 clean (no exhausted-cycles ship).
3. Probe leak judge with: tutor turn that says "The answer is C" — must
   surface `answer_leaked=True`.
4. Negative probe: tutor turn that explains "larger scale = more detail"
   without naming C — must surface `answer_leaked=False`.

Then deploy + run real session as the chat in `memory/chat_ht.md` and
confirm the smuggled-Q + grader-pointer regression doesn't reproduce.

## Next step

Implement Phase A and Phase B together (single PR; they're coupled —
Phase B isn't useful without Phase A because the smuggled-Q path keeps
generating things to leak about).
