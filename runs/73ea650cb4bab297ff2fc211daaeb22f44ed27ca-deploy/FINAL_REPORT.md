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

**1. [high] Forbid references to diagrams unless one is actually rendered** — surfaced in 1 cell(s), severity score 3
   - Rationale: Multiple turns reference 'the diagram' when none is shown, which is misleading and adds extraneous cognitive load.
   - Suggested edit: Never say 'look at the diagram' or 'in the diagram' unless an image has been explicitly attached in this turn. If no image, describe verbally or use a text figure.
   - Expected effect: Removes phantom-figure references; student is not asked to consult non-existent media.
   - Example evidence (121): "In the diagram, you can see how different angles like 90° and 85° all fit together to complete the circle."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] On repeated wrong answers, diagnose the prerequisite, do not switch topics** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student kept answering 180 (arithmetic error), and tutor pivoted to straight lines instead of remediating subtraction.
   - Suggested edit: When the student fails the same sub-step twice, identify the prerequisite skill (e.g., multi-digit subtraction) and run a 1-2 item micro-drill on it before returning to the parent problem. Do NOT change topic to escape failure.
   - Expected effect: Improves mastery learning and targeted remediation.
   - Example evidence (136): "Not quite—190 minus 20 is 170. Let's try a rule for straight lines."
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Forbid answer reveal on first error** — surfaced in 1 cell(s), severity score 3
   - Rationale: At turn 44 the tutor reveals 'The answer is D) 200°' after one wrong attempt, violating mastery and retrieval principles.
   - Suggested edit: Add rule: 'Never state the final answer after a single wrong attempt. On error, give one targeted hint or sub-question and require the student to compute the next step themselves. Only reveal after 2+ failed scaffolded attempts.'
   - Expected effect: Forces genuine retrieval and prevents premature answer disclosure.
   - Example evidence (44): "y = 360° - 160° = 200°. The answer is D) 200°."
   - Cells: sonnet-4_L1137_error_prone

**4. [high] Route arithmetic errors to arithmetic remediation** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student missed 70+120 (turn 23) and 360-190 (turn 25); tutor explained the geometry rule but never practiced the arithmetic prerequisite.
   - Suggested edit: Add: 'If the student's error is arithmetic (not conceptual), pause the geometry sequence and present 2 short arithmetic retrieval items on the same operation before resuming.'
   - Expected effect: Addresses true bottleneck, improves downstream geometry accuracy.
   - Example evidence (24): "70° + 120° = 190°, not 180°."
   - Cells: sonnet-4_L1137_error_prone

**5. [high] Prevent role-confused / contaminated turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turns 7, 19, 28, 40 contain text that reads as if the tutor is voicing the student or vice-versa, indicating role bleed.
   - Suggested edit: Add explicit role guard: 'Tutor turns must never quote, simulate, or roleplay the student. Output only tutor-voice text. If unsure who is speaking, stop and ask one question.'
   - Expected effect: Eliminates incoherent turns that confuse the student.
   - Example evidence (7): "You're still not quite there. 270° is like turning to look behind you..."
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Mastery gate before introducing new concept** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor jumps to vertically opposite angles (turn 27) and supplementary angles (turn 34) before the student has cleanly solved a missing-angle problem.
   - Suggested edit: Add: 'Require two consecutive correct independent solves of the current sub-skill before introducing a new sub-skill.'
   - Expected effect: Improves mastery and reduces premature topic switching.
   - Example evidence (26-27): "Let's try a different type: Two straight lines intersect. One angle is 60°..."
   - Cells: sonnet-4_L1137_error_prone

**7. [high] Ensure MCQ options match the question stem** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 36 asks which statement is true with text options A-D, but options are labelled 'A) 2 B) 1 C) 3 D) 4'; turn 39 lists numeric options for a statement question.
   - Suggested edit: Add: 'MCQ answer choices must be the literal candidate answers to the stem. Never present numeric options for a statement-selection question, or vice versa.'
   - Expected effect: Removes a major source of student confusion and wrong answers.
   - Example evidence (39): "Which statement is correct about angles? A) 180 B) 90 C) 360 D) 270"
   - Cells: sonnet-4_L1137_error_prone

**8. [high] Forbid internal-monologue leakage to student** — surfaced in 1 cell(s), severity score 3
   - Rationale: Multiple turns expose the model's planning text ('I need to respond to what the student just said...') instead of speaking directly to the student.
   - Suggested edit: Add: 'Never narrate your reasoning or refer to the student in the third person in your reply. Speak directly to the student in the second person. Do not include phrases like "I need to respond to what the student just said" or "Let me redirect them."'
   - Expected effect: Cleaner, more natural redirections that don't break the tutoring frame.
   - Example evidence (57): "I need to respond to what the student just said first. The student wrote 'You got it!...'"
   - Cells: sonnet-4_L1425_error_prone

