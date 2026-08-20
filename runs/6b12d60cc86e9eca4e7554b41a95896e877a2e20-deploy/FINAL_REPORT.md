# A/B Run — Recommendations to Improve the Tutoring System Prompt

Companion to `design/AB_TESTING_PLAN.md`. **This is not a model bake-off.** The purpose of this run is to surface evidence-anchored recommendations for improving the tutoring system prompt (primary), engine flow (secondary), and student experience (secondary). Models are a robustness axis, not the unit of evaluation.

## Setup

- **Models (robustness axis)**: Claude Sonnet 4, Gemini 3 Flash
- **Lessons**: L1137, L1425
- **Personas**: error_prone (synthetic LLM students)
- **Content source**: prod_content_dump.sql loaded into local Postgres
- **Tutor model swap**: in-memory monkey-patch on `ModelConfig.get_for`, no DB writes
- **Judge**: Claude Opus (temperature=0), 10-principle rubric + structured recommendations
- **Scope**: OpenAI/GPT explicitly excluded — see `design/AB_TESTING_PLAN.md`

## Headline — Top recommendations (ranked across all cells)

Ranked by aggregated severity (high=3, medium=2, low=1) summed across the cells where the recommendation appeared, then by frequency. Use this list to drive the next revision of the tutoring system prompt.

### System-prompt edits

**1. [high] Enforce non-empty opening turn with mandatory question stem** — surfaced in 1 cell(s), severity score 3
   - Rationale: The system produced no visible tutor output at all, despite the opener instruction requiring an immediate greeting and posed question.
   - Suggested edit: Add: 'Your FIRST reply MUST contain (1) a one-line greeting, (2) a one-sentence lesson goal, and (3) the full question stem with all options rendered inline as visible text. Never respond with an empty message or a tool call alone — always include visible text alongside any tool use.'
   - Expected effect: Guarantees the student sees a warm-up question on turn 1 rather than a blank/empty tutor turn.
   - Example evidence (student_5): "Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question"
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Require visible text accompaniment for every tool call** — surfaced in 1 cell(s), severity score 3
   - Rationale: If pose_question was called silently, the student would see nothing. The prompt should forbid tool-only turns.
   - Suggested edit: Add rule: 'Every tool invocation must be paired with a visible message that restates the question stem and options. A tool call without accompanying visible text is a protocol violation.'
   - Expected effect: Prevents silent tool calls that leave the student with no content on screen.
   - Example evidence (student_5): "include the full question stem (and A/B/C/D options for MCQ) in your visible text reply"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Handle 'let's try again' recovery explicitly** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student asked 'what was your answer to the last question?' indicating a broken prior state. The prompt should specify a recovery script.
   - Suggested edit: Add: 'If the student references a prior question that is not in your visible context, acknowledge the gap, re-pose a fresh warm-up question of appropriate difficulty, and do NOT fabricate a previous answer.'
   - Expected effect: Gives the tutor a deterministic recovery path when session state is lost.
   - Example evidence (student_6): "let's try again. what was your answer to the last question?"
   - Cells: gemini-3-flash_L1137_error_prone

**4. [high] Enforce compliant opening structure** — surfaced in 1 cell(s), severity score 3
   - Rationale: The system was told to greet, name the learning objective in one sentence, and then pose a warm-up. The visible opening skips the greet+objective and just poses '360 ÷ 4'.
   - Suggested edit: OPENING TEMPLATE (mandatory on first tutor turn): (1) one-line greeting with student-friendly tone, (2) one sentence naming today's learning objective tied to the lesson topic, (3) call pose_question with the full stem visible. Never open with only a raw question.
   - Expected effect: Students receive orientation before retrieval, improving engagement and reducing confusion on restart.
   - Example evidence (student_8): "no problem! let's just try again then. what is 360 ÷ 4?"
   - Cells: gemini-3-flash_L1425_error_prone

**5. [high] Enforce mandatory opening question in first turn** — surfaced in 1 cell(s), severity score 3
   - Rationale: The system instruction explicitly required a greeting + first warm-up question, but no tutor output was produced. The prompt should hard-require a pose_question call on turn 1.
   - Suggested edit: Add a rule: 'TURN 1 IS MANDATORY: You MUST output a short greeting, a one-sentence learning goal, and call pose_question with the full stem (and options if MCQ) visible in your reply. Never end turn 1 without a posed question.'
   - Expected effect: Guarantees the lesson actually starts and the student sees a question.
   - Example evidence (student_1): "Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question"
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Add explicit 'repeat last question' behavior** — surfaced in 1 cell(s), severity score 3
   - Rationale: When the student said 'can you ask me the question again?' the tutor produced nothing. The prompt should specify how to handle re-ask requests by restating the last posed stem verbatim.
   - Suggested edit: Add: 'If the student asks to hear/see the question again, repeat the most recent question stem verbatim (including options) without changing the problem or giving hints.'
   - Expected effect: Prevents dead-end turns when students request a repeat.
   - Example evidence (student_2): "can you ask me the question again?"
   - Cells: sonnet-4_L1137_error_prone

