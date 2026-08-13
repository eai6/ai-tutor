# tutoring

The core conversational tutoring engine, session management, skill extraction, personalization, grading, and exit ticket assessments.

This app contains the AI logic students interact with. It manages the full lifecycle: starting a lesson, step-by-step LLM-driven instruction, answer evaluation, media delivery, exit ticket assessment, and mastery tracking.

---

## Models

### Session Tracking

#### TutorSession

A single student-lesson interaction.

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution) | Scoped to school |
| `student` | ForeignKey(User) | The learner |
| `lesson` | ForeignKey(Lesson) | The lesson being tutored |
| `prompt_pack` | ForeignKey(PromptPack, nullable) | Snapshot of prompts used |
| `model_config` | ForeignKey(ModelConfig, nullable) | Snapshot of LLM config used |
| `current_step_index` | PositiveIntegerField | Current position in step sequence |
| `status` | CharField | `active`, `completed`, `abandoned` |
| `mastery_achieved` | BooleanField | Whether the student passed |
| `engine_state` | JSONField | Full engine state (session_state, step index, shown media, etc.) |
| `is_flagged` | BooleanField | Safety flag |
| `flag_reason` | TextField | Why the session was flagged |
| `flag_reviewed` | BooleanField | Whether a staff member reviewed the flag |

#### SessionTurn

Individual messages in the conversation.

| Field | Type | Description |
|-------|------|-------------|
| `session` | ForeignKey(TutorSession) | Parent session |
| `role` | CharField | `system`, `tutor`, `student` |
| `content` | TextField | Message text |
| `step` | ForeignKey(LessonStep, nullable) | Which step this relates to |
| `tokens_in` / `tokens_out` | IntegerField | Token usage tracking |
| `metadata` | JSONField | Hints used, grading results, media shown |
| `is_flagged` | BooleanField | Content safety flag |

#### StudentLessonProgress

Cross-session mastery tracking per student per lesson.

| Field | Type | Description |
|-------|------|-------------|
| `mastery_level` | CharField | `not_started`, `in_progress`, `mastered` |
| `correct_streak` | PositiveIntegerField | Current streak of correct answers |
| `total_attempts` / `total_correct` | PositiveIntegerField | Lifetime counts |
| `best_score` | FloatField | Best exit ticket score (percentage) |

**Constraint:** `unique_together = ['student', 'lesson']`

### Exit Ticket Assessment

#### ExitTicket

Summative assessment for a lesson. One per lesson (OneToOneField).

| Field | Type | Description |
|-------|------|-------------|
| `lesson` | OneToOneField(Lesson) | The assessed lesson |
| `passing_score` | PositiveIntegerField | Default: 8 (out of 10) |
| `time_limit_minutes` | PositiveIntegerField | Default: 10 |

#### ExitTicketQuestion

Individual MCQ in the question bank.

| Field | Type | Description |
|-------|------|-------------|
| `exit_ticket` | ForeignKey(ExitTicket) | Parent ticket |
| `question_text` | TextField | The question stem |
| `option_a/b/c/d` | CharField(500) | Four answer choices |
| `correct_answer` | CharField(1) | A, B, C, or D |
| `explanation` | TextField | Why the answer is correct |
| `concept_tag` | CharField(200) | The concept this question assesses |
| `difficulty` | CharField | `easy` (recall), `medium` (apply), `hard` (analyze) |
| `image` | ImageField (nullable) | Optional question figure |

Each lesson has 30-35 questions in its bank. 10 are randomly selected per session.

#### ExitTicketAttempt

Records each student attempt.

| Field | Type | Description |
|-------|------|-------------|
| `score` | PositiveIntegerField | Correct answers out of 10 |
| `passed` | BooleanField | Score >= passing_score |
| `answers` | JSONField | `{question_id: {answer: 'A', correct: true}}` |

### Skill Knowledge Graph

#### Skill

An atomic, measurable learning outcome.

