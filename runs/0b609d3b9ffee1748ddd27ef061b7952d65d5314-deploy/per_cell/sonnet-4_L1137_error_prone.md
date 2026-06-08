# Cell: sonnet-4_L1137_error_prone

- Model: **Claude Sonnet 4** (anthropic/claude-sonnet-4-20250514)
- Lesson: L1137 — Math — Angles around a point
- Persona: **error_prone**
- Session ID (Postgres): 1
- Reason: `exit_ticket` — 7 turn(s)

## Programmatic metrics

| Metric | Value |
|---|---|
| tutor turns | 8 |
| tool-use rate | 38% |
| regen triggered | 5 |
| regen clean cycle-1 | 1 |
| regen shipped dirty | 2 |
| **answer-leak incidents** | **0** |
| repeated-question incidents | 1 |
| no-question incidents | 0 |
| wall seconds | 117.8 |
| student tokens (in/out) | 12459 / 148 |

Validator issue breakdown:

- `authoring_violation`: 3
- `numeric_mutation`: 5
- `numeric_claim_contradicted`: 1
- `repeated_question`: 1
- `tutor_incoherent`: 1
- `regen_did_not_clean`: 2
- `figure_ref_without_signal`: 1

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 4 | Most turns ask the student to compute or set up equations (turns 3, 7, 9, 13); minimal lecture. |
| direct_instruction_active_practice | 4 | Each teaching moment is followed by a question (e.g., turn 3 explains rule then asks 8×45, turn 13 reviews then poses new problem). |
| deliberate_practice | 2 | On arithmetic errors tutor decomposes (turn 5: 8×(40+5)), but problems remain same type and corrective feedback was undermined by tutor's own arithmetic errors. |
| mastery_learning | 2 | Tutor moves to a new problem at turn 13 after the student only got the arithmetic right with heavy scaffolding, not independent mastery. |
| cognitive_load | 3 | Subgoals are explicit (add knowns, subtract from 360), but turn 11 introduces contradictory numbers that increase load. |
| layering | 3 | Links 360° rotation to angle sum and uses prior arithmetic skill; layering is present but routine. |
| non_interference | 4 | Single topic (angles around a point) throughout, no confusable concepts introduced. |
| interleaving | 1 | All practice items are the same 'find x given angles around a point' template; no variation. |
| testing_effect | 3 | Turn 9 and 15 ask for working before giving help, supporting retrieval; but hints sometimes too revealing. |
| targeted_remediation | 2 | Turn 5 targets multiplication error well, but turn 11 contains tutor's own arithmetic error (210 vs claim) which undermines remediation. |

**Judge overall summary**

The session covers angles-around-a-point with reasonable structure — short instruction, immediate practice, retrieval prompts — but is undermined by multiple tutor-side arithmetic errors and a glaring self-contradiction in turn 11 ('Perfect working...210...150' immediately after praising the student's 140°). The student's geometric reasoning is sound; the real bottleneck is arithmetic, which the tutor never addresses as a prerequisite. Problems lack variation, and progression is not gated on mastery. Highest-impact fixes: enforce numeric verification, ban contradictory praise-then-correct patterns, and route persistent arithmetic errors to remedial practice.

**Strongest behaviors**

- Asking the student to show their working before correcting (turns 9, 15) supports retrieval and diagnosis.
- Decomposing 8×45 into 8×40 + 8×5 (turn 5) is a clean worked-example scaffold for arithmetic.

**Weakest behaviors**

- Turn 11 contains a self-contradictory statement: 'Perfect working! ...80+60+70 = 210...= 150' immediately after praising 140° — confusing and incorrect framing.
- No interleaving or variation in problem types; repeated 'angles-around-a-point' template gives no mixed practice.

### System-prompt edits (prompt_recommendations)

- **[high] Forbid contradictory praise-then-correct sequences**
  - Rationale: Turn 11 says 'Perfect working! You're absolutely right' then immediately contradicts the student's numbers. This confuses an error-prone learner.
  - Evidence (11): "Perfect working! You're absolutely right — 80° + 60° + 70° = 210°, and 360° - 210° = 150°."
  - Suggested edit: Never affirm an answer you are about to correct. Before writing a praise phrase, verify the student's numeric result matches the correct value. If incorrect, open with neutral language ('Let's check your arithmetic') instead of 'Perfect' or 'You're absolutely right'.
  - Expected effect: Eliminates whiplash feedback and protects student trust in tutor judgments.
