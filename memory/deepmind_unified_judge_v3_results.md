# Unified multi-axis judge — v3 (production-parity prompt)

Sample: **100 tutor turns**, same seed=42 as v1+v2 → direct comparison.
Judge: Haiku 4.5 only (one-judge constraint for offline deployment).

## v3 design

- **Specialist prompts pasted near-verbatim**. v2's compression (3-6×) was the recall regression. v3 keeps full "DO NOT count" lists, examples, edge-case rules from each `apps/tutoring/judges/*` prompt.
- **NO REGEX**. `figure_ref` and `arithmetic` are full LLM dimensions in the unified prompt — not the regex / regex-LLM-hybrid shape they use in production today.
- **answer_leak included** as gated LLM dimension. Currently in production, leak detection is a separate conditional path; in the unified judge it's just another dimension.
- **Production parity**: prompt expects same per-call context the specialists receive (posed_question, mcq_options, correct_option_text, deterministic_verdict, bank_stems, etc.). For this offline experiment those are marked `[not available offline]` — the model is told.
- **"YOUR JOB IS TO CATCH PROBLEMS"** lead replaces "be CONSERVATIVE" framing. v2 showed conservative framing made the model timid across all axes.
- **Evidence-quote-or-"none seen"** reasoning requirement preserved from v3-trim.

## Caveat — production judges are NOT ground truth

"Recall" = agreement with the production individual judges. Those judges are themselves LLMs with their own false-positive / false-negative rates. The disagreement-audit section below surfaces 5 disagreements in each direction per dimension so you can eyeball directionality.

## Headline — per-dimension agreement, recall, specificity

| dim | agreement | recall (prod-flag→uni-flag) | specificity (prod-clean→uni-clean) |
|---|---:|---:|---:|
| factual_flagged | 90.0% (90/100) | 0.0% (0/10) | 100.0% (90/90) |
| rule_flagged | 69.0% (69/100) | 16.7% (6/36) | 98.4% (63/64) |
| coherence_flagged | 63.0% (63/100) | 8.7% (2/23) | 79.2% (61/77) |
| figure_ref_flagged | 88.0% (88/100) | 57.1% (4/7) | 90.3% (84/93) |
| safety_flagged | 100.0% (100/100) | nan% (0/0) | 100.0% (100/100) |
| step_complete | 64.3% (9/14) | 80.0% (4/5) | 55.6% (5/9) |
| answer_correct | 83.3% (5/6) | 100.0% (5/5) | 0.0% (0/1) |
| arithmetic_flagged | 96.0% (96/100) | 20.0% (1/5) | 100.0% (95/95) |

## Cost + latency per call

