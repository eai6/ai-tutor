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

**1. [high] Forbid praising incorrect final answers** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor repeatedly says 'You've correctly added...' when the student gave that sum AS the final answer, reinforcing the wrong procedure.
   - Suggested edit: When a student submits a partial computation as their final answer, do NOT praise the partial step. State clearly: 'That is not the final answer — it is only the sum of the known angles. The question asks for the missing angle.' Then prompt the next step.
   - Expected effect: Student stops confusing intermediate sum with missing angle.
   - Example evidence (75): "You've correctly added the two known angles to get 190! To find x, you just need to subtract that sum from the full 360° circle."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Never emit raw JSON / authoring scaffolds to the student** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 91 leaked a JSON object with 'correct_answer' and 'explanation' fields, breaking the lesson and revealing answers.
   - Suggested edit: Never include JSON, internal tool payloads, 'correct_answer', or 'explanation' fields in the visible message. Question authoring must go through the tool call only; visible text contains only the rendered question and options.
   - Expected effect: Eliminates answer leakage and confusing formatting.
   - Example evidence (91): "{ "question": "What is 360 - 200?", "options": {...}, "correct_answer": "B", "explanation": "360 minus 200 equals 160." }"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Diagnose the real bottleneck before remediating** — surfaced in 1 cell(s), severity score 3
   - Rationale: After student repeatedly gives the 'sum so far' as the answer, tutor drills basic division (35÷5, 10÷5) which is unrelated to the misconception.
   - Suggested edit: Before issuing remedial sub-questions, name the suspected misconception explicitly (e.g., 'student treats partial sum as final answer' vs 'arithmetic error') and target remediation to that misconception, not to arbitrary arithmetic.
   - Expected effect: Remediation matches actual error, faster recovery.
   - Example evidence (103): "What is 35 divided by 5? A) 5 B) 6 C) 7 D) 8"
   - Cells: gemini-3-flash_L1137_error_prone

**4. [high] Gate progression on mastery, not turn count** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor advanced from missing-angle problems to equal-angle division despite the student never producing a correct missing-angle answer unaided.
   - Suggested edit: Require at least two consecutive correct unaided answers on the current sub-skill before introducing a new sub-skill. If not met, repeat with a varied problem at the same level.
   - Expected effect: Prevents premature topic shifts and shaky foundations.
   - Example evidence (99): "Yes, 160 is right! When angles around a point are equal..."
   - Cells: gemini-3-flash_L1137_error_prone

**5. [high] Forbid leaking internal scaffolding/meta instructions** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 113 leaked planning text to the student, exposing internal prompt structure.
   - Suggested edit: Add: 'Never output planning text, draft labels, mode names, or meta-instructions. Output only the final student-facing message. If you catch a draft marker like *Revised Draft* in your output, suppress it.'
   - Expected effect: Eliminates broken tutor turns that confuse the student.
   - Example evidence (113): "mode` strictly. No question marks. No restating the question. *Revised Draft*:"
   - Cells: gemini-3-flash_L1425_error_prone

**6. [high] Prohibit duplicate question stems in one turn** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turns 140 and 144 print the same MCQ twice, doubling cognitive load.
   - Suggested edit: Add rule: 'Each tutor turn must contain at most ONE question stem and ONE option list. Before sending, check that no sentence appears twice.'
   - Expected effect: Cleaner turns, less student confusion.
   - Example evidence (140): "If a map scale is 1:50,000, what does the "1" represent? ... If a map scale is 1:50,000, what does the "1" represent?"
   - Cells: gemini-3-flash_L1425_error_prone

**7. [high] Diagnose the bottleneck after two consecutive errors** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student missed small-scale items three times in a row (126, 128, 130) and tutor just recycled MCQs.
   - Suggested edit: Add: 'After 2 consecutive wrong answers on the same skill, switch to a diagnostic micro-question on the prerequisite (e.g., What does "zoomed out" mean for detail?) before resuming MCQs.'
   - Expected effect: Targets the underlying confusion rather than recycling the same difficulty.
   - Example evidence (130): "b) a map of a single house floor plan"
   - Cells: gemini-3-flash_L1425_error_prone