- **[high] Require internal arithmetic verification before responding**
  - Rationale: The tutor itself produced wrong arithmetic ('8 × 45 isn't 350' implied 350 was student's claim, but later said 320+45=365 was 'Close'). Numeric mutation flags appear repeatedly.
  - Evidence (7): "you got 8 × 40 = 320 correct, but check 8 × 5 again."
  - Suggested edit: Before sending any reply that contains a number, recompute every arithmetic claim step-by-step in a hidden scratch space and compare to the student's number. Only then write the response.
  - Expected effect: Prevents tutor-introduced numeric errors that confuse the student.
- **[medium] Vary problem types after one successful instance**
  - Rationale: All practice problems follow the same 'sum known angles, subtract from 360' template, providing no interleaving.
  - Evidence (7): "Four angles around a point are 80°, 60°, 70°, and x°. Find x."
  - Suggested edit: After a student solves one angles-around-a-point problem, switch the variation: include an algebraic expression for one angle (e.g., 2x), give the unknown in the middle, or ask for two unknowns with a relationship. Do not repeat the same surface template twice in a row.
  - Expected effect: Prevents pattern-matching and forces transfer of the underlying rule.
- **[medium] Gate progression on independent correct answer**
  - Rationale: Tutor advanced to a new problem (turn 13) immediately after a heavily scaffolded arithmetic correction, without re-checking mastery.
  - Evidence (13): "Your method was spot on... Let's try another one."
  - Suggested edit: Do not move to a new problem until the student has produced a fully correct answer (concept AND arithmetic) without tutor scaffolding on the immediately preceding step.
  - Expected effect: Genuine mastery before progression; reduces compounding errors.
- **[high] Diagnose arithmetic weakness as separate skill**
  - Rationale: Student repeatedly fails basic arithmetic (8×5=45; 140+70=200). Geometry method is correct but arithmetic is the bottleneck.
  - Evidence (6): "8 times 40 is 320. and 8 times 5 is 45. so 320 plus 45 is 365."
  - Suggested edit: If a student makes 2+ arithmetic errors in a session, pause the main lesson and offer a short retrieval drill on the specific arithmetic fact family (e.g., multiplication of single digits, two-digit addition) before returning to the geometry problem.
  - Expected effect: Targets the true prerequisite bottleneck.
- **[medium] Ensure diagram references match what is shown**
  - Rationale: Turn 13 references 'the diagram below' but the flow flags figure_ref_without_signal — the figure may not be inline.
  - Evidence (13): "Look at the diagram below showing four angles around a point."
  - Suggested edit: Only reference a diagram if a tool call has produced it in this same turn. If no diagram exists, describe the angles in text and do not write 'the diagram below'.
  - Expected effect: Avoids referencing missing media and student confusion.

### Engine / flow changes (flow_recommendations)

- **[high] Add arithmetic-error retry policy**
  - Rationale: Multiple arithmetic slips by both student and tutor were not caught by any verification loop.
  - Evidence (12): "140 plus 70 is 200."
  - Expected effect: Catches numeric errors before they enter the conversation.
- **[high] Prerequisite-routing branch for arithmetic**
  - Rationale: Student's geometry reasoning is correct; arithmetic is the bottleneck. Engine should detect and route to a remedial micro-lesson.
  - Evidence (10): "i added 80, 60, and 70. that's 220."
  - Expected effect: Targets root cause and improves long-term mastery.
- **[medium] Mastery gate before advancing problems**
  - Rationale: Engine advanced to a new problem without independent demonstration of mastery.
  - Evidence (13): "Let's try another one."
  - Expected effect: Enforces mastery-based progression.

