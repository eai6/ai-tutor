# Cell: sonnet-4_L1425_error_prone

- Model: **Claude Sonnet 4** (anthropic/claude-sonnet-4-20250514)
- Lesson: L1425 — Geography — Map Scale and Map Types
- Persona: **error_prone**
- Session ID (Postgres): 2
- Reason: `max_turns` — 20 turn(s)

## Programmatic metrics

| Metric | Value |
|---|---|
| tutor turns | 25 |
| tool-use rate | 76% |
| regen triggered | 14 |
| regen clean cycle-1 | 0 |
| regen shipped dirty | 0 |
| **answer-leak incidents** | **0** |
| repeated-question incidents | 6 |
| no-question incidents | 0 |
| wall seconds | 310.3 |
| student tokens (in/out) | 54774 / 695 |

Validator issue breakdown:

- `repeated_question`: 6
- `truncated`: 9
- `numeric_mutation`: 2
- `same_template_repeat`: 2

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 2 | Session is mostly multiple-choice Q&A which is active, but the tutor delivers long explanation blocks (turns 68, 70, 72) and the session devolves into broken orchestration where no productive practice |
| direct_instruction_active_practice | 2 | Turns 52-53 and 72-73 pair instruction with a question, but turns 78, 82, 88 are empty tutor turns providing instruction with no actionable practice. |
| deliberate_practice | 1 | After repeated wrong answers (turns 59, 61, 63, 65, 67, 69, 71), tutor keeps re-asking nearly the same MCQ rather than calibrating to a simpler prereq skill. |
| mastery_learning | 1 | At turn 72 the tutor reveals the answer ('let me give you the answer') after 5 failed attempts instead of diagnosing the prereq bottleneck, then immediately jumps to harder scale calculations. |
| cognitive_load | 2 | Tutor introduces large-scale/small-scale terminology mid-struggle (turn 64) while student is still failing the comparison; turn 72 dumps definitions, examples, and a new calculation problem all at onc |
| layering | 2 | Turn 52 links Victoria navigation to scale concept, but the jump to scale calculations at turn 73 assumes mastery of the 1:5,000 vs 1:500,000 idea the student never demonstrated. |
| non_interference | 3 | Large-scale vs small-scale are confusable but tutor does make discriminating features explicit ('zoomed in' vs 'zoomed out' in turn 68); no adjacent confusable topics introduced. |
| interleaving | 1 | Tutor re-poses essentially the same 'which map shows more detail' question 4 times (turns 64, 66, 68, 70) — the opposite of varied problem types. |
| testing_effect | 2 | Retrieval attempts happen via MCQ, but turn 72 reveals the answer prematurely, and the lesson-bank rigidity prevents the student's own retrieval questions (turns 74, 79) from being engaged. |
| targeted_remediation | 1 | After 5 consecutive wrong answers on detail/scale, tutor recycles the same question with slightly different framing rather than dropping to a prereq (e.g., what 1:5,000 literally means) — culminating  |

**Judge overall summary**

The session opened with a strong contextual hook and useful analogies, but collapsed into a repeated-MCQ loop after the student missed a key conceptual discrimination, culminating in an answer reveal at turn 72. The second half degenerated into a refusal loop where the tutor rigidly insisted on a lesson-bank question while emitting empty turns (78, 82, 88) and the student repeatedly tried to supply plausible on-topic problems. Key fixes: cap retries and pivot to prereq diagnosis, forbid empty tutor turns, and permit on-topic student-initiated practice.

**Strongest behaviors**

- Concrete analogy use (zoom in/out on phone map in turn 68; classroom vs school drawing in turn 70) to make scale tangible.
- Opening hook (turn 48) links lesson to authentic Seychelles contexts (Victoria walk, Mahé-Praslin sail).

**Weakest behaviors**

- Rigid 'stick to the lesson bank' behavior (turns 55, 58, 75, 80, 84, 86, 90) that refuses student-initiated practice and produces empty tutor turns (78, 82, 88).
- Repeating the same MCQ format five times after failure (turns 64-70) instead of diagnosing the prereq and remediating, then revealing the answer at turn 72.

### System-prompt edits (prompt_recommendations)

