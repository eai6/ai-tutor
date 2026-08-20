# Cell: gemini-3-flash_L1137_error_prone

- Model: **Gemini 3 Flash** (google/gemini-3-flash-preview)
- Lesson: L1137 — Math — Angles around a point
- Persona: **error_prone**
- Session ID (Postgres): 3
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
| student tokens (in/out) | 1268 / 15 |

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 0 | The transcript contains no tutor turns — no instruction or practice was delivered. |
| direct_instruction_active_practice | 0 | No teaching segments and no practice occurred; tutor never responded. |
| deliberate_practice | 0 | No question was ever posed, so no calibrated practice occurred. |
| mastery_learning | 0 | No progression or mastery check took place; the session is empty of tutor output. |
| cognitive_load | 0 | No content delivered, so cognitive load management cannot be assessed positively. |
| layering | 0 | No explanations or links to prior knowledge were provided. |
| non_interference | 0 | No topics discussed; principle inapplicable but scored 0 due to absence of instruction. |
| interleaving | 0 | No problems posed, so no variation possible. |
| testing_effect | 0 | No retrieval opportunities offered — tutor never posed the warm-up question requested. |
| targeted_remediation | 0 | Student's turn 6 ('let's try again. what was your answer to the last question?') suggests prior confusion, but no remediation is present. |

**Judge overall summary**

The transcript contains only two student turns and zero tutor turns, so the session never actually began. All 10 principles score 0 because no instruction, practice, or remediation occurred. Highest-priority fixes are engine-level: guarantee non-empty opening turns, pair tool calls with visible text, and persist last-question state so the tutor can recover when the student says 'let's try again'.

**Strongest behaviors**

- None identifiable — transcript contains no tutor turns
- N/A

**Weakest behaviors**

- Tutor never opened the lesson or posed the warm-up question despite explicit instruction
- Tutor never responded to the student's follow-up asking about the last question

### System-prompt edits (prompt_recommendations)

- **[high] Enforce non-empty opening turn with mandatory question stem**
  - Rationale: The system produced no visible tutor output at all, despite the opener instruction requiring an immediate greeting and posed question.
  - Evidence (student_5): "Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question"
  - Suggested edit: Add: 'Your FIRST reply MUST contain (1) a one-line greeting, (2) a one-sentence lesson goal, and (3) the full question stem with all options rendered inline as visible text. Never respond with an empty message or a tool call alone — always include visible text alongside any tool use.'
  - Expected effect: Guarantees the student sees a warm-up question on turn 1 rather than a blank/empty tutor turn.
- **[high] Require visible text accompaniment for every tool call**
  - Rationale: If pose_question was called silently, the student would see nothing. The prompt should forbid tool-only turns.
  - Evidence (student_5): "include the full question stem (and A/B/C/D options for MCQ) in your visible text reply"
  - Suggested edit: Add rule: 'Every tool invocation must be paired with a visible message that restates the question stem and options. A tool call without accompanying visible text is a protocol violation.'
  - Expected effect: Prevents silent tool calls that leave the student with no content on screen.
- **[high] Handle 'let's try again' recovery explicitly**
  - Rationale: Student asked 'what was your answer to the last question?' indicating a broken prior state. The prompt should specify a recovery script.
  - Evidence (student_6): "let's try again. what was your answer to the last question?"
  - Suggested edit: Add: 'If the student references a prior question that is not in your visible context, acknowledge the gap, re-pose a fresh warm-up question of appropriate difficulty, and do NOT fabricate a previous answer.'
  - Expected effect: Gives the tutor a deterministic recovery path when session state is lost.

### Engine / flow changes (flow_recommendations)

- **[high] Detect and retry empty tutor turns**
  - Rationale: The orchestration allowed the tutor to produce zero visible content on the opener; a validator should catch this.
  - Evidence (student_5): "Begin the lesson. ... just open and pose."
  - Expected effect: Empty or tool-only opening turns are auto-retried before display, ensuring the student always receives a stem.
- **[medium] Persist last-posed-question in session state**
  - Rationale: Student's turn 6 implies loss of question context; the engine should surface the last pose_question payload back into the tutor's context on continuation.
  - Evidence (student_6): "what was your answer to the last question?"
  - Expected effect: Enables graceful continuation of interrupted sessions without hallucinating prior content.

### Student-experience changes (experience_recommendations)

- **[high] Show a fallback message on empty tutor response**
  - Rationale: The student experienced silence, which is disorienting and unhelpful.
  - Evidence (student_6): "let's try again."
  - Expected effect: Student sees an explicit 'Let me re-open the lesson' message rather than emptiness.

## BEA-2025 per-turn evaluation

_Maurya et al. 2025. Per-tutor-turn 3-class scoring on 4 dimensions._

- **Coverage**: 0 in-scope of 1 tutor turns (0%)

_No in-scope turns — synthetic student did not make a remediation-worthy mistake._

## Transcript

```
# Transcript — model=Gemini 3 Flash  lesson=1137  persona=error_prone
session_id=3  status=active

--- STUDENT (id=5, tools=0)
Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question — include the full question stem (and A/B/C/D options for MCQ) in your visible text reply. Do not ask for permission to start; just open and pose.

--- STUDENT (id=6, tools=0)
let's try again. what was your answer to the last question?

```
