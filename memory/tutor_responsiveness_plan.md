# Tutor responsiveness plan

*AI Tutor • drafted 2026-05-20*

Make the engine robust to three observed failure modes that all share a
root cause: the per-turn flow has **no typed signal for "what the
student just did"** and **no guard against a degenerate tutor turn**.

1. **Empty tutor turns.** A blank / near-empty response can be saved and
   sent to the student. No length floor exists before `_save_turn`.
2. **Dropped student requests.** Once the engine advances past a step,
   a student question *about the previous step/question* is stranded —
   the tutor is pinned to the new step and the prior `_awaiting_answer`
   is cleared.
3. **Stuck state transitions.** Reported 2026-05-20: a lesson could not
   move explanation → exit ticket. Same class as commit `8b0d1c3`
   (stuck exit-ticket loop, state drift).

This plan does NOT introduce multi-agent decomposition — see CLAUDE.md
"Conservative bias". It adds typed flags and guards to the existing
single-prompt + state-machine engine.

---

## Code anchors (verified 2026-05-20)

- `respond()` / `_respond_impl` — `apps/tutoring/conversational_tutor.py:1946`, `:1997`.
- `clean_response` computed (media signal stripped) — `:2200`.
- Validator + regen block — `:2662-2830`. Two regen paths: self-retry
  (`_dispatch_self_retry`, dev default) and `run_regen_ensemble`
  (prod). Regen-exhaustion ships the best dirty candidate; only adds
  `regen_did_not_clean` to `validator_issues` (`:2802-2809`). **No
  empty-text check anywhere.**
- Tutor turn saved — `_save_turn("tutor", clean_content, ...)` `:3357`;
  `_save_turn` impl `:12103`.
- `_awaiting_answer` — set via `_set_awaiting_answer` `:7832`; cleared
  via `_clear_awaiting_answer` `:7834`; rendered into the system prompt
  by `_build_active_bank_question_block` `:2404-2541`.
- Post-accept advance block — `:10201-10238`. Flags: `_step_just_advanced`,
  `step_exchange_count`, `concept_boundary_attempts`, `current_topic_index`.
- `StepEvaluationResult` Pydantic schema — `:53-73` (`answer_correct`,
  `step_complete`, `reasoning`). `_evaluate_step` LLM call `:9544-9682`.
- Exit-ticket transition — `:2846-2894`. Gated by `_can_trigger_exit_ticket()`;
  hold gate increments `exit_ticket_hold_count`; escape valve at
  `MAX_EXIT_TICKET_HOLD_CYCLES`.
- `engine_state` JSON — saved `:1302-1381`, loaded `:1127-1288`.
  Unversioned; new keys are opt-in `.get()` reads. `awaiting_answer`,
  `current_topic_index`, `step_exchange_count` already persisted.

---

## Phase 0 — Fix the stuck explanation → exit-ticket transition (BUG)

This is a bug, not a feature; fix before the Phase 1-3 work.

**Diagnosis needed first.** Requires the reported session (local vs
production, session id). Reproduce per CLAUDE.md bug-fix workflow:
Django-shell replay of the transcript, or chrome-devtools walk of the
lesson locally.

**Suspect mechanisms** (`:2846-2894`):
- `current_topic_index` never reaches `len(self.steps)` — the last step
  never gets `should_advance=True` and the 10-exchange hard cap in
  `_should_advance_step` is not firing. Check the hard-cap path
  (`:10458`) and whether the last step is gated by concept-boundary
  logic (`_is_at_concept_boundary`).
- `current_topic_index >= len(steps)` IS true but `_can_trigger_exit_ticket()`
  holds every turn on an unresolved bank Q, and `exit_ticket_hold_count`
  is reset (so the `MAX_EXIT_TICKET_HOLD_CYCLES` escape never fires).
  Check every reset site of `exit_ticket_hold_count`.

**Deliverable.** A regression test in
`apps/tutoring/tests/` that replays the stuck transcript and asserts
`session_state` reaches `EXIT_TICKET` within N turns.

---