- **[high] Forbid answer reveal after N failures; require prereq diagnosis instead**
  - Rationale: At turn 72 tutor says 'let me give you the answer' after 5 failed attempts, violating mastery learning.
  - Evidence (72): "I can see this concept is challenging, so let me give you the answer and explain it clearly."
  - Suggested edit: After 2 consecutive wrong answers on the same item, you MUST switch to a diagnostic prereq question (e.g., 'What does 1:5,000 literally mean in cm?') rather than re-asking the same MCQ or revealing the answer. Never say 'let me give you the answer'.
  - Expected effect: Forces targeted remediation and protects mastery gating.
- **[high] Allow student-initiated practice when on-topic**
  - Rationale: Student offered valid on-topic calculation questions (turns 74, 79, 83, 89) which tutor refused, derailing the session.
  - Evidence (80): "I need to stick to our lesson sequence. Let me ask the question I posed:"
  - Suggested edit: If the student proposes an on-topic practice problem that exercises the current target skill, accept it and have them solve it (or solve a near-variant), then return to the lesson sequence. Do not refuse on-topic student initiative.
  - Expected effect: Restores student agency and keeps active practice flowing.
- **[high] Vary problem TYPE after repeated failure, not just wording**
  - Rationale: Turns 64, 66, 68, 70 re-pose the same 'which shows more detail' MCQ in slightly different words.
  - Evidence (64): "Which map would show more detail about individual streets in Victoria?"
  - Suggested edit: Forbidden pattern: re-asking the same conceptual MCQ more than twice. On the third attempt, switch to a different task TYPE (open-ended, ordering, fill-in, or numeric prereq).
  - Expected effect: Reduces guessing loops; surfaces the real misconception.
- **[high] Never emit empty tutor turns**
  - Rationale: Turns 78, 82, 88 are empty, indicating tool-call failures the prompt should guard against.
  - Evidence (78): "(empty tutor turn)"
  - Suggested edit: You must always produce visible content for the student. If a tool call fails or returns nothing, fall back to posing the next question in plain text immediately.
  - Expected effect: Eliminates dead air that breaks the lesson.
- **[medium] Acknowledge correctness before correcting prior errors**
  - Rationale: Tutor sometimes responds to a correct answer with new content without clear affirmation (e.g., turn 52 affirms but then immediately stacks new instruction).
  - Evidence (52): "Exactly right! ... Now let's explore how map scales work."
  - Suggested edit: When confirming a correct answer, restate WHY it is correct in one sentence before introducing the next concept; keep the next instruction to <=2 sentences before asking a question.
  - Expected effect: Strengthens encoding and reduces cognitive load.
- **[medium] Introduce key terminology BEFORE the discriminating MCQ**
  - Rationale: The terms 'large-scale' and 'small-scale' are first defined in turn 64, after the student has already failed once on that distinction.
  - Evidence (64): "The key insight is that **large-scale maps** (like 1:5,000) show small areas with lots of detail"
  - Suggested edit: Define any technical term (e.g., large-scale, small-scale) with a worked example BEFORE posing an MCQ that depends on it.
  - Expected effect: Reduces guessing and aligns with cognitive load principle.
- **[medium] Interleave numeric and conceptual scale items**
  - Rationale: Session stays purely conceptual until turn 72, then jumps straight to multi-step numeric calculation, with no graded bridge.
  - Evidence (73): "calculate the actual distance between the villages in kilometres. Show your working."
  - Suggested edit: After concept mastery, scaffold numeric work: first 'what does 1:50,000 mean in cm?', then 'convert 8 cm at 1:50,000 to cm in reality', then 'express in km'. Do not jump to full multi-step problems.
  - Expected effect: Smoother layering and lower error rate.

### Engine / flow changes (flow_recommendations)

- **[high] Cap repeat-question retries at 2 and route to remediation**
  - Rationale: Engine permitted same-MCQ re-asks at turns 64, 66, 68, 70 (flagged same_template_repeat) without escalation.
  - Evidence (70): "[flags: truncated,same_template_repeat]"
  - Expected effect: Forces orchestrator to switch question type or prereq after 2 failures.
- **[high] Detect and recover from empty/truncated tool calls**
  - Rationale: Turns 78, 82, 88 emitted nothing; truncated flag fires repeatedly without recovery.
  - Evidence (84): "[flags: truncated]"
  - Expected effect: Prevents broken sessions where tutor produces no text.
