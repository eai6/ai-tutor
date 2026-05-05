# Pose-Question Tool Plan

**Goal:** Make it structurally impossible for the math tutor to author its
own numerical questions. Replace the optional `|||QUESTION:N|||` signal
with a mandatory Anthropic tool-use call. Mirror the media pattern but
take it further — the LLM has native ability to type questions in
prose, so we have to *remove* that capability via tool use, not just
discourage it via prompt rules.

**Why prompt-only enforcement keeps failing:** The LLM has free-form
prose as a default channel for questions. Every prompt rule we add
("don't author questions") fights that default. As context grows, the
rules dilute. Tool use re-routes the entire question-posing capability
through a parameter (`slot: int`) that accepts only an index — there is
no `question_text` parameter the LLM could fill with arbitrary text.

---

## Design

### One tool — `pose_question`

```jsonc
{
  "name": "pose_question",
  "description": "Pose a verified bank question to the student. \
This is the ONLY way to ask a numerical question. Do not type \
questions in your text response — use this tool instead. The slot \
index refers to the <question_bank> block in the system prompt.",
  "input_schema": {
    "type": "object",
    "properties": {
      "slot": {
        "type": "integer",
        "minimum": 0,
        "description": "Which bank entry to pose. 0 = the current step's canonical question. 1..N = exit-ticket bank questions tagged to this step's concept."
      },
      "lead_in": {
        "type": "string",
        "description": "(Optional) One sentence of pedagogical lead-in to display before the question. Keep it short. Do NOT include a question yourself."
      }
    },
    "required": ["slot"]
  }
}
```

The tool has NO `question_text` parameter. The LLM cannot author.
Optional `lead_in` lets the LLM frame the question with a tight
contextual sentence ("Right — let's apply that. Try this:") without
us inventing the framing server-side.

### Bank scope (per the user's spec)

