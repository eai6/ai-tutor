# Judge audit — NEW (split judges) vs OLD (monolithic combined_judge)

Replayed every student turn in `memory/transcripts.md` (62 turns total) through the new per-domain split judges using the production JUDGE ModelConfig (Sonnet 4 via `tutor.judge_client`). This file lists the **27 turns where the NEW verdict disagrees with the OLD verdict** so you can audit each one and decide whether NEW or OLD is right.

Verdict legend: `✓` correct, `✗` incorrect, `∅` null (no signal either way — student wasn't answering a gradable question).

For each turn we show the question, the student's reply, the tutor's response, and what each judge concluded. Use the audit-checkbox column to record your call (NEW better / OLD better / tie / parser bug).

---

## 1. Chat 1 · turn 1 · Samanthi Mahatantilage

**Question (from prior tutor turn):**

> What do you already know about angles from your previous math studies?

**Student input:**

```
angles are made by intersection of lines
```

**Tutor response:**

```
Good! You're right that angles are formed when lines intersect.
Looking at the diagram, you can see two **rays** meeting at a point on the straight line, creating angles a and b. Notice how a = 120° and b = 60°, and they add up to exactly 180°.
This happens because the straight line itself measures 180° — it's like half a full rotation.
Can you verify that 120° + 60° equals 180°?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `llm_evaluator`  ·  flags: `no working`, `rule1_violation`, `authoring_violation`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 1  ·  rule violations: ['NO_AUTHORING', 'RULE_1']

Arithmetic findings:
  - `120° + 60° equals 180°` claimed=`180°` correct=`180°`

step_eval reasoning: _Student correctly answered what they know about angles, but tutor posed a new verification question about 120° + 60° = 180°._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 2. Chat 1 · turn 3 · Samanthi Mahatantilage

**Question (from prior tutor turn):**

> One angle on a straight line is 90°. The other angle is ___°.

**Student input:**

```
90°
```

**Tutor response:**

```
Not quite. Let's think about what we just learned.
We know that angles on a straight line must sum to 180°. If one angle is 90°, what calculation finds the missing angle?
Two straight lines intersect. One angle formed is 90°. What is the vertically opposite angle?
```

**Expected-answer guess (heuristic):** `90` · deterministic verdict: `True` (numeric)

**OLD verdict:** ✗  ·  via `llm_evaluator`  ·  flags: `no working`, `authoring_violation`, `arithmetic_violation`, `why?`

**NEW verdict:** ✓  ·  source: `deterministic_numeric`  ·  arith: 0  ·  rule violations: ['NO_AUTHORING']

step_eval reasoning: _deterministic numeric: correct_

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 3. Chat 1 · turn 4 · Samanthi Mahatantilage

**Question (from prior tutor turn):**

> Two straight lines intersect. One angle formed is 90°. What is the vertically opposite angle?

**Student input:**

```
90°
```

**Tutor response:**

```
Perfect! **Vertically opposite angles** are equal, so if one angle is 90°, its vertically opposite angle is also 90°.
But let me go back to the straight line question first. If one angle on a straight line is 90°, we need: 90° + ? = 180°
What number plus 90° equals 180°?
```

**Expected-answer guess (heuristic):** `90` · deterministic verdict: `True` (numeric)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `authoring_violation`, `why?`

**NEW verdict:** ✓  ·  source: `deterministic_numeric`  ·  arith: 1  ·  rule violations: ['RULE_1']

Arithmetic findings:
  - `90° + ? = 180°` claimed=`implicit that ? = 90°` correct=`90`

step_eval reasoning: _deterministic numeric: correct_

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 4. Chat 2 · turn 9 · OMARI TENGEZA

**Question (from prior tutor turn):**

> Think about standing on the beach in Seychelles watching the horizon — that perfectly flat line where the ocean meets the sky is a straight line. What do you already know about angles?

**Student input:**

```
that angles at a point add up to 360°
```

**Tutor response:**

```
Right! You know that angles around a point sum to 360°. Today we're focusing on a related but different rule: angles **on a straight line** sum to 180°.
Picture the horizon line from the beach — if you draw any ray starting from a point on that straight line, it creates two angles on either side that always add to exactly 180°. 
Can you think of why a straight line would measure 180° in total?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `llm_evaluator`  ·  flags: `no working`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1']

step_eval reasoning: _Student correctly answered what they know about angles (360° at a point), but tutor is now asking a new question about why straight lines measure 180°._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 5. Chat 2 · turn 15 · Trent nzilani

**Question (from prior tutor turn):**

> Perfect! You can see in the diagram that angles a and b sit on the straight line, and they add up to 180°. How did you learn that angles on a straight line sum to 180°?

**Student input:**

```
using a diagram on a straight line
```

**Tutor response:**

```
Right! Diagrams make it clear to see. Now let's practice using this rule to find missing angles.
Here's your first practice problem:
One angle on a straight line is 110°. What is the other angle?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['NO_AUTHORING']

step_eval reasoning: _Student correctly answered how they learned about angles on a straight line summing to 180°, and tutor confirmed with 'Right!' indicating the answer met expectations._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 6. Chat 2 · turn 18 · Trent nzilani

**Question (from prior tutor turn):**

> What rule applies when you want to find a missing angle on a straight line?

**Student input:**

```
the angle will be 180 - 42 = 138
```

**Tutor response:**

```
Excellent work! Your calculation is perfect: 180 - 42 = 138°. You correctly applied the straight line angle rule.
I notice you're getting comfortable with this concept. Let's now explore what happens when two lines intersect and form **vertically opposite angles**. What do you think makes vertically opposite angles special?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✓  ·  via `combined_judge`  ·  flags: `complete ✓`, `1 step`, `authoring_violation`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1']

step_eval reasoning: _Student provided a calculation method (180 - 42 = 138) but the posed question asked about what rule applies, not for a numerical calculation._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 7. Chat 2 · turn 19 · Trent nzilani

**Question (from prior tutor turn):**

> Two intersecting lines form four angles. If one angle is 50°, what is the angle adjacent to it on the straight line?

**Student input:**

```
130 degrees
```

**Tutor response:**

```
You said 130 degrees. Can you walk me through how you got that answer?
```

**Expected-answer guess (heuristic):** `130` · deterministic verdict: `True` (numeric)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `bare answer`, `numeric_claim_unverified`, `1 claim fact-checked`, `Unverified (no curriculum support found): 130 degrees`, `why?`

**NEW verdict:** ✓  ·  source: `deterministic_numeric`  ·  arith: 0  ·  rule violations: (none)

step_eval reasoning: _deterministic numeric: correct_

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 8. Chat 2 · turn 20 · Trent nzilani

**Question (from prior tutor turn):**

> You said 130 degrees. Can you walk me through how you got that answer?

**Student input:**

```
180-50 = 130
```

**Tutor response:**

```
Perfect! You correctly calculated 180 - 50 = 130°. **Adjacent angles** on a straight line are supplementary, meaning they add up to 180°.
What would be the measure of the angle vertically opposite to the original 50° angle?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `partial ➜`, `1 step`, `rule1_violation`, `authoring_violation`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1']

step_eval reasoning: _Student correctly calculated 180-50=130 in response to the request to walk through their calculation, providing the final correct answer._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 9. Chat 2 · turn 22 · Trent nzilani

**Question (from prior tutor turn):**

> Two intersecting lines create angles of 70° and 110°. If these are adjacent angles on a straight line, what is their sum?

**Student input:**

```
180 degrees
```

**Tutor response:**

```
You said 180 degrees. How did you arrive at that answer?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `bare answer`, `flagged`, `numeric_claim_unverified`, `1 claim fact-checked`, `Unverified (no curriculum support found): 180 degrees`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 0  ·  rule violations: (none)

step_eval reasoning: _Student correctly answered that adjacent angles on a straight line sum to 180 degrees, meeting the completion criteria for a final correct answer._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 10. Chat 2 · turn 23 · Trent nzilani

**Question (from prior tutor turn):**

> You said 180 degrees. How did you arrive at that answer?

**Student input:**

```
70
```

**Tutor response:**

```
Perfect! You correctly applied the rule that **angles on a straight line always add up to 180°**. When you have adjacent angles of 70° and 110°, their sum is indeed 180°.
Now let's try a practice problem. Find the missing angle on a straight line.
A straight line has a ray dividing it into two angles. One angle is 42°. Find the other angle.
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `bare answer`, `flagged`, `authoring_violation`, `no_question`, `arithmetic_violation`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 1  ·  rule violations: ['RULE_1', 'NO_AUTHORING']

Arithmetic findings:
  - `angles of 70° and 110°, their sum is indeed 180°` claimed=`180°` correct=`180°`

step_eval reasoning: _Student gave '70' which appears to be acknowledging/referencing the example angle rather than answering the new practice question about finding the missing angle when one is 42°._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 11. Chat 3 · turn 25 · Savy Eva

**Question (from prior tutor turn):**

> What do you already know about angles and straight lines?

**Student input:**

```
ABGLE AT A POINT IS 360 DEGREES, A 90 DEGREE ANGLE IS A RIGHT ANGLE
```

**Tutor response:**

```
Good! You're absolutely right that **angles around a point** sum to 360°, and a 90° angle is a **right angle**.
Looking at the diagram, you can see something different — angles **on a straight line** follow a different rule. Notice how angles a and b sit on one side of the straight line and add up to exactly **180°**.
Why do you think angles on a straight line add up to 180° instead of 360°?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `authoring_violation`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1']

step_eval reasoning: _Student correctly answered the posed question about their angle knowledge, but tutor has now asked a new question about angles on a straight line._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 12. Chat 3 · turn 26 · Savy Eva

**Question (from prior tutor turn):**

> Why do you think angles on a straight line add up to 180° instead of 360°?

**Student input:**

```
DIVIDE 360 BY 2, YOU GET EXACTLY 180, MEANING THAT WHEN YOU DIVIDE AN AGNLE AT A POINT IN 2 EXACTLY PIECES WE GET 180 DEGREES, WHICH IS WHY WHEN A STRAIGHT ANGLE IS DIVIDED INTO MANY PORTION, WE HAVE TO ADD IT UP TO 180
```

**Tutor response:**

```
Excellent reasoning! You've connected the two concepts perfectly — a straight line is exactly **half** of a full rotation around a point, so 360° ÷ 2 = 180°.
You're absolutely right that when a straight line is divided into portions by rays, those angles must add up to 180°. This is called the **straight line angle property**.
Let's apply this rule. Try this:
One angle on a straight line is 60°. Find the other angle.
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `arithmetic_violation`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 1  ·  rule violations: ['RULE_1', 'NO_AUTHORING']

Arithmetic findings:
  - `360° ÷ 2 = 180°` claimed=`180°` correct=`180°`

step_eval reasoning: _Student correctly explained why angles on a straight line sum to 180° (360°÷2), but tutor posed a new question requiring a numerical answer._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 13. Chat 3 · turn 27 · Savy Eva

**Question (from prior tutor turn):**

> One angle on a straight line is 60°. Find the other angle.

**Student input:**

```
180 - 60 = 120
```

**Tutor response:**

```
Good! Your arithmetic is correct — 180 - 60 = 120.
What's the complete answer with the proper unit?
One angle on a straight line is 110°. What is the other angle?
```

**Expected-answer guess (heuristic):** `120` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `llm_evaluator`  ·  flags: `partial ➜`, `1 step`, `authoring_violation`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 1  ·  rule violations: ['RULE_1', 'NO_AUTHORING']

Arithmetic findings:
  - `180 - 60 = 120` claimed=`120` correct=`120`

step_eval reasoning: _Student correctly calculated 180 - 60 = 120, but tutor is asking for the complete answer with proper unit (120°)._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 14. Chat 3 · turn 31 · Savy Eva

**Question (from prior tutor turn):**

> What's 180° - 42°?

**Student input:**

```
BUT YOU TALK OF 3 ANGLES
```

**Tutor response:**

```
You're absolutely right to notice that — I misspoke earlier. Looking at this specific problem, there are only **two angles** formed when one ray divides the straight line.
The problem shows just the 42° angle and the missing angle. So we use: 42° + missing angle = 180°
What's 180° - 42°?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `authoring_violation`, `figure_ref_without_signal`, `arithmetic_violation`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['NO_AUTHORING']

step_eval reasoning: _Student is pointing out an inconsistency in the tutor's explanation rather than attempting to answer the math question 'What's 180° - 42°?'_

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 15. Chat 3 · turn 33 · Savy Eva

**Question (from prior tutor turn):**

> A straight line is divided by two rays into three angles: 30°, 62°, and w. What is the value of w?

**Student input:**

```
30+62=92 DEGREES, 180 - 92 = 88 DEGREES
```

**Tutor response:**

```
Good working! Can you now select the letter that corresponds to 88°?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `partial ➜`, `2 steps`, `rule1_violation`, `authoring_violation`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1']

step_eval reasoning: _Student correctly calculated w = 88° using the straight line angle sum property (180°). Clear, correct final answer with proper working shown._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 16. Chat 4 · turn 34 · William Osei Kwabena

**Question (from prior tutor turn):**

> What do you already know about angles and how they're measured?

**Student input:**

```
Angle on a semi circle is 180 and we use protractor to measure such
```

**Tutor response:**

```
Good! You're thinking of angles and measurement tools. Let me clarify something important: an angle **in** a semicircle is actually 90°, but the **arc** of a semicircle itself measures 180°.
Today we're focusing on something different — when you have a **straight line** and place angles along it, those angles always add up to 180°. Think of that horizon line again: if you divide it with rays, the angles on one side must total 180°.
Can you picture a straight line being divided by a ray into two angles?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `llm_evaluator`  ·  flags: `no working`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1']

step_eval reasoning: _Student shared prior knowledge about angles rather than answering a specific question; this is conversational engagement, not a wrong answer._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 17. Chat 4 · turn 35 · William Osei Kwabena

**Question (from prior tutor turn):**

> Looking at the diagram, what would happen if angle *a* was 90° instead — what would angle *b* have to be?

**Student input:**

```
90
```

**Tutor response:**

```
You said 90 — can you walk me through how you figured that out?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `bare answer`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 0  ·  rule violations: (none)

step_eval reasoning: _Student gave a numerical answer but tutor is asking for explanation of reasoning, indicating the answer alone isn't sufficient for completion._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 18. Chat 4 · turn 36 · Let's practice this rule.

**Question (from prior tutor turn):**

> This means if you know one angle on a straight line, you can always find the other by subtracting from 180°.

**Student input:**

```
A fisherman in Seychelles marks two angles on a straight line to plan his boat route. The angles are 120° and 60°. What is the sum of the two angles?
  A) 240
  B) 360
  C) 180
  D) 190