### Student-experience changes (experience_recommendations)

- **[high] Soften contradictory feedback**
  - Rationale: Saying 'Perfect working! You're absolutely right' then contradicting the student feels jarring for an error-prone student.
  - Evidence (11): "Perfect working! You're absolutely right — 80° + 60° + 70° = 210°, and 360° - 210° = 150°."
  - Expected effect: Protects student confidence and trust.
- **[low] Acknowledge arithmetic struggle warmly**
  - Rationale: Student is making repeated small arithmetic slips; tone could be more supportive and explicitly normalize the difficulty.
  - Evidence (13): "Just watch that arithmetic!"
  - Expected effect: Reduces anxiety and encourages persistence.

## BEA-2025 per-turn evaluation

_Maurya et al. 2025. Per-tutor-turn 3-class scoring on 4 dimensions._

- **Coverage**: 6 in-scope of 8 tutor turns (75%)
- **All-4-dims strict pass**: 17%  ·  **lenient**: 67%

| Dimension | Exact (Yes) | Lenient (Yes + Somewhat) |
|---|---:|---:|
| mistake_identification | 83% | 100% |
| mistake_location | 50% | 67% |
| providing_guidance | 33% | 100% |
| actionability | 83% | 100% |

### Per-turn verdicts

- **turn 5** — mistake: _Student said 8 × 45 = 350 (correct is 360) and claimed 350 'close to 360' validates the rule._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Hold on — let's redo that calculation. 8 × 45° isn't 350°. Think of it this way: 8 × 45 is the same as 8 × (40 + 5)..."
  - Rationale: Tutor clearly identifies the wrong product, locates it precisely, and gives an actionable decomposition strategy.
- **turn 7** — mistake: _Student said 8 × 5 = 45 and 320 + 45 = 365._
  - MI: `Yes`  · ML: `Yes`  · PG: `To some extent`  · Act: `Yes`
  - Tutor: "Close! You got 8 × 40 = 320 correct, but check 8 × 5 again. 8 × 5 = 40, not 45."
  - Rationale: Tutor identifies the exact error but states '8 × 5 = 40' which is itself wrong (8 × 5 = 40 is correct actually — wait, 8×5=40 is correct). Guidance correct; addition error 320+40=360 also correct. Marked Yes/Yes/Yes/Yes on reconsideration.
- **turn 9** — mistake: _Student answered x = 140° (correct is 150°)._
  - MI: `Yes`  · ML: `No`  · PG: `To some extent`  · Act: `Yes`
  - Tutor: "That's not quite right. Let me see your working so I can help you spot where it went sideways."
  - Rationale: Tutor recognizes the answer is wrong but does not locate where; asks the student to share working — actionable next step but no specific location yet.
- **turn 11** — mistake: _Student added 80+60+70 = 220 (correct is 210), then 360−220=140._
  - MI: `To some extent`  · ML: `To some extent`  · PG: `To some extent`  · Act: `Yes`
  - Tutor: "Perfect working! You're absolutely right — 80° + 60° + 70° = 210°, and 360° - 210° = 150°. Wait, let me double-check you"
  - Rationale: Tutor self-contradicts: praises as 'Perfect' yet supplies different numbers than student. Eventually points to the addition step and asks the student to recompute 140+70, which is actionable, but the framing is incoherent and misleading.
- **turn 13** — mistake: _Student said 140 + 70 = 200 (correct 210)._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "Close — 140 + 70 = 210, not 200. So the three known angles sum to 210°, which means x = 360° - 210° = 150°."
  - Rationale: Tutor corrects the arithmetic clearly and shows the full computation, but then moves on without having the student redo the problem, reducing actionability for mastery.
- **turn 15** — mistake: _Student said x = 70 for a new problem (70+85+100+x=360 → x=105)._
  - MI: `Yes`  · ML: `No`  · PG: `To some extent`  · Act: `Yes`
  - Tutor: "That's not it — show me how you set it up so I can see where it went sideways."
  - Rationale: Tutor flags the answer as wrong and asks for working — clear next action but no diagnosis of where the error lies.

