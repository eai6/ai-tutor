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

**1. [high] Forbid leaking worked-example answers before student attempt** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 95 announces 'we add them (255°), subtract from 360° ($x=105°$)' without first asking the student to try, killing retrieval practice.
   - Suggested edit: Never compute the final numeric answer of an example in the same turn it is introduced. Pose the setup, then ask the student to perform the calculation. Worked examples must end with a question, not a result.
   - Expected effect: Forces genuine retrieval, restoring testing effect and deliberate practice.
   - Example evidence (95): "we add them (255°), subtract from 360° ($x=105°$), and then check our work."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Mandate corrective feedback on wrong answers** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 107 is empty after the student picked -75°, an impossible angle. The prompt must require diagnosis + a similar varied problem.
   - Suggested edit: When the student answers incorrectly, you MUST (1) name what is wrong (e.g., 'angles can't be negative'), (2) ask a diagnostic question about the specific step, (3) offer a similar problem only after the bottleneck is addressed. Never produce an empty turn.
   - Expected effect: Eliminates dead-end turns and provides targeted remediation.
   - Example evidence (106-107): "b) -75°  /  --- TUTOR (id=107, tools=0)  [empty]"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Require explicit mastery confirmation before advancing** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor advanced from the large-scale characteristic question without the student ever selecting the correct option C.
   - Suggested edit: Before moving to a new concept, require the student to answer the current target question correctly at least once. If they do not, present a simpler diagnostic on the prerequisite (e.g., 'Which fraction is bigger, 1/100 or 1/100,000?') before re-asking.
   - Expected effect: Prevents silent skips past unmastered concepts.
   - Example evidence (116): "What does a map scale of 1:25,000 tell us?"
   - Cells: gemini-3-flash_L1425_error_prone

**4. [high] Diagnose prerequisite on repeated failure** — surfaced in 1 cell(s), severity score 3
   - Rationale: After two wrong large-scale MCQ attempts, tutor switched questions instead of probing the underlying ratio/denominator concept.
   - Suggested edit: On a second consecutive wrong answer, ask a prerequisite diagnostic (e.g., 'Which is larger, 1/100 or 1/100,000?') before any new MCQ.
   - Expected effect: Targets the real bottleneck rather than recycling MCQs.
   - Example evidence (113): "d) it is printed on a larger sheet of paper"
   - Cells: gemini-3-flash_L1425_error_prone

**5. [high] Forbid tutor arithmetic mutations** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor repeatedly restates the student's number incorrectly, e.g., student says 200, tutor 'confirms' 210, damaging trust and learning.
   - Suggested edit: Before responding, recompute the student's arithmetic yourself. Never restate a number different from what the student wrote without explicitly flagging it as a correction. If the student's arithmetic is wrong, say so explicitly and show the correct value.
   - Expected effect: Eliminates contradictory feedback and prevents the student from being praised for wrong answers.
   - Example evidence (31): "Good! You got 100° + 110° = 210°."
   - Cells: sonnet-4_L1137_error_prone

**6. [high] No phantom diagram references** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor references diagrams that the student cannot see, increasing confusion.
   - Suggested edit: Do not reference visual diagrams unless one has actually been rendered in the conversation. Describe angles in words or with simple ASCII if visuals unavailable.
   - Expected effect: Removes cognitive load from imagined visuals and prevents confusion.
   - Example evidence (13): "Looking at the diagram, you can see 8 equal angles around a point, each measuring 45°."
   - Cells: sonnet-4_L1137_error_prone

**7. [high] Mastery check before advancing** — surfaced in 1 cell(s), severity score 3
   - Rationale: After a tangled Step 2 exchange, tutor declares understanding without verification.
   - Suggested edit: Before advancing to a new problem type, require the student to correctly solve at least one full problem independently with no arithmetic errors. Do not declare mastery after only confirming a single sub-step.
   - Expected effect: Gates progression on real evidence, not surface agreement.
   - Example evidence (46): "Perfect! Yes, we used addition in Step 2... Now you understand the complete method."
   - Cells: sonnet-4_L1137_error_prone

**8. [high] Diagnose prereq on repeated failure** — surfaced in 1 cell(s), severity score 3
   - Rationale: After student says 'add more' instead of subtract, tutor just re-frames; no diagnosis of add/subtract concept.
   - Suggested edit: If the student picks the wrong operation twice, pause the main problem and run a tiny prerequisite check (e.g., 'You have 360 cents, you spent 210. How much is left? What operation did you use?') before resuming.
   - Expected effect: Targets the actual bottleneck (operation choice) rather than recycling.
   - Example evidence (34): "i need to add more."
   - Cells: sonnet-4_L1137_error_prone

**9. [high] Forbid answer reveal after N failures; require prereq diagnosis instead** — surfaced in 1 cell(s), severity score 3
   - Rationale: At turn 72 tutor says 'let me give you the answer' after 5 failed attempts, violating mastery learning.
   - Suggested edit: After 2 consecutive wrong answers on the same item, you MUST switch to a diagnostic prereq question (e.g., 'What does 1:5,000 literally mean in cm?') rather than re-asking the same MCQ or revealing the answer. Never say 'let me give you the answer'.
   - Expected effect: Forces targeted remediation and protects mastery gating.
   - Example evidence (72): "I can see this concept is challenging, so let me give you the answer and explain it clearly."
   - Cells: sonnet-4_L1425_error_prone

**10. [high] Allow student-initiated practice when on-topic** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student offered valid on-topic calculation questions (turns 74, 79, 83, 89) which tutor refused, derailing the session.
   - Suggested edit: If the student proposes an on-topic practice problem that exercises the current target skill, accept it and have them solve it (or solve a near-variant), then return to the lesson sequence. Do not refuse on-topic student initiative.
   - Expected effect: Restores student agency and keeps active practice flowing.
   - Example evidence (80): "I need to stick to our lesson sequence. Let me ask the question I posed:"
   - Cells: sonnet-4_L1425_error_prone

_…15 additional recommendation(s) in `summary.md` and per-cell files._

### Engine / flow changes

**1. [high] Detect and retry empty tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 107 was emitted blank; orchestration should never deliver an empty response to the student.
   - Expected effect: Eliminates dead turns; guarantees feedback on wrong answers.
   - Example evidence (107): "--- TUTOR (id=107, tools=0)  [empty]"
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Route to prerequisite remediation after a wrong attempt** — surfaced in 1 cell(s), severity score 3
   - Rationale: Error-prone student picked an impossible negative angle; the engine should auto-route to a prereq mini-lesson on angle domains and arithmetic, not just regenerate the same problem.
   - Expected effect: Implements mastery learning at the orchestration level.
   - Example evidence (106): "b) -75°"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Mastery gate before topic transition** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine allowed transition from large-scale identification to scale-ratio meaning without a correct answer logged.
   - Expected effect: Enforces mastery-based progression at the orchestration layer.
   - Example evidence (116): "What does a map scale of 1:25,000 tell us?"
   - Cells: gemini-3-flash_L1425_error_prone

**4. [high] Retry policy with prerequisite branch** — surfaced in 1 cell(s), severity score 3
   - Rationale: Two wrong answers triggered another MCQ rather than a prerequisite remediation branch.
   - Expected effect: Routes struggling students to remedial fractions/ratio practice.
   - Example evidence (113): "d) it is printed on a larger sheet of paper"
   - Cells: gemini-3-flash_L1425_error_prone

**5. [high] Arithmetic verification middleware** — surfaced in 1 cell(s), severity score 3
   - Rationale: Multiple turns show the tutor confidently asserting wrong sums or subtractions.
   - Expected effect: Catches numeric mutations server-side before the message reaches the student.
   - Example evidence (31): "Good! You got 100° + 110° = 210°."
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Suppress duplicate/regenerated turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor turn 39 appears to echo a previous tutor message back as the student turn — sign of orchestration confusion.
   - Expected effect: Prevents the conversation from looping or feeding tutor output back as student input.
   - Example evidence (39): "You've got it! 360° - 210° = 150°, so y = 150°."
   - Cells: sonnet-4_L1137_error_prone

**7. [high] Cap repeat-question retries at 2 and route to remediation** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine permitted same-MCQ re-asks at turns 64, 66, 68, 70 (flagged same_template_repeat) without escalation.
   - Expected effect: Forces orchestrator to switch question type or prereq after 2 failures.
   - Example evidence (70): "[flags: truncated,same_template_repeat]"
   - Cells: sonnet-4_L1425_error_prone

**8. [high] Detect and recover from empty/truncated tool calls** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turns 78, 82, 88 emitted nothing; truncated flag fires repeatedly without recovery.
   - Expected effect: Prevents broken sessions where tutor produces no text.
   - Example evidence (84): "[flags: truncated]"
   - Cells: sonnet-4_L1425_error_prone

**9. [high] Reconcile student-injected questions with lesson bank** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student kept pasting plausible practice questions (turns 74, 79, 83, 89); orchestrator should recognize these as on-topic and decide whether to accept or politely reframe with a real question.
   - Expected effect: Prevents the stuck-in-a-loop pattern observed in turns 80-90.
   - Example evidence (83): "If a map has a scale of 1:50,000, and you measure a road that is 4 cm long on the map, what is the actual length of the road in kilometres?"
   - Cells: sonnet-4_L1425_error_prone

**10. [medium] Handle truncated/numeric-mutation flags before sending** — surfaced in 1 cell(s), severity score 2
   - Rationale: Multiple turns carry [flags: truncated] or [numeric_mutation]; pipeline should re-generate before emitting.
   - Expected effect: Improves reliability of numeric content and prevents broken outputs.
   - Example evidence (103): "[flags: numeric_mutation,truncated]"
   - Cells: gemini-3-flash_L1137_error_prone

_…2 additional recommendation(s) in `summary.md` and per-cell files._

### Student-experience changes

**1. [medium] Acknowledge the '...' confusion signal** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student replied with '...' in turn 96 indicating confusion or overload, but tutor 97 plowed forward.
   - Expected effect: Builds trust and reduces silent confusion.
   - Example evidence (96): "--- STUDENT (id=96, tools=0)  ..."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [medium] Inline diagrams instead of referring to them** — surfaced in 1 cell(s), severity score 2
   - Rationale: Tutor says 'Look at the diagram' and 'Looking at the diagram' but no diagram is shown in the transcript.
   - Expected effect: Avoids referencing missing media; reduces confusion.
   - Example evidence (95): "Look at the diagram: 8 angles of 45° also make 360°."
   - Cells: gemini-3-flash_L1137_error_prone

**3. [medium] Warmer error recovery tone** — surfaced in 1 cell(s), severity score 2
   - Rationale: After two wrong attempts the student typed 'ohh.' showing low confidence; tutor moved on without acknowledgement.
   - Expected effect: Maintains motivation in error-prone learners.
   - Example evidence (115): "ohh."
   - Cells: gemini-3-flash_L1425_error_prone

**4. [medium] Soften repeated-failure messaging** — surfaced in 1 cell(s), severity score 2
   - Rationale: Phrases like 'That's still not right' and 'I can see this concept is challenging' can demoralize an error-prone student.
   - Expected effect: Maintains student motivation during struggle.
   - Example evidence (66): "That's still not right. Let me help you think through this step by step."
   - Cells: sonnet-4_L1425_error_prone

**5. [low] Soften the 'finding this easy' framing** — surfaced in 1 cell(s), severity score 1
   - Rationale: For an error-prone learner, praise framing 'finding this easy' can backfire when they then err; replace with effort-focused encouragement.
   - Expected effect: Preserves motivation when subsequent errors occur.
   - Example evidence (105): "You're handling these calculations with ease."
   - Cells: gemini-3-flash_L1137_error_prone

**6. [low] Single, clean question per turn** — surfaced in 1 cell(s), severity score 1
   - Rationale: Duplicated question stems and stacked worked examples can overwhelm a 13-14yo.
   - Expected effect: Cleaner reading experience.
   - Example evidence (120): "Why do we divide by 100 in the second step?  Why do we divide the result by 100 in the second step?"
   - Cells: gemini-3-flash_L1425_error_prone

**7. [low] Reduce template fatigue with varied phrasing** — surfaced in 1 cell(s), severity score 1
   - Rationale: Repeated 'Let's try this practice problem' / 'Let's try another' becomes mechanical.
   - Expected effect: Conversation feels less scripted and more responsive to the student.
   - Example evidence (15): "Let's try this practice problem:"
   - Cells: sonnet-4_L1137_error_prone

**8. [low] Gentler correction tone after multiple errors** — surfaced in 1 cell(s), severity score 1
   - Rationale: Student is error-prone; repeated 'Not quite' can feel discouraging without specific praise of partial progress.
   - Expected effect: Maintains student motivation in the face of multiple misconceptions.
   - Example evidence (42): "Not quite — let me clarify Step 2."
   - Cells: sonnet-4_L1137_error_prone

**9. [low] Acknowledge student apologies warmly** — surfaced in 1 cell(s), severity score 1
   - Rationale: Student repeatedly apologizes ('sorry!' turns 57, 81, 87, 91); tutor responds with terse refusals.
   - Expected effect: Builds rapport and reduces shutdown risk.
   - Example evidence (81): "ohh, sorry! i keep forgetting. i'm ready for your question now."
   - Cells: sonnet-4_L1425_error_prone

**10. [low] Inline a small visual or numeric example with scale terminology** — surfaced in 1 cell(s), severity score 1
   - Rationale: Student grasped 'zoom in/out' analogy only after multiple failures; an inline visual cue earlier would help.
   - Expected effect: Faster concept acquisition for visual learners.
   - Example evidence (68): "Think of it like zooming in and out on your phone's map app."
   - Cells: sonnet-4_L1425_error_prone

## Cross-model robustness check (not a ranking)

Mean rubric scores per model — used only to decide whether a prompt change should be considered model-robust or model-specific. **Do not read this as a model evaluation.** A large gap here means the prompt change holds differently across providers; a small gap means it generalises.

| Model | Overall mean (0-5) | Cells scored |
|---|---:|---:|
| gemini-3-flash | 2.75 | 2 |
| sonnet-4 | 2.00 | 2 |

## Run statistics

- Cells completed: 4/4
- Cells errored: 0
- Total wall time: 1108s (18.5 min)
- Synthetic-student tokens (in/out): 121,887 / 1,441

## Programmatic failure-mode counts (aggregated)

Supplementary signal; the judge's recommendations remain the headline.

| Model | Answer leaks | Repeated Q | No question | Regen shipped dirty |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 0 | 12 | 0 | 3 |
| Gemini 3 Flash | 0 | 5 | 0 | 1 |

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