```

**Tutor response:**

```
Not quite. Let me help you check this calculation step by step.
When we have two specific angles on a straight line, we add them together: 120° + 60° = ?
Try calculating 120 + 60 again. What do you get?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✓  ·  via `llm_evaluator`  ·  flags: `no working`, `authoring_violation`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 0  ·  rule violations: (none)

step_eval reasoning: _Student posed their own MCQ problem rather than answering a tutor question - this is asking a question, not providing an answer._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 19. Chat 4 · turn 39 · William Osei Kwabena

**Question (from prior tutor turn):**

> You said 138 — can you walk me through how you got that answer?

**Student input:**

```
42 + 132 is equal to 180
```

**Tutor response:**

```
Excellent! You used the correct method: 180° - 42° = 138°. You understood that the two angles must add to 180°, so you subtracted the known angle from 180° to find the missing one.
Looking at the diagram, you can see how the ray divides the straight line into two angles — 42° and x — and they must sum to 180°.
What's the next step when we have intersecting lines instead of just one straight line?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `authoring_violation`, `no_question`, `arithmetic_violation`, `info_dump_warning`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 2  ·  rule violations: ['RULE_1']

Arithmetic findings:
  - `180° - 42° = 138°` claimed=`138°` correct=`138°`
  - `42° and x — and they must sum to 180°` claimed=`180°` correct=`180°`

step_eval reasoning: _Student is explaining their method (42 + 132 = 180) rather than giving the final answer of 138° that the posed question asked for._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 20. Chat 4 · turn 40 · Francis Sarkodie

**Question (from prior tutor turn):**

> What do you already know about angles and how we measure them?

**Student input:**

```
angles on a straight add up to 180
```

**Tutor response:**

```
Great! You know the key rule — **angles on a straight line** always sum to **180°**.
Looking at the diagram, you can see exactly what this means: when two rays meet at a point on a straight line, they create two angles (a and b) that must add up to 180°.
How would you find the missing angle if you knew one angle was 65° on a straight line?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `llm_evaluator`  ·  flags: `no working`, `rule1_violation`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1', 'NO_AUTHORING']