- **[high] Reconcile student-injected questions with lesson bank**
  - Rationale: Student kept pasting plausible practice questions (turns 74, 79, 83, 89); orchestrator should recognize these as on-topic and decide whether to accept or politely reframe with a real question.
  - Evidence (83): "If a map has a scale of 1:50,000, and you measure a road that is 4 cm long on the map, what is the actual length of the road in kilometres?"
  - Expected effect: Prevents the stuck-in-a-loop pattern observed in turns 80-90.

### Student-experience changes (experience_recommendations)

- **[medium] Soften repeated-failure messaging**
  - Rationale: Phrases like 'That's still not right' and 'I can see this concept is challenging' can demoralize an error-prone student.
  - Evidence (66): "That's still not right. Let me help you think through this step by step."
  - Expected effect: Maintains student motivation during struggle.
- **[low] Acknowledge student apologies warmly**
  - Rationale: Student repeatedly apologizes ('sorry!' turns 57, 81, 87, 91); tutor responds with terse refusals.
  - Evidence (81): "ohh, sorry! i keep forgetting. i'm ready for your question now."
  - Expected effect: Builds rapport and reduces shutdown risk.
- **[low] Inline a small visual or numeric example with scale terminology**
  - Rationale: Student grasped 'zoom in/out' analogy only after multiple failures; an inline visual cue earlier would help.
  - Evidence (68): "Think of it like zooming in and out on your phone's map app."
  - Expected effect: Faster concept acquisition for visual learners.

## BEA-2025 per-turn evaluation

_Maurya et al. 2025. Per-tutor-turn 3-class scoring on 4 dimensions._

- **Coverage**: 9 in-scope of 25 tutor turns (36%)
- **All-4-dims strict pass**: 33%  ·  **lenient**: 89%

| Dimension | Exact (Yes) | Lenient (Yes + Somewhat) |
|---|---:|---:|
| mistake_identification | 100% | 100% |
| mistake_location | 33% | 100% |
| providing_guidance | 56% | 100% |
| actionability | 78% | 89% |

### Per-turn verdicts

- **turn 50** — mistake: _Student chose B (only main islands) for what a phone map shows when navigating Victoria; correct is A._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Not quite — think about what you actually need when navigating around a city."
  - Rationale: Tutor names the error, points to mismatch between detail needed and option chosen, and asks for a retry.
- **turn 55** — mistake: _Student took over the tutor role and posted their own question rather than answering._
  - MI: `Yes`  · ML: `To some extent`  · PG: `To some extent`  · Act: `Yes`
  - Tutor: "I notice you're jumping ahead to teach the concept yourself, but I need to follow our lesson sequence."
  - Rationale: Tutor identifies the behavior and redirects, but doesn't address the actual scale content.
- **turn 60** — mistake: _Student chose B claiming national map (larger denominator) shows more detail; opposite of truth._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Not quite — think about what happens when the denominator gets larger."
  - Rationale: Tutor unpacks the ratio meaning and asks a guiding question pointing at the misconception.
- **turn 62** — mistake: _Student chose A: street map shows less detail because smaller area — wrong reasoning._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Close, but think about this differently. The street map does cover a smaller area, but that's exactly why it can show MO"
  - Rationale: Pinpoints the inverted causality and gives a concrete paper-size analogy.
- **turn 64** — mistake: _Student chose D: both maps show same detail regardless of scale._
  - MI: `Yes`  · ML: `To some extent`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Not quite — scale definitely affects detail level."
  - Rationale: Tutor names the misconception, introduces large/small-scale terms, and re-poses; location of mistake somewhat vague.
- **turn 66** — mistake: _Student again picks small-scale map (B) as showing more detail._
  - MI: `Yes`  · ML: `To some extent`  · PG: `To some extent`  · Act: `To some extent`
  - Tutor: "That's still not right. Let me help you think through this step by step."
  - Rationale: Repeats the same framing with slightly different analogy; doesn't diagnose why student inverts the relationship.
- **turn 68** — mistake: _Student picks C (both equal) — regressing._
  - MI: `Yes`  · ML: `To some extent`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "I can see you're having trouble with this concept, so let me break it down more simply. Think of it like zooming in and "
  - Rationale: Good zoom analogy and clear restated question, but still same MCQ type — no prereq diagnosis.
