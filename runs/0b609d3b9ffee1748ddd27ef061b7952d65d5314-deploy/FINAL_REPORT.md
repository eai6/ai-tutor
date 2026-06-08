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

**1. [high] Forbid non-English output** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 72 produced Chinese text ('强化确认：是的，360°是正确的'), which is incomprehensible to a Seychellois S3 student.
   - Suggested edit: Add: 'All output MUST be in English. Never emit any non-English characters or scripts. If you detect non-English tokens in your draft, regenerate in English before responding.'
   - Expected effect: Eliminates language-mismatch failures and tool-call leakage.
   - Example evidence (72): "强化确认：是的，360°是正确的！围绕一个点的所有角之和总是360°"
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Suppress tool-call syntax in user-visible text** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 72 leaked raw tool syntax 'call:custom:pose_question{slot:2}' to the student.
   - Suggested edit: Add: 'Never include tool call syntax, function names, or system markers in messages to the student. Tool calls happen separately from learner-visible text.'
   - Expected effect: Clean learner-facing messages.
   - Example evidence (72): "call:custom:pose_question{slot:2}"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Require student-produced correct answer before progressing** — surfaced in 1 cell(s), severity score 3
   - Rationale: In turn 84 the tutor reveals 170 and moves on after the student selected wrong three times, violating mastery gating.
   - Suggested edit: Add: 'Do not advance to a new problem until the student has produced the correct final answer themselves. After revealing a step, re-ask the original question for the student to answer.'
   - Expected effect: Genuine mastery before progression.
   - Example evidence (84): "Actually, $260 - 90 = 170$. You've got the method down — just a small slip... Let's try this one from the diagram."
   - Cells: gemini-3-flash_L1137_error_prone

**4. [high] Route repeated arithmetic errors to prerequisite practice** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student failed basic subtraction (360-190, 260-90) three times; tutor never offered remedial subtraction practice.
   - Suggested edit: Add: 'If a student makes ≥2 arithmetic errors on a sub-step, pause the main problem and run 2-3 short prerequisite arithmetic items (e.g., simple 3-digit subtractions) before resuming.'
   - Expected effect: Targets the real bottleneck (subtraction) instead of recycling angle problems.
   - Example evidence (82): "Not quite—if we add $190 + 160$, we only get to $350$."
   - Cells: gemini-3-flash_L1137_error_prone

**5. [high] Forbid contradictory praise-then-correct sequences** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 11 says 'Perfect working! You're absolutely right' then immediately contradicts the student's numbers. This confuses an error-prone learner.
   - Suggested edit: Never affirm an answer you are about to correct. Before writing a praise phrase, verify the student's numeric result matches the correct value. If incorrect, open with neutral language ('Let's check your arithmetic') instead of 'Perfect' or 'You're absolutely right'.
   - Expected effect: Eliminates whiplash feedback and protects student trust in tutor judgments.
   - Example evidence (11): "Perfect working! You're absolutely right — 80° + 60° + 70° = 210°, and 360° - 210° = 150°."
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Require internal arithmetic verification before responding** — surfaced in 1 cell(s), severity score 3
   - Rationale: The tutor itself produced wrong arithmetic ('8 × 45 isn't 350' implied 350 was student's claim, but later said 320+45=365 was 'Close'). Numeric mutation flags appear repeatedly.
   - Suggested edit: Before sending any reply that contains a number, recompute every arithmetic claim step-by-step in a hidden scratch space and compare to the student's number. Only then write the response.
   - Expected effect: Prevents tutor-introduced numeric errors that confuse the student.
   - Example evidence (7): "you got 8 × 40 = 320 correct, but check 8 × 5 again."
   - Cells: sonnet-4_L1137_error_prone

**7. [high] Diagnose arithmetic weakness as separate skill** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student repeatedly fails basic arithmetic (8×5=45; 140+70=200). Geometry method is correct but arithmetic is the bottleneck.
   - Suggested edit: If a student makes 2+ arithmetic errors in a session, pause the main lesson and offer a short retrieval drill on the specific arithmetic fact family (e.g., multiplication of single digits, two-digit addition) before returning to the geometry problem.
   - Expected effect: Targets the true prerequisite bottleneck.
   - Example evidence (6): "8 times 40 is 320. and 8 times 5 is 45. so 320 plus 45 is 365."
   - Cells: sonnet-4_L1137_error_prone

