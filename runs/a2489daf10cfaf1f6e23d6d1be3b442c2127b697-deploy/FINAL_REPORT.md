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

**1. [high] Force a non-empty visible reply on every tutor turn** — surfaced in 1 cell(s), severity score 3
   - Rationale: The transcript shows no tutor content at all, suggesting the model may have emitted only tool calls or empty text. The system prompt should mandate visible text accompanying any tool call.
   - Suggested edit: HARD RULE: Every tutor turn MUST contain visible natural-language text for the student. If you call a tool (e.g., pose_question), you must ALSO include the full question stem and any options in your visible reply. Never emit a turn with empty or tool-only content.
   - Expected effect: Eliminates blank tutor turns and guarantees the student always sees the question or feedback.
   - Example evidence (student_5): "Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question — include the full question stem"
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Specify recovery behavior after a failed/empty turn** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student turn 6 ('no problem! let's try again') implies the previous tutor attempt failed. The prompt should instruct the tutor how to recover: re-pose the intended question cleanly, not defer.
   - Suggested edit: If the previous turn failed or the student signals a restart ('let's try again', 'retry'), immediately re-issue the intended warm-up question in full visible text, without apology loops or meta-discussion.
   - Expected effect: Ensures graceful recovery from tool/format failures and preserves lesson momentum.
   - Example evidence (student_6): "no problem! let's try again.  what is the sum of angles on a straight line?"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Require verbatim question restatement on retry** — surfaced in 1 cell(s), severity score 3
   - Rationale: The student asked 'can we try the question again?' — the prompt should mandate that the tutor re-pose the exact same stem (and options) without leaking the answer.
   - Suggested edit: When a student asks to retry or re-attempt a question, re-pose the ORIGINAL question stem verbatim (including all MCQ options) via pose_question. Do NOT reveal or hint at the answer. Add a brief encouragement like 'Take your time — here it is again.'
   - Expected effect: Retries preserve retrieval opportunity and avoid answer leakage.
   - Example evidence (student_id=8): "ohh, okay. can we try the question again?"
   - Cells: gemini-3-flash_L1425_error_prone

**4. [high] Forbid empty tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: The transcript shows two student turns with no visible tutor reply text, suggesting the model may have emitted only a tool call with no visible message.
   - Suggested edit: Every tutor turn MUST include visible text to the student in addition to any tool call. If invoking pose_question, also include the full question stem and options in the visible reply.
   - Expected effect: Eliminates blank turns; ensures student always sees content.
   - Example evidence (student_id=8): "ohh, okay. can we try the question again?"
   - Cells: gemini-3-flash_L1425_error_prone

**5. [high] Enforce immediate question posing on session start** — surfaced in 1 cell(s), severity score 3
   - Rationale: The tutor asked the student to choose a question instead of opening with a warm-up as instructed. The system prompt should make this non-negotiable and give an example open.
   - Suggested edit: On session start you MUST: (1) greet in ≤1 sentence, (2) state the lesson goal in ≤1 sentence, (3) immediately call pose_question with a warm-up. NEVER ask the student to choose the question or ask permission to begin. Example: 'Hi! Today we'll practice X. Here's your first question: ...'
   - Expected effect: Guarantees the session begins with active retrieval practice instead of stalling.
   - Example evidence (tutor turn following student id=2): "what's the first question you'd like to try?"
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Forbid meta-questions that offload lesson planning to student** — surfaced in 1 cell(s), severity score 3
   - Rationale: Phrases like 'what's the first question you'd like to try?' shift curricular responsibility to a 13-14 year old. Explicit prohibition prevents recurrence.
   - Suggested edit: FORBIDDEN PATTERNS: Do not ask the student 'what question would you like', 'where should we start', 'do you want to try…'. You choose the next problem based on the lesson plan and mastery state.
   - Expected effect: Keeps the tutor in control of scope and sequence.
   - Example evidence (tutor turn following student id=2): "what's the first question you'd like to try?"
   - Cells: sonnet-4_L1137_error_prone

**7. [high] Require pose_question tool call in first turn** — surfaced in 1 cell(s), severity score 3
   - Rationale: The tutor produced only text and did not invoke pose_question. A hard requirement plus a fallback rule closes this gap.
   - Suggested edit: Your first assistant message MUST include a pose_question tool call whose stem (with any A/B/C/D options) is also mirrored verbatim in your visible text reply.
   - Expected effect: Ensures structured question delivery and downstream engine tracking.
   - Example evidence (student id=1 (system directive)): "immediately pose the first warm-up question via pose_question"
   - Cells: sonnet-4_L1137_error_prone

**8. [high] Enforce mandatory opening turn with pose_question** — surfaced in 1 cell(s), severity score 3
   - Rationale: The system was told explicitly to greet and pose the first warm-up question, but the transcript shows no tutor output, indicating the opening protocol failed.
   - Suggested edit: Add to system prompt: 'Your FIRST message MUST contain: (1) a one-line greeting, (2) a one-sentence lesson objective, (3) a call to pose_question with the full question stem and options visible in the reply. Never respond with an empty or tool-only message on turn 1.'
   - Expected effect: Guarantees a visible opening question, preventing dead-start sessions.
   - Example evidence (student_id=3): "Begin the lesson. Greet the student briefly, name what they'll learn in one short sentence, then immediately pose the first warm-up question via pose_question"
   - Cells: sonnet-4_L1425_error_prone

**9. [high] Require visible text alongside every tool call** — surfaced in 1 cell(s), severity score 3
   - Rationale: Repeated silent/empty replies suggest tool calls without visible text; student had to prompt 'try asking your question again'.
   - Suggested edit: Add: 'Every assistant turn MUST include non-empty visible text for the student, even when invoking tools. Tool-only turns are forbidden.'
   - Expected effect: Prevents blank turns and improves recovery from tool failures.
   - Example evidence (student_id=4): "No worries! It seems like we had a little hiccup. Could you please try asking your question again?"
   - Cells: sonnet-4_L1425_error_prone

**10. [high] Add a fallback for tool failure** — surfaced in 1 cell(s), severity score 3
   - Rationale: When pose_question fails, the tutor should still present the question inline as plain text.
   - Suggested edit: Add: 'If a tool call fails or returns no output, immediately present the question stem (and options) as plain text in the visible reply and continue the lesson.'
   - Expected effect: Session continues even under tool errors.
   - Example evidence (student_id=4): "It seems like we had a little hiccup."
   - Cells: sonnet-4_L1425_error_prone

_…3 additional recommendation(s) in `summary.md` and per-cell files._

### Engine / flow changes

**1. [high] Detect and auto-retry empty tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: The orchestrator appears to have accepted an empty or invisible tutor output. A guard should catch this and force regeneration before the student ever sees a blank.
   - Expected effect: Prevents dead sessions caused by tool-only or empty tutor responses.
   - Example evidence (student_6): "no problem! let's try again."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Detect and repair empty tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: The engine should not advance to the next student turn if the previous tutor turn had no visible content.
   - Expected effect: Prevents silent tutor failures; forces a retry of the tutor generation.
   - Example evidence (student_id=8): "ohh, okay. can we try the question again?"
   - Cells: gemini-3-flash_L1425_error_prone

**3. [high] Auto-inject warm-up if tutor fails to call pose_question on turn 1** — surfaced in 1 cell(s), severity score 3
   - Rationale: The engine allowed a first tutor turn with no question. A guard should detect this and either resubmit or inject a default warm-up from the lesson bank.
   - Expected effect: Session never begins without an actual question on screen.
   - Example evidence (tutor turn following student id=2): "what's the first question you'd like to try?"
   - Cells: sonnet-4_L1137_error_prone

**4. [high] Detect and retry empty tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine allowed two consecutive empty tutor turns; a watchdog should force a retry with a plain-text fallback.
   - Expected effect: Prevents user-visible dead sessions caused by tool errors.
   - Example evidence (student_id=4): "Could you please try asking your question again?"
   - Cells: sonnet-4_L1425_error_prone

**5. [medium] Log and surface tool-call outcomes** — surfaced in 1 cell(s), severity score 2
   - Rationale: Without visibility into whether pose_question actually executed and rendered, silent failures propagate. Add a validator that pose_question fired AND the stem is present in visible text.
   - Expected effect: Systematic detection of tool/visible-text mismatch failures.
   - Example evidence (student_5): "immediately pose the first warm-up question via pose_question — include the full question stem"
   - Cells: gemini-3-flash_L1137_error_prone

**6. [medium] Retry-request routing** — surfaced in 1 cell(s), severity score 2
   - Rationale: When the student explicitly asks to retry, the orchestrator should re-inject the last question state rather than treat it as a new turn.
   - Expected effect: Preserves problem context across retries.
   - Example evidence (student_id=8): "can we try the question again?"
   - Cells: gemini-3-flash_L1425_error_prone

**7. [medium] Retry policy on empty/malformed tutor opening** — surfaced in 1 cell(s), severity score 2
   - Rationale: The evidence shows a second attempt still lacked content. A cap of one retry with a stricter re-prompt (or fallback template) should be enforced.
   - Expected effect: Guarantees a valid opening within two attempts.
   - Example evidence (tutor turn following student id=2): "no problem! let's try again."
   - Cells: sonnet-4_L1137_error_prone

**8. [medium] Log and alert on pose_question failures** — surfaced in 1 cell(s), severity score 2
   - Rationale: Silent failure of the primary question-posing tool halted the lesson entirely.
   - Expected effect: Enables engineering to diagnose and fix tool integration issues.
   - Example evidence (student_id=4): "It seems like we had a little hiccup."
   - Cells: sonnet-4_L1425_error_prone

### Student-experience changes

**1. [high] Never leave the student staring at nothing** — surfaced in 1 cell(s), severity score 3
   - Rationale: The student's second turn suggests they had to prompt the tutor to try again, which is a poor experience. A friendly fallback ('Sorry — let me re-ask that') plus the re-posed question should be automatic.
   - Expected effect: Reduces student frustration and preserves trust after any glitch.
   - Example evidence (student_6): "no problem! let's try again."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [medium] Show a visible warm-up immediately on entry** — surfaced in 1 cell(s), severity score 2
   - Rationale: An error_prone S3 learner needs momentum; being asked to choose a question is disorienting and demotivating.
   - Expected effect: Reduces cold-start friction and increases time-on-task.
   - Example evidence (tutor turn following student id=2): "what's the first question you'd like to try?"
   - Cells: sonnet-4_L1137_error_prone

**3. [medium] Provide a friendly recovery message when the tutor stalls** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student had to prompt the tutor to try again, which is a poor experience.
   - Expected effect: Reduces frustration when errors occur.
   - Example evidence (student_id=4): "No worries! It seems like we had a little hiccup."
   - Cells: sonnet-4_L1425_error_prone

**4. [low] Warm acknowledgment on retry** — surfaced in 1 cell(s), severity score 1
   - Rationale: An error-prone learner benefits from low-stakes framing when asking to retry.
   - Expected effect: Reduces anxiety and supports persistence.
   - Example evidence (student_id=8): "ohh, okay. can we try the question again?"
   - Cells: gemini-3-flash_L1425_error_prone

## Cross-model robustness check (not a ranking)

Mean rubric scores per model — used only to decide whether a prompt change should be considered model-robust or model-specific. **Do not read this as a model evaluation.** A large gap here means the prompt change holds differently across providers; a small gap means it generalises.

| Model | Overall mean (0-5) | Cells scored |
|---|---:|---:|
| gemini-3-flash | 1.35 | 2 |
| sonnet-4 | 0.00 | 2 |

## Run statistics

- Cells completed: 4/4
- Cells errored: 0
- Total wall time: 16s (0.3 min)
- Synthetic-student tokens (in/out): 5,072 / 78

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