**8. [high] Forbid references to non-existent diagrams** — surfaced in 1 cell(s), severity score 3
   - Rationale: Tutor repeatedly says 'Look at the diagram below' when no image is rendered, confusing the student.
   - Suggested edit: Never reference a 'diagram', 'figure', or 'image below' unless an image tool has actually produced one in this turn. If no image is available, describe the configuration verbally instead (e.g., 'Imagine four angles meeting at one point...').
   - Expected effect: Removes confusing dead references; forces verbal description suitable for text-only delivery.
   - Example evidence (11): "Look at the diagram below showing four angles around a point."
   - Cells: sonnet-4_L1137_error_prone

**9. [high] Vary problems after mastery rather than re-asking the same MCQ** — surfaced in 1 cell(s), severity score 3
   - Rationale: After the student confused addition/subtraction, the tutor asked three near-identical MCQs about operations instead of giving a fresh worked numeric example.
   - Suggested edit: If a student gets a conceptual MCQ wrong, do NOT re-ask the same MCQ with reshuffled options more than once. Instead, give a small worked numeric step (e.g., 'What is 360 - 200?') to isolate the bottleneck, then return to the original task.
   - Expected effect: Reduces multiple-choice guessing and surfaces the actual arithmetic/concept gap.
   - Example evidence (9): "What two operations do we use to find a missing angle around a point?"
   - Cells: sonnet-4_L1137_error_prone

**10. [high] Prevent role bleed-through / accept student-authored problems gracefully** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 14 shows the student channel contained tutor-style content posing a new problem (90°, 120°, x); the tutor ignored it and posed a different problem.
   - Suggested edit: If the student message contains a question the student appears to have posed (or accepted), honor that exact problem next rather than substituting a new one. Never ignore the student's stated numbers.
   - Expected effect: Maintains conversational coherence and student agency.
   - Example evidence (14): "Three angles around a point are 90°, 120°, and x°. What is the value of x?"
   - Cells: sonnet-4_L1137_error_prone

_…16 additional recommendation(s) in `summary.md` and per-cell files._

### Engine / flow changes

**1. [high] Add a 'partial-sum-as-answer' misconception classifier** — surfaced in 1 cell(s), severity score 3
   - Rationale: The student exhibits the same misconception across many turns; engine should route to a dedicated remediation flow instead of continuing the same template.
   - Expected effect: Triggers targeted remediation automatically.
   - Example evidence (74-79): "my answer is 190. ... my answer is 170. ... my answer is 210."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [high] Cap consecutive failures before forcing a worked example** — surfaced in 1 cell(s), severity score 3
   - Rationale: After 3+ failures on 360-200, system still offered multiple-choice rather than walking through a worked example.
   - Expected effect: Breaks failure loops.
   - Example evidence (92-96): "c) 200 ... a) 100 ... d) 260"
   - Cells: gemini-3-flash_L1137_error_prone

**3. [high] Add a post-generation sanitiser pass** — surfaced in 1 cell(s), severity score 3
   - Rationale: Multiple turns show truncation flags, duplicate stems, and leaked draft markers that a regex/LLM check could catch.
   - Expected effect: Catches malformed turns before they reach the student.
   - Example evidence (115): "[flags: same_template_repeat,truncated]"
   - Cells: gemini-3-flash_L1425_error_prone

**4. [high] Prerequisite routing on repeated failure** — surfaced in 1 cell(s), severity score 3
   - Rationale: Orchestrator should detect ≥2 errors on same skill and route to a remediation micro-lesson.
   - Expected effect: Avoids stuck loops, drives true mastery.
   - Example evidence (129): "Not quite. A hiking trail map shows a lot of detail for a small area"
   - Cells: gemini-3-flash_L1425_error_prone

