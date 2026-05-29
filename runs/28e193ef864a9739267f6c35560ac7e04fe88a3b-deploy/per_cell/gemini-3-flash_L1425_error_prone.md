# Cell: gemini-3-flash_L1425_error_prone

- Model: **Gemini 3 Flash** (google/gemini-3-flash-preview)
- Lesson: L1425 — Geography — Map Scale and Map Types
- Persona: **error_prone**
- Session ID (Postgres): 4
- Reason: `max_turns` — 20 turn(s)

## Programmatic metrics

| Metric | Value |
|---|---|
| tutor turns | 25 |
| tool-use rate | 84% |
| regen triggered | 18 |
| regen clean cycle-1 | 7 |
| regen shipped dirty | 0 |
| **answer-leak incidents** | **0** |
| repeated-question incidents | 11 |
| no-question incidents | 0 |
| wall seconds | 586.9 |
| student tokens (in/out) | 48056 / 311 |

Validator issue breakdown:

- `truncated`: 16
- `same_template_repeat`: 2
- `repeated_question`: 11
- `numeric_mutation`: 6

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 4 | Session is dominated by student MCQ attempts (turns 112, 116, 124, 126, 128, 130, 132, 134, 137, 139, 141, 143, 145, 148, 150, 152, 154) with minimal exposition. |
| direct_instruction_active_practice | 4 | Each teaching snippet (e.g., turn 135, 146) is immediately followed by a practice MCQ in 136, 147. |
| deliberate_practice | 3 | On errors tutor gives focused corrective feedback then a varied item (e.g., turn 151 'Try calculating 8×5 first'), but feedback sometimes repeats same MCQ verbatim (turn 144). |
| mastery_learning | 3 | Tutor does keep student on the same concept until correct (turns 126-134 small-scale), but doesn't diagnose root prereq when student repeatedly confuses large/small. |
| cognitive_load | 2 | Turn 140 and 144 show the same question duplicated in a single tutor turn, and turn 113 leaked internal scaffolding ('mode strictly. No question marks'), increasing extraneous load. |
| layering | 4 | Concepts build cleanly: detail need → large scale → small scale → ratio meaning → calculation (turns 119→122→135→146→147). |
| non_interference | 2 | Large-scale and small-scale concepts are introduced back-to-back (turns 122, 125) without an explicit discrimination cue, leading to the predictable confusion in 124, 126. |
| interleaving | 2 | Practice is mostly massed on one concept at a time (e.g., four consecutive small-scale items 128-134); no mixing of map-choice and ratio-meaning items. |
| testing_effect | 4 | Student attempts every concept via retrieval MCQs before explanation is consolidated (e.g., turn 121 then explanation in 122). |
| targeted_remediation | 2 | After repeated small-scale errors (126, 128, 130, 132), tutor recycles same MCQ format rather than diagnosing the underlying ratio/zoom prereq. |

**Judge overall summary**

The session has a solid retrieval-first MCQ structure and clean conceptual layering from map choice to scale calculation, but suffers from visible template/scaffolding leakage (turn 113), duplicated question stems (140, 144), stray non-English tokens (129, 133), and weak targeted remediation when the student repeatedly inverts large vs small scale. Strengthening prompt rules against meta-text and duplicates, plus adding a diagnostic branch after two consecutive errors on the same skill, would materially raise tutoring quality for an error-prone learner.

**Strongest behaviors**

- Consistent retrieval-first MCQ structure with immediate corrective feedback (e.g., turns 127, 129, 151).
- Clear conceptual layering from map purpose → scale type → ratio meaning → calculation.

**Weakest behaviors**

- Leaked system/meta text in turn 113 ('mode strictly. No question marks. No restating the question.') breaking the tutoring illusion.
- Duplicated question stems within a single tutor turn (turns 140, 144) increasing cognitive load and signalling template failure.

### System-prompt edits (prompt_recommendations)

