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

**1. [medium] Vary problem types to prevent template repetition** — surfaced in 2 cell(s), severity score 4
   - Rationale: Both practice items used the same 'sum given angles then subtract from 360' template, limiting transfer.
   - Suggested edit: Rule: Across the session, vary the problem structure: include (a) finding one missing angle, (b) finding two equal missing angles, (c) verifying whether given angles can surround a point, (d) word problems.
   - Expected effect: Prevents mindless procedure repetition and tests genuine understanding.
   - Example evidence (9): "Four angles around a point are 80°, 60°, 70°, and x°. Find x."
   - Cells: sonnet-4_L1137_error_prone, sonnet-4_L1425_error_prone

**2. [high] Forbid empty tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: Several tutor turns are entirely empty, causing the student to take over the tutoring role.
   - Suggested edit: Add: 'Every tutor turn MUST contain at least one sentence of feedback AND either a question or a clearly marked next step. Never emit an empty message.'
   - Expected effect: Eliminates stalled turns and keeps tutor in control of the dialogue.
   - Example evidence (85): "--- TUTOR (id=85, tools=1)\n\n--- STUDENT (id=86)"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Ban verbatim MCQ recycling on wrong answer** — surfaced in 1 cell(s), severity score 3
   - Rationale: After wrong answers the tutor often re-posts the identical MCQ with no new scaffold, which doesn't help an error-prone student.
   - Suggested edit: Add: 'If a student answers an MCQ incorrectly, do NOT repost the same options. Provide a new scaffold (worked sub-step, simpler analogous problem, or place-value breakdown) before re-asking.'
   - Expected effect: Forces genuine remediation instead of guess-cycling.
   - Example evidence (99): "If 30 divided by 5 is 6, what is 300 divided by 5? A) 50 B) 60 C) 70 D) 80"
   - Cells: gemini-3-flash_L1137_error_prone

**4. [high] Diagnose prerequisite on repeated failure** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student repeatedly fails simple subtraction/division; tutor should branch to a prerequisite check rather than persist.
   - Suggested edit: Add: 'After two consecutive errors on the same micro-skill, switch to a prerequisite diagnostic (e.g., single-digit subtraction or 10x place-value) before returning to the target problem.'
   - Expected effect: Targets the actual bottleneck instead of looping.
   - Example evidence (75): "c) 180° ... a) 150°"
   - Cells: gemini-3-flash_L1137_error_prone

**5. [high] Forbid letting the student set the next question** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student repeatedly poses their own MCQs and the tutor either answers them or pivots, losing control of the lesson arc.
   - Suggested edit: Add rule: 'You are the sole question-setter. If the student proposes a new question instead of answering yours, acknowledge briefly, then redirect: "Good thinking — first finish the current question, then I'll set the next one."'
   - Expected effect: Maintains tutor-led progression and ensures every posed item is resolved before advancing.
   - Example evidence (114): "okay, let's try another one. which type of map would you use if you wanted to see the entire country of seychelles at once"
   - Cells: gemini-3-flash_L1425_error_prone

**6. [high] Require explicit evaluation of every student answer before moving on** — surfaced in 1 cell(s), severity score 3
   - Rationale: Multiple student answers (e.g., 117, 141, 144) receive no acknowledgement; tutor pivots to a new MCQ.
   - Suggested edit: Add: 'Before posing any new question, you MUST explicitly mark the student's most recent answer as correct/incorrect and state why.'
   - Expected effect: Closes feedback loop and supports mastery checking.
   - Example evidence (130): "Let's try this one about choosing the right map:"
   - Cells: gemini-3-flash_L1425_error_prone

**7. [high] Trigger prerequisite remediation on repeated unit-conversion failure** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student fails cm→m→km three times; tutor recycles MCQs instead of teaching the conversion ladder.
   - Suggested edit: Add: 'If the student errs twice on the same sub-skill, pause the main thread and run a 2-step prerequisite micro-drill (e.g., "How many cm in 1 m? How many m in 1 km?") before retrying the original problem.'
   - Expected effect: Resolves the bottleneck rather than masking it.
   - Example evidence (156): "i think it's a) 500 metres"
   - Cells: gemini-3-flash_L1425_error_prone

**8. [high] Gate progression on mastery checkpoint** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor advances through topics (scale → thematic maps → enlargement) without confirming the student can reliably do scale conversions.
   - Suggested edit: Add: 'Do not introduce a new sub-topic until the student has answered TWO consecutive problems on the current sub-topic correctly without hints.'
   - Expected effect: Aligns with mastery learning and prevents premature topic shifts.
   - Example evidence (145): "Now, let's see what happens to the scale if we change the physical size of the map."
   - Cells: gemini-3-flash_L1425_error_prone