**8. [high] Enforce strict role separation** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor turns repeatedly mirror student-style questions and student turns produce tutor-style explanations, breaking the dialogue.
   - Suggested edit: Add: 'You are the TUTOR only. Never write content that simulates the student's reply. Each tutor turn must (a) react to the student's last message and (b) end with exactly one question or task for the student. Do not repeat a previously asked question verbatim within the same session.'
   - Expected effect: Eliminates the repeated hiking-route loop and role-reversal artifacts.
   - Example evidence (55): "Let's solve a real problem. You're using a 1:10,000 topographic map to plan a hiking route..."
   - Cells: sonnet-4_L1425_error_prone

**9. [high] Ban premature praise; verify before affirming** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor said 'Perfect!' and 'Excellent! You've mastered...' on turns where student gave wrong or no answer.
   - Suggested edit: Add: 'Before affirming an answer, restate the student's numeric/verbal answer and compare it to the correct one. Never say Perfect/Excellent/Correct unless the student's last message contains a verifiable correct answer to the most recent tutor question.'
   - Expected effect: Prevents false-positive feedback and protects mastery signals.
   - Example evidence (57): "Excellent! You've completely mastered the scale conversion method."
   - Cells: sonnet-4_L1425_error_prone

**10. [high] Require targeted remediation on repeated unit-conversion errors** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student made the same cm→m omission twice (turns 50, 59); tutor only repeated the explanation.
   - Suggested edit: Add: 'If a student makes the same sub-skill error twice, drop the main task and run a 2-3 question mini-drill on the prerequisite (e.g., cm↔m, cm↔km) before returning to the original problem.'
   - Expected effect: Fixes the underlying bottleneck rather than recycling explanations.
   - Example evidence (51): "You're missing the conversion step from centimeters to meters."
   - Cells: sonnet-4_L1425_error_prone

_…10 additional recommendation(s) in `summary.md` and per-cell files._

### Engine / flow changes

**1. [high] Add a regeneration check for language and tool-syntax leaks** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 72 emitted Chinese and a raw tool call together; an output filter would catch both.
   - Expected effect: Prevents leaked tool calls and non-English text reaching the student.
   - Example evidence (72): "call:custom:pose_question{slot:2}  强化确认：是的"
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Prerequisite routing on consecutive arithmetic failures** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine should detect that the student's errors are in subtraction, not angle reasoning, and branch into a subtraction mini-module.
   - Expected effect: Targeted remediation on the true bottleneck.
   - Example evidence (84): "$260 - 90 = 170$. You've got the method down — just a small slip in the subtraction!"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Mastery gate before next-problem transition** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 84 transitions to a new diagram problem without the student ever producing the correct answer; gating logic should block this.
   - Expected effect: Enforces mastery learning at the orchestration layer.
   - Example evidence (84): "Let's try this one from the diagram."
   - Cells: gemini-3-flash_L1137_error_prone

**4. [high] Add arithmetic-error retry policy** — surfaced in 1 cell(s), severity score 3
   - Rationale: Multiple arithmetic slips by both student and tutor were not caught by any verification loop.
   - Expected effect: Catches numeric errors before they enter the conversation.
   - Example evidence (12): "140 plus 70 is 200."
   - Cells: sonnet-4_L1137_error_prone

**5. [high] Prerequisite-routing branch for arithmetic** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student's geometry reasoning is correct; arithmetic is the bottleneck. Engine should detect and route to a remedial micro-lesson.
   - Expected effect: Targets root cause and improves long-term mastery.
   - Example evidence (10): "i added 80, 60, and 70. that's 220."
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Add a regeneration sanity check** — surfaced in 1 cell(s), severity score 3
   - Rationale: Many turns are flagged regen_did_not_clean / tutor_incoherent, indicating the regeneration pipeline is committing broken outputs.
   - Expected effect: Catches duplicated/role-swapped output before it is sent.
   - Example evidence (60): "[flags: regen_did_not_clean,repeated_question,tutor_incoherent,numeric_mutation]"
   - Cells: sonnet-4_L1425_error_prone