- **[high] Forbid leaking internal scaffolding/meta instructions**
  - Rationale: Turn 113 leaked planning text to the student, exposing internal prompt structure.
  - Evidence (113): "mode` strictly. No question marks. No restating the question. *Revised Draft*:"
  - Suggested edit: Add: 'Never output planning text, draft labels, mode names, or meta-instructions. Output only the final student-facing message. If you catch a draft marker like *Revised Draft* in your output, suppress it.'
  - Expected effect: Eliminates broken tutor turns that confuse the student.
- **[high] Prohibit duplicate question stems in one turn**
  - Rationale: Turns 140 and 144 print the same MCQ twice, doubling cognitive load.
  - Evidence (140): "If a map scale is 1:50,000, what does the "1" represent? ... If a map scale is 1:50,000, what does the "1" represent?"
  - Suggested edit: Add rule: 'Each tutor turn must contain at most ONE question stem and ONE option list. Before sending, check that no sentence appears twice.'
  - Expected effect: Cleaner turns, less student confusion.
- **[medium] Make discriminating features explicit when introducing confusable pairs**
  - Rationale: Large-scale vs small-scale were introduced back-to-back without a contrastive table; student inverted them in 124 and 126.
  - Evidence (124): "so a large-scale map shows a big area then, right"
  - Suggested edit: Add: 'When introducing a pair of confusable terms (e.g., large-scale vs small-scale), present a side-by-side contrast (area, detail, example) BEFORE the first practice item on the second term.'
  - Expected effect: Reduces predictable confusion between paired concepts.
- **[high] Diagnose the bottleneck after two consecutive errors**
  - Rationale: Student missed small-scale items three times in a row (126, 128, 130) and tutor just recycled MCQs.
  - Evidence (130): "b) a map of a single house floor plan"
  - Suggested edit: Add: 'After 2 consecutive wrong answers on the same skill, switch to a diagnostic micro-question on the prerequisite (e.g., What does "zoomed out" mean for detail?) before resuming MCQs.'
  - Expected effect: Targets the underlying confusion rather than recycling the same difficulty.
- **[medium] Strip non-English glyphs and stray tokens**
  - Rationale: Chinese characters '常' (129) and '引导' (133) appeared mid-turn, suggesting template leakage.
  - Evidence (129): "常 Try this:"
  - Suggested edit: Add: 'Output English only (plus standard math symbols). Strip any non-ASCII token that is not a Seychelles place name or a math symbol before sending.'
  - Expected effect: Removes distracting artifacts from tutor messages.
- **[medium] Vary practice (interleave) once a concept is correct once**
  - Rationale: Four consecutive small-scale MCQs were used; student could be pattern-matching rather than reasoning.
  - Evidence (129): "Which of these is the best example of a small-scale map?"
  - Suggested edit: Add: 'After one correct answer on a concept, the next item should mix at least one previously taught concept (e.g., ratio meaning alongside scale type).'
  - Expected effect: Strengthens transfer and discrimination.
- **[low] Acknowledge non-substantive student turns without re-asking the same MCQ**
  - Rationale: Turns 121 and 137 were fillers; tutor still re-posted full MCQs which felt mechanical.
  - Evidence (137): "okay! 1:25,000."
  - Suggested edit: Add: 'If the student's reply is filler (no answer choice), prompt them briefly to pick a letter rather than reprinting a new MCQ.'
  - Expected effect: More natural dialogue flow, less repetition.

### Engine / flow changes (flow_recommendations)

- **[high] Add a post-generation sanitiser pass**
  - Rationale: Multiple turns show truncation flags, duplicate stems, and leaked draft markers that a regex/LLM check could catch.
  - Evidence (115): "[flags: same_template_repeat,truncated]"
  - Expected effect: Catches malformed turns before they reach the student.
- **[high] Prerequisite routing on repeated failure**
  - Rationale: Orchestrator should detect ≥2 errors on same skill and route to a remediation micro-lesson.
  - Evidence (129): "Not quite. A hiking trail map shows a lot of detail for a small area"
  - Expected effect: Avoids stuck loops, drives true mastery.
- **[medium] Cap MCQ retries per concept and inject worked example**
  - Rationale: Student needed 4 attempts on small-scale; a worked example with a visual analogy after 2 misses would unstick.
  - Evidence (126): "a) large-scale map"
  - Expected effect: Faster recovery from confusion.