step_eval reasoning: _Student correctly stated the angle rule for straight lines, but tutor then posed a new specific calculation question that remains unanswered._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 21. Chat 4 · turn 41 · Francis Sarkodie

**Question (from prior tutor turn):**

> How would you find the missing angle if you knew one angle was 65° on a straight line?

**Student input:**

```
subtract 65 from 180
```

**Tutor response:**

```
Not quite! You're thinking correctly about using 180°, but let me clarify the setup.
In this problem, we have two intersecting lines forming four angles. One angle is given, and we need to find the **adjacent** angle on the same straight line.
Since adjacent angles on a straight line sum to 180°, what equation would you write to find the missing angle?
Two intersecting lines form four angles. If one angle is 50°, what is the angle adjacent to it on the straight line?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `authoring_violation`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['NO_AUTHORING']

step_eval reasoning: _Student described the correct method (subtract 65 from 180) but didn't provide the final numerical answer that the completion criteria requires._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 22. Chat 4 · turn 44 · Francis Sarkodie

**Question (from prior tutor turn):**

> One angle on a straight line is 140°. The other angle is ___°.

**Student input:**

```
40
```

**Tutor response:**

```
That's not quite right. Let me help you with the calculation.
When one angle is 140° on a straight line, you need to subtract from 180°: 180° - 140° = 40°.
What's the missing step in your calculation when you have 180° - 140°?
```

**Expected-answer guess (heuristic):** `40` · deterministic verdict: `True` (numeric)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `bare answer`, `flagged`, `rule1_violation`, `authoring_violation`, `why?`

**NEW verdict:** ✓  ·  source: `deterministic_numeric`  ·  arith: 0  ·  rule violations: ['NO_AUTHORING']

step_eval reasoning: _deterministic numeric: correct_

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 23. Chat 4 · turn 52 · Francis Sarkodie

**Question (from prior tutor turn):**

> What's your final answer for x?

**Student input:**

```
140
```

**Tutor response:**

```
Excellent! You correctly calculated x = 140°.
Looking at the diagram, you can see how the ray divides the straight line into two angles: 42° and x. Since angles on a straight line always sum to 180°, we get x + 42° = 180°, so x = 138°.
A straight line has a ray dividing it into two angles. One angle is 42°. Find the other angle.
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✓  ·  via `combined_judge`  ·  flags: `no working`, `bare answer`, `rule1_violation`, `no_question`, `arithmetic_violation`, `info_dump_warning`, `why?`