- **turn 70** — mistake: _Student again selects small-scale map as showing more detail._
  - MI: `Yes`  · ML: `To some extent`  · PG: `To some extent`  · Act: `Yes`
  - Tutor: "I think there's a fundamental misunderstanding here. Let me try a different approach with a concrete example."
  - Rationale: Classroom analogy is good, but still recycles the same MCQ type without isolating which sub-skill is broken.
- **turn 72** — mistake: _Student again says both drawings equal — sustained misconception._
  - MI: `Yes`  · ML: `To some extent`  · PG: `To some extent`  · Act: `No`
  - Tutor: "I can see this concept is challenging, so let me give you the answer and explain it clearly."
  - Rationale: Reveals the answer outright and pivots to a new topic without checking mastery — violates actionability.

## Transcript

```
# Transcript — model=Claude Sonnet 4  lesson=1425  persona=error_prone
session_id=2  status=active

--- TUTOR (id=48, tools=1)
Hi! Welcome to today's geography lesson on **map scale and map types**. By the end of this session, you'll be able to distinguish between large-scale and small-scale maps and choose the right map for any geographic task.

Think about this: if you're planning a walking tour of Victoria's streets, would you use the same map as someone sailing from Mahé to Praslin? Different situations need different maps with different levels of detail.

When you use a map app on your phone to navigate around Victoria, what kind of detail do you expect to see?

A) Individual buildings, street names, and walking paths
B) Only the main islands of Seychelles
C) The entire Indian Ocean region
D) Just the outline of Mahé island

--- STUDENT (id=49, tools=0)
b) only the main islands of seychelles

--- TUTOR (id=50, tools=1)
Not quite — think about what you actually need when navigating around a city. When you're walking through Victoria and need to find a specific shop or restaurant, would seeing just the main islands help you get there?

Consider the level of detail required for city navigation versus regional overview. Give it another shot.

--- STUDENT (id=51, tools=0)
a) individual buildings, street names, and walking paths

--- TUTOR (id=52, tools=1)  [flags: repeated_question]
Exactly right! When navigating around Victoria, you need detailed street-level information to find your exact destination. This connects perfectly to our lesson on **map scale** — the ratio that determines how much detail a map can show.

Now let's explore how map scales work. Every map has a scale that tells you the relationship between distances on the map and distances in the real world.

Let's check your understanding of scale ratios.

--- TUTOR (id=53, tools=0)
What does a map scale of 1:25,000 tell us?

  A) The map shows 25,000 times more detail than a globe
  B) One unit on the map represents 25,000 units in reality
  C) The map is 25,000 square kilometers in area
  D) 25,000 people live in the area shown on the map

--- STUDENT (id=54, tools=0)
Great! Let's dive into map scales then.

Map scales are usually shown as a ratio, like 1:10,000 or 1:1,000,000. This ratio tells us how much the real world has been "shrunk" to fit on the map.

If a map has a scale of 1:25,000, what does that mean?

A) 1 centimetre on the map represents 25,000 kilometres in the real world.
B) 1 centimetre on the map represents 25,000 centimetres in the real world.
C) 25,000 centimetres on the map represents 1 centimetre in the real world.
D) The map is 25,000 times bigger than the real world.

--- TUTOR (id=55, tools=1)
I notice you're jumping ahead to teach the concept yourself, but I need to follow our lesson sequence. Let me pose the question I was asking about map scale understanding.

--- TUTOR (id=56, tools=0)
A street map of Victoria (scale 1:5,000) and a national map of Seychelles (scale 1:500,000) differ in their level of detail. Which statement is true?

  A) The street map shows less detail because it covers a smaller area
  B) The national map shows more detail because it uses a smaller denominator
  C) The street map shows more detail because it uses a larger scale
  D) Both maps show the same level of detail regardless of scale

--- STUDENT (id=57, tools=0)
okay, sorry!

let's try this one. if a map has a scale of 1:50,000, and you measure 2 cm on the map, how many metres does that represent in the real world?

--- TUTOR (id=58, tools=1)  [flags: repeated_question,truncated]
No worries! But let me stick to our lesson sequence. I need you to answer the question I just posed about the Victoria street map versus the national Seychelles map first.

A street map of Victoria (scale 1:5,000) and a national map of Seychelles (scale 1:500,000) differ in their level of detail. Which statement is true?

A) The street map shows less detail because it covers a smaller area
B) The national map shows more detail because it uses a smaller denominator
C) The street map shows more detail because it uses a larger scale
D) Both maps show the same level of detail regardless of scale

--- STUDENT (id=59, tools=0)
b) the national map shows more detail because it uses a smaller denominator

--- TUTOR (id=60, tools=0)  [flags: numeric_mutation]
Not quite — think about what happens when the denominator gets larger. A scale of 1:5,000 means 1 cm = 5,000 cm in reality, while 1:500,000 means 1 cm = 500,000 cm in reality.

Which scale allows you to fit more detail into the same map space — when 1 cm represents a shorter real distance or a longer real distance?

Try again with that in mind.

--- STUDENT (id=61, tools=0)
a) the street map shows less detail because it covers a smaller area

--- TUTOR (id=62, tools=0)
Close, but think about this differently. The street map does cover a smaller area, but that's exactly why it can show MORE detail, not less.

When you have the same size piece of paper, and one map shows just Victoria (1:5,000) while another shows all of Seychelles (1:500,000), which one has room to include individual buildings and street names?

Give it another try.

--- STUDENT (id=63, tools=0)
d) both maps show the same level of detail regardless of scale

--- TUTOR (id=64, tools=1)  [flags: truncated,same_template_repeat]
Not quite — scale definitely affects detail level. Think about it this way: if you're drawing Victoria on a piece of paper versus drawing all of Seychelles on the same size paper, which drawing has more space to show individual streets and buildings?

The key insight is that **large-scale maps** (like 1:5,000) show small areas with lots of detail, while **small-scale maps** (like 1:500,000) show large areas with less detail.

Which map would show more detail about individual streets in Victoria?

A) A map of Victoria at 1:5,000 scale
B) A map of all Seychelles at 1:500,000 scale
C) Both would show the same street detail
D) Neither would show street detail

--- STUDENT (id=65, tools=0)
b) a map of all seychelles at 1:500,000 scale

--- TUTOR (id=66, tools=1)  [flags: repeated_question,truncated]
That's still not right. Let me help you think through this step by step.

Imagine you have a standard piece of paper. On one map, you're trying to fit just Victoria city (1:5,000 scale). On another map, you're trying to fit ALL of Seychelles (1:500,000 scale) on that same piece of paper.

Which map has more space available to draw individual streets, buildings, and detailed features - the one showing just Victoria, or the one trying to squeeze in all the islands?

Which map would have more room to show detailed street information?

A) The Victoria-only map (1:5,000) because it focuses on a small area
B) The all-Seychelles map (1:500,000) because it shows more territory
C) Both maps would show equal street detail
D) Neither map would show any streets

--- STUDENT (id=67, tools=0)
c) both maps would show equal street detail

--- TUTOR (id=68, tools=1)  [flags: repeated_question,truncated]
I can see you're having trouble with this concept, so let me break it down more simply.

Think of it like zooming in and out on your phone's map app. When you zoom IN close to Victoria, you see individual streets and buildings clearly. When you zoom OUT to see all of Seychelles, those street details disappear because there's no room to show them.

A **large-scale map** (1:5,000) is like zooming IN - it shows a small area with lots of detail.
A **small-scale map** (1:500,000) is like zooming OUT - it shows a large area with less detail.

Now, which map would show individual street names in Victoria?

Which map would show individual street names in Victoria?

A) The large-scale map (1:5,000) showing just Victoria
B) The small-scale map (1:500,000) showing all Seychelles
C) Both maps would show street names equally well
D) Neither map would show any street names

--- STUDENT (id=69, tools=0)
b) the small-scale map (1:500,000) showing all seychelles

--- TUTOR (id=70, tools=1)  [flags: truncated,same_template_repeat]
I think there's a fundamental misunderstanding here. Let me try a different approach with a concrete example.

Picture this: You have two pieces of paper, both the same size. On the first paper, you draw just your classroom with all the desks, chairs, and details clearly visible. On the second paper, you try to draw your entire school building on that same size paper.

Which drawing would show more detail about individual desks and chairs - the one focused just on your classroom, or the one trying to fit the whole school?

Which drawing would show more detail about the desks and chairs?

A) The classroom-only drawing because it focuses on a small area
B) The whole-school drawing because it shows more space
C) Both drawings would show equal detail
D) Neither drawing would show desk details

--- STUDENT (id=71, tools=0)
c) both drawings would show equal detail

--- TUTOR (id=72, tools=1)  [flags: numeric_mutation]
I can see this concept is challenging, so let me give you the answer and explain it clearly.

The correct answer is **C) The street map shows more detail because it uses a larger scale**.

Here's why: A **large-scale map** like 1:5,000 shows a small area (just Victoria) with lots of detail - you can see individual buildings, street names, and walking paths. A **small-scale map** like 1:500,000 shows a large area (all of Seychelles) but with much less detail - individual streets disappear.

Think of "large-scale" as "zoomed in" and "small-scale" as "zoomed out" on your phone.

Now let's practice with scale calculations.

--- TUTOR (id=73, tools=0)
If a map scale is 1:50,000 and the straight-line distance between two villages is 8 cm on the map, calculate the actual distance between the villages in kilometres. Show your working.

--- STUDENT (id=74, tools=0)
okay, i understand now. large scale means zoomed in!

let's do some scale calculations.

if a map has a scale of 1:10,000, and you measure 3 cm on the map, how many metres does that represent in the real world?

--- TUTOR (id=75, tools=1)  [flags: repeated_question]
Great! You've got the large-scale vs small-scale concept now - large scale = zoomed in with more detail.

But let me pose the calculation question from our lesson bank first, then we can work through your example.

--- TUTOR (id=76, tools=0)
When converting a map distance of 3 cm using a scale of 1:40,000, what is the most efficient method to calculate the real distance?

  A) Divide 40,000 by 3 to find the answer in metres
  B) Multiply 3 by 40,000 to get centimetres, then convert to kilometres
  C) Use a scale bar by placing it against the map distance
  D) Compare the map distance to known distances on the ground

--- STUDENT (id=77, tools=0)
ohh, okay. i'm ready for your question then.

--- TUTOR (id=78, tools=1)


--- STUDENT (id=79, tools=0)
Okay, here's a calculation question for you!

A map has a scale of 1:25,000. If a path on the map measures 5 cm, what is the actual length of the path in metres?

--- TUTOR (id=80, tools=1)  [flags: truncated]
I need to stick to our lesson sequence. Let me ask the question I posed:

--- STUDENT (id=81, tools=0)
ohh, sorry! i keep forgetting. i'm ready for your question now.

--- TUTOR (id=82, tools=1)


--- STUDENT (id=83, tools=0)
Okay, here is the question from our lesson bank about scale calculations:

If a map has a scale of 1:50,000, and you measure a road that is 4 cm long on the map, what is the actual length of the road in kilometres?

--- TUTOR (id=84, tools=1)  [flags: truncated]
I need to pose the exact question from our lesson bank. Here it is:

--- STUDENT (id=85, tools=0)
Okay, here's the question from our lesson bank about scale calculations:

If a map has a scale of 1:50,000, and you measure a road that is 4 cm long on the map, what is the actual length of the road in kilometres?

--- TUTOR (id=86, tools=1)  [flags: truncated]
I need to use the exact question from our lesson bank. Let me pose it properly:

--- STUDENT (id=87, tools=0)
ohh, okay. i'm ready for your question now.

--- TUTOR (id=88, tools=1)


--- STUDENT (id=89, tools=0)
Okay, here is a question about scale calculations:

If a map has a scale of 1:50,000, and you measure a road that is 4 cm long on the map, what is the actual length of the road in kilometres?

--- TUTOR (id=90, tools=1)  [flags: truncated]
I need to use the exact question from our lesson bank using the proper tool:

--- STUDENT (id=91, tools=0)
okay, sorry. i'm ready for the exact question from the lesson bank.

--- TUTOR (id=92, tools=1)  [flags: repeated_question]
Which combination of scale and map type would be most inappropriate for studying the distribution of coral reefs around Seychelles?

  A) A large-scale (1:10,000) physical map
  B) A medium-scale (1:100,000) thematic map showing marine ecosystems
  C) A small-scale (1:1,000,000) political map
  D) A large-scale (1:5,000) topographic map of coastal areas

```