| Field | Type | Description |
|-------|------|-------------|
| `code` | CharField(100) | Unique identifier, e.g., `geo_identify_fault_types` |
| `name` | CharField(200) | Human-readable name |
| `institution` | ForeignKey(Institution) | Scoped to school |
| `course` / `unit` / `primary_lesson` | ForeignKey | Curriculum context |
| `lessons` | ManyToManyField(Lesson) | All lessons teaching this skill |
| `prerequisites` | ManyToManyField(self) | Skill dependency graph |
| `difficulty` | CharField | `foundational`, `intermediate`, `advanced` |
| `difficulty_score` | FloatField | 0.0 (easy) to 1.0 (hard) |
| `bloom_level` | CharField | `remember` through `create` |
| `importance` | FloatField | 0.0 to 1.0 |
| `tags` | JSONField | Topic categorization |

**Key method:** `get_prerequisite_chain(max_depth=5)` -- Recursively resolves the full prerequisite tree.

#### LessonPrerequisite

Prerequisite relationship between lessons. Powers the lock UI in the student catalog.

| Field | Type | Description |
|-------|------|-------------|
| `lesson` | ForeignKey(Lesson) | The lesson that requires the prerequisite |
| `prerequisite` | ForeignKey(Lesson) | The required lesson |
| `strength` | FloatField | 1.0 = essential, 0.5 = helpful, 0.0 = loosely related |
| `is_direct` | BooleanField | `True` = immediate, `False` = transitive |

**Constraint:** `unique_together = ['lesson', 'prerequisite']`

#### StudentSkillMastery

Per-student skill tracking with SM-2 spaced repetition.

| Field | Type | Description |
|-------|------|-------------|
| `mastery_level` | FloatField | 0.0 to 1.0 |
| `state` | CharField | `not_started`, `learning`, `reviewing`, `mastered` |
| `ease_factor` | FloatField | SM-2 difficulty factor (default: 2.5) |
| `interval_days` | IntegerField | Days until next review |
| `next_review_due` | DateTimeField | When to review |
| `current_streak` / `best_streak` | IntegerField | Correct answer streaks |

**Key methods:**
- `calculate_retention()` -- Exponential decay: `R = e^(-t/S)` where S is stability
- `record_attempt(was_correct, quality)` -- Updates SM-2 schedule, mastery level, and streak
- `get_review_priority(for_lesson)` -- Weighted priority score (overdue, prerequisite, retention, importance)

#### SkillPracticeLog

Detailed log of every practice event.

| Field | Type | Description |
|-------|------|-------------|
| `practice_type` | CharField | `initial`, `retrieval`, `interleaved`, `review`, `remediation` |
| `was_correct` | BooleanField | Whether the answer was correct |
| `quality` | IntegerField | 0-5 scale (SM-2 quality rating) |
| `hints_used` | IntegerField | Number of hints consumed |
| `mastery_before` / `mastery_after` | FloatField | Mastery delta |

#### StudentKnowledgeProfile

Aggregated student state per course with gamification.

| Field | Type | Description |
|-------|------|-------------|
| `total_skills` / `mastered_skills` | IntegerField | Counts |
| `average_mastery` / `average_retention` | FloatField | Aggregate metrics |
| `total_xp` | IntegerField | Experience points |
| `level` | IntegerField | XP-based level (1000 XP per level) |
| `current_streak_days` / `longest_streak_days` | IntegerField | Daily streaks |

**Methods:** `recalculate()` -- Re-aggregates from `StudentSkillMastery` records. `add_xp(amount, reason)` -- Awards XP and handles level-ups.

---

## Conversational Tutor Engine

### `ConversationalTutor` class (`conversational_tutor.py`, ~3,250 lines)

The central engine that manages tutoring sessions. Maintains state across exchanges, interfaces with the LLM, and controls lesson flow.

### Session States

```
SessionState.TUTORING  ──(all steps done)──>  SessionState.EXIT_TICKET  ──(passed/completed)──>  SessionState.COMPLETED
```

The display phase (Engage/Explore/Explain/Practice/Evaluate) comes from each step's `phase` field, not from the session state.

### Core Methods

