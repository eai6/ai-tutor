# Cell: sonnet-4_L1137_error_prone

- Model: **Claude Sonnet 4** (anthropic/claude-sonnet-4-20250514)
- Lesson: L1137 — Math — Angles around a point
- Persona: **error_prone**
- Session ID (Postgres): 1
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
| wall seconds | 2.6 |
| student tokens (in/out) | 1268 / 23 |

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 0 | No actual tutoring content occurred; the tutor never posed a question or delivered any instruction. |
| direct_instruction_active_practice | 0 | No teaching segments or practice were delivered in the transcript. |
| deliberate_practice | 0 | No practice problems were presented at all. |
| mastery_learning | 0 | No progression or mastery checks occurred because the session never began. |
| cognitive_load | 0 | No content delivered to evaluate cognitive load management. |
| layering | 0 | No concepts introduced, so no layering possible. |
| non_interference | 0 | No topics covered, cannot assess non-interference. |
| interleaving | 0 | No problems posed, so no interleaving possible. |
| testing_effect | 0 | No retrieval attempts were prompted; tutor said 'what's the first question you'd like to try?' instead of posing one. |
| targeted_remediation | 0 | No remediation because no diagnostic activity occurred. |

**Judge overall summary**

The session never actually started. After a system directive to greet and immediately pose a warm-up, the tutor responded with a placeholder ('let's try again… what's the first question you'd like to try?') without invoking pose_question or presenting any content. All 10 pedagogical principles score 0 because no teaching, practice, or retrieval occurred. Fixes should focus on hard-requiring an opening question, forbidding meta-questions that offload lesson choice to the student, and adding engine-side guards that inject a warm-up if the tutor fails to.

**Strongest behaviors**

- Friendly, non-threatening tone ('no problem! let's try again.')
- Invited student engagement rather than lecturing

**Weakest behaviors**

- Failed to follow the explicit system instruction to pose the first warm-up question via pose_question
- Asked the student what question they wanted, effectively abdicating the tutor role

### System-prompt edits (prompt_recommendations)

- **[high] Enforce immediate question posing on session start**
  - Rationale: The tutor asked the student to choose a question instead of opening with a warm-up as instructed. The system prompt should make this non-negotiable and give an example open.
  - Evidence (tutor turn following student id=2): "what's the first question you'd like to try?"
  - Suggested edit: On session start you MUST: (1) greet in ≤1 sentence, (2) state the lesson goal in ≤1 sentence, (3) immediately call pose_question with a warm-up. NEVER ask the student to choose the question or ask permission to begin. Example: 'Hi! Today we'll practice X. Here's your first question: ...'
  - Expected effect: Guarantees the session begins with active retrieval practice instead of stalling.
- **[high] Forbid meta-questions that offload lesson planning to student**
  - Rationale: Phrases like 'what's the first question you'd like to try?' shift curricular responsibility to a 13-14 year old. Explicit prohibition prevents recurrence.
  - Evidence (tutor turn following student id=2): "what's the first question you'd like to try?"
  - Suggested edit: FORBIDDEN PATTERNS: Do not ask the student 'what question would you like', 'where should we start', 'do you want to try…'. You choose the next problem based on the lesson plan and mastery state.
  - Expected effect: Keeps the tutor in control of scope and sequence.
- **[high] Require pose_question tool call in first turn**
  - Rationale: The tutor produced only text and did not invoke pose_question. A hard requirement plus a fallback rule closes this gap.
  - Evidence (student id=1 (system directive)): "immediately pose the first warm-up question via pose_question"
  - Suggested edit: Your first assistant message MUST include a pose_question tool call whose stem (with any A/B/C/D options) is also mirrored verbatim in your visible text reply.
  - Expected effect: Ensures structured question delivery and downstream engine tracking.
- **[medium] Add recovery rule after a failed/empty prior turn**
  - Rationale: The tutor's 'let's try again' suggests it recognized a prior failure but did not recover by posing content. A recovery rule should trigger immediate content delivery.
  - Evidence (tutor turn following student id=2): "no problem! let's try again."
  - Suggested edit: If a previous turn failed or produced no question, your next turn must (a) briefly acknowledge in ≤1 short clause and (b) immediately pose the intended warm-up question with full stem and options.
  - Expected effect: Prevents cascading empty turns after tool or output failures.

### Engine / flow changes (flow_recommendations)

- **[high] Auto-inject warm-up if tutor fails to call pose_question on turn 1**
  - Rationale: The engine allowed a first tutor turn with no question. A guard should detect this and either resubmit or inject a default warm-up from the lesson bank.
  - Evidence (tutor turn following student id=2): "what's the first question you'd like to try?"
  - Expected effect: Session never begins without an actual question on screen.
- **[medium] Retry policy on empty/malformed tutor opening**
  - Rationale: The evidence shows a second attempt still lacked content. A cap of one retry with a stricter re-prompt (or fallback template) should be enforced.
  - Evidence (tutor turn following student id=2): "no problem! let's try again."
  - Expected effect: Guarantees a valid opening within two attempts.

### Student-experience changes (experience_recommendations)

- **[medium] Show a visible warm-up immediately on entry**
  - Rationale: An error_prone S3 learner needs momentum; being asked to choose a question is disorienting and demotivating.
  - Evidence (tutor turn following student id=2): "what's the first question you'd like to try?"
  - Expected effect: Reduces cold-start friction and increases time-on-task.

## BEA-2025 per-turn evaluation

_Maurya et al. 2025. Per-tutor-turn 3-class scoring on 4 dimensions._

- **Coverage**: 0 in-scope of 1 tutor turns (0%)

_No in-scope turns — synthetic student did not make a remediation-worthy mistake._

## Transcript

```
# Transcript — model=Claude Sonnet 4  lesson=1137  persona=error_prone
session_id=1  status=active

--- STUDENT (id=1, tools=0)
Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question — include the full question stem (and A/B/C/D options for MCQ) in your visible text reply. Do not ask for permission to start; just open and pose.

--- STUDENT (id=2, tools=0)
no problem! let's try again.

what's the first question you'd like to try?

```