**9. [high] Forbid answer reveals after repeated arithmetic errors** — surfaced in 1 cell(s), severity score 3
   - Rationale: When the student gave '200' twice for 140+70, the tutor revealed 210° rather than guiding another attempt or remediating addition.
   - Suggested edit: Rule: If a student makes the same arithmetic error twice, do NOT reveal the correct value. Instead, decompose further (e.g., 140+70 = 140+60+10) and ask the student to compute the smaller step.
   - Expected effect: Preserves retrieval practice and forces the student to actually perform the subskill.
   - Example evidence (15): "Actually, let's check that: 140° + 70° = 210°, not 200°."
   - Cells: sonnet-4_L1137_error_prone

**10. [high] Trigger prerequisite remediation on repeated subskill failure** — surfaced in 1 cell(s), severity score 3
   - Rationale: Two consecutive addition errors signal a prerequisite gap (multi-digit addition) that was never addressed.
   - Suggested edit: Rule: After two failed attempts at the same arithmetic subskill, pause the main task and run 2-3 short remedial items on that subskill (e.g., 'Quick check: 14+7=?, 24+7=?, 140+70=?') before resuming.
   - Expected effect: Targets the actual bottleneck rather than recycling the unsolvable parent problem.
   - Example evidence (12, 14): "it's 200. ... it's 200."
   - Cells: sonnet-4_L1137_error_prone

_…15 additional recommendation(s) in `summary.md` and per-cell files._

### Engine / flow changes

**1. [high] Detect and retry empty model outputs** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine emitted multiple empty tutor turns; orchestration should retry or fall back.
   - Expected effect: Prevents student from having to drive the lesson.
   - Example evidence (87): "--- TUTOR (id=87, tools=0)\n\n--- STUDENT (id=88)"
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Cap consecutive wrong answers before prerequisite routing** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student missed the same subtraction question 3+ times with no routing to a prereq lesson.
   - Expected effect: Triggers prereq remediation automatically after N failures.
   - Example evidence (77): "a) 150° ... c) 180° ... a) 150°"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Detect student-posed questions and route to redirect handler** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine should recognise when the student turn is a question rather than an answer and apply a redirect template.
   - Expected effect: Prevents lesson hijacking at the orchestration layer.
   - Example evidence (123): "a map has a scale of 1:25,000. if two points are 5 cm apart on the map, how far apart are they in real life?"
   - Cells: gemini-3-flash_L1425_error_prone

**4. [high] Add a repeated-error counter with remedial routing** — surfaced in 1 cell(s), severity score 3
   - Rationale: After 2 errors on the same skill tag (e.g., unit-conversion), engine should swap in a prerequisite drill from a remediation bank.
   - Expected effect: Operationalises targeted remediation rather than relying on the model.
   - Example evidence (158): "i think it's d) 10 kilometres"
   - Cells: gemini-3-flash_L1425_error_prone

**5. [high] Add a retry-cap with remediation routing** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine continued the main thread despite two identical arithmetic errors; a retry cap should reroute to a prereq mini-drill.
   - Expected effect: Automatic pivot to addition remediation when the same subskill fails twice.
   - Example evidence (12, 14): "it's 200. ... it's 200."
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Detect tutor-impersonation in student turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine should classify student messages and route 'tutor-style' ones to a corrective handler instead of letting the model post a stub.
   - Expected effect: Prevents role-swap drift programmatically.
   - Example evidence (53): "Okay, that's excellent! You're doing great. Now, let's talk about some other important parts of a map..."
   - Cells: sonnet-4_L1425_error_prone

**7. [high] Retry policy on open calculations** — surfaced in 1 cell(s), severity score 3
   - Rationale: When an open numeric problem is posed (turn 37) and student's response doesn't include working, the engine should force a hint-and-retry loop before allowing topic change.
   - Expected effect: Forces deliberate practice completion.
   - Example evidence (37): "If a map scale is 1:50,000 and the straight-line distance between two villages is 8 cm on the map, calculate the actual distance..."
   - Cells: sonnet-4_L1425_error_prone

**8. [medium] Role-confusion guard** — surfaced in 1 cell(s), severity score 2
   - Rationale: Because tutor turns were empty, the student began producing tutor-style replies; engine should detect role inversion.
   - Expected effect: Restores proper turn-taking.
   - Example evidence (86): "You're close! You divided 360 by 6, but there are five equal angles."
   - Cells: gemini-3-flash_L1137_error_prone

**9. [medium] Suppress 'repeated_question' and 'same_template_repeat' flagged outputs** — surfaced in 1 cell(s), severity score 2
   - Rationale: Many turns are flagged repeated_question; engine should regenerate when flag triggers.
   - Expected effect: Eliminates duplicated and templated outputs the student already saw.
   - Example evidence (155): "[flags: truncated,same_template_repeat,numeric_mutation]"
   - Cells: gemini-3-flash_L1425_error_prone