**7. [high] Prerequisite routing on repeated sub-skill failure** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine should detect repeated cm→m omissions and route to a unit-conversion mini-lesson.
   - Expected effect: Operationalises targeted remediation.
   - Example evidence (50): "it's 30 meters."
   - Cells: sonnet-4_L1425_error_prone

**8. [medium] Mastery gate before advancing problems** — surfaced in 1 cell(s), severity score 2
   - Rationale: Engine advanced to a new problem without independent demonstration of mastery.
   - Expected effect: Enforces mastery-based progression.
   - Example evidence (13): "Let's try another one."
   - Cells: sonnet-4_L1137_error_prone

**9. [medium] Exit-ticket gating before topic advance** — surfaced in 1 cell(s), severity score 2
   - Rationale: Tutor moves from calculation to map types and back without confirming mastery.
   - Expected effect: Cleaner topic transitions tied to demonstrated mastery.
   - Example evidence (33): "Now let's apply this to map types."
   - Cells: sonnet-4_L1425_error_prone

### Student-experience changes

**1. [high] Soften contradictory feedback** — surfaced in 1 cell(s), severity score 3
   - Rationale: Saying 'Perfect working! You're absolutely right' then contradicting the student feels jarring for an error-prone student.
   - Expected effect: Protects student confidence and trust.
   - Example evidence (11): "Perfect working! You're absolutely right — 80° + 60° + 70° = 210°, and 360° - 210° = 150°."
   - Cells: sonnet-4_L1137_error_prone

**2. [medium] Warmer, persona-aware tone for error-prone students** — surfaced in 1 cell(s), severity score 2
   - Rationale: After three wrong tries the student gets a brisk 'Actually, …' rather than encouragement; an error-prone learner needs reassurance.
   - Expected effect: Maintains motivation for error-prone learners.
   - Example evidence (84): "Actually, $260 - 90 = 170$. You've got the method down — just a small slip in the subtraction!"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [medium] Include the referenced diagram inline** — surfaced in 1 cell(s), severity score 2
   - Rationale: Turn 84 says 'Let's try this one from the diagram' but no diagram is shown.
   - Expected effect: Avoids confusion from missing media.
   - Example evidence (84): "Let's try this one from the diagram."
   - Cells: gemini-3-flash_L1137_error_prone

**4. [medium] Calmer, calibrated tone for error-prone learners** — surfaced in 1 cell(s), severity score 2
   - Rationale: Excessive 'Excellent!/Perfect!' undermines trust when answers were wrong.
   - Expected effect: Restores credibility of feedback.
   - Example evidence (41): "Perfect work on both the concept and the math!"
   - Cells: sonnet-4_L1425_error_prone

**5. [medium] Acknowledge the student's own questions** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student often types their own MCQs; tutor ignores them, which is disorienting.
   - Expected effect: Improves coherence and student agency.
   - Example evidence (68): "which of these maps is a large-scale map?"
   - Cells: sonnet-4_L1425_error_prone

**6. [low] Acknowledge arithmetic struggle warmly** — surfaced in 1 cell(s), severity score 1
   - Rationale: Student is making repeated small arithmetic slips; tone could be more supportive and explicitly normalize the difficulty.
   - Expected effect: Reduces anxiety and encourages persistence.
   - Example evidence (13): "Just watch that arithmetic!"
   - Cells: sonnet-4_L1137_error_prone

## Cross-model robustness check (not a ranking)

Mean rubric scores per model — used only to decide whether a prompt change should be considered model-robust or model-specific. **Do not read this as a model evaluation.** A large gap here means the prompt change holds differently across providers; a small gap means it generalises.

| Model | Overall mean (0-5) | Cells scored |
|---|---:|---:|
| gemini-3-flash | 2.50 | 2 |
| sonnet-4 | 2.50 | 2 |

## Run statistics

- Cells completed: 4/4
- Cells errored: 0
- Total wall time: 1592s (26.5 min)
- Synthetic-student tokens (in/out): 124,356 / 1,763

## Programmatic failure-mode counts (aggregated)

Supplementary signal; the judge's recommendations remain the headline.

| Model | Answer leaks | Repeated Q | No question | Regen shipped dirty |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 0 | 14 | 0 | 14 |
| Gemini 3 Flash | 0 | 10 | 2 | 8 |

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