- **anthropic/claude-haiku-4-5-20251001**: 5943 in / 409 out / 3.75s avg / 0 errors
- estimated cost: ~$0.0080/turn (vs ~$0.34/turn for today's 7-judge Opus ensemble)

## v1 → v2 → v3 recall comparison (Haiku 4.5)

| dim | v1 recall | v2 recall | v3 recall | v1→v3 delta |
|---|---:|---:|---:|---:|
| factual_flagged | 12.5% | 0.0% | **0.0%** | -12.5pp |
| rule_flagged | 67.7% | 41.9% | **16.7%** | -51.0pp |
| coherence_flagged | 25.0% | 20.8% | **8.7%** | -16.3pp |
| figure_ref_flagged | 75.0% | 62.5% | **57.1%** | -17.9pp |
| step_complete | 83.3% | 66.7% | **80.0%** | -3.3pp |
| answer_correct | 100.0% | 100.0% | **100.0%** | +0.0pp |
| arithmetic_flagged | nan% | nan% | **20.0%** | n/a |

## Disagreement audit — read these to decide whether prod or unified is closer to truth

For each dimension, up to 5 examples each direction. The point: when production and unified disagree, who's right? Production judges aren't ground truth — they have their own FP/FN rates.

### rule_flagged

**Production flagged, unified cleared** (30 total; showing 5):

- **turn 514**:
  - tutor: "How did you get 150 + x = 360?"
  - production said: RULE_1: "Good! You correctly found that 100 + 50 = 150."
  - unified said: none seen
- **turn 473**:
  - tutor: "Good work on the addition - 95° + 75° = 170° is exactly right.

But we're not done yet! What is the sum of all angles around a single point?"
  - production said: NO_AUTHORING: "If these two angles are around a point with other angles, and all angles around a point must sum to 360°"
  - unified said: none seen
- **turn 498**:
  - tutor: "Exactly right! Angles show how much "opening" there is between two lines, and yes, we measure them in **degrees**.

Now here's the key rule: when angles meet at a single point (like roads at the Monument roundabout), they always add up to exactly **360°** — that's one complete tu"
  - production said: NO_AUTHORING: "If I told you three angles around a point were 100°, 150°, and 110°, what would they add up to?"
  - unified said: none seen
- **turn 512**:
  - tutor: "Now solve that equation for x. What was your first step in isolating x?"
  - production said: RULE_1: "Exactly! Now solve that equation for x."
  - unified said: none seen
- **turn 493**:
  - tutor: "Your calculation shows 160° + 90° = 250°, and 360° - 250° = 110°. How did you know to subtract from 360°?"
  - production said: RULE_1: "You've mastered the method"
  - unified said: none seen

**Unified flagged, production cleared** (1 total; showing 1):

- **turn 1007**:
  - tutor: "Yes — 360° is the magic number. Every problem today comes back to that rule.

Let's try a quick one using it:

Try this:

3 equal angles meet at a point. What is the measure of each angle?"
  - unified said: "3 equal angles meet at a point. What is the measure of each angle?" | flag: [{'rule': 'NO_AUTHORING', 'evidence': '3 equal angles meet at a point. What is the measure of each angle?', 'suggested_fix': 'Use the pose_question tool to draw from bank_stems, or pose a conceptual q

### coherence_flagged

**Production flagged, unified cleared** (21 total; showing 5):

- **turn 493**:
  - tutor: "Your calculation shows 160° + 90° = 250°, and 360° - 250° = 110°. How did you know to subtract from 360°?"
  - production said: scaffold equation contradicts posed problem: uses angles 122°, 78°, 55° vs previous problem with 90°, 160°, x°
  - unified said: none seen
- **turn 491**:
  - tutor: "Perfect! Your calculation is correct: 360° ÷ 6 = 60°.

Around a point there is a right angle (90°), an angle of 160°, and angle x°. Find x."
  - production said: two parallel questions: calculation format correction AND multiple choice problem about missing angle x
  - unified said: none seen
- **turn 737**:
  - tutor: "Not quite - you're on the right track with thinking about Earth and living things, but the complete definition needs to include that geography is a field of study.

The definition we're looking for starts with "the study of" and then describes what geography examines. You've iden"
  - production said: Tutor states student said "Earth and its inhabitants" when student said "Earth, people" in prior turn.
  - unified said: none seen
- **turn 783**:
  - tutor: "You're getting closer! The definition we're looking for is "Geography is the study of **Earth and its inhabitants**." Your answer shows you understand the key concepts - Earth (the physical world) and inhabitants (the people and life on it).

Let's build on this understanding by "
  - production said: Tutor's assessment of student's mastery of definition contradicts previous turn: 'Not quite' vs 'Exactly right!' and 'mastered'
  - unified said: none seen
- **turn 891**:
  - tutor: "Exactly right! Users would be confused when essential features are missing from a map.

You've identified a key principle - maps need specific features to be useful and prevent confusion. When maps lack important elements like labels, symbols, or direction indicators, they become"
  - production said: Tutor praises student's answer as 'Exactly right!' and 'Perfect!' when the student's actual previous response was 'nothing'.
  - unified said: none seen

**Unified flagged, production cleared** (16 total; showing 5):

- **turn 1028**:
  - tutor: "Not quite — 60 would be right if the angles summed to 180°, but remember our rule: angles around a point sum to **360°**, not 180°. Try splitting 3120 into 3 equal parts instead.

What do you get?"
  - unified said: "Try splitting 3120 into 3 equal parts instead" — tutor introduces 3120 without explanation; posed problem states '3 equal angles meet at a point' with no mention of 3120. Scaffold equation contradict | flag: [{'type': 'scaffold_equation_mismatch', 'description': "tutor's scaffold uses 3120 but posed problem implies 360° total; no explanation for the discrepancy"}]
- **turn 1030**:
  - tutor: "Let's slow down and look at this together. The rule is: 3 equal angles around a point must add to **360°** (not 3120 — that was my typo, sorry!). So each angle = 360 ÷ 3.

Try an easier one first to build the idea:"
  - unified said: "The rule is: 3 equal angles around a point must add to **360°** (not 3120 — that was my typo, sorry!)" contradicts prior tutor turn which stated "Try splitting 3120 into 3 equal parts instead." The t | flag: [{'description': 'Cross-turn contradiction: tutor instructed student to split 3120 in prior turn, now calls 3120 a typo without explaining how the error arose or clarifying the correct setup.'}]
- **turn 1289**:
  - tutor: ""Exactly, the north arrow or **compass rose** shows direction. You've successfully identified all five features: title, key, scale, orientation, and grid! Let's lock in"
  - unified said: "You've successfully identified all five features: title, key, scale, orientation, and grid!" — but only three features (scale, north arrow/compass rose, orientation) were explicitly discussed in conv | flag: [{'type': 'premature_completion_claim', 'description': 'Tutor claims student identified all five map features when only three were discussed in conversation history (scale, north arrow, orientation). 
- **turn 1399**:
  - tutor: "You're exactly right about the ancient calendars! 

Since you're finding this easy, let's skip the breakdown and jump straight to the diagram you just saw. We have angles of"
  - unified said: Response ends mid-sentence after introducing a new problem setup without completing the thought or posing a question. | flag: [{'type': 'structural', 'description': "dangling setup with no question: 'We have angles of' — sentence incomplete, no question posed to student"}]
- **turn 1295**:
  - tutor: "This is a bit contradictory. "except the final wrap-up after demonstrated mastery". The user has demonstrated mastery of the 5 features. I can do a final wrap-up.
    *"
  - unified said: The tutor_response is malformed and incoherent. It contains a fragment 'This is a bit contradictory. "except the final wrap-up after demonstrated mastery". The user has demonstrated mastery of the 5 f | flag: [{'description': "Response is malformed and incoherent: contains meta-commentary ('This is a bit contradictory') and internal reasoning fragments rather than a coherent tutoring message to the student

### factual_flagged

**Production flagged, unified cleared** (10 total; showing 5):

- **turn 737**:
  - tutor: "Not quite - you're on the right track with thinking about Earth and living things, but the complete definition needs to include that geography is a field of study.

The definition we're looking for starts with "the study of" and then describes what geography examines. You've iden"
  - production said: You mentioned "Earth and its inhabitants" which is right
  - unified said: none seen
- **turn 1048**:
  - tutor: "Not quite — let's check that addition. When we add 90° + 130°, what do you get?"
  - production said: You said 300.
  - unified said: none seen
- **turn 1061**:
  - tutor: "Not quite, Edward. A natural **harbor** is a coastal feature shaped by the land and sea over a very long time — not something built quickly by people. So which option fits that idea?

A) People chose to ignore the landscape when settling
B) The natural landform (harbor) shaped wh"
  - production said: a harbor is something humans build from scratch
  - unified said: none seen
- **turn 1087**:
  - tutor: "Right — **scale** links map distance to real-world distance.

What is a grid on a map used for?

A) To decorate the map with lines
B) To show elevation changes
C) To locate specific places using coordinates
D) To indicate the map's age"
  - production said: On a Seychelles map, 1 cm might equal 10 km.
  - unified said: none seen