| Method | Description |
|--------|-------------|
| `respond(student_input)` | Main entry point. Evaluates answers, advances steps, generates tutor response via LLM. Returns response dict with content, media, and metadata. |
| `respond_stream(student_input)` | Streaming variant for SSE. Yields chunks, returns full response on completion. |
| `_evaluate_step(student_input, tutor_response)` | LLM evaluator: determines answer correctness and step completion in a single call. Returns `StepEvaluationResult`. |
| `_should_advance_step(student_input, tutor_response, is_correct)` | Decides when to move to the next step. Safety valve at 8 exchanges per step. |
| `_maybe_show_exit_ticket()` | Triggers exit ticket when `current_topic_index >= len(steps)`. |

### Media Signal Pipeline

The tutor shows media (images, diagrams) during sessions using a deterministic signal system:

1. **`_build_media_catalog()`** -- Creates numbered entries: `[1] Tectonic Plate Diagram`, `[2] Fault Types Map`. Builds `self._media_id_map = {int: media_dict}` for O(1) lookup.
2. **System prompt** includes the catalog in `<media_catalog>` XML tags with instruction to append `|||MEDIA:N|||` as the last line.
3. **`_parse_media_signal(text)`** -- Regex extracts the signal, returns `(clean_text, media_dict|None)`.
4. **Signal is stripped before `_save_turn()`** -- DB content is always clean.
5. **Frontend `sanitizeContent()`** strips any leaked signals as defense-in-depth.

### Step Advancement Logic

```
_evaluate_step() → StepEvaluationResult(is_correct, is_complete, reasoning)
    ↓
_should_advance_step() checks:
  - is_complete from evaluator
  - 8-exchange hard cap (safety valve)
  - Minimum exchange floor (don't skip too fast)
  - Practice fast-path (correct answer → advance)
    ↓
If advancing: increment current_topic_index, set _step_needs_media flag
If all steps done: trigger exit ticket
```

### Engine State Persistence

All state is stored in `TutorSession.engine_state` (JSONField):

```json
{
  "session_state": "TUTORING",
  "current_topic_index": 3,
  "last_answer_correct": true,
  "shown_media_urls": ["/media/img1.png"],
  "step_exchange_counts": {"0": 4, "1": 2, "2": 3}
}
```

Backward compatible: old `phase` key is mapped to `SessionState` on load.

---

## Views

| View | Method | URL | Description |
|------|--------|-----|-------------|
| `lesson_catalog` | GET | `/tutor/` | Subject and lesson catalog with progress tracking, prerequisite locking |
| `chat_tutor_interface` | GET | `/tutor/lesson/<id>/` | Chat interface HTML page |
| `chat_start_session` | POST | `/tutor/api/chat/start/<lesson_id>/` | Initialize a new TutorSession |
| `chat_respond` | POST | `/tutor/api/chat/<session_id>/respond/` | Process student input, return tutor response |
| `chat_exit_ticket` | POST | `/tutor/api/chat/<session_id>/exit-ticket/` | Submit exit ticket answers |
| `chat_start_review` | POST | `/tutor/api/chat/<session_id>/review/` | Start review mode for completed lesson |
| `generate_image` | POST | `/tutor/api/generate-image/` | Generate educational image on demand |
| `lesson_list` | GET | `/tutor/api/lessons/` | JSON list of available lessons |

### API Response Format (`chat_respond`)

```json
{
  "response": "Let's explore tectonic plates...",
  "session_state": "TUTORING",
  "display_phase": "explore",
  "current_step": 3,
  "total_steps": 10,
  "media": {
    "url": "/media/diagrams/plates.png",
    "title": "Tectonic Plates",
    "type": "diagram"
  },
  "exit_ticket": null
}
```

---

## Skill Extraction

### `SkillExtractionService` class (`skill_extraction.py`)

Extracts skills from lesson content using LLM analysis.

| Method | Description |
|--------|-------------|
| `extract_skills_for_lesson(lesson)` | Analyzes lesson content, extracts 2-5 atomic skills, creates `Skill` and `LessonPrerequisite` records. Uses Instructor for structured output. |
| `extract_skills_for_course(course)` | Runs extraction for all lessons, then cross-lesson prerequisite detection. |
| `detect_course_prerequisites(course)` | Public method: detects prerequisites from skill graph (no LLM call). |
| `_detect_lesson_prerequisites(course)` | Uses skill prerequisite relationships to infer lesson-level prerequisites. If skill A requires skill B, and B's primary lesson is different from A's, creates a `LessonPrerequisite`. |