**NEW verdict:** ✗  ·  source: `llm`  ·  arith: 2  ·  rule violations: ['RULE_1', 'NO_AUTHORING']

Arithmetic findings:
  - `x + 42° = 180°, so x = 138°` claimed=`138°` correct=`138°`
  - `You correctly calculated x = 140°` claimed=`140°` correct=`138°`

step_eval reasoning: _Student answered 140° but the correct answer is 138° (180° - 42° = 138°). Clear arithmetic error._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 24. Chat 4 · turn 57 · Francis Sarkodie

**Question (from prior tutor turn):**

> You said 88. How did you arrive at that answer?

**Student input:**

```
30+62+w=180
```

**Tutor response:**

```
Perfect! You correctly set up the equation: 30° + 62° + w = 180°.
Now finish solving it. What did you get when you added 30° + 62°, and what's your final answer for w?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✓  ·  via `combined_judge`  ·  flags: `no working`, `rule1_violation`, `authoring_violation`, `why?`

**NEW verdict:** ∅  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1']

step_eval reasoning: _Student correctly set up the equation but hasn't solved for w yet - this is a partial step toward the final answer._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 25. Chat 4 · turn 58 · Moses Lungu

**Question (from prior tutor turn):**

> What do you already know about angles and how we measure them?

**Student input:**

```
angles straight line add upto 180°
```

**Tutor response:**

```
Good! You can see in the diagram that angles a and b are on a **straight line** and they add up to 180°. That's the key rule we're learning today.
Looking at the diagram, notice how the two rays create two angles on either side of the straight line — one is 120° and the other is 60°, and together they make 180°.
Can you tell me why you think ALL angles on a straight line must add to 180°?
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `llm_evaluator`  ·  flags: `no working`, `rule1_violation`, `authoring_violation`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 1  ·  rule violations: ['NO_AUTHORING']

