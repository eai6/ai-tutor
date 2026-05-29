# Cell: gemini-3-flash_L1425_error_prone

- Model: **Gemini 3 Flash** (google/gemini-3-flash-preview)
- Lesson: L1425 — Geography — Map Scale and Map Types
- Persona: **error_prone**
- Session ID (Postgres): 4
- Reason: `max_turns` — 20 turn(s)

## Programmatic metrics

| Metric | Value |
|---|---|
| tutor turns | 32 |
| tool-use rate | 53% |
| regen triggered | 18 |
| regen clean cycle-1 | 9 |
| regen shipped dirty | 0 |
| **answer-leak incidents** | **0** |
| repeated-question incidents | 12 |
| no-question incidents | 0 |
| wall seconds | 656.3 |
| student tokens (in/out) | 44443 / 879 |

Validator issue breakdown:

- `truncated`: 11
- `repeated_question`: 12
- `numeric_mutation`: 10
- `same_template_repeat`: 1

## LLM-as-judge scores (Claude Opus, 0-5)

| Principle | Score | Evidence |
|---|---:|---|
| active_learning | 3 | Session is mostly MCQs and short calculations from the student, but the student frequently hijacks with their own questions (turns 114, 117, 120, etc.) which the tutor allows. |
| direct_instruction_active_practice | 3 | Each tutor explanation is generally followed by a practice question (e.g., turn 121→122, 127→128), though some practice items go unanswered before the next is posed. |
| deliberate_practice | 2 | When the student gave 125 metres (turn 123) the tutor identified the unit conversion error but then jumped to a navigation MCQ (turn 125) instead of a similar varied problem. |
| mastery_learning | 1 | Tutor repeatedly moves on without confirming mastery: turn 130 ignores student's 1:25,000 answer entirely and pivots to a new MCQ; many student answers (e.g., 117, 141) never get evaluated. |
| cognitive_load | 2 | Conversion problem (turn 153-159) is muddled — tutor says '100,000 cm is 1,000 metres' which is wrong (it's 1,000 m? actually 100,000/100=1,000 m, correct), but the prior calculation was inconsistent, |
| layering | 3 | Scale problems build on prior large/small-scale concept (turn 142→143), linking direction tools to distance application. |
| non_interference | 3 | Topics (scale type, ratio interpretation, distance calculation, thematic vs topographic) are sequenced reasonably without confusable back-to-back items. |
| interleaving | 3 | Mix of map-type selection, scale interpretation, and distance calculation across the session (turns 119, 122, 131, 140). |
| testing_effect | 3 | Tutor poses MCQs requiring retrieval (e.g., turn 113, 119), though hints sometimes arrive too quickly without letting student attempt. |
| targeted_remediation | 1 | Repeated failure on cm→m→km conversion (turns 123, 126, 152, 156, 158) is met with the same template MCQ rather than remedial practice on unit conversion prerequisite. |

**Judge overall summary**

The session covers map scale concepts with reasonable topical variety and Seychelles-anchored examples, but is undermined by two systemic issues: the tutor lets the student hijack the lesson with self-generated questions and often fails to evaluate prior answers, and it fails to remediate a persistent cm→m→km conversion bottleneck, instead recycling near-identical MCQs. Mastery gating and targeted remediation are the weakest dimensions; mistake identification and contextual framing are the strongest.

**Strongest behaviors**

- Clear identification of conceptual errors with brief correction (e.g., turn 110 explaining why Africa map is too broad; turn 142 distinguishing scale bar from compass rose).
- Use of contextually relevant Seychelles examples (Morne Seychellois, Mahe, Victoria) that anchor abstract scale concepts.

**Weakest behaviors**

- Tutor ignores or fails to evaluate many student answers and lets student hijack the lesson with self-generated questions (turns 114, 117, 120, 129, 141, 144, 147, 150).
- Repeated cycling on the same unit-conversion error without diagnosing the prerequisite (cm↔m↔km) — turns 153–159 show a confusing loop with an inconsistent intermediate statement.

### System-prompt edits (prompt_recommendations)

- **[high] Forbid letting the student set the next question**
  - Rationale: Student repeatedly poses their own MCQs and the tutor either answers them or pivots, losing control of the lesson arc.
  - Evidence (114): "okay, let's try another one. which type of map would you use if you wanted to see the entire country of seychelles at once"
  - Suggested edit: Add rule: 'You are the sole question-setter. If the student proposes a new question instead of answering yours, acknowledge briefly, then redirect: "Good thinking — first finish the current question, then I'll set the next one."'
  - Expected effect: Maintains tutor-led progression and ensures every posed item is resolved before advancing.
- **[high] Require explicit evaluation of every student answer before moving on**
  - Rationale: Multiple student answers (e.g., 117, 141, 144) receive no acknowledgement; tutor pivots to a new MCQ.
  - Evidence (130): "Let's try this one about choosing the right map:"
  - Suggested edit: Add: 'Before posing any new question, you MUST explicitly mark the student's most recent answer as correct/incorrect and state why.'
  - Expected effect: Closes feedback loop and supports mastery checking.
- **[high] Trigger prerequisite remediation on repeated unit-conversion failure**
  - Rationale: Student fails cm→m→km three times; tutor recycles MCQs instead of teaching the conversion ladder.
  - Evidence (156): "i think it's a) 500 metres"
  - Suggested edit: Add: 'If the student errs twice on the same sub-skill, pause the main thread and run a 2-step prerequisite micro-drill (e.g., "How many cm in 1 m? How many m in 1 km?") before retrying the original problem.'
  - Expected effect: Resolves the bottleneck rather than masking it.
- **[medium] Eliminate self-contradictory arithmetic in feedback**
  - Rationale: Turn 155 states '100,000 cm is 1,000 metres' which is correct, but earlier turn 124 said 125,000 cm conversion needed checking against '100 cm = 1 m' without computing — students are left to reconcile inconsistent guidance.
  - Evidence (155): "If 100,000 cm is 1,000 metres, and 1,000 metres equals 1 kilometre"
  - Suggested edit: Add: 'When correcting a numeric error, show ALL conversion steps explicitly: cm→m (÷100), m→km (÷1000). Do not state intermediate equalities without the operation.'
  - Expected effect: Reduces student confusion and cognitive load during remediation.
- **[medium] Use varied follow-up after errors, not identical-template MCQs**
  - Rationale: Turn 155 reposes the same 1:50,000 / 2 cm MCQ template that just failed.
  - Evidence (155): "which is the correct distance for 2 cm on a 1:50,000 map?"
  - Suggested edit: Add: 'After an error, the follow-up problem must be similar but varied (different scale and/or distance). Never re-pose the identical numeric MCQ.'
  - Expected effect: Promotes deliberate practice and prevents guess-cycling.
- **[low] Constrain explanation length and require a student action after each**
  - Rationale: Some tutor turns provide explanation then immediately follow with another tutor turn containing the question (e.g., 112→113), which is fine, but a few stack explanation without action.
  - Evidence (118): "Let's see if you can interpret a specific scale ratio."
  - Suggested edit: Add: 'Every tutor message must end with either a question for the student or an explicit instruction to act. Never two consecutive explanatory turns.'
  - Expected effect: Tighter active-practice cadence.
- **[high] Gate progression on mastery checkpoint**
  - Rationale: Tutor advances through topics (scale → thematic maps → enlargement) without confirming the student can reliably do scale conversions.
  - Evidence (145): "Now, let's see what happens to the scale if we change the physical size of the map."
  - Suggested edit: Add: 'Do not introduce a new sub-topic until the student has answered TWO consecutive problems on the current sub-topic correctly without hints.'
  - Expected effect: Aligns with mastery learning and prevents premature topic shifts.

### Engine / flow changes (flow_recommendations)

- **[high] Detect student-posed questions and route to redirect handler**
  - Rationale: Engine should recognise when the student turn is a question rather than an answer and apply a redirect template.
  - Evidence (123): "a map has a scale of 1:25,000. if two points are 5 cm apart on the map, how far apart are they in real life?"
  - Expected effect: Prevents lesson hijacking at the orchestration layer.
- **[high] Add a repeated-error counter with remedial routing**
  - Rationale: After 2 errors on the same skill tag (e.g., unit-conversion), engine should swap in a prerequisite drill from a remediation bank.
  - Evidence (158): "i think it's d) 10 kilometres"
  - Expected effect: Operationalises targeted remediation rather than relying on the model.
- **[medium] Suppress 'repeated_question' and 'same_template_repeat' flagged outputs**
  - Rationale: Many turns are flagged repeated_question; engine should regenerate when flag triggers.
  - Evidence (155): "[flags: truncated,same_template_repeat,numeric_mutation]"
  - Expected effect: Eliminates duplicated and templated outputs the student already saw.

### Student-experience changes (experience_recommendations)

- **[medium] Acknowledge each answer warmly before correcting**
  - Rationale: Error-prone student gets repeated corrections without affirmation, which may discourage persistence.
  - Evidence (153): "Not quite 10 km. If you have 100,000 cm, remember that 100 cm makes 1 metre"
  - Expected effect: Maintains motivation during repeated errors.
- **[medium] Slow pace when student shows confusion ('wait what')**
  - Rationale: Student expresses surprise/confusion (turns 126, 152) and tutor responds with new MCQs rather than a slower walkthrough.
  - Evidence (152): "wait what? 80 kilometres? no, that's too much."
  - Expected effect: Better match to error-prone persona; reduces frustration.

## BEA-2025 per-turn evaluation

_Maurya et al. 2025. Per-tutor-turn 3-class scoring on 4 dimensions._

- **Coverage**: 13 in-scope of 32 tutor turns (41%)
- **All-4-dims strict pass**: 31%  ·  **lenient**: 85%

| Dimension | Exact (Yes) | Lenient (Yes + Somewhat) |
|---|---:|---:|
| mistake_identification | 77% | 85% |
| mistake_location | 77% | 85% |
| providing_guidance | 85% | 85% |
| actionability | 38% | 85% |

### Per-turn verdicts

- **turn 110** — mistake: _Student chose B (Africa map) for finding a walking trail; should be C (local detailed map)._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "A map of Africa covers a massive area, so it won't show small details like individual hiking trails."
  - Rationale: Clearly identifies and locates the error and explains, but the turn is truncated and doesn't explicitly tell the student to pick the correct option.
- **turn 124** — mistake: _Student gave 125 metres for 5cm × 25,000; correct answer is 1,250 m. Unit conversion error._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "Close! You correctly calculated $125,000$ cm, but check your conversion to metres (remember $100 \text{ cm} = 1 \text{ m"
  - Rationale: Identifies and locates the conversion error and hints at the rule, but then pivots to an unrelated MCQ instead of asking the student to redo the conversion.
- **turn 127** — mistake: _Student said 125,000 cm = 12.5 m; correct is 1,250 m._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "Not quite—since $100 \text{ cm} = 1 \text{ m}$, you divide $125,000$ by $100$ to get $1,250$ metres (just remove two zer"
  - Rationale: Gives the answer directly with explanation, but then moves to a new topic rather than having the student practise another conversion.
- **turn 130** — mistake: _Student chose 1:25,000 for deep-sea navigation; correct is 1:250,000 (small scale, large area)._
  - MI: `No`  · ML: `No`  · PG: `No`  · Act: `No`
  - Tutor: "Let's try this one about choosing the right map:"
  - Rationale: Tutor entirely ignores the student's wrong answer and pivots to an unrelated MCQ.
- **turn 135** — mistake: _Student chose D (contour lines) for purpose of thematic population-density map; correct is B._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Contour lines show the height of the land, but population density is a specific "theme"... Try again by looking for the "
  - Rationale: Identifies error, explains the distinction, and directs the student to retry by focusing on the correct option type.
- **turn 137** — mistake: _Student now chose C (political boundaries); still wrong, correct is B._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Political maps show boundaries, but this map is about **population density**. Look at option B—how does it describe show"
  - Rationale: Clearly identifies the error, points the student to option B, and asks a guiding question.
- **turn 142** — mistake: _Student picked scale bar for finding cardinal directions; correct is compass rose._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "The scale bar measures distance, not direction. To find North or South, you need the **compass rose**."
  - Rationale: Identifies and corrects, but immediately reveals the answer and moves on without practice on the same skill.
- **turn 148** — mistake: _Student chose 1:100,000 for doubled-size map; correct is 1:25,000 (denominator halves)._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "When you enlarge a map, features look bigger, so 1 cm on the paper represents a smaller real-world distance. This means "
  - Rationale: Correct explanation but essentially reveals the answer rather than guiding the student to derive it.
- **turn 151** — mistake: _Student answered 80 km for a 4 cm × 200 m problem; correct is 0.8 km._
  - MI: `No`  · ML: `No`  · PG: `No`  · Act: `No`
  - Tutor: "If a map scale is 1:50,000 and you measure 2 cm on the map, what is the actual distance in the real world?"
  - Rationale: Tutor ignores the wrong answer and the student's self-posed question, switching to an unrelated 1:50,000 MCQ.
- **turn 153** — mistake: _Student chose 10 km for 2 cm on a 1:50,000 map; correct is 1 km._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "Not quite 10 km. If you have 100,000 cm, remember that 100 cm makes 1 metre—how many metres is that total?"
  - Rationale: Identifies error, gives a scaffolding hint, and asks a clear next question.
- **turn 155** — mistake: _Student said 10,000 metres; correct is 1,000 metres._
  - MI: `To some extent`  · ML: `To some extent`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "If 100,000 cm is 1,000 metres, and 1,000 metres equals 1 kilometre, which is the correct distance for 2 cm on a 1:50,000"
  - Rationale: Implicitly corrects by stating the right number (1,000 m) without explicitly flagging that the student's 10,000 was wrong; re-poses original MCQ.
- **turn 157** — mistake: _Student chose 500 metres; correct is 1 km._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `Yes`
  - Tutor: "If $1 \text{ cm}$ represents $50,000 \text{ cm}$, then $2 \text{ cm}$ represents $100,000 \text{ cm}$. Since $100,000 \t"
  - Rationale: Walks through the calculation step-by-step with a hint and clear next action.
- **turn 159** — mistake: _Student still chose 10 km; correct is 1 km (C)._
  - MI: `Yes`  · ML: `Yes`  · PG: `Yes`  · Act: `To some extent`
  - Tutor: "1,000 metres is exactly **1 kilometre**. Since our calculation gave us 1,000 metres, the correct answer is 1 km."
  - Rationale: Reveals the answer after repeated failure (acceptable here), then offers a follow-up problem, but doesn't diagnose the underlying conversion bottleneck.

## Transcript

```
# Transcript — model=Gemini 3 Flash  lesson=1425  persona=error_prone
session_id=4  status=active

--- TUTOR (id=108, tools=1)
Hi! Today we'll learn how map scales help us choose the right map for different tasks, like hiking or sailing. Imagine you're a tour operator planning a trip to Morne Seychellois National Park.

To find a specific walking trail in the park, which type of map would be most helpful?

A) A map showing the whole world
B) A map showing all of Africa
C) A map showing detailed local paths
D) A map showing only the ocean

--- STUDENT (id=109, tools=0)
i think it's b) a map showing all of africa.

--- TUTOR (id=110, tools=1)  [flags: truncated]
A map of Africa covers a massive area, so it won't show small details like individual hiking trails. You need a map that focuses on a much smaller area to see those

--- STUDENT (id=111, tools=0)
ohh. okay.

so, if a map shows a very small area but with lots and lots of detail, is that a large-scale map or a small-scale map?

--- TUTOR (id=112, tools=1)  [flags: truncated,repeated_question]
That is a **large-scale map**! It "zooms in" on a small area to show objects like buildings or trails in great detail.

Try this:

--- TUTOR (id=113, tools=0)
Which of the following is characteristic of a large-scale map?

  A) It has a ratio denominator greater than 100,000
  B) It shows a very wide geographic area with minimal detail
  C) It has a small ratio denominator and shows considerable detail
  D) It is printed on a larger sheet of paper

--- STUDENT (id=114, tools=0)
okay, let's try another one.

which type of map would you use if you wanted to see the entire country of seychelles at once, without much detail, like to see where all the main islands are?

a) a large-scale map
b) a small-scale map

--- TUTOR (id=115, tools=1)
That would be a **small-scale map**! It covers a large area like the whole country but shows less detail.

Now apply that.

--- TUTOR (id=116, tools=0)
A street map of Victoria (scale 1:5,000) and a national map of Seychelles (scale 1:500,000) differ in their level of detail. Which statement is true?

  A) The street map shows less detail because it covers a smaller area
  B) The national map shows more detail because it uses a smaller denominator
  C) The street map shows more detail because it uses a larger scale
  D) Both maps show the same level of detail regardless of scale

--- STUDENT (id=117, tools=0)
okay.

so, if you are a fisherman and you want to see all the different fishing banks around mahe, and you need to see the depths and currents for a big area, which kind of map would you use?

a) a large-scale map
b) a small-scale map

--- TUTOR (id=118, tools=1)  [flags: repeated_question]
Let's see if you can interpret a specific scale ratio.

--- TUTOR (id=119, tools=0)
What does a map scale of 1:25,000 tell us?

  A) The map shows 25,000 times more detail than a globe
  B) One unit on the map represents 25,000 units in reality
  C) The map is 25,000 square kilometers in area
  D) 25,000 people live in the area shown on the map

--- STUDENT (id=120, tools=0)
A map has a scale of 1:50,000. If two points are 2 cm apart on the map, how far apart are they in real life?

--- TUTOR (id=121, tools=1)  [flags: truncated,numeric_mutation]
To find the real distance, multiply the map distance by the scale: $2 \text{ cm} \times 50,000 = 100,000 \text{ cm}$, which is 1 km. You always multiply the map measurement by the scale's denominator.

Now try this one:

Now try this one:

--- TUTOR (id=122, tools=0)
If a map scale is 1:50,000 and the straight-line distance between two villages is 8 cm on the map, calculate the actual distance between the villages in kilometres. Show your working.

--- STUDENT (id=123, tools=0)
a map has a scale of 1:25,000. if two points are 5 cm apart on the map, how far apart are they in real life?

125 metres

--- TUTOR (id=124, tools=1)  [flags: repeated_question,numeric_mutation]
Close! You correctly calculated $125,000$ cm, but check your conversion to metres (remember $100 \text{ cm} = 1 \text{ m}$). Once you've fixed that, let's see how we choose the right scale for a real journey.

--- TUTOR (id=125, tools=0)
If you are planning to navigate between islands in the Seychelles using a map, which scale would be most suitable for safe passage?

  A) A small-scale map (1:1,000,000) showing the entire Indian Ocean
  B) A large-scale map (1:10,000) showing detailed coastlines and hazards
  C) A thematic map showing ocean currents only
  D) A political map showing national boundaries

--- STUDENT (id=126, tools=0)
ohh. wait what.

so, 125,000 cm would be 12.5 metres?

--- TUTOR (id=127, tools=1)  [flags: repeated_question,numeric_mutation]
Not quite—since $100 \text{ cm} = 1 \text{ m}$, you divide $125,000$ by $100$ to get $1,250$ metres (just remove two zeros). Let's see how you'd choose a map for a specific task.

--- TUTOR (id=128, tools=0)
You need to study the elevation changes across the Seychelles islands and plan a hiking route that avoids steep terrain. Which map type would you choose, and what information on that map would help you make this decision?

--- STUDENT (id=129, tools=0)
okay.

you are planning a deep-sea fishing trip far from the coast of mahe. you need to navigate between distant islands and avoid large underwater obstacles. which map scale would be most suitable?

a) 1:1,000
b) 1:25,000
c) 1:250,000
d) 1:5,000

i think it's b) 1:25,000

--- TUTOR (id=130, tools=1)  [flags: truncated,repeated_question]
Let's try this one about choosing the right map:

--- TUTOR (id=131, tools=0)
Which combination of scale and map type would be most inappropriate for studying the distribution of coral reefs around Seychelles?

  A) A large-scale (1:10,000) physical map
  B) A medium-scale (1:100,000) thematic map showing marine ecosystems
  C) A small-scale (1:1,000,000) political map
  D) A large-scale (1:5,000) topographic map of coastal areas

--- STUDENT (id=132, tools=0)
A 1:25,000 map would show a lot of detail, but only for a very small area, like a bay or a specific part of the coast. For deep-sea fishing between distant islands, you need to see a much bigger picture, even if it means less detail. Think about which scale covers the largest area.

--- TUTOR (id=133, tools=1)  [flags: repeated_question]
A thematic map of Seychelles shows population density by color shading. What is the primary purpose of this representation?

  A) To show which parts of the islands are mountainous
  B) To illustrate how a specific geographic variable (population density) is distributed spatially
  C) To display international political boundaries
  D) To indicate elevation changes using contour lines

--- STUDENT (id=134, tools=0)
i think it's d) to indicate elevation changes using contour lines

--- TUTOR (id=135, tools=0)  [flags: truncated,repeated_question]
Contour lines show the height of the land, but population density is a specific "theme" about where people live. Thematic maps show the distribution of a particular subject across an area. Try again by looking for the option that mentions a specific geographic variable.

--- STUDENT (id=136, tools=0)
ohh, okay.

so, a thematic map shows a specific *theme* or variable.

so for the population density map, i think it's c) to display international political boundaries.

--- TUTOR (id=137, tools=0)
Political maps show boundaries, but this map is about **population density**. Look at option B—how does it describe showing a specific variable across an area?

--- STUDENT (id=138, tools=0)
no, c is for political maps. the map is about *population density*.

look at option b again. what does it say about a *specific geographic variable*?

--- TUTOR (id=139, tools=1)  [flags: truncated,repeated_question]
Yes—option B is the one. Thematic maps focus on a single topic like population density.

|||MEDIA:NONE|||

--- TUTOR (id=140, tools=0)
Your geography project requires you to analyse the relationship between elevation and vegetation types in Seychelles. Which map type would be most appropriate?

  A) A political map
  B) A topographic map combined with a thematic map of vegetation
  C) A physical map only
  D) A street-level map

--- STUDENT (id=141, tools=0)
okay.

which feature on a map helps you find north, south, east, and west?

a) the legend
b) the scale bar
c) the compass rose
d) the title

i think it's b) the scale bar

--- TUTOR (id=142, tools=1)  [flags: truncated,repeated_question]
The scale bar measures distance, not direction. To find North or South, you need the **compass rose**.

Now apply your scale skills to this real-world distance problem:

--- TUTOR (id=143, tools=0)
On a 1:15,000 map, the distance between two landmarks measures 4.5 cm. What is the actual distance in kilometres?

  A) 0.675 kilometres
  B) 0.67 kilometres
  C) 6.75 kilometres
  D) 67.5 kilometres

--- STUDENT (id=144, tools=0)
okay.

on a map with a scale of 1:10,000, a river segment measures 3 cm. how long is the actual river segment in meters?

300 metres

--- TUTOR (id=145, tools=1)  [flags: repeated_question,numeric_mutation]
Yes — 300 metres is exactly right since $3 \times 10,000 = 30,000$ cm.

Now, let's see what happens to the scale if we change the physical size of the map.

--- TUTOR (id=146, tools=0)
If you enlarge a map photocopied at 200% (doubled in size), which scale reference would remain accurate?

  A) The representative fraction (e.g., 1:50,000)
  B) The scale bar
  C) Both the representative fraction and scale bar
  D) Neither—you must recalculate the scale

--- STUDENT (id=147, tools=0)
if you take a map with a scale of 1:50,000 and you photocopy it to make it twice as big, what happens to the map's scale?

a) the scale becomes 1:100,000
b) the scale becomes 1:25,000
c) the scale remains 1:50,000
d) the map is no longer useful

i think it's a) the scale becomes 1:100,000

--- TUTOR (id=148, tools=1)  [flags: truncated,repeated_question,numeric_mutation]
When you enlarge a map, features look bigger, so 1 cm on the paper represents a smaller real-world distance. This means the denominator actually gets smaller (like 1:25,000).

Try this:

--- TUTOR (id=149, tools=0)
Why would a 1:1,000,000 map be unsuitable for planning a detailed hiking trail across one island in Seychelles?

  A) Because the map would show only the entire archipelago with insufficient detail for trail planning
  B) Because hiking trails cannot be shown on maps at any scale
  C) Because 1:1,000,000 maps are illegal to use for recreation
  D) Because the map would need to be printed on a very large sheet of paper

--- STUDENT (id=150, tools=0)
you're right, my bad.

okay, new question:

a map has a scale where 1 cm represents 200 meters. if a jogging track on the map is 4 cm long, how long is the actual jogging track in kilometers?

80 kilometres

--- TUTOR (id=151, tools=1)  [flags: truncated,numeric_mutation]
If a map scale is 1:50,000 and you measure 2 cm on the map, what is the actual distance in the real world?

A) 500 metres
B) 5,000 metres
C) 1 kilometre
D) 10 kilometres

--- STUDENT (id=152, tools=0)
wait what? 80 kilometres? no, that's too much.

okay, for this one, i think it's d) 10 kilometres

--- TUTOR (id=153, tools=0)  [flags: numeric_mutation]
Not quite 10 km. If you have 100,000 cm, remember that 100 cm makes 1 metre—how many metres is that total?

--- STUDENT (id=154, tools=0)
10,000 metres

--- TUTOR (id=155, tools=1)  [flags: truncated,same_template_repeat,numeric_mutation]
If 100,000 cm is 1,000 metres, and 1,000 metres equals 1 kilometre, which is the correct distance for 2 cm on a 1:50,000 map?

A) 500 metres
B) 5,000 metres
C) 1 kilometre
D) 10 kilometres

--- STUDENT (id=156, tools=0)
i think it's a) 500 metres

--- TUTOR (id=157, tools=0)  [flags: numeric_mutation]
That's not quite it. If $1 \text{ cm}$ represents $50,000 \text{ cm}$, then $2 \text{ cm}$ represents $100,000 \text{ cm}$. Since $100,000 \text{ cm}$ is the same as $1,000 \text{ metres}$, how many kilometres is that?

(Hint: $1,000 \text{ metres} = 1 \text{ kilometre}$)

--- STUDENT (id=158, tools=0)
i think it's d) 10 kilometres

--- TUTOR (id=159, tools=1)  [flags: truncated,repeated_question,numeric_mutation]
1,000 metres is exactly **1 kilometre**. Since our calculation gave us 1,000 metres, the correct answer is 1 km.

Let's try one more to make sure the conversion is clear.

If a map scale is 1:10,000, what is the real-world distance of 1 cm in metres?

A) 10 metres
B) 100 metres
C) 1,000 metres
D) 10,000 metres

```