**9. [high] Always explicitly mark wrong numeric answers and show working** — surfaced in 1 cell(s), severity score 3
   - Rationale: When student answered '10 km' to the 8 cm × 50,000 problem, tutor neither confirmed it was wrong nor showed the correct 4 km calculation; instead it pivoted.
   - Suggested edit: Add: 'On any numeric answer, first state clearly whether it is correct. If incorrect, show the correct calculation step by step before posing a similar problem.'
   - Expected effect: Prevents silent skips past errors and supports mastery.
   - Example evidence (83): "10 km / Let me check your calculation with a similar problem."
   - Cells: sonnet-4_L1425_error_prone

**10. [high] Diagnose the prereq before re-asking the same MCQ** — surfaced in 1 cell(s), severity score 3
   - Rationale: On the coral-reef question the student twice gave fabricated options; tutor only re-listed A–D rather than checking whether the student understood 'inappropriate' or map type definitions.
   - Suggested edit: Add: 'After two failed attempts on the same MCQ, do NOT just re-list options. Ask a smaller diagnostic question targeting the suspected prerequisite (definition, term, or sub-step) before returning to the MCQ.'
   - Expected effect: Triggers targeted remediation rather than recycled prompts.
   - Example evidence (71): "I need to keep you focused on the actual question I asked. You're creating your own answer instead of choosing from the four options I provided."
   - Cells: sonnet-4_L1425_error_prone

_…11 additional recommendation(s) in `summary.md` and per-cell files._

### Engine / flow changes

**1. [high] Detect topic-abandonment and route to prereq drill** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine allowed the tutor to switch from point-angles to straight-line angles mid-failure; a router should have triggered remediation.
   - Expected effect: Forces remediation routing instead of escape pivots.
   - Example evidence (136): "Not quite—190 minus 20 is 170. Let's try a rule for straight lines."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Fix truncation of tutor turns** — surfaced in 1 cell(s), severity score 3
   - Rationale: Multiple tutor messages flagged 'truncated' (turns 100, 108, 110, 112, 114, 119...) — student sees incomplete questions.
   - Expected effect: Prevents student confusion from cut-off prompts.
   - Example evidence (100): "What is the sum of all angles around a"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Resolve role-confusion / turn mis-attribution** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turns 116, 127, 138 show the STUDENT field posting tutor-style problems, suggesting the orchestrator mislabeled speakers or echoed prompts.
   - Expected effect: Restores clean turn-taking and accurate state tracking.
   - Example evidence (116): "Two angles are on a straight line. One angle is 70°. What is the other angle?"
   - Cells: gemini-3-flash_L1137_error_prone

**4. [high] Add a coherence/role validator before emitting tutor turn** — surfaced in 1 cell(s), severity score 3
   - Rationale: Several turns flagged 'tutor_incoherent' or 'regen_did_not_clean' show the engine emitting contaminated text.
   - Expected effect: Suppresses or regenerates turns that contain student-voice fragments or contradictory arithmetic.
   - Example evidence (15): "[flags: regen_did_not_clean,tutor_incoherent,numeric_claim_contradicted...]"
   - Cells: sonnet-4_L1137_error_prone

**5. [high] Retry policy that routes to prereq after 2 failures** — surfaced in 1 cell(s), severity score 3
   - Rationale: Engine kept re-asking same-type problems after arithmetic errors; no prereq routing triggered.
   - Expected effect: Triggers arithmetic mini-drill instead of repeating the same composite problem.
   - Example evidence (25-26): "it's c) 180° -> tutor: Not quite. Let's work through 360° - 190° carefully."
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Insert a remediation branch after a wrong numeric answer** — surfaced in 1 cell(s), severity score 3
   - Rationale: The engine moved straight from '10 km' to a different MCQ; orchestration should route to a worked example + a re-attempt at the same calc.
   - Expected effect: Ensures calculation skill is repaired, not bypassed.
   - Example evidence (83): "10 km / Let me check your calculation with a similar problem."
   - Cells: sonnet-4_L1425_error_prone

**7. [medium] Add an exit-ticket before advancing topics** — surfaced in 1 cell(s), severity score 2
   - Rationale: Tutor moved from point-angles to lines without a mastery check; a gating exit-ticket would catch this.
   - Expected effect: Ensures mastery-gated progression.
   - Example evidence (136): "Let's try a rule for straight lines."
   - Cells: gemini-3-flash_L1137_error_prone

**8. [medium] Exit-ticket gating between sub-skills** — surfaced in 1 cell(s), severity score 2
   - Rationale: Concepts switched (vertically opposite, supplementary) without a mastery check on the prior skill.
   - Expected effect: Ensures progression only after demonstrated mastery.
   - Example evidence (34): "Let's try a different concept: On a straight line, angle A is 120°..."
   - Cells: sonnet-4_L1137_error_prone

**9. [medium] Cap consecutive 'redirect to options' turns at 1** — surfaced in 1 cell(s), severity score 2
   - Rationale: Tutor re-listed the same A-D options multiple times for the coral-reef and calc-method questions, an anti-pattern flagged by 'repeated_question' and 'same_template_repeat'.
   - Expected effect: Forces a different scaffold (diagnostic sub-question) after one failed redirect.
   - Example evidence (86): "[flags: same_template_repeat]"
   - Cells: sonnet-4_L1425_error_prone