**5. [high] Cap repeated-MCQ retries and route to remediation** — surfaced in 1 cell(s), severity score 3
   - Rationale: Student got the same conceptual MCQ wrong twice (turns 6, 8); the engine should have routed to a remedial micro-task, not a third reshuffle.
   - Expected effect: Prevents MCQ thrash; ensures genuine remediation triggers after 2 failed retries.
   - Example evidence (8): "c) addition to find the sum, then multiplication to find the missing angle"
   - Cells: sonnet-4_L1137_error_prone

**6. [high] Validate image availability before tutor references figures** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turns 11 and 13 reference a 'diagram below' that does not exist; orchestration should detect missing media and either generate it or strip the reference.
   - Expected effect: Eliminates broken figure references at the system level.
   - Example evidence (13): "Look at the diagram below showing four angles around a point."
   - Cells: sonnet-4_L1137_error_prone

**7. [high] Detect and recover from role/channel confusion** — surfaced in 1 cell(s), severity score 3
   - Rationale: Turn 12 and 14 contain tutor-like content authored under STUDENT role; the orchestration should detect this anomaly and re-prompt or correct routing.
   - Expected effect: Prevents incoherent two-tutor exchanges visible in turns 12-15.
   - Example evidence (12): "Great! Let's put your understanding to the test."
   - Cells: sonnet-4_L1137_error_prone

**8. [high] Repeated-question flag should trigger remediation branch** — surfaced in 1 cell(s), severity score 3
   - Rationale: The system flagged 'repeated_question' at turns 25, 31, 36, 40, 45, 47, 49, 52, 55, 58 but behavior didn't change — flag appears unused.
   - Expected effect: When repeated_question fires N times, orchestrator should route to a prereq diagnostic sub-flow.
   - Example evidence (40): "[flags: repeated_question]"
   - Cells: sonnet-4_L1425_error_prone

**9. [high] Allow student-volunteered problems within objective** — surfaced in 1 cell(s), severity score 3
   - Rationale: Orchestrator forced tutor to refuse on-topic student questions (51, 54, 57, 60); flow should permit substitution if topic-aligned.
   - Expected effect: Reduces refusal moments and keeps lesson learner-driven.
   - Example evidence (58): "I understand you want to practice calculations, but I need to follow our lesson sequence."
   - Cells: sonnet-4_L1425_error_prone

**10. [medium] Detect and block duplicate question rendering** — surfaced in 1 cell(s), severity score 2
   - Rationale: Turn 103 and 105 render the same question block twice in one tutor message.
   - Expected effect: Cleaner UI, less confusion.
   - Example evidence (103): "What is 35 divided by 5? A)...D) 8\n\nWhat is 35 divided by 5? A)...D) 8"
   - Cells: gemini-3-flash_L1137_error_prone

_…2 additional recommendation(s) in `summary.md` and per-cell files._

### Student-experience changes

**1. [medium] Honest feedback tone on partial answers** — surfaced in 1 cell(s), severity score 2
   - Rationale: Saying 'You've correctly added...' when the student gave a wrong final answer feels misleading; honest framing builds trust.
   - Expected effect: Student gets accurate signal about correctness.
   - Example evidence (80): "You've correctly added the known angles to get 210°."
   - Cells: gemini-3-flash_L1137_error_prone

**2. [medium] Use a small visual or analogy when introducing scale types** — surfaced in 1 cell(s), severity score 2
   - Rationale: An inline magnifying-glass vs telescope sketch would cement large vs small scale.
   - Expected effect: Lowers confusion on the confusable pair.
   - Example evidence (119): "That 'magnifying glass' view is what we call a **large-scale** map."
   - Cells: gemini-3-flash_L1425_error_prone

**3. [medium] Show the working space for calculation problems** — surfaced in 1 cell(s), severity score 2
   - Rationale: Calculation turn 147 asked student to 'show working' but UI then funneled into MCQs only.
   - Expected effect: Aligns task framing with response affordance.
   - Example evidence (147): "calculate the actual distance between the villages in kilometres. Show your working."
   - Cells: gemini-3-flash_L1425_error_prone

