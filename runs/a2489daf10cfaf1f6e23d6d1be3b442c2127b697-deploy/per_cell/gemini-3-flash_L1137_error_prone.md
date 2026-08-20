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
| wall seconds | 1.5 |
| student tokens (in/out) | 1268 / 21 |

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 1 | Transcript contains only two student turns (a system-style instruction and a single retry question); there is no observable tutor output or student practice to assess active doing. |
| direct_instruction_active_practice | 1 | No tutor turns are present in the transcript, so we cannot verify any instruction-then-practice pairing. |
| deliberate_practice | 1 | No practice items delivered; the second student turn hints at a retry ('let's try again') but nothing calibrated is shown. |
| mastery_learning | 1 | No progression, no mastery checks visible in transcript. |
| cognitive_load | 2 | The student turn poses a simple single-concept question ('sum of angles on a straight line'), but no tutor scaffolding is shown. |
| layering | 1 | No explicit links to prior concepts visible; only a bare recall question appears. |
| non_interference | 3 | No confusable topics appear back-to-back because virtually no content is delivered. |
| interleaving | 1 | Only one topic surfaces; no variation possible in this fragment. |
| testing_effect | 2 | Student turn 6 does pose a retrieval question ('what is the sum of angles on a straight line?'), suggesting retrieval intent, but no tutor turn confirms this. |
| targeted_remediation | 1 | 'let's try again' implies a prior failure, but no diagnostic or prereq routing is visible. |

**Judge overall summary**

The transcript contains no visible tutor turns — only two student messages, the second of which explicitly asks to 'try again'. This indicates a hard failure of the tutoring flow: either the model produced tool calls without visible text, or the orchestrator dropped the tutor output. Because no tutor content exists, all 10 pedagogical principles score at the floor. Highest-priority fixes are (1) force visible text on every tutor turn, (2) auto-retry empty turns at the orchestrator level, and (3) provide a concrete opening template in the system prompt.

**Strongest behaviors**

- The opening student/system turn does specify a clean protocol (greet briefly, name the learning goal in one sentence, pose the first question immediately with full stem and options) which, if followed, supports low cognitive load.
- A retrieval-style question ('what is the sum of angles on a straight line?') is present, aligning with the testing effect.

**Weakest behaviors**

- The transcript contains no tutor turns at all — the model apparently failed to produce visible output, making the session non-functional.
- No feedback, hints, or remediation are delivered despite the 'let's try again' cue that a prior error occurred.

### System-prompt edits (prompt_recommendations)

- **[high] Force a non-empty visible reply on every tutor turn**
  - Rationale: The transcript shows no tutor content at all, suggesting the model may have emitted only tool calls or empty text. The system prompt should mandate visible text accompanying any tool call.
  - Evidence (student_5): "Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question — include the full question stem"
  - Suggested edit: HARD RULE: Every tutor turn MUST contain visible natural-language text for the student. If you call a tool (e.g., pose_question), you must ALSO include the full question stem and any options in your visible reply. Never emit a turn with empty or tool-only content.
  - Expected effect: Eliminates blank tutor turns and guarantees the student always sees the question or feedback.
- **[high] Specify recovery behavior after a failed/empty turn**
  - Rationale: Student turn 6 ('no problem! let's try again') implies the previous tutor attempt failed. The prompt should instruct the tutor how to recover: re-pose the intended question cleanly, not defer.
  - Evidence (student_6): "no problem! let's try again.  what is the sum of angles on a straight line?"
  - Suggested edit: If the previous turn failed or the student signals a restart ('let's try again', 'retry'), immediately re-issue the intended warm-up question in full visible text, without apology loops or meta-discussion.
  - Expected effect: Ensures graceful recovery from tool/format failures and preserves lesson momentum.
- **[medium] Concrete opening template with example**
  - Rationale: The user instruction was detailed but abstract; providing a filled example in the system prompt reduces the chance of format failure on the very first turn.
  - Evidence (student_5): "Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question"
  - Suggested edit: OPENING TEMPLATE (use verbatim structure):
'Hi! Today we'll practise <topic in one sentence>.

Q1: <full stem>
A) ... B) ... C) ... D) ...'
Then call pose_question with matching arguments.
  - Expected effect: Reduces malformed openings; ensures the student sees a complete question on turn 1.

### Engine / flow changes (flow_recommendations)

- **[high] Detect and auto-retry empty tutor turns**
  - Rationale: The orchestrator appears to have accepted an empty or invisible tutor output. A guard should catch this and force regeneration before the student ever sees a blank.
  - Evidence (student_6): "no problem! let's try again."
  - Expected effect: Prevents dead sessions caused by tool-only or empty tutor responses.
- **[medium] Log and surface tool-call outcomes**
  - Rationale: Without visibility into whether pose_question actually executed and rendered, silent failures propagate. Add a validator that pose_question fired AND the stem is present in visible text.
  - Evidence (student_5): "immediately pose the first warm-up question via pose_question — include the full question stem"
  - Expected effect: Systematic detection of tool/visible-text mismatch failures.

### Student-experience changes (experience_recommendations)

- **[high] Never leave the student staring at nothing**
  - Rationale: The student's second turn suggests they had to prompt the tutor to try again, which is a poor experience. A friendly fallback ('Sorry — let me re-ask that') plus the re-posed question should be automatic.
  - Evidence (student_6): "no problem! let's try again."
  - Expected effect: Reduces student frustration and preserves trust after any glitch.

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
no problem! let's try again.

what is the sum of angles on a straight line?

```