**7. [high] Forbid empty tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: The tutor produced no visible content across the session. A rule forbidding empty responses would surface failures earlier.
   - Suggested edit: Add: 'Every tutor turn must contain visible text for the student. Never respond with an empty message or a tool call alone.'
   - Expected effect: Eliminates silent-failure turns.
   - Example evidence (session-wide): "(no tutor turns present in transcript)"
   - Cells: sonnet-4_L1137_error_prone

**8. [high] Enforce mandatory opening turn with visible question stem** — surfaced in 1 cell(s), severity score 3
   - Rationale: The tutor produced no visible content despite an explicit instruction to greet, name the topic, and pose the first question inline. The system prompt must make this non-optional and specify that tool calls must be accompanied by visible text.
   - Suggested edit: OPENING TURN (REQUIRED): Your first message MUST contain (a) a one-line greeting, (b) a one-sentence topic statement, (c) the full question stem with any A/B/C/D options rendered as visible text, and (d) a pose_question tool call whose text mirrors the visible stem. Never emit a tool call without accompanying visible text. If any of (a)-(d) is missing, regenerate before sending.
   - Expected effect: Guarantees the student sees a real warm-up question on turn 1 instead of an empty response.
   - Example evidence (student_id=3): "Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question — include the full question stem"
   - Cells: sonnet-4_L1425_error_prone