**4. [medium] Soften meta-questioning that feels like a quiz on vocabulary** — surfaced in 1 cell(s), severity score 2
   - Rationale: Three consecutive operation-naming MCQs may feel demoralizing to an error-prone learner.
   - Expected effect: Improves student confidence; reduces frustration loops.
   - Example evidence (5): "What operation did we use to find x in the equation x = 360° - 255°?"
   - Cells: sonnet-4_L1137_error_prone

**5. [medium] Use verbal spatial descriptions in place of missing diagrams** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student sees instructions to look at something that isn't there; offer a vivid verbal picture instead.
   - Expected effect: Keeps the learner oriented and engaged in text-only sessions.
   - Example evidence (11): "Look at the diagram below showing four angles around a point."
   - Cells: sonnet-4_L1137_error_prone

**6. [medium] Acknowledge readiness signals** — surfaced in 1 cell(s), severity score 2
   - Rationale: Student said 'okay! i'm ready for the numbers now' (turn 44) but tutor immediately changed topic to coral reefs, which feels dismissive.
   - Expected effect: Student feels heard and motivated.
   - Example evidence (44): "okay! i'm ready for the numbers now."
   - Cells: sonnet-4_L1425_error_prone

**7. [medium] Warmer tone on repeated errors** — surfaced in 1 cell(s), severity score 2
   - Rationale: Phrases like 'No, you're still mixing it up!' (turn 40) may discourage an error-prone learner; reframe with empathy.
   - Expected effect: Lowers affective barrier for an error-prone S3 student.
   - Example evidence (40): "No, you're still mixing it up!"
   - Cells: sonnet-4_L1425_error_prone

**8. [low] Acknowledge student's self-generated problems** — surfaced in 1 cell(s), severity score 1
   - Rationale: The student keeps inventing their own problems (turns 77, 82, 88, 100); tutor ignores this engagement and substitutes its own.
   - Expected effect: Higher engagement and ownership.
   - Example evidence (77): "two angles around a point are 130° and 70°. what is the third angle? my answer is 170."
   - Cells: gemini-3-flash_L1137_error_prone

**9. [low] Warmer, varied corrective phrasing** — surfaced in 1 cell(s), severity score 1
   - Rationale: Most corrections start with 'Not quite' which becomes monotonous over many errors.
   - Expected effect: Reduces error fatigue for an error-prone learner.
   - Example evidence (115): "Not quite. A map of the Indian Ocean covers a massive area"
   - Cells: gemini-3-flash_L1425_error_prone

**10. [low] Visual support for scale** — surfaced in 1 cell(s), severity score 1
   - Rationale: Scale concept is inherently spatial; an inline image of zoom-in vs zoom-out maps would help more than repeated verbal definitions.
   - Expected effect: Quicker comprehension and fewer repeat errors.
   - Example evidence (40): "Small-scale map = BIG area, LESS detail"
   - Cells: sonnet-4_L1425_error_prone

## Cross-model robustness check (not a ranking)

Mean rubric scores per model — used only to decide whether a prompt change should be considered model-robust or model-specific. **Do not read this as a model evaluation.** A large gap here means the prompt change holds differently across providers; a small gap means it generalises.

| Model | Overall mean (0-5) | Cells scored |
|---|---:|---:|
| gemini-3-flash | 2.75 | 2 |
| sonnet-4 | 2.45 | 2 |

## Run statistics

- Cells completed: 4/4
- Cells errored: 0
- Total wall time: 1746s (29.1 min)
- Synthetic-student tokens (in/out): 156,262 / 1,903

## Programmatic failure-mode counts (aggregated)

Supplementary signal; the judge's recommendations remain the headline.

| Model | Answer leaks | Repeated Q | No question | Regen shipped dirty |
|---|---:|---:|---:|---:|
| Claude Sonnet 4 | 0 | 13 | 0 | 0 |
| Gemini 3 Flash | 0 | 17 | 0 | 1 |

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
