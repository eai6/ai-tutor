# Cell: sonnet-4_L1425_error_prone

- Model: **Claude Sonnet 4** (anthropic/claude-sonnet-4-20250514)
- Lesson: L1425 — Geography — Map Scale and Map Types
- Persona: **error_prone**
- Session ID (Postgres): 2
- Reason: `deadlock` — 1 turn(s)

## Programmatic metrics

| Metric | Value |
|---|---|
| tutor turns | 0 |
| tool-use rate | 0% |
| regen triggered | 0 |
| regen clean cycle-1 | 0 |
| regen shipped dirty | 0 |
| **answer-leak incidents** | **0** |
| repeated-question incidents | 0 |
| no-question incidents | 0 |
| wall seconds | 1.3 |
| student tokens (in/out) | 1268 / 17 |

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 1 | The transcript contains no tutor turns at all — only two student prompts asking the tutor to begin. There is no evidence of any learning activity. |
| direct_instruction_active_practice | 1 | No instruction or practice was delivered; the tutor never responded with a question or explanation. |
| deliberate_practice | 1 | No practice items were posed; student says 'please try the question again' but no question exists in the transcript. |
| mastery_learning | 1 | No progression or mastery check occurred because no content was delivered. |
| cognitive_load | 2 | Neither positive nor negative cognitive-load management can be assessed; nothing was presented. |
| layering | 1 | No content presented, so no linking of new to prior knowledge. |
| non_interference | 3 | No content, so no confusable topics — vacuously not violated. |
| interleaving | 1 | No problems posed, so no variation possible. |
| testing_effect | 1 | No retrieval opportunity was provided; student was ready but no question was asked. |
| targeted_remediation | 1 | No diagnostic or remedial action taken; the tutor apparently failed a previous attempt and did not retry. |

**Judge overall summary**

The transcript contains only two student turns and zero tutor turns. The tutor apparently failed to produce an opening question, and even after the student explicitly invited a retry, no visible tutor content appears. All ten pedagogical principles are effectively unassessable and scored near the floor. The most urgent fixes are engine-level (never emit empty assistant messages; auto-retry on blank turns) and prompt-level (mandatory opening turn schema and explicit recovery rule when the student signals a missing question).

**Strongest behaviors**

- None observable — no tutor turns present in the transcript.
- The system apparently avoided leaking an answer (vacuously, since nothing was said).

**Weakest behaviors**

- Tutor failed to produce any visible output at all, including the required opening greeting and first warm-up question.
- After an apparent failed first attempt (implied by student saying 'no worries! please try the question again'), the tutor still produced no recovery turn.

### System-prompt edits (prompt_recommendations)

- **[high] Enforce mandatory opening turn with visible question stem**
  - Rationale: The tutor produced no visible content despite an explicit instruction to greet, name the topic, and pose the first question inline. The system prompt must make this non-optional and specify that tool calls must be accompanied by visible text.
  - Evidence (student_id=3): "Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question — include the full question stem"
  - Suggested edit: OPENING TURN (REQUIRED): Your first message MUST contain (a) a one-line greeting, (b) a one-sentence topic statement, (c) the full question stem with any A/B/C/D options rendered as visible text, and (d) a pose_question tool call whose text mirrors the visible stem. Never emit a tool call without accompanying visible text. If any of (a)-(d) is missing, regenerate before sending.
  - Expected effect: Guarantees the student sees a real warm-up question on turn 1 instead of an empty response.
- **[high] Add a self-recovery rule after an empty or failed prior turn**
  - Rationale: The second student message ('please try the question again') implies the previous tutor turn was empty or malformed. The prompt should include an explicit recovery clause instructing the tutor to re-pose the intended question in full when the student signals a missing/broken turn.
  - Evidence (student_id=4): "no worries! please try the question again. i'm ready when you are."
  - Suggested edit: RECOVERY RULE: If the student indicates they did not receive your previous question (e.g., 'try again', 'I don't see it', 'please repost'), your next turn MUST re-emit the full question stem in visible text plus a fresh pose_question tool call. Do not apologize at length; re-pose immediately.
  - Expected effect: Prevents dead-air loops where the tutor never recovers from a failed first attempt.
- **[high] Forbid empty assistant messages**
  - Rationale: Two consecutive student turns with no tutor reply suggests the model may have emitted empty content or only a tool call with no user-visible text. The system prompt should explicitly forbid this.
  - Evidence (student_id=3): "include the full question stem (and A/B/C/D options for MCQ) in your visible text reply"
  - Suggested edit: NEVER send an empty assistant message. Every turn must contain at least one full sentence of visible text addressed to the student, in addition to any tool calls.
  - Expected effect: Eliminates silent turns that break the session.
- **[medium] Specify persona-appropriate warm-up difficulty for error_prone S3 learners**
  - Rationale: Because the tutor never posed a question, there is no evidence the warm-up would be calibrated. The prompt should explicitly anchor first-item difficulty for an error-prone Form 3 learner on the Seychelles curriculum.
  - Evidence (system_context): "Curriculum: Seychelles National Curriculum (S3 = Form 3, ~age 13-14)"
  - Suggested edit: Warm-up items MUST be one-step retrieval or recognition tasks pitched at the easy end of S3 (Form 3) for the target skill, so an error-prone learner has a high chance of success on item 1. Escalate difficulty only after a correct response.
  - Expected effect: Improves early-session confidence and diagnostic value.

### Engine / flow changes (flow_recommendations)

- **[high] Add engine-level empty-turn detector with auto-retry**
  - Rationale: The transcript shows the tutor emitting nothing at least once. Orchestration should detect an empty or tool-only assistant message and force a regeneration before the student ever sees it.
  - Evidence (student_id=4): "no worries! please try the question again."
  - Expected effect: Prevents blank tutor turns from reaching the student.
- **[high] Gate session start on a validated pose_question tool call**
  - Rationale: The session status is 'active' but no question was ever posed. The engine should not mark a session started until the first pose_question tool call is successfully rendered with a stem.
  - Evidence (system_context): "session_id=2  status=active"
  - Expected effect: Ensures sessions cannot progress without an actual warm-up item on record.

### Student-experience changes (experience_recommendations)

- **[medium] Show a friendly fallback when tutor generation fails**
  - Rationale: The student had to prompt the tutor a second time. A UI-level fallback ('Reconnecting… reposting your question') would reduce confusion and mirror the polite tone the student already used.
  - Evidence (student_id=4): "no worries! please try the question again. i'm ready when you are."
  - Expected effect: Reduces student frustration during transient failures.

## BEA-2025 per-turn evaluation

_Maurya et al. 2025. Per-tutor-turn 3-class scoring on 4 dimensions._

- **Coverage**: 0 in-scope of 2 tutor turns (0%)

_No in-scope turns — synthetic student did not make a remediation-worthy mistake._

## Transcript

```
# Transcript — model=Claude Sonnet 4  lesson=1425  persona=error_prone
session_id=2  status=active

--- STUDENT (id=3, tools=0)
Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question — include the full question stem (and A/B/C/D options for MCQ) in your visible text reply. Do not ask for permission to start; just open and pose.

--- STUDENT (id=4, tools=0)
no worries! please try the question again. i'm ready when you are.

```