## Phase 1 — Empty-turn guard + repose (small, self-contained)

Goal: a degenerate tutor turn never reaches the student; the awaiting
question is re-posed instead.

**Where.** A guard immediately before `_save_turn("tutor", ...)` at
`:3357` (and the regen-exhaustion branch). Centralise in a helper
`_guard_empty_response(clean_content) -> str`.

**Logic.**
1. If `clean_content.strip()` is empty or below a small floor
   (e.g. `< 10` non-whitespace chars after media-signal strip):
2. If `_awaiting_answer` is set → re-render that question via the
   existing `_build_active_bank_question_block` machinery / the
   `Question` dataclass `render_*` helpers, and send that.
3. Else → a single safe fallback line (reuse the regen `STOCK_FALLBACK`
   string; do not invent a new one).
4. Emit a new validator issue code **`empty_turn`** (and
   `empty_turn_reposed` when a question was re-posed) into
   `turn_metadata['validator_issues']` so the harness + `judge_outputs`
   count it. Add the code to the validator-code table in
   `memory/research_narrative.md` §3.

**Tests.** Unit: empty string with `_awaiting_answer` set → output is
the re-posed question; empty with no awaiting → fallback line; non-empty
→ untouched. The simulation harness already surfaces `validator_issues`,
so `empty_turn` will appear in the next sweep automatically.

---

## Phase 2 — Student-request intent detection

Goal: the engine knows when the student's input is a *request /
question* rather than an *answer*, and does not blindly advance.

**Where.** Extend `StepEvaluationResult` (`:53-73`) — it is already an
instructor structured-output call per turn, so adding a field is free
(no extra LLM call):

```python
intent: Optional[str] = Field(
    default=None,
    description="answer_given | question_asked | revisit_request | "
                "clarification_request | off_topic | engagement",
)
```

Add one classification line to the eval system prompt (`:9648`).
**Consult the prompting skills before writing it** (CLAUDE.md
non-negotiable): `prompting-fundamentals-expert` then
`gemini-prompting-expert` (judge/eval model is Gemini).

**Use it** in the post-accept advance block (`:10201-10238`): when
`intent in {question_asked, revisit_request, clarification_request}`,
**suppress the advance** for that turn and set a `_pending_request`
flag. The tutor answers the request; advancement resumes once the
student re-engages with the step. The hard-cap safety valve still
applies so this cannot deadlock.

---

## Phase 3 — Revisit support

Goal: a student can ask about an *earlier* step's question and the
tutor has the context to answer.

**Approach.** Add `revisit_step_index: Optional[int]` to `engine_state`
(persist at `:1302`, load at `:1127`). When Phase-2 intent is
`revisit_request` and the input references a prior step, set
`revisit_step_index`; the next `_build_system_prompt` call includes
that step's figure-facts + question context *in addition to* the
current step. Cleared when the student returns to the current step.

`current_topic_index` stays monotonic — progress is never rewound; the
revisit only widens the context window the tutor sees.

**Open question.** Detecting *which* prior step a vague reference
("that earlier question") points to. Cheapest first cut: default to the
immediately-preceding step (`current_topic_index - 1`). Only generalise
if traces show multi-step-back references.

---

## Sequencing & risk

- Phase 0 first — it is a live bug.
- Phase 1 is independent, low-risk, ship-and-measure (the sweep will
  show `empty_turn` frequency).
- Phases 2-3 touch the transition core — land Phase 2 behind a flag,
  validate on the simulation harness (struggler persona deliberately
  asks questions) before Phase 3.
- Before implementing any phase that touches `conversational_tutor.py`:
  consult `tutoring-engine-expert`. Before writing the Phase-2 intent
  prompt: consult the prompting skills.

## Open questions for Edward

1. The stuck session (Phase 0) — local dev or production? Session id?
2. Phase 2: should a `revisit_request` ever *rewind* `current_topic_index`,
   or only widen context (this plan assumes widen-only)?
3. Empty-turn floor: is `< 10` chars the right threshold, or should it
   be "no question/CTA AND no substantive content"?
