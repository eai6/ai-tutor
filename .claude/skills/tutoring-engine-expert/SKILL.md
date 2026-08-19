---
name: tutoring-engine-expert
description: Expert on the ConversationalTutor engine in apps/tutoring/. Auto-loads when working on the tutoring session engine, 5E flow, step evaluation, media signaling, exit tickets, or remediation. Covers SessionState, engine_state JSON shape, LLM call sites, step advancement logic, math-tutor rules, and pedagogy constraints specific to this project.
paths:
  - "apps/tutoring/**/*.py"
  - "templates/tutoring/**/*.html"
  - "static/js/tutor*.js"
---

# Tutoring Engine Expert

Expert on `apps/tutoring/conversational_tutor.py` and related files. This engine is the heart of the product — most pedagogy lives here.

## Mental model

The engine is a **state machine that wraps an LLM conversation**. Each student message triggers:

1. Save student turn
2. Generate tutor reply (LLM call)
3. Parse media signal from reply
4. Evaluate answer correctness + step completion (LLM call)
5. Advance state deterministically based on evaluation
6. Save tutor turn (cleaned)
7. Return `TutorMessage` to caller

The state machine has **three session states** (`SessionState` enum):
- `TUTORING` — walking through steps (includes remediation, which replays steps in this state)
- `EXIT_TICKET` — administering exit ticket after all steps complete
- `COMPLETED` — session done, mastery achieved OR abandoned

Display-level 5E labels (Engage / Explore / Explain / Practice / Evaluate) come from each `LessonStep.phase`, not the session state.

## File layout

| File | Responsibility |
|---|---|
| `conversational_tutor.py` | Main engine class `ConversationalTutor` (~4000+ lines) |
| `views.py` | HTTP endpoints: `chat_start`, `chat_respond`, `exit_ticket_submit`, etc. |
| `tutoring_models.py` | `TutorSession`, `SessionTurn`, `ExitTicket`, `ExitTicketQuestion`, `ExitTicketAttempt`, `StudentLessonProgress` |
| `grader.py` | Exit ticket answer grading (numeric, keyword, LLM) |
| `personalization.py` | `RemediationService`, difficulty adaptation, interleaved practice |
| `skills_models.py` | `StudentSkillMastery` (separate skill-graph system) |

## Public engine API

```python
tutor = ConversationalTutor(session: TutorSession)
msg: TutorMessage = tutor.respond(student_input: str)
# msg.content, msg.phase, msg.media, msg.show_exit_ticket, msg.exit_ticket_data,
# msg.is_complete, msg.is_correct, msg.streak_count, msg.practice_score, msg.milestone,
# msg.artifact_html, msg.step_number, msg.total_steps

# Streaming version exists but NOT USED in prod (Azure buffers):
for token_json in tutor.respond_stream(student_input):
    ...
```

Constructor loads: session, lesson, steps, exit ticket concepts, enabling objectives, conversation history, LLM clients. Do NOT access tutor state before calling `respond()` — initialization is implicit in constructor.

## Engine state (persistence)

`TutorSession.engine_state` is a JSONField. Shape (abridged):

```json
{
  "session_state": "tutoring",       // enum: tutoring | exit_ticket | completed
  "display_phase": "practice",        // 5E phase for UI
  "current_topic_index": 3,           // index into self.steps
  "step_exchange_count": 2,           // exchanges on current step
  "exchange_count": 12,               // total session exchanges
  "practice_correct": 5,
  "practice_total": 7,
  "correct_streak": 3,
  "concepts_covered": [...],
  "covered_concept_ids": [...],
  "covered_enabling_objectives": [...],
  "is_remediation": false,
  "failed_exit_questions": [...],
  "_failed_eos": [...],
  "shown_media_urls": [...],
  "shown_worked_example_indices": [...],
  "difficulty_level": "medium",
  "cognitive_load": 0.4,
  "consecutive_wrong": 0,
  "_step_needs_media": true           // deterministic media attach flag
}
```

**Rules for adding attributes**:
- Add to `_save_state()` (serialization) and `_load_state()` (deserialization)
- Default sensibly if missing (backwards compat for in-flight sessions)
- Don't add redundant derivations — compute on read

## LLM call sites

Approximately **3–5 LLM calls per turn** today. Key sites:

| Call | Location | Purpose |
|---|---|---|
| Response generation | `_generate_response()` ~line 2424 | Tutor dialogue |
| Step evaluation | `_evaluate_step()` ~line 3006 | StepEvaluationResult: answer_correct + step_complete |
| Response evaluation (fallback) | `_llm_evaluate_response()` ~line 3465 | Legacy keyword/LLM eval |
| Exit ticket coverage | `_coverage_check()` ~line 3640 | Matches tutoring dialogue to exit ticket concepts |
| Remediation planning | `RemediationService.get_remediation_plan()` | Weak-skill + prerequisite analysis |

**When adding LLM calls**: use the `apps/llm/client.py` abstraction, not direct SDK calls. `self.llm_client.generate(messages, system_prompt)`.

## System prompt assembly

Built in `_build_response_prompt()` (~line 2240). Structure:

1. Base template `TUTOR_SYSTEM_PROMPT_TEMPLATE` (~line 68)
2. Formatted via `template.format_map()` with: `institution_name`, `locale_context`, `tutor_name`, `language`, `grade_level`, `safety_prompt`, `personality_modifier`
3. Math-specific rules appended if `lesson.is_math`
4. Student profile block
5. Retrieval/warmup block
6. Interleaved practice block
7. Step directive (current step `teacher_script`, `question`, `expected_answer`, `hints`)
8. Media catalog (`<media_catalog>` XML)
9. Lesson context (exit ticket concepts, enabling objectives, step overview)

**To add a new prompt injection**: add a method that returns the block, call it from `_build_response_prompt()` in logical order. Don't inline in the main method.

## Media signal format (CRITICAL)

**The tutor appends `|||MEDIA:N|||` as the LAST line of its response.** `N` is 1-based index into `self._media_id_map` (built by `_build_media_catalog()`).

Flow:
1. `_generate_response()` returns raw response (may or may not have signal)
2. `_parse_media_signal(text)` → `(clean_text, media_dict | None)` — regex, no fuzzy matching
3. `respond()` / `_finalize_response()` parse BEFORE `_save_turn()` — DB stays clean
4. Clean text saved; media dict returned in `TutorMessage.media`

**Never**:
- Reintroduce fuzzy title matching (`_parse_show_media_tag` was deleted — 85 lines of bugs)
- Use the legacy `[SHOW_MEDIA:title]` format
- Save raw (uncleaned) tutor text to DB
- Let the signal leak to the frontend — `sanitizeContent()` in `chat_tutor.html` is defense-in-depth, not the primary strip

**Streaming case**: `respond_stream()` done chunk uses `metadata.get('clean_content', full_content)`. The signal is parsed from the final accumulated text, not mid-stream.

## Step advancement

`_should_advance_step()` — deterministic given `is_correct` + step context:

- `teach` / `worked_example` / `summary`: advance after tutor delivers (1-2 exchanges, user acknowledges)
- `practice` / `quiz`: advance after N correct, with safety valve at 8 exchanges
- `min_exchange_floor` prevents premature advance on a single correct answer
- `last_answer_correct` (bool, not `last_practice_correct` — renamed during phase-system removal)

**Renamed attributes (since P7 phase-system removal)** — don't use the old names:
- `self.phase` → `self.session_state`
- `self.last_practice_correct` → `self.last_answer_correct`
- REMOVED: `self.phase_exchange_count`, `self.instruction_checks_correct`

## Exit ticket flow

Trigger: `current_topic_index >= len(self.steps)` in `respond()`. Sets `session_state = EXIT_TICKET` and returns `TutorMessage(show_exit_ticket=True, exit_ticket_data={...})`.

Frontend submits answers to `/tutor/api/chat/<session_id>/exit-ticket/`. Handler: `_handle_exit_ticket()` (~line 3901).

Grading: `_grade_exit_question()` per question type:
- MCQ: exact letter match
- Math numeric: numerical comparison
- Free text / fill-in-blank / matching: LLM grading, fallback to keyword match

**Current bug** (see `memory/lesson_competency_plan.md`): `passed = correct >= 8` is hardcoded; should use `self.exit_ticket.passing_score`. Fix in competency work (phase C1).