---

## Grading

### `grader.py`

Three grading strategies:

| Strategy | When Used | How It Works |
|----------|-----------|--------------|
| **Exact match** | MCQ, True/False | Normalized string comparison (lowercase, strip, collapse spaces) |
| **Numeric tolerance** | Math answers | Parsed float comparison with configurable tolerance |
| **LLM rubric** | Free-text responses | LLM evaluates against rubric, returns `GradingLLMResult` with score 0.0-1.0 |

```python
GradingOutcome:
  result: GradeResult  # CORRECT, INCORRECT, PARTIAL
  feedback: str        # Explanation for the student
  score: float         # 0.0 to 1.0
  details: dict        # Raw grading data
```

---

## Personalization

### `RetrievalService` class (`personalization.py`)

Embedded spaced repetition system. Selects personalized review questions at lesson start.

**Data structures:**
- `RetrievalQuestion` -- A review question with skill, lesson_step, priority_reason, retention_estimate
- `SessionPersonalization` -- Full personalization payload: retrieval questions, weak/strong skills, recommended pace

**Logic:**
1. Query `StudentSkillMastery` for overdue reviews (`next_review_due < now`)
2. Prioritize by: overdue days, prerequisite relevance, retention decay, skill importance
3. Select practice steps from prerequisite lessons
4. Return personalized questions for session warm-up

---

## Image Generation

### `ImageGenerationService` class (`image_service.py`)

Generates educational diagrams via Google Gemini.

**Prompt enhancement (`_enhance_prompt()`):**
- Adds lesson context: subject, grade level, lesson title
- Enforces "SCHEMATIC map" style for consistency
- Anti-hallucination rules: no text on images, no real faces, educational style only

---

## Management Commands

| Command | Description |
|---------|-------------|
| `generate_exit_tickets` | Generates exit ticket question banks for lessons |
| `detect_prerequisites` | Backfills lesson prerequisites from skill graph. `--course <id>`, `--clear` flags. |

---

## Test Suite

16 test files covering requirements R1-R14:

| File | Requirement | Coverage |
|------|-------------|----------|
| `test_r1_skill_extraction_pipeline.py` | R1 | Skill extraction from lessons |
| `test_r2_skill_assessment_wiring.py` | R2 | Skill assessment integration |
| `test_r3_r4_session_personalization.py` | R3-R4 | Adaptive difficulty, personalization |
| `test_r5_remediation_wiring.py` | R5 | Remediation before exit ticket |
| `test_r6_interleaved_practice.py` | R6 | Spaced practice scheduling |
| `test_r7_prerequisite_gating.py` | R7 | Prerequisite lock/unlock logic |
| `test_r8_safety_wiring.py` | R8 | Content safety flagging |
| `test_r9_system_prompt.py` | R9 | System prompt construction |
| `test_r10_mastery_transitions.py` | R10 | SessionState transitions |
| `test_r11_student_profile.py` | R11 | Profile-based adaptation |
| `test_r12_concept_coverage.py` | R12 | Concept coverage tracking |
| `test_r13_gamification.py` | R13 | XP, levels, streaks |
| `test_r14_worked_examples.py` | R14 | Worked example presentation |

---

## Architecture Decisions

- **`SessionState` enum (3 values)** replaces the old `ConversationPhase` (7 values). Display phase comes from the step's 5E `phase` field, not the session state. This makes steps the single source of truth for lesson flow.
- **Deterministic media signals** (`|||MEDIA:N|||`) replaced fuzzy title matching. No more regex-based `_parse_show_media_tag()` -- the numbered catalog gives O(1) lookup.
- **Combined step evaluator** -- `_evaluate_step()` assesses both answer correctness and step completion in a single LLM call, reducing latency.
- **Engine state in JSONField** -- All conversation state is serialized into `TutorSession.engine_state`, enabling stateless request handling.
- **SM-2 spaced repetition** -- Industry-standard algorithm for scheduling skill reviews via `StudentSkillMastery`.