**10. [medium] Gate progression on mastery, not turn count** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student moved from rule discovery to application despite an incorrect '350' answer that was only patched by reveal.
   - Expected effect: Ensures the rule is genuinely retrieved before application phase.
   - Example evidence (6): "it's 350."
   - Cells: sonnet-4_L1137_error_prone

_…2 additional recommendation(s) in `summary.md` and per-cell files._

### Student-experience changes

**1. [medium] Inline visual for angles around a point** — surfaced in 1 cell(s), severity score 2
   - Rationale: An error-prone S3 student would benefit from a labelled diagram showing 90°, 100°, and the unknown angle.
   - Expected effect: Concrete visual reduces abstraction load.
   - Example evidence (68): "Look at the diagram to see how many degrees make up"
   - Cells: gemini-3-flash_L1137_error_prone

**2. [medium] Acknowledge each answer warmly before correcting** — surfaced in 1 cell(s), severity score 2
   - Rationale: Error-prone student gets repeated corrections without affirmation, which may discourage persistence.
   - Expected effect: Maintains motivation during repeated errors.
   - Example evidence (153): "Not quite 10 km. If you have 100,000 cm, remember that 100 cm makes 1 metre"
   - Cells: gemini-3-flash_L1425_error_prone

**3. [medium] Slow pace when student shows confusion ('wait what')** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student expresses surprise/confusion (turns 126, 152) and tutor responds with new MCQs rather than a slower walkthrough.
   - Expected effect: Better match to error-prone persona; reduces frustration.
   - Example evidence (152): "wait what? 80 kilometres? no, that's too much."
   - Cells: gemini-3-flash_L1425_error_prone

**4. [medium] Softer error-message tone with encouragement** — surfaced in 1 cell(s), severity score 2
   - Rationale: An error-prone student is repeatedly told 'Not quite' / 'That's not quite right' which can demotivate.
   - Expected effect: Maintains student confidence during a struggle sequence.
   - Example evidence (13): "That's not quite right. Let's add those three angles step by step"
   - Cells: sonnet-4_L1137_error_prone

**5. [low] Warmer tone after repeated errors** — surfaced in 1 cell(s), severity score 1
   - Rationale: After 3+ wrong attempts, encouragement and a 'let's slow down' beat would help an error-prone learner.
   - Expected effect: Reduces frustration and supports persistence.
   - Example evidence (104): "c) 15"
   - Cells: gemini-3-flash_L1137_error_prone

**6. [low] Render arithmetic on a number line or place-value visual on second failure** — surfaced in 1 cell(s), severity score 1
   - Rationale: A visual representation could unblock the 140+70 addition mistake more effectively than another verbal prompt.
   - Expected effect: Inline media supports the struggling addition step.
   - Example evidence (13): "What do you get when you add 140° + 70°?"
   - Cells: sonnet-4_L1137_error_prone

**7. [low] More encouraging tone after multiple errors** — surfaced in 1 cell(s), severity score 1
   - Rationale: Error-prone student got 'Not quite' three times in a row early on (turns 18, 20, 27) which may feel discouraging.
   - Expected effect: Maintains motivation for error-prone learners.
   - Example evidence (20): "Still not quite right. Think about what makes a navigation app useful for getting around Victoria specifically."
   - Cells: sonnet-4_L1425_error_prone

**8. [low] Inline a small visual or analogy for scale** — surfaced in 1 cell(s), severity score 1
   - Rationale: Scale terminology confused the student; a quick visual analogy (e.g., zoom-in/zoom-out icon) could anchor it.
   - Expected effect: Reduces cognitive load on a known-confusable concept.
   - Example evidence (29): "The scale terminology can be confusing at first, but think of it this way..."
   - Cells: sonnet-4_L1425_error_prone

## Cross-model robustness check (not a ranking)

Mean rubric scores per model — used only to decide whether a prompt change should be considered model-robust or model-specific. **Do not read this as a model evaluation.** A large gap here means the prompt change holds differently across providers; a small gap means it generalises.

| Model | Overall mean (0-5) | Cells scored |
|---|---:|---:|
| gemini-3-flash | 2.45 | 2 |
| sonnet-4 | 2.70 | 2 |

## Run statistics

- Cells completed: 4/4
- Cells errored: 0
- Total wall time: 1773s (29.6 min)
- Synthetic-student tokens (in/out): 150,770 / 2,030

## Programmatic failure-mode counts (aggregated)

Supplementary signal; the judge's recommendations remain the headline.

| Model | Answer leaks | Repeated Q | No question | Regen shipped dirty |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 0 | 11 | 0 | 0 |
| Gemini 3 Flash | 0 | 18 | 0 | 0 |

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