- **Slot 0** = current step's `LessonStep.teacher_script` + `expected_answer`. The canonical practice question for this step.
- **Slots 1..N** = `ExitTicketQuestion` rows where:
    - `exit_ticket__lesson == self.lesson` AND
    - `exit_ticket__is_published == True` AND
    - `concept_tag == current_step.concept_tag` (or matches one of the current step's enabling_objectives).
- **NO** other-step lesson questions.
- **NO** fallback to `pool[:N]` when concept_tag has no match. If there are no matching bank questions, slots 1..N are empty — the LLM can still pose slot 0.
- **NO** unbounded EO selection. The replacement of `|||QUESTION_EO:N|||` is a tool variant: same `pose_question` tool, but the slot space already covers exit-ticket questions for the relevant EOs. The LLM doesn't pick EOs free-form.

### Response shape

The Anthropic API returns a `Message` with `content` as a list of
content blocks. For our tutor turn we expect one of:

1. **Tool use turn:**
   - 0 or more `text` blocks (lead-in prose — usually empty since `lead_in` is in the tool args)
   - exactly 1 `tool_use` block (`pose_question`)
   - `stop_reason == "tool_use"`

2. **Text-only turn (no question posed):**
   - 1 `text` block (just teaching prose, no question)
   - `stop_reason == "end_turn"`

We don't send a tool_result back — we don't need a multi-turn agent
loop. The tool call IS the question. We render server-side, append to
the text, ship.

### Server flow

For each math turn in `respond()`:

1. Build bank → populates `self._question_id_map = {slot: entry}`.
2. Build tool definition with the slot range from `id_map` baked into the description.
3. Call `llm_client.generate_with_tools(messages, system_prompt, tools, max_tokens)` — new method on `AnthropicClient` that returns the raw `Message` object (not just text).
4. Process content blocks in order:
    - `text` block → append to `text_parts`
    - `tool_use` block where `name == 'pose_question'`:
        - Validate `slot` ∈ id_map keys. If invalid: log error, fall back to slot 0.
        - Render bank entry via `render_question_to_prose(entry)`.
        - Set `bank_question_ref` on turn metadata so next-turn grading works.
        - Append rendered stem to `text_parts`.
        - If `lead_in` arg is present, prepend it.
5. Defense in depth: scan final `text_parts` for any sentence ending with `?` AND containing 2+ digits ("if angles are 100°, 120°…?"). Strip such sentences. Log a warning. (This catches the rare case where the LLM put a numerical question in a `text` block instead of using the tool.)
6. Return `"\n\n".join(text_parts).strip()`.

### Conversation history replay — does the tutor see what it asked?

Yes, three layers cooperate:

1. **DB turn = rendered string.** After processing the tool call, we save `lead_in + "\n\n" + rendered_bank_stem` as `SessionTurn.content` for that tutor turn. The signal/tool-use scaffolding is server-internal; what hits the DB is exactly what the student saw.
2. **`_load_conversation()` replays as a plain assistant text message.** Subsequent `respond()` calls rebuild history from DB rows: `{"role": "assistant", "content": "<lead_in + rendered stem>"}, {"role": "user", "content": <student reply>}`. The LLM reads the full text, sees the question it asked, and can respond coherently to "I don't know" / "x = 65" / wrong answers / etc.
3. **Bank grader + eval signal injects the verdict.** When the student replies, `_grade_against_last_bank_question` runs deterministically. The verdict is injected into the next turn's system prompt as `<bank_evaluation_signal>` — the LLM is told "student answered correctly; canonical_working = …" so it doesn't need to derive anything from history.

We do NOT replay `tool_use` / `tool_result` blocks on subsequent calls. We don't need the agent-loop "I called a tool" memory; we only need the LLM to see the question it posed, which the saved text already provides. Same pattern as media (the saved text contains the visible reference; the `|||MEDIA:N|||` signal does not).

### Non-math turns

Tools are math-only. Non-math sessions keep using `llm_client.generate(...)` (no tools). The branch is gated on `self.lesson.unit.course.is_math`.

### What gets removed

- `|||QUESTION:N|||` signal parsing in `respond()` (the prose-tail
  path). Tool use replaces it. The signal's parsing helper can stay
  for tests but isn't invoked by the main flow.
- `|||QUESTION_EO:N|||` signal — same as above.
- The "Additional lesson-step questions" augmentation in
  `_build_question_bank_block` (the leak the user found earlier).
- `pick_candidates_for_step`'s `pool[:N]` fallback. Returns `[]` when
  no concept_tag match.
- The (now-dormant) `_force_inject_bank_question` helper — no longer
  needed because tool use prevents the failure mode upstream.
  Delete to keep the file clean.

### What stays

- Combined judge — runs as before. With tool use it should rarely
  flag NO_AUTHORING (the LLM can't author), so the judge becomes a
  sanity check + ARITHMETIC + RULE_1 detector.
- Validator V3 regen path — unchanged. If the judge does flag a
  violation (RULE_1 praise, etc.), regen still fires.
- `<final_reminder>` block — kept for the LLM's text-block prose
  guidance.
- Bank grader (`_grade_against_last_bank_question`) — already
  deterministic, drives the eval signal for the next turn. The
  `bank_question_ref` is now populated from tool_use instead of
  signal parsing, but the grader is unchanged.
- Force-inject helper code — removed (no longer needed).

---

## Logging plan

Every function in the pipeline logs entry + key state. Consistent
prefix `[QuestionTool]` so logs grep cleanly. We add this so the next
time something fails, we can see exactly which function silently
returned the wrong thing.

```
[QuestionTool] build_bank: step=2 tag='angles_around_point'
                eos=['around_point_360','find_missing_angle']
                pool_size=12 step_tag_matches=4
[QuestionTool] build_tool: slots=[0,1,2,3,4] max_slot=4
[QuestionTool] llm_call: messages=8 system_prompt_chars=18204
                tools=1 max_tokens=2048
[QuestionTool] llm_response: stop_reason=tool_use input=15324 output=287
                blocks=text(1)+tool_use(1) tool_name=pose_question
[QuestionTool] tool_call: slot=2 lead_in='Right — let''s apply that. Try this:'
[QuestionTool] resolve_slot: slot=2 → ExitTicketQuestion(id=4413,
                concept='angles_around_point') OK
[QuestionTool] render: chars=124 multiline=True
[QuestionTool] defense_strip: scanned=1 stripped=0
[QuestionTool] final: text_chars=312 has_bank_question=True
[BankRef] recorded kind=exit_ticket_question id=4413 question_type=mcq
[CombinedJudge] running on response chars=312 sub_gates=arith,fact,rule
[CombinedJudge] result: arith_corr=0 fact_claims=0 violations=[]
```

Failure paths each have a distinct log line:
- `[QuestionTool] build_bank: NO_BANK_FOR_STEP — current step has no concept_tag matches; only slot 0 will be available`
- `[QuestionTool] llm_response: NO_TOOL_CALL stop_reason=end_turn — text-only response (acceptable for teach turns)`
- `[QuestionTool] tool_call: INVALID_SLOT slot=99 max=4 — falling back to slot 0`
- `[QuestionTool] resolve_slot: slot=2 NOT_IN_ID_MAP — falling back to slot 0`
- `[QuestionTool] render: EMPTY — bank entry rendered to empty string; falling back to slot 0`
- `[QuestionTool] defense_strip: STRIPPED chars=88 — LLM put a numerical question in text block, removing`

---

## Files

### `apps/llm/client.py`
- Add `AnthropicClient.generate_with_tools(messages, system_prompt, tools, max_tokens) → AnthropicMessage`. Non-streaming `messages.create` call. Returns the full final message (not just text) so callers can introspect content blocks.
- Reuse `_supports_temperature` + `_clamp_max_tokens`.
- Logging.

### `apps/tutoring/question_bank.py`
- `pick_candidates_for_step`: drop `pool[:N]` fallback. Return `[]` when no concept_tag match. Add log line.
- `pick_published_for_concept_tag`: drop the "any published bank question for this lesson" fallback. Same approach. Add log line.
- `pick_question_for_eo`: only return a question if its EO matches the **current step's** EO. Caller passes the current step's EOs explicitly.
- Keep `parse_question_signal` and `parse_question_eo_signal` for back-compat tests, but they're not invoked by the main flow.

### `apps/tutoring/conversational_tutor.py`
- New method `_build_pose_question_tool()` → tool definition dict.
- New method `_handle_pose_question_message(message, turn_metadata) → str` that processes the Anthropic message into a final response string.
- Refactor `_generate_contextual_response()` for math turns: build tool, call `generate_with_tools`, process message.
- Remove `|||QUESTION:N|||` and `|||QUESTION_EO:N|||` parsing in `respond()` and `respond_stream()` and `_finalize_response()`.
- Update `_build_question_bank_block()`: drop other-steps augmentation. Bank is just slot 0 + concept_tag-matched exit ticket questions.
- Delete `_force_inject_bank_question` (no longer needed).
- Add logging throughout.

### `apps/tutoring/tests/test_pose_question_tool.py` (new)
- Mock `generate_with_tools` to return a `Message` with a `tool_use` block.
- Assertions:
    - Bank rendered into final response.
    - `bank_question_ref` on turn metadata.
    - When LLM emits a `text` block with a numerical question, defense regex strips it.
    - When `slot` is invalid, falls back to slot 0.
    - When `slot` resolves to None, returns text-only.
    - When math course, tool is included; non-math, no tool.
- Update existing tests in `test_question_bank.py` and `test_combined_judge.py` for the dropped fallbacks.
- Update `test_force_inject.py` — remove (force-inject deleted) or skip the helper test.

---

## Risk + rollout

- **API shape change.** Tool use is non-streaming on the call we make.
  Latency profile for the main tutor turn changes slightly (no
  token-by-token stream). Acceptable — the response is buffered to
  the frontend anyway (Azure CA SSE limitation).
- **Frontend.** No change — it already renders buffered JSON.
- **Test mocks.** Several existing tests mock `llm_client.generate`.
  Math-turn tests need to switch to mocking `generate_with_tools`.
- **Tool-use ergonomics on Opus 4.7.** Empirical question — does Opus
  reliably call the tool when prompted to? We'll log every
  `stop_reason` and verify in the first deploy.
- **Migration.** No DB migration. Pure code change.
- **Rollback.** Single git revert.

---

## Implementation order

1. **Plan committed** (this file).
2. **`AnthropicClient.generate_with_tools`** — minimal, with tests.
3. **`pick_candidates_for_step` + `pick_published_for_concept_tag` + `pick_question_for_eo`** — drop fallbacks, add logging. Tests updated.
4. **`_build_question_bank_block`** — drop other-steps augmentation. Tests updated.
5. **`_build_pose_question_tool`** + **`_handle_pose_question_message`** in conversational_tutor. Logging. Defense-in-depth regex.
6. **`_generate_contextual_response`** refactor. Math turns route through the tool path. Tests + mocks updated.
7. **Remove `|||QUESTION*|||` parsing in `respond()` / `respond_stream()` / `_finalize_response()`**. Delete `_force_inject_bank_question`.
8. **End-to-end test** with a mocked tool-use response.
9. **Full test suite green.**
10. **Commit + push** as one bundle. Watch logs on first live test.

Estimated time: 2–3 hours including tests + log audit.