**On pass**: `_complete_session_with_results()` sets `session.mastery_achieved = True`, `progress.mastery_level = 'mastered'`.

**On fail**: `_start_remediation()` — resets to step 0, `session_state = TUTORING`, `is_remediation = True`, targets failed `_failed_eos` for re-teaching. Safety valve: 15 exchanges → force re-attempt.

## Math tutoring rules (pedagogy)

From `feedback_math_tutoring.md` (auto-memory): **the math tutor must NOT evaluate a bare numeric answer.** Instead:
- Teach via named subskills (e.g., "unit conversion", "isolating variables")
- Use named tips ("tip: check your units")
- Follow a rung-based complexity ladder (simpler → harder problems)
- Require workings, not just answers

If you're touching math paths, read `~/.claude/projects/-Users-edwardamoah-Documents-GitHub-ai-tutor/memory/feedback_math_tutoring.md` for the full guideline.

Enforcement is via the math-specific system prompt block (appended when `lesson.is_math`) + the LLM-evaluator's reasoning field. Don't weaken these.

## Remediation

`_start_remediation()` (~line 4176):
1. Record failed `ExitTicketAttempt`
2. Extract failed EOs from question `concept_tag` values
3. Mark failed concepts as `covered=False` in `exit_ticket_concepts`
4. Reset: `session_state = TUTORING`, `current_topic_index = 0`, `is_remediation = True`
5. Generate EO-focused remediation opening prompt
6. Student re-walks steps targeted to weak EOs
7. Re-attempt exit ticket (multiple attempts allowed; best score wins — see competency plan)

Safety valve in `respond()`: force re-attempt after 15 remediation exchanges.

## Group sessions (planned, see `memory/group_lessons_plan.md`)

Minor engine change: if `session.is_group`, inject `{group_mode: True, student_names: [...]}` into the system prompt builder so the tutor addresses the group by name. Everything else — answer evaluation, step advance, exit ticket — unchanged. Students answer as a collective.

## Common modifications

### Adding a new step type

1. Add choice to `LessonStep.step_type` enum
2. Update `_get_step_phase_instructions()` to handle the new type
3. Update `_should_advance_step()` with the advancement rule
4. Update `_evaluate_step()` prompt if evaluation semantics differ
5. Update content generator schema (`apps/curriculum/content_generator.py`) if LLM should produce this type

### Adding a new metric to `engine_state`

1. Add init in `_reset_engine_state()` or `_load_state()` default
2. Update in the relevant handler (e.g., `_handle_student_answer()`)
3. Add to `_save_state()` serialization
4. Add to `TutorMessage` if student-facing or frontend-visible
5. Add test for persistence + resumption

### Adding a new LLM call site

1. Use `self.llm_client.generate(...)` — never direct SDK
2. For structured output, use `self.instructor_client.create(...)` with a Pydantic model
3. Log the call: `logger.info(f"[Tutor] {purpose} — tokens_in={x} tokens_out={y}")`
4. Add a fallback for API failures (don't crash the session)

## Testing

Key test file: `apps/tutoring/tests/test_r10_mastery_transitions.py`.

Mock the LLM client:
```python
from unittest.mock import Mock, patch

@pytest.fixture
def mock_llm():
    with patch('ai_tutor.apps.tutoring.conversational_tutor.get_llm_client') as m:
        client = Mock()
        client.generate.return_value = LLMResponse(content='...', tokens_in=0, tokens_out=0, model='test')
        m.return_value = client
        yield client
```

For integration tests, prefer small real lessons with deterministic step types (MCQ practice) to keep LLM variance out of asserts.

## Things that will break the engine

- Changing `SessionState` enum values without a migration path
- Saving raw (uncleaned) tutor text to DB (media signals leak)
- Re-introducing `ConversationPhase` or any form of phase-based flow control
- Direct LLM SDK calls bypassing `apps/llm/client.py`
- Forgetting `_save_state()` update when adding `self.` attributes
- Adding new state without matching `_load_state()` default (breaks in-flight sessions after deploy)

## Further context

- `memory/lesson_competency_plan.md` — in-flight work changing mastery to exit-ticket-driven
- `memory/group_lessons_plan.md` — upcoming group session feature
- `CLAUDE.md` — always-apply rules for this project
- Auto-memory P7 section — historical context on phase-system removal