## Transcript

```
# Transcript — model=Claude Sonnet 4  lesson=1137  persona=error_prone
session_id=1  status=active

--- TUTOR (id=1, tools=1)
Hi! Today we're learning about **angles around a point** — a fundamental rule that helps solve many geometry problems.

Imagine you're standing at the top of Morne Seychellois and slowly spinning in a complete circle, taking in the full view of Mahé's coastline. That complete turn is always 360°, no matter where you stand or how you divide up the view.

When you make a complete turn in any direction, how many degrees do you rotate through?

A) 180°
B) 270°
C) 360°
D) 450°

--- STUDENT (id=2, tools=0)
c) 360°

--- TUTOR (id=3, tools=0)  [flags: authoring_violation]
Yes — 360° is right, since a complete turn is always a full rotation.

Now let's see how this applies to geometry. Looking at the diagram, you can see a point with 8 rays extending outward, creating 8 equal angles of 45° each. Notice how all these angles together make one complete rotation around the central point.

This leads us to the key rule: **angles around any point always sum to 360°**. Whether you have 2 angles, 4 angles, or 8 angles around a point, they must add up to exactly 360°.

Can you tell me what 8 × 45° equals, and why this confirms our rule?

--- STUDENT (id=4, tools=0)
8 times 45 is 350. this shows the rule is right because it's close to 360.

--- TUTOR (id=5, tools=0)  [flags: authoring_violation,numeric_mutation]
Hold on — let's redo that calculation. 8 × 45° isn't 350°.

Think of it this way: 8 × 45 is the same as 8 × (40 + 5). What do you get when you calculate 8 × 40, then 8 × 5, and add them together?

--- STUDENT (id=6, tools=0)
okay, 8 times 40 is 320. and 8 times 5 is 45. so 320 plus 45 is 365.

--- TUTOR (id=7, tools=1)  [flags: numeric_mutation]
Close! You got 8 × 40 = 320 correct, but check 8 × 5 again. 

8 × 5 = 40, not 45. So 320 + 40 = 360°.

This confirms our rule perfectly — the 8 equal angles of 45° each sum to exactly 360°, which is why angles around any point always total 360°.

Now let's apply this rule.

Four angles around a point are 80°, 60°, 70°, and x°. Find x.

--- STUDENT (id=8, tools=0)
x is 140 degrees.

--- TUTOR (id=9, tools=0)  [flags: numeric_mutation]
That's not quite right. Let me see your working so I can help you spot where it went sideways.

Show me how you set up the equation and what steps you took to get 140°.

--- STUDENT (id=10, tools=0)
i added 80, 60, and 70. that's 220. then i did 360 minus 220, which is 140.

--- TUTOR (id=11, tools=0)  [flags: authoring_violation,numeric_claim_contradicted,repeated_question,tutor_incoherent,numeric_mutation,regen_did_not_clean]
Perfect working! You're absolutely right — 80° + 60° + 70° = 210°, and 360° - 210° = 150°.

Wait, let me double-check your addition: 80 + 60 + 70. Let's break it down: 80 + 60 = 140, then 140 + 70 = ?

--- STUDENT (id=12, tools=0)
140 plus 70 is 200.

--- TUTOR (id=13, tools=1)  [flags: regen_did_not_clean,figure_ref_without_signal,numeric_mutation]
Close — 140 + 70 = 210, not 200.

So the three known angles sum to 210°, which means x = 360° - 210° = 150°.

Your method was spot on: add the known angles, then subtract from 360°. Just watch that arithmetic!

Let's try another one.

Now let's use this rule to find a missing angle. Look at the diagram below showing four angles around a point. Three angles are given: 70°, 85°, and 100°. We need to find the fourth angle (marked as x). I'll show you the step-by-step method.

--- STUDENT (id=14, tools=0)
okay, so x is 70.

--- TUTOR (id=15, tools=0)
That's not it — show me how you set it up so I can see where it went sideways.

```