**9. [high] Add a self-recovery rule after an empty or failed prior turn** — surfaced in 1 cell(s), severity score 3
   - Rationale: The second student message ('please try the question again') implies the previous tutor turn was empty or malformed. The prompt should include an explicit recovery clause instructing the tutor to re-pose the intended question in full when the student signals a missing/broken turn.
   - Suggested edit: RECOVERY RULE: If the student indicates they did not receive your previous question (e.g., 'try again', 'I don't see it', 'please repost'), your next turn MUST re-emit the full question stem in visible text plus a fresh pose_question tool call. Do not apologize at length; re-pose immediately.
   - Expected effect: Prevents dead-air loops where the tutor never recovers from a failed first attempt.
   - Example evidence (student_id=4): "no worries! please try the question again. i'm ready when you are."
   - Cells: sonnet-4_L1425_error_prone

**10. [high] Forbid empty assistant messages** — surfaced in 1 cell(s), severity score 3
   - Rationale: Two consecutive student turns with no tutor reply suggests the model may have emitted empty content or only a tool call with no user-visible text. The system prompt should explicitly forbid this.
   - Suggested edit: NEVER send an empty assistant message. Every turn must contain at least one full sentence of visible text addressed to the student, in addition to any tool calls.
   - Expected effect: Eliminates silent turns that break the session.
   - Example evidence (student_id=3): "include the full question stem (and A/B/C/D options for MCQ) in your visible text reply"
   - Cells: sonnet-4_L1425_error_prone

_…4 additional recommendation(s) in `summary.md` and per-cell files._

### Engine / flow changes

**1. [high] Detect and retry empty tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: The orchestration allowed the tutor to produce zero visible content on the opener; a validator should catch this.
   - Expected effect: Empty or tool-only opening turns are auto-retried before display, ensuring the student always receives a stem.
   - Example evidence (student_5): "Begin the lesson. ... just open and pose."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Detect stalled sessions and auto-resume** — surfaced in 1 cell(s), severity score 3
   - Rationale: The transcript shows status=active but only two turns; the engine appears to have halted after the opening.
   - Expected effect: Prevents dead sessions; ensures at least a full warm-up cycle completes.
   - Example evidence (header): "session_id=4  status=active"
   - Cells: gemini-3-flash_L1425_error_prone

**3. [high] Retry/fallback when tutor produces no output** — surfaced in 1 cell(s), severity score 3
   - Rationale: The orchestration allowed a completely empty session. A retry or canned fallback ('Let me re-ask: ...') should trigger when the model returns no visible content.
   - Expected effect: Prevents user-facing dead sessions.
   - Example evidence (student_2): "can you ask me the question again?"
   - Cells: sonnet-4_L1137_error_prone

**4. [high] Add engine-level empty-turn detector with auto-retry** — surfaced in 1 cell(s), severity score 3
   - Rationale: The transcript shows the tutor emitting nothing at least once. Orchestration should detect an empty or tool-only assistant message and force a regeneration before the student ever sees it.
   - Expected effect: Prevents blank tutor turns from reaching the student.
   - Example evidence (student_id=4): "no worries! please try the question again."
   - Cells: sonnet-4_L1425_error_prone

**5. [high] Gate session start on a validated pose_question tool call** — surfaced in 1 cell(s), severity score 3
   - Rationale: The session status is 'active' but no question was ever posed. The engine should not mark a session started until the first pose_question tool call is successfully rendered with a stem.
   - Expected effect: Ensures sessions cannot progress without an actual warm-up item on record.
   - Example evidence (system_context): "session_id=2  status=active"
   - Cells: sonnet-4_L1425_error_prone

**6. [medium] Persist last-posed-question in session state** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student's turn 6 implies loss of question context; the engine should surface the last pose_question payload back into the tutor's context on continuation.
   - Expected effect: Enables graceful continuation of interrupted sessions without hallucinating prior content.
   - Example evidence (student_6): "what was your answer to the last question?"
   - Cells: gemini-3-flash_L1137_error_prone

**7. [medium] Route error_prone persona through prerequisite diagnostic first** — surfaced in 1 cell(s), severity score 2
   - Rationale: For error_prone learners, jumping to a raw division stem risks another failure loop. Engine should route through a short prereq diagnostic before the graded warm-up.
   - Expected effect: Better calibration and targeted remediation for weaker students.
   - Example evidence (student_8): "let's just try again then. what is 360 ÷ 4?"
   - Cells: gemini-3-flash_L1425_error_prone

**8. [medium] Cache last posed question for automatic re-display** — surfaced in 1 cell(s), severity score 2
   - Rationale: Engine should store the last pose_question payload so 're-ask' requests can be served deterministically even if the model fails.
   - Expected effect: Reliable repeat behavior independent of LLM variance.
   - Example evidence (student_2): "can you ask me the question again?"
   - Cells: sonnet-4_L1137_error_prone

### Student-experience changes

**1. [high] Show a fallback message on empty tutor response** — surfaced in 1 cell(s), severity score 3
   - Rationale: The student experienced silence, which is disorienting and unhelpful.
   - Expected effect: Student sees an explicit 'Let me re-open the lesson' message rather than emptiness.
   - Example evidence (student_6): "let's try again."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [medium] Provide immediate acknowledgement on repeat request** — surfaced in 1 cell(s), severity score 2
   - Rationale: An error-prone Form-3 student asking to hear the question again needs a friendly, immediate restatement to stay engaged; silence is discouraging.
   - Expected effect: Better student trust and engagement, particularly for error-prone learners.
   - Example evidence (student_2): "can you ask me the question again?"
   - Cells: sonnet-4_L1137_error_prone

**3. [medium] Show a friendly fallback when tutor generation fails** — surfaced in 1 cell(s), severity score 2
   - Rationale: The student had to prompt the tutor a second time. A UI-level fallback ('Reconnecting… reposting your question') would reduce confusion and mirror the polite tone the student already used.
   - Expected effect: Reduces student frustration during transient failures.
   - Example evidence (student_id=4): "no worries! please try the question again. i'm ready when you are."
   - Cells: sonnet-4_L1425_error_prone

**4. [low] Warmer restart tone** — surfaced in 1 cell(s), severity score 1
   - Rationale: After an implied prior failure, the student would benefit from a brief encouraging line before the next problem.
   - Expected effect: Reduces frustration for error_prone learners on restarts.
   - Example evidence (student_8): "no problem! let's just try again then."
   - Cells: gemini-3-flash_L1425_error_prone

## Cross-model robustness check (not a ranking)

Mean rubric scores per model — used only to decide whether a prompt change should be considered model-robust or model-specific. **Do not read this as a model evaluation.** A large gap here means the prompt change holds differently across providers; a small gap means it generalises.

| Model | Overall mean (0-5) | Cells scored |
|---|---:|---:|
| gemini-3-flash | 0.80 | 2 |
| sonnet-4 | 0.65 | 2 |

## Run statistics

- Cells completed: 4/4
- Cells errored: 0
- Total wall time: 7s (0.1 min)
- Synthetic-student tokens (in/out): 5,072 / 62

## Programmatic failure-mode counts (aggregated)

Supplementary signal; the judge's recommendations remain the headline.

| Model | Answer leaks | Repeated Q | No question | Regen shipped dirty |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 0 | 0 | 0 | 0 |
| Gemini 3 Flash | 0 | 0 | 0 | 0 |

## Files

- `summary.md` — full pivot tables (model × principle, model × persona)
- `cost_latency.md` — wall-time + token spend breakdown
- `per_cell/<key>.md` — per-cell transcript + programmatic metrics + judge scores + recommendations
- `raw_transcripts/<key>.md` — raw transcript only
- `judge_scores/<key>.json` — per-cell judge JSON output (scores + recommendations)
- `judge_rubric.md` — exact judge prompt + rubric for reproducibility
- `cell_results.jsonl` — raw programmatic metrics (one cell per line)

## Caveats

- **Synthetic-student personas, not real students** — broad strokes (struggler/capable). Real student long-tail misconceptions not represented.
- **Single run per cell** — no variance estimate. A 3-run-per-cell sweep would tighten signal.
- **Cross-prompt is the canonical comparison** — cross-model variation here is a robustness check on prompt changes, not a model evaluation. See `design/AB_TESTING_PLAN.md`.
- **20-turn cap per cell** — sessions that would naturally run longer are truncated.