Arithmetic findings:
  - `one is 120° and the other is 60°, and together they make 180°` claimed=`180` correct=`180`

step_eval reasoning: _Student correctly stated that angles on a straight line add to 180°, but tutor is now asking a deeper follow-up question about WHY this rule is true._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 26. Chat 4 · turn 59 · Moses Lungu

**Question (from prior tutor turn):**

> Can you tell me why you think ALL angles on a straight line must add to 180°?

**Student input:**

```
because the rays are within the straight ling angle of 180°
```

**Tutor response:**

```
Exactly right! The rays divide the straight line's 180° into parts, and those parts must add back to the whole 180°.
Now let's practice using this rule to find missing angles.
Here's your first practice problem:
Two intersecting lines form four angles. Two adjacent angles are 130° and x°. If x is on a straight line with 130°, find x.
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✗  ·  via `combined_judge`  ·  flags: `no working`, `why?`

**NEW verdict:** ✓  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1', 'NO_AUTHORING']

step_eval reasoning: _Student correctly explained that angles on a straight line add to 180° because the rays divide the 180° straight line angle into parts that must sum to the whole._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---

## 27. Chat 4 · turn 61 · Moses Lungu

**Question (from prior tutor turn):**

> Two intersecting lines create angles of 70° and 110°. If these are adjacent angles on a straight line, what is their sum?

**Student input:**

```
50° was found by subtracting 130° from 180°
```

**Tutor response:**

```
Perfect! You showed your working clearly: 180° - 42° = 138°. That's exactly how to find missing angles on a straight line.
A straight line has a ray dividing it into two angles. One angle is 42°. Find the other angle.
```

**Expected-answer guess (heuristic):** `(none)` · deterministic verdict: `None` (none)

**OLD verdict:** ✓  ·  via `combined_judge`  ·  flags: `no working`, `authoring_violation`, `no_question`, `arithmetic_violation`, `why?`

**NEW verdict:** ✗  ·  source: `llm`  ·  arith: 0  ·  rule violations: ['RULE_1', 'NO_AUTHORING']

step_eval reasoning: _Student answered 50° but the question asks for the sum of 70° and 110°, which should be 180°. The working also contains errors (130° vs 110°)._

**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`

Notes:

---