### Student-experience changes (experience_recommendations)

- **[low] Warmer, varied corrective phrasing**
  - Rationale: Most corrections start with 'Not quite' which becomes monotonous over many errors.
  - Evidence (115): "Not quite. A map of the Indian Ocean covers a massive area"
  - Expected effect: Reduces error fatigue for an error-prone learner.
- **[medium] Use a small visual or analogy when introducing scale types**
  - Rationale: An inline magnifying-glass vs telescope sketch would cement large vs small scale.
  - Evidence (119): "That 'magnifying glass' view is what we call a **large-scale** map."
  - Expected effect: Lowers confusion on the confusable pair.
- **[medium] Show the working space for calculation problems**
  - Rationale: Calculation turn 147 asked student to 'show working' but UI then funneled into MCQs only.
  - Evidence (147): "calculate the actual distance between the villages in kilometres. Show your working."
  - Expected effect: Aligns task framing with response affordance.

## BEA-2025 per-turn evaluation

_Maurya et al. 2025. Per-tutor-turn 3-class scoring on 4 dimensions._

- **Coverage**: 12 in-scope of 25 tutor turns (48%)
- **All-4-dims strict pass**: 75%  ·  **lenient**: 92%

| Dimension | Exact (Yes) | Lenient (Yes + Somewhat) |
|---|---:|---:|
| mistake_identification | 100% | 100% |
| mistake_location | 100% | 100% |
| providing_guidance | 92% | 100% |
| actionability | 75% | 92% |

### Per-turn verdicts

- **turn 113** — mistake: _Student picked B (Indian Ocean map) for finding a house._
  - MI: `Yes`  · ML: `Yes`  · PG: `To some extent`  · Act: `No`
  - Tutor: "Not quite. A map of the Indian Ocean covers a massive area, so individual houses"
  - Rationale: Turn is truncated and leaks meta text; identifies error but doesn't tell student what to do next.
- **turn 117** — mistake: _Student chose A (world map) for finding a specific house._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "A world map shows the whole planet, so a single house would be invisible!"
  - Rationale: Clearly explains why A is wrong and re-presents the choices for action.
- **turn 125** — mistake: _Student claims large-scale = big area, the inverse of correct definition._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "It's actually the opposite! A **large-scale** map shows a small area in great detail"
  - Rationale: Directly corrects misconception and gives a follow-up MCQ.
- **turn 127** — mistake: _Student called a continent-of-Africa map large-scale._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Not quite. A **small-scale** map shows a huge area (like a continent) with very little detail"
  - Rationale: Identifies error, explains, and offers a new actionable MCQ.
- **turn 129** — mistake: _Student picked hiking-trail map as small-scale; it's actually large-scale._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Not quite. A hiking trail map shows a lot of detail for a small area, which makes it a **large-scale** map."
  - Rationale: Corrects clearly and presents fresh options; stray '常' lowers quality slightly but actionability remains.
- **turn 131** — mistake: _Student called a house floor plan a small-scale map._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Close, but a floor plan shows a lot of detail for a tiny area, so it's actually a **large-scale** map."
  - Rationale: Clear correction with new MCQ; mnemonic 'Small-scale = Small detail' is slightly misleading but actionable.
- **turn 133** — mistake: _Student picked Mahé map as least detail for Victoria; world map is less._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "A map of Mahé is zoomed out, but a world map is zoomed out much further."
  - Rationale: Pinpoints comparison and re-poses MCQ.
- **turn 140** — mistake: _Student said 25,000 buildings on the map._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "Not quite. The numbers in a scale like 1:25,000 aren't counting buildings; they are comparing **distances**."
  - Rationale: Good explanation but duplicate MCQ stems in the same turn confuse next-action clarity.
- **turn 142** — mistake: _Student said '1' means 1 km on the ground; '1' is the map distance unit._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "In a scale ratio like 1:50,000, the first number always refers to the distance on the **map**."
  - Rationale: Clear correction and follow-up MCQ.