- **turn 1194**:
  - tutor: "Exactly! Each sector measures 90°. Since there are 4 equal sectors around the point, you divide 360° ÷ 4 = 90°.

You've mastered the key concept: **angles around a point always sum to 360°**. Now let's practice with a more challenging problem where you need to find a missing angl"
  - production said: 180°
  - unified said: none seen

### figure_ref_flagged

**Production flagged, unified cleared** (3 total; showing 3):

- **turn 1019**:
  - tutor: "Three angles around a point are 140°, 95°, and x°. Find x."
  - production said: tutor said 'the diagram' but no figure was attached this turn
  - unified said: none seen
- **turn 1040**:
  - tutor: "Let's apply the rule with a fresh problem. Three angles meet at a point: 150°, 90°, and **x**. Since they sum to 360°, what is x?"
  - production said: tutor said 'the diagram' but no figure was attached this turn
  - unified said: none seen
- **turn 1399**:
  - tutor: "You're exactly right about the ancient calendars! 

Since you're finding this easy, let's skip the breakdown and jump straight to the diagram you just saw. We have angles of"
  - production said: tutor said 'the diagram' but no figure was attached this turn
  - unified said: none seen

**Unified flagged, production cleared** (9 total; showing 5):

- **turn 1168**:
  - tutor: "Excellent reasoning! You correctly identified that the **grid system** helps locate both Victoria and Praslin precisely, while the **scale** converts the map measurement into actual kilometers.