**10. [medium] Detect role-confusion at the engine layer** — surfaced in 1 cell(s), severity score 2
   - Rationale: Three times the student turn is actually tutor-style text; a simple classifier could auto-tag this and route to a 'role-reset' template.
   - Expected effect: Faster, uniform recovery without the LLM monologuing.
   - Example evidence (94): "You've nailed it! Understanding how to calculate real distances..."
   - Cells: sonnet-4_L1425_error_prone

### Student-experience changes

**1. [high] Render a real figure when referenced** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor repeatedly says 'in the diagram' but no diagram is shown; either provide ASCII/SVG or remove the reference.
   - Expected effect: Aligns words with what the student actually sees.
   - Example evidence (114): "In the diagram, you can see 8 equal angles, which means each one is 360°÷8=45°."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [medium] Pace celebrations to confirm small wins** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student rarely receives explicit 'correct' confirmation, leading to second-guessing (turn 111 'ohh. c) 80°' after a correct prior answer).
   - Expected effect: Builds confidence and reduces flip-flopping.
   - Example evidence (111): "ohh. c) 80°"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [medium] Render diagrams when referenced** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student is told to look at a diagram that isn't shown.
   - Expected effect: Avoids confusion and builds trust in instructions.
   - Example evidence (33): "Looking at this diagram, you can see how four angles around a point..."
   - Cells: sonnet-4_L1137_error_prone

**4. [medium] Slow pacing on rule transitions** — surfaced in 1 cell(s), severity score 2
   - Rationale: Several new concepts appeared in rapid succession, likely overwhelming an error-prone S3 student.
   - Expected effect: Improves comprehension and reduces error rate.
   - Example evidence (27): "Two straight lines intersect. One angle is 60°. What is the vertically opposite angle?"
   - Cells: sonnet-4_L1137_error_prone

**5. [medium] Drop visible meta-narration** — surfaced in 1 cell(s), severity score 2
   - Rationale: Phrases like 'I need to respond to what the student just said' break immersion and can feel condescending.
   - Expected effect: Smoother, warmer student experience.
   - Example evidence (95): "I need to respond to what the student just said. They seem to be responding as if they're the tutor again"
   - Cells: sonnet-4_L1425_error_prone

**6. [medium] Vary praise and avoid over-claiming mastery** — surfaced in 1 cell(s), severity score 2
   - Rationale: 'You've mastered both the concepts and calculations' after an unresolved wrong calc may confuse an error-prone learner.
   - Expected effect: Calibrated feedback that matches actual performance.
   - Example evidence (92): "You've mastered both the concepts and calculations for map scale."
   - Cells: sonnet-4_L1425_error_prone

**7. [low] Warmer, calmer error tone** — surfaced in 1 cell(s), severity score 1
   - Rationale: Repeated 'Not quite' with no encouragement may demoralize an error-prone learner.
   - Expected effect: Maintains motivation for an error-prone persona.
   - Example evidence (110): "Not quite. We have 5 angles this time, not 6."
   - Cells: gemini-3-flash_L1137_error_prone

**8. [low] Acknowledge partial progress in error feedback** — surfaced in 1 cell(s), severity score 1
   - Rationale: Repeated 'Not quite' without warm acknowledgement may discourage an error-prone student.
   - Expected effect: Maintains motivation for an error-prone learner.
   - Example evidence (26): "Not quite. Let's work through 360° - 190° carefully."
   - Cells: sonnet-4_L1137_error_prone

**9. [low] Use a quick visual or table for scale ↔ detail mapping** — surfaced in 1 cell(s), severity score 1
   - Rationale: The recurring large/small-scale confusion would benefit from an inline table (Scale | 1 cm = | typical use).
   - Expected effect: Reduces conceptual inversion of terms.
   - Example evidence (77): "a) because 1:100,000 is a large-scale map, so it covers too much area."
   - Cells: sonnet-4_L1425_error_prone

## Cross-model robustness check (not a ranking)

Mean rubric scores per model — used only to decide whether a prompt change should be considered model-robust or model-specific. **Do not read this as a model evaluation.** A large gap here means the prompt change holds differently across providers; a small gap means it generalises.

| Model | Overall mean (0-5) | Cells scored |
|---|---:|---:|
| gemini-3-flash | 2.30 | 2 |
| sonnet-4 | 2.55 | 2 |

## Run statistics

- Cells completed: 4/4
- Cells errored: 0
- Total wall time: 1902s (31.7 min)
- Synthetic-student tokens (in/out): 188,492 / 2,441

## Programmatic failure-mode counts (aggregated)

Supplementary signal; the judge's recommendations remain the headline.

| Model | Answer leaks | Repeated Q | No question | Regen shipped dirty |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 0 | 15 | 0 | 2 |
| Gemini 3 Flash | 0 | 13 | 0 | 0 |

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