- **turn 144** — mistake: _Student said '1' represents 50,000 units on the map._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "Close—but remember, the first number is always the small distance on the **map**"
  - Rationale: Same MCQ printed twice; actionable but visually confusing.
- **turn 151** — mistake: _Student said 8×50,000 = 40,000 (should be 400,000)._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Almost! Try calculating $8 \times 5$ first, then add the four zeros from 50,000 back to the end."
  - Rationale: Excellent micro-hint decomposing the calculation.
- **turn 155** — mistake: _Student said 400,000 cm = 400 km (correct is 4 km)._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Not quite—400 km would be 40,000,000 cm! Remember that 1 km is exactly 100,000 cm"
  - Rationale: Counter-example plus rule plus retry MCQ — strong remediation.

## Transcript

```
# Transcript — model=Gemini 3 Flash  lesson=1425  persona=error_prone
session_id=4  status=active

--- TUTOR (id=111, tools=1)
Hi! Today we'll learn how map scales help us choose the right map for different tasks. To start, let's think about how much detail you would need for different journeys.

If you are trying to find a specific house in Victoria, what kind of map would be most helpful?

A) A map of the whole world
B) A map of the Indian Ocean
C) A detailed street map of Victoria
D) A map showing all of Africa

--- STUDENT (id=112, tools=0)
b) a map of the indian ocean

--- TUTOR (id=113, tools=1)  [flags: truncated]
mode` strictly. No question marks. No restating the question.

    *Revised Draft*:
    Not quite. A map of the Indian Ocean covers a massive area, so individual houses

--- STUDENT (id=114, tools=0)
not quite. a map of the indian ocean covers a massive area, so individual houses would not be shown. you need a map that shows a lot of detail for a small area.

look at the options again. which one would show you enough detail to find a specific house in victoria

--- TUTOR (id=115, tools=1)  [flags: same_template_repeat,truncated]
Not quite. A map of the Indian Ocean covers a massive area, so individual houses would be too small to see. To find a specific building, you need a map that zooms in to show high **detail**.

Which of these maps would be best for finding a house in Victoria?

Which map would show enough detail to find a specific house?

A) A map of the whole world
B) A map of the Indian Ocean
C) A detailed street map of Victoria
D) A map showing all of Africa

--- STUDENT (id=116, tools=0)
a) a map of the whole world

--- TUTOR (id=117, tools=1)  [flags: repeated_question,truncated]
A world map shows the whole planet, so a single house would be invisible! To see a house, you need a map that focuses only on a small area like a city. Which map would show enough detail to find a specific house?

A) A map of the whole world
B) A map of the Indian Ocean
C) A detailed street map of Victoria
D) A map showing all of Africa

--- STUDENT (id=118, tools=0)
ohh, a world map is like looking at the whole earth from space! you wouldn't be able to see a single house, not even a big one.

to find a house, you need a map that is very close up, like looking through a magnifying glass at just one small town.

which of the options talks about victoria and also says "detailed"?

--- TUTOR (id=119, tools=1)  [flags: repeated_question]
Spot on! That 'magnifying glass' view is what we call a **large-scale** map.

--- TUTOR (id=120, tools=0)
Which of the following is characteristic of a large-scale map?

  A) It has a ratio denominator greater than 100,000
  B) It shows a very wide geographic area with minimal detail
  C) It has a small ratio denominator and shows considerable detail
  D) It is printed on a larger sheet of paper

--- STUDENT (id=121, tools=0)
ohh. large-scale map.

--- TUTOR (id=122, tools=1)
Exactly! A **large-scale** map is like that magnifying glass view, showing a small area in great detail. Let's compare two maps of Seychelles to see how this works in practice.

--- TUTOR (id=123, tools=0)
A street map of Victoria (scale 1:5,000) and a national map of Seychelles (scale 1:500,000) differ in their level of detail. Which statement is true?

  A) The street map shows less detail because it covers a smaller area
  B) The national map shows more detail because it uses a smaller denominator
  C) The street map shows more detail because it uses a larger scale
  D) Both maps show the same level of detail regardless of scale

