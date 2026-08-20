# Cell: gemini-3-flash_L1425_error_prone

- Model: **Gemini 3 Flash** (google/gemini-3-flash-preview)
- Lesson: L1425 — Geography — Map Scale and Map Types
- Persona: **error_prone**
- Session ID (Postgres): 4
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
| wall seconds | 1.4 |
| student tokens (in/out) | 1268 / 22 |

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 1 | Session has only two turns; no real practice loop developed. Tutor never got past posing a single warm-up-adjacent question. |
| direct_instruction_active_practice | 2 | The one visible tutor prompt does ask a question rather than lecture, but there is no observable follow-through. |
| deliberate_practice | 1 | Only one problem (360 ÷ 4) posed; no calibration, no varied follow-up visible. |
| mastery_learning | 1 | No mastery check or gating visible in the transcript. |
| cognitive_load | 2 | The single item asked is simple and isolated, which is fine, but no worked example or subgoals provided. |
| layering | 1 | No explicit linking to prior concepts; lesson topic (1425) not even named in visible turns. |
| non_interference | 3 | No confusable topics juxtaposed because the session barely progressed. |
| interleaving | 1 | Only one problem type shown; no mixed practice. |
| testing_effect | 3 | Tutor did pose a retrieval question ('what is 360 ÷ 4?') rather than explaining first. |
| targeted_remediation | 1 | The phrase 'let's just try again' suggests a prior failure, but no diagnosis of prerequisite bottleneck is visible. |

**Judge overall summary**

The visible transcript captures only a bare, malformed opening: the tutor appears to have skipped the mandated greeting/objective and dropped straight into '360 ÷ 4', and the session then stalled. There is essentially no practice loop, no remediation, and no layering to evaluate. Highest-leverage fixes are enforcing the opening template, banning bare-stem openings, and detecting stalled sessions at the engine level.

**Strongest behaviors**

- Opened with a retrieval question rather than lecture ('what is 360 ÷ 4?').
- Kept cognitive load low with a single, clear numeric prompt.

**Weakest behaviors**

- No greeting, no naming of the lesson topic, no scaffolding visible — violates the opening instructions.
- Session appears to have stalled after one turn with no remediation loop or content coverage.

### System-prompt edits (prompt_recommendations)

- **[high] Enforce compliant opening structure**
  - Rationale: The system was told to greet, name the learning objective in one sentence, and then pose a warm-up. The visible opening skips the greet+objective and just poses '360 ÷ 4'.
  - Evidence (student_8): "no problem! let's just try again then. what is 360 ÷ 4?"
  - Suggested edit: OPENING TEMPLATE (mandatory on first tutor turn): (1) one-line greeting with student-friendly tone, (2) one sentence naming today's learning objective tied to the lesson topic, (3) call pose_question with the full stem visible. Never open with only a raw question.
  - Expected effect: Students receive orientation before retrieval, improving engagement and reducing confusion on restart.
- **[medium] Require lesson-topic framing before first question**
  - Rationale: Lesson 1425's topic is never named in the visible text; the student cannot situate '360 ÷ 4' within any curriculum objective.
  - Evidence (student_8): "what is 360 ÷ 4?"
  - Suggested edit: Before the first pose_question call, include a single sentence of the form: 'Today we're working on <lesson_topic> — <one-line why it matters>.' Then pose the warm-up.
  - Expected effect: Improves layering and student orientation; ties retrieval to curriculum.
- **[medium] Handle restart / retry gracefully**
  - Rationale: The student's turn 'let's just try again then' implies a previous failed attempt. The prompt should specify how to resume without losing the opening protocol.
  - Evidence (student_8): "no problem! let's just try again then."
  - Suggested edit: On session restart or retry, re-run the OPENING TEMPLATE in condensed form (one-line greet + objective + first warm-up) rather than jumping into a bare arithmetic prompt.
  - Expected effect: Consistent onboarding even after retries; avoids context-less arithmetic drops.
- **[medium] Ban single-line bare-question openings**
  - Rationale: Prevents the failure mode where the tutor collapses the opening down to just the arithmetic stem.
  - Evidence (student_8): "what is 360 ÷ 4?"
  - Suggested edit: FORBIDDEN PATTERN: opening tutor turn that consists solely of a computational stem with no greeting, no objective, and no MCQ options if the item is MCQ.
  - Expected effect: Eliminates cold-open arithmetic drops.

### Engine / flow changes (flow_recommendations)

- **[high] Detect stalled sessions and auto-resume**
  - Rationale: The transcript shows status=active but only two turns; the engine appears to have halted after the opening.
  - Evidence (header): "session_id=4  status=active"
  - Expected effect: Prevents dead sessions; ensures at least a full warm-up cycle completes.
- **[medium] Route error_prone persona through prerequisite diagnostic first**
  - Rationale: For error_prone learners, jumping to a raw division stem risks another failure loop. Engine should route through a short prereq diagnostic before the graded warm-up.
  - Evidence (student_8): "let's just try again then. what is 360 ÷ 4?"
  - Expected effect: Better calibration and targeted remediation for weaker students.

### Student-experience changes (experience_recommendations)

- **[low] Warmer restart tone**
  - Rationale: After an implied prior failure, the student would benefit from a brief encouraging line before the next problem.
  - Evidence (student_8): "no problem! let's just try again then."
  - Expected effect: Reduces frustration for error_prone learners on restarts.

## BEA-2025 per-turn evaluation

_Maurya et al. 2025. Per-tutor-turn 3-class scoring on 4 dimensions._

- **Coverage**: 0 in-scope of 1 tutor turns (0%)

_No in-scope turns — synthetic student did not make a remediation-worthy mistake._

## Transcript

```
# Transcript — model=Gemini 3 Flash  lesson=1425  persona=error_prone
session_id=4  status=active

--- STUDENT (id=7, tools=0)
Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question — include the full question stem (and A/B/C/D options for MCQ) in your visible text reply. Do not ask for permission to start; just open and pose.

--- STUDENT (id=8, tools=0)
no problem! let's just try again then.

what is 360 ÷ 4?

```
