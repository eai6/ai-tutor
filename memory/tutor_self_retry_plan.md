
# Tutor Self-Retry — replace text-only regen with tool-aware retry loop

**Date**: 2026-05-17
**Driven by**: pilot E2E lesson 540 session 57 turn 937 — bank_question_ref/render mismatch (task #176).

## Problem

The current regen path (`apps/tutoring/regen/`) is **text-only**. When validator flags issues, regen invokes a separate LLM (could be Sonnet, Haiku, Opus depending on `regen_clients`) with the previous response + repair instructions. The regen LLM returns plain text — no tool calls.

Bug observed in production (lesson 540 session 57 turn 937):
1. Tutor's original response called `pose_question(slot=N)` → resolved to Q3109. Engine set `bank_question_ref={id:3109}` + `_awaiting_answer={question_id:3109}` + appended Q3109's rendered text.
2. Validator flagged the original with `repeated_question` → regen triggered.
3. Regen LLM saw the bank-slot menu in the system prompt, took a different path, produced prose ending in Q3106's stem + options *verbatim*. Judges declared it clean → accepted.
4. **Saved content** = regen output (Q3106 text). **Engine state** = Q3109 (from the pre-regen tool call). Bank ref + awaiting are STALE.
5. Student answered "B" to what they see (Q3106, correct=C). Grader graded against Q3109 (correct=B) → marked **correct**. Cascaded into more wrong-Q grading on the next turn.

Root cause: regen rewrites the **text** but doesn't reconcile the **engine state** that was set by pre-regen tool calls. Engine and screen diverge silently.

## Why "give regen the same tools" / "let the tutor do its own regen"

User directive (2026-05-17):
> "the regen should have the same tools as the tutor. perhaps the regens should be done by the tutor itself. so we have a true loop"

Two equivalent framings:
- **Tool-aware regen**: keep regen as a separate fn but offer the same tools (`pose_question`, `pose_inline_question`) so regen tool calls are processed the same way as the original.
- **Tutor self-retry**: collapse regen into a re-invocation of the tutor's own generate path with feedback prepended. Same prompt, same tools, same model.

The cleaner architecture is **tutor self-retry** — one LLM path, no parallel code to maintain, the feedback loop becomes "judges critique → tutor revises" rather than "judges critique → different LLM rewrites".

## Current state — files of interest

- `apps/tutoring/regen/__init__.py` — `run_regen_ensemble()` orchestrator. Takes `regen_clients`, runs each in parallel per cycle, scores candidates, picks cleanest.
- `apps/tutoring/regen/prompt.py` — `build_regen_prompt(previous_response, issues, validation_metadata, bank_stems, media_catalog_text, student_input)` — translates judge findings into a system + user prompt for the regen LLM.
- `apps/tutoring/regen/score.py` — `score_candidate(judge_result)` — scoring rubric (clean / dirty / soft penalties).
- `apps/tutoring/conversational_tutor.py:2900-3450` (approx) — `_respond_impl` runs the validator + judges + regen ensemble call.
- `apps/tutoring/conversational_tutor.py::_handle_pose_question_message` (line 7050+) — processes tool_use blocks from `generate_with_tools`. THIS is what regen currently bypasses.

## Target design — Tutor Self-Retry

**Replace** `run_regen_ensemble` invocation in `_respond_impl` with a new helper `_run_self_retry()` defined on `ConversationalTutor`:

```python
def _run_self_retry(
    self,
    *,
    previous_response: str,
    validation,
    combined_judge_result,
    turn_metadata: Dict,
    student_input: str,
    max_cycles: int = 2,
) -> SelfRetryResult:
    """Re-invoke the tutor's own generate path with judge feedback
    prepended, so tool calls in the retry are processed via the same
    `_handle_pose_question_message` flow as the original — engine
    state stays consistent.

    Returns SelfRetryResult { text, clean, cycles_run, fallback_used,
                              picked_model, audit, turn_metadata_updates }.
    """
    ...
```

### Flow per cycle

1. Build a `<system_feedback>` block from `validation.issues` + `combined_judge_result` using the existing `regen/prompt.py` translators (verbatim re-use — they already produce concrete fixes).
2. Append the feedback as a synthetic message to the conversation history:
   - Format: an extra `user` turn with the prefix `[system_reminder: your previous response had these issues — please revise:]` followed by the structured feedback.
   - Doesn't pollute persisted history — only used for the retry LLM call.
3. Call `_generate_response_with_tools(...)` (or whatever the tool-enabled entry is) with this augmented history. Same system prompt, same tools, same model as the initial generation.
4. Process the result through `_handle_pose_question_message` — bank refs + awaiting state get updated to reflect the RETRY's tool calls, not the original's.
5. Re-run judges on the new response.
6. If `score_candidate(judges)` is clean → return. Otherwise loop (cap at `max_cycles`).

### After loop exhausted

- If best cycle's text is non-empty AND judged "soft clean" (< -1 penalty), accept best-effort.
- Otherwise fall back to stock CTA (existing `STOCK_FALLBACK`).

### Engine-state reconciliation (the bug we're fixing)

Because retry goes through `_handle_pose_question_message`, any tool calls in the retry response are processed normally:
- New `pose_question(slot=M)` → bank_question_ref updated to slot M's Q. Old Q3109 ref overwritten.
- No tool calls in the retry → bank_question_ref + awaiting are CLEARED at the start of the retry (the original's tool effects are rolled back).

This is the structural fix for the lesson 540 session 57 bug.

## Cost considerations

Current regen: separate Sonnet calls (cheaper than Opus tutor). Tutor self-retry: same Opus model, full system prompt, full history.

Cost back-of-envelope per dirty turn:
- Initial Opus call: ~3000 input tokens (system prompt) + ~500 output = ~$0.10
- Retry Opus call: ~3000 input + ~500 output = ~$0.10
- Worst case 2 retries: ~$0.30 per dirty turn

vs current ensemble at ~$0.04/cycle (Sonnet). 2.5x cost per retry, but:
- Tutor self-retry is structurally correct (no engine-state drift)
- We can configure the retry model independently (use Sonnet for retry, Opus for initial — same tool interface)

**Decision** (default): retry uses the **same model** as initial (Opus). Override-able via a per-session config so we can A/B Sonnet retries later.

## Migration plan

### Phase 1 — feature flag, parallel paths
- Build `_run_self_retry` alongside existing `run_regen_ensemble` invocation.
- Settings flag `TUTOR_SELF_RETRY_ENABLED` (default off in prod, on in dev).
- Both paths log to `regen_audit` with a `mechanism: self_retry | ensemble` field.

### Phase 2 — pilot validation
- Run E2E lesson 540 + lesson 538 on self-retry path.
- Compare regen success rate, latency, cost, engine-state consistency.
- Specifically verify: bank_question_ref always matches the rendered Q in the final saved content.

### Phase 3 — flip default + remove old code
- Default flag on in prod (with rollback switch).
- After 1-2 weeks stable, remove `regen/` ensemble path entirely (keep `regen/prompt.py` and `regen/score.py` — the issue-translator + scoring rubric are reused).

## Open questions

1. **Should retry preserve the prior validator issues across cycles?** — i.e., if cycle 1 fixed issue A but introduced B, does cycle 2 see {A, B} or just {B}?
   - **Recommend**: just {B} — feedback is what's wrong NOW, not the cumulative history. Otherwise the LLM gets confused by stale feedback.

2. **Should the synthetic feedback message be a `user` turn or a `system` reminder?**
   - **Recommend**: `user` turn with explicit `[system_feedback]` prefix. Claude treats user turns as actionable; system prompts as policy. Feedback is actionable.

3. **What if the retry doesn't call a tool when the original did?** — e.g., original posed Q3109 via tool, retry just gives a hint without posing.
   - **Default behavior**: clear `bank_question_ref` + `_awaiting_answer`. The retry has decided not to keep the Q in flight.
   - Risk: student might still see "Q3109" if frontend lagged. But that's a UI render race, not a grader bug.

4. **Concurrent retry across multiple models?** — current ensemble runs Sonnet + Haiku + Opus in parallel and picks the best.
   - **Recommend**: drop the multi-model parallelism. Sequential single-model retry is simpler. If cost+latency are concerns, a single non-Opus retry model is a config switch.

5. **Cap cycles at 2 like current?**
   - **Yes** — same cap as current (`DEFAULT_MAX_CYCLES`). Drop dynamic decay since we're not running multiple temperatures.

## Out of scope

- Multi-agent tutor (separate planner / responder / tool-caller). User has explicitly preferred single-LLM per `agentic_platform_architecture_plan.md`.
- Pre-emptive avoidance — letting the judges run during generation (would require streaming + interrupt).
- Changing the judges themselves (already shipped, work well).

## Next step

Confirm shape of `_run_self_retry` + the user/system feedback message format, then implement Phase 1 (feature flag, parallel paths) so we can compare on the same E2E.

EOF