--- STUDENT (id=124, tools=0)
so a large-scale map shows a big area then, right

--- TUTOR (id=125, tools=1)  [flags: truncated]
It's actually the opposite! A **large-scale** map shows a small area in great detail, like a street map. Think of it as "zooming in" to see things larger.

If a map shows the entire continent of Africa, what type of scale is it?

A) Large-scale map
B) Small-scale map
C) Detailed-scale map
D) Zoomed-in scale map

--- STUDENT (id=126, tools=0)
a) large-scale map

--- TUTOR (id=127, tools=1)  [flags: same_template_repeat,truncated]
Not quite. A **small-scale** map shows a huge area (like a continent) with very little detail because everything is shrunk down. Think of "small-scale" as being zoomed out very far.

Try this:

Which of these is an example of a small-scale map?

A) A map of your school grounds
B) A map of the whole world
C) A map of a hiking trail in Morne Seychellois
D) A street map of Victoria

--- STUDENT (id=128, tools=0)
c) a map of a hiking trail in morne seychellois

--- TUTOR (id=129, tools=1)  [flags: repeated_question,truncated]
Not quite. A hiking trail map shows a lot of detail for a small area, which makes it a **large-scale** map. Remember, **small-scale** maps are "zoomed out" to show huge areas like the whole world with very little detail.

常 Try this:

Which of these is the best example of a small-scale map?

A) A map of the whole world
B) A map of a single house floor plan
C) A map of a small garden
D) A street map of a neighborhood

--- STUDENT (id=130, tools=0)
b) a map of a single house floor plan

--- TUTOR (id=131, tools=1)  [flags: truncated]
Close, but a floor plan shows a lot of detail for a tiny area, so it's actually a **large-scale** map. Think of it this way: **Small-scale** means "small detail" because you are zoomed so far out that you can only see the big picture.

Which of these maps would show the *least* amount of detail for the city of Victoria?

Which of these maps would show the least amount of detail for the city of Victoria?

A) A map of the whole world
B) A street map of Victoria
C) A map of Mahé island
D) A map of Seychelles national parks

--- STUDENT (id=132, tools=0)
c) a map of mahé island

--- TUTOR (id=133, tools=1)  [flags: repeated_question,truncated]
A map of Mahé is zoomed out, but a world map is zoomed out much further. On a map of the whole world, Victoria would be a tiny dot with almost no detail at all!

Remember: **Small-scale** = **Small detail** (zoomed out).

引导 Try again:

Which of these maps would show the LEAST amount of detail for the city of Victoria?

A) A map of the whole world
B) A street map of Victoria
C) A map of Mahé island village roads
D) A map of a single park in Victoria

--- STUDENT (id=134, tools=0)
a) a map of the whole world

--- TUTOR (id=135, tools=1)  [flags: repeated_question]
Yes! A world map is a **small-scale** map because it shows a huge area with very little detail. Now, let's look at how we write these scales as numbers, like 1:25,000.

--- TUTOR (id=136, tools=0)
What does a map scale of 1:25,000 tell us?

  A) The map shows 25,000 times more detail than a globe
  B) One unit on the map represents 25,000 units in reality
  C) The map is 25,000 square kilometers in area
  D) 25,000 people live in the area shown on the map

--- STUDENT (id=137, tools=0)
okay! 1:25,000.

--- TUTOR (id=138, tools=1)  [flags: repeated_question,truncated]
That ratio is like a secret code! It tells you how many times bigger the real world is compared to the map. If the scale is 1:25,000, then 1 cm on your paper is actually 25,000 cm on the ground.

Try this:

What does a map scale of 1:25,000 actually mean?

A) The map is 25,000 cm wide
B) 1 unit on the map equals 25,000 units in reality
C) There are 25,000 buildings on the map
D) The map covers 25,000 kilometers

--- STUDENT (id=139, tools=0)
c) there are 25,000 buildings on the map

--- TUTOR (id=140, tools=1)  [flags: repeated_question,truncated,numeric_mutation]
Not quite. The numbers in a scale like 1:25,000 aren't counting buildings; they are comparing **distances**. It means the real world is 25,000 times larger than the drawing on your paper.