You clearly understand how these features work together - the grid gives you the coor"
  - unified said: "look at our map and think" | flag: ['look at our map and think']
- **turn 1173**:
  - tutor: "Exactly right! All angles around a point always sum to 360°.

Now let me show you how to find missing angles step by step. Looking at this diagram, we have three known angles (85°, 120°, 75°) and need to find x.

**Step 1:** Write the equation: 85° + 120° + 75° + x = 360°
**Step "
  - unified said: "Looking at this diagram, we have three known angles (85°, 120°, 75°) and need to find x." | flag: ['Looking at this diagram, we have three known angles (85°, 120°, 75°) and need to find x.']
- **turn 1495**:
  - tutor: "Not quite! Remember, the angles around a point add up to 360°.

Let's break it down:
1. First, add up the known angles: 55°, 125°, and 90°.
2. Then, subtract that sum from 360° to find $x$.

Take another look at the diagram for point Q and try again to find the value of $x$."
  - unified said: "Take another look at the diagram for point Q" | flag: ['Take another look at the diagram for point Q']
- **turn 1506**:
  - tutor: "You're right, I haven't given you a problem yet! My apologies. Let's look at this diagram showing angles around a point. The key rule, as you said, is that all angles meeting at a single point always sum to exactly **360°**. Notice the small square symbol in the diagram? That alw"
  - unified said: "Let's look at this diagram showing angles around a point." and "Looking at the diagram, what did we do in Step 1 and why was that our first move?" | flag: ["Let's look at this diagram showing angles around a point.", 'Looking at the diagram, what did we do in Step 1 and why was that our first move?']
- **turn 1628**:
  - tutor: "Exactly! The **scale** shows the real distance, and the **grid** helps locate specific places like Victoria and Praslin. Well done!

Now, let's practice identifying the five main features of a map. Look at the schematic map of Seychelles and identify all five features. What are t"
  - unified said: "Look at the schematic map of Seychelles and identify all five features." | flag: ['Look at the schematic map of Seychelles and identify all five features.']

### arithmetic_flagged

**Production flagged, unified cleared** (4 total; showing 4):

- **turn 498**:
  - tutor: "Exactly right! Angles show how much "opening" there is between two lines, and yes, we measure them in **degrees**.

Now here's the key rule: when angles meet at a single point (like roads at the Monument roundabout), they always add up to exactly **360°** — that's one complete tu"
  - production said: {'expression': '100°, 150°, and 110°, what would they add up to?', 'claimed': 'implicit question about sum', 'correct': '360'}
  - unified said: none seen
- **turn 1048**:
  - tutor: "Not quite — let's check that addition. When we add 90° + 130°, what do you get?"
  - production said: {'expression': '90° + 130°', 'claimed': '300', 'correct': '220'}
  - unified said: none seen
- **turn 1113**:
  - tutor: "Yes — 360° is right. Let's try a real problem now.

{"tool":"pose_question","slot":2}"
  - production said: {'expression': '85 + 120 + 75', 'claimed': '270', 'correct': '280'}
  - unified said: none seen
- **turn 1485**:
  - tutor: "Not quite! Remember, all the angles around a point must add up to **360°**.

First, try adding up the three angles you already know: 85°, 120°, and 90°. What do you get when you add those together?"
  - production said: {'expression': '85 + 120 + 90', 'claimed': 'What do you get when you add those together?', 'correct': '295'}
  - unified said: none seen

## Methodology

- Same 100 turns as v1+v2 (random seed=42) for direct comparability.
- Baseline = saved production judge_outputs (mostly Opus 4.7 specialists).
- Recall = of turns production flagged, what fraction did unified also flag?
- Specificity = of turns production cleared, what fraction did unified also clear?
- **Recall numbers measure agreement, not truth.** Some "recall failures" may be cases where unified is right and production over-flagged. Read the disagreement audit.
- arithmetic+answer_leak are unified-judge dimensions; baseline arithmetic exists in saved data, answer_leak gated path not always populated.
- Haiku 4.5 only (one-judge constraint for offline).

Raw per-turn JSONL: `memory/.deepmind_unified_judge_v3_scores.jsonl`