# Welcome / resume / review path through the validator stack

**Date**: 2026-05-17
**Driven by**: pilot directive 2026-05-17 ("every text to the student must go through judges and regenerate if needed"). Lesson 540 session 61 turn 960: welcome posed the warmup question TWICE (LLM typed it in prose AND called pose_inline_question) → no validator caught it because welcome doesn't run through `_respond_impl`.

## Problem

Three entry points generate tutor turns WITHOUT running through the validator/judge/regen stack:

1. **`start_session`** (new session) — calls `_generate_response` directly
2. **`resume_session`** (returning student) — line 2264, `_generate_response(prompt, fallback_context="resume")`
3. **`start_review`** / `_generate_review_opening` — line 2327 + 2333

All three go through `_handle_pose_question_message` (so tool calls update engine state) but skip:
- `validate_tutor_response` (no `validator_issues`, no `repeated_question` check)
- `run_all_judges` (no `combined_judge_result`)
- `run_tutor_self_retry` (no regen if dirty)
- Post-regen leak check (lines 3203+)

Result: welcome turns can ship with leaked answers, duplicated questions, missing handoff CTAs, etc.

## Current state

`_respond_impl` (apps/tutoring/conversational_tutor.py:2419+) is the entry that DOES run the full stack. It expects a `student_input` and runs the grader on it. Welcome doesn't have a student input.

## Target design

Extract a helper `_validate_and_finalize(text, delta, turn_metadata, ...) -> (final_text, final_delta)` from `_respond_impl` that:
1. Runs `validate_tutor_response`
2. Runs `run_all_judges` (combined result)
3. If regen needed → `_dispatch_self_retry`
4. Returns the final (text, delta) ready to commit

Then welcome paths:
```python
def _welcome_generate(self, prompt: str) -> Tuple[str, Dict]:
    message = self.llm_client.generate_with_tools(...)
    text, delta = self._pose_dry_run(message, turn_metadata)
    final_text, final_delta = self._validate_and_finalize(
        text, delta, turn_metadata,
        student_input="",  # no student input for welcome
        ...
    )
    self._apply_pose_delta(final_delta, turn_metadata)
    return final_text, turn_metadata
```

## Implementation phases

### Phase 1 — extract helper
- Lift `validate_tutor_response` + `run_all_judges` + regen dispatch out of `_respond_impl` into `_validate_and_finalize`
- `_respond_impl` calls the helper after generation
- No behavior change — pure refactor
- Tests: existing tests should all still pass

### Phase 2 — welcome wired
- Update `resume_session` (line 2235) to use the dry-run + validate path
- Update `start_session` similarly
- Update `start_review` / `_generate_review_opening`
- E2E lesson 540 fresh session: confirm welcome's MCQ doesn't dupe, no leaks

### Phase 3 — extend coverage to other generated paths
- `_generate_remediation_opening` (line 11214 approx)
- Any other `_generate_response()` call site

## Out of scope

- Re-architecting the LLM call layer
- Touching how the welcome prompt itself is built
- Frontend changes

## Open questions

1. **Cost**: welcome turn now runs the full judge fan-out (~7 LLM calls). Per session, that's 1 extra round of judges. Acceptable? Probably yes — welcome is rare per student.
2. **Latency**: welcome perceived latency goes from ~3-4s (LLM only) to ~8-10s (LLM + judges + maybe regen). Could pre-cache the most common welcome variants. Defer.
3. **What if welcome has no question (e.g. pure intro)?** — validator's NO_QUESTION_TOOL won't fire because it requires MCQ pattern + no tool. Welcome without a question would skip those checks naturally. The leak detector still runs (defensive).

## Refs

- `memory/tutor_self_retry_plan.md` — the broader retry architecture
- task #198 (self-retry), #199 (MCQ-only), #200 (dry-run commit)
- Pilot directive 2026-05-17: every text to student goes through judges

EOF