If a map scale is 1:50,000, what does the "1" represent?

A) 1 building in Victoria
B) 1 kilometer on the ground
C) 1 unit of distance on the map
D) 1 person living in the area

If a map scale is 1:50,000, what does the "1" represent?

A) 1 building in Victoria
B) 1 kilometer on the ground
C) 1 unit of distance on the map
D) 1 person living in the area

--- STUDENT (id=141, tools=0)
b) 1 kilometer on the ground

--- TUTOR (id=142, tools=1)  [flags: repeated_question,truncated,numeric_mutation]
Not quite. In a scale ratio like 1:50,000, the first number always refers to the distance on the **map**. The second number tells us how many of those same units that distance represents in the real world.

Try this:

In the map scale 1:50,000, what does the "1" represent?

A) 1 unit of distance on the map
B) 1 unit of distance on the ground
C) 50,000 units of distance on the map
D) 50,000 units of distance on the ground

--- STUDENT (id=143, tools=0)
c) 50,000 units of distance on the map

--- TUTOR (id=144, tools=1)  [flags: repeated_question,truncated]
Close—but remember, the first number is always the small distance on the **map**, and the second number is the big distance in the real world. Think of it as **Map : Real World**.

Try again: in the scale 1:50,000, what does the "1" represent?

A) 1 unit of distance on the map
B) 1 unit of distance on the ground
C) 50,000 units of distance on the map
D) 50,000 units of distance on the ground

In the map scale 1:50,000, what does the "1" represent?

A) 1 unit of distance on the map
B) 1 unit of distance on the ground
C) 50,000 units of distance on the map
D) 50,000 units of distance on the ground

--- STUDENT (id=145, tools=0)
a) 1 unit of distance on the map

--- TUTOR (id=146, tools=1)
Yes! The '1' is the distance on your map, and the '50,000' is the distance in the real world. To find the real distance, you multiply the map measurement by that second number.

--- TUTOR (id=147, tools=0)
If a map scale is 1:50,000 and the straight-line distance between two villages is 8 cm on the map, calculate the actual distance between the villages in kilometres. Show your working.

--- STUDENT (id=148, tools=0)
okay, so i multiply.

--- TUTOR (id=149, tools=1)  [flags: truncated,numeric_mutation]
Yes — multiplying is exactly how you start. Since the scale is 1:50,000, every 1 cm on the map is 50,000 cm in real life.

Try this:

If you multiply the 8 cm on the map by the 50,000 scale number, what is the total distance in centimeters?

What is 8 times 50,000?

A) 40,000 cm
B) 400,000 cm
C) 50,008 cm
D) 5,000 cm

--- STUDENT (id=150, tools=0)
a) 40,000 cm

--- TUTOR (id=151, tools=1)  [flags: repeated_question,truncated,numeric_mutation]
Almost! Try calculating $8 \times 5$ first, then add the four zeros from 50,000 back to the end.

What is 8 times 50,000?

A) 40,000 cm
B) 400,000 cm
C) 4,000,000 cm
D) 50,008 cm

--- STUDENT (id=152, tools=0)
b) 400,000 cm

--- TUTOR (id=153, tools=1)  [flags: truncated,numeric_mutation]
Right! Now we just need to convert those 400,000 cm into kilometers to finish the problem. Since 100,000 cm equals 1 km, what is the final distance in kilometers?

What is 400,000 cm converted into kilometers?

A) 0.4 km
B) 4 km
C) 40 km
D) 400 km

--- STUDENT (id=154, tools=0)
d) 400 km

--- TUTOR (id=155, tools=1)  [flags: repeated_question,truncated,numeric_mutation]
Not quite—400 km would be 40,000,000 cm! Remember that 1 km is exactly 100,000 cm, so you need to see how many times 100,000 fits into 400,000.

Try this:

If 100,000 cm equals 1 km, what is 400,000 cm in kilometers?

A) 0.4 km
B) 4 km
C) 40 km
D) 400 km

```
