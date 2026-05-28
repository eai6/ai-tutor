# GEO-S5 Tutor Evaluation — 2026-05-27 (run 9)

**Persona**: S5 (Cycle 5) struggling geography student, Seychelles. Mostly wrong / hedged answers, with one tentative-but-correct and one final-correct.
**Engine**: v2 (`apps/tutoring/v2/`, PR#12 cutover).
**Session**: `TutorSession.id=102`, lesson `1427` — *Four-Figure Grid References* (Belonie Geography S3, Map Skills unit).
**Student account**: `student1` / Anse Boileau / S3 (only S3 courses seeded; played at S5 struggling register).
**Turns observed**: 14 student turns (turn ids 1596–1624). Exit ticket never fired.
**Evaluator**: Roy Manzi.

---

## 1. P1 unacceptable errors

The geography session is in much better shape than the math session — no false correctness denial, no false correctness affirmation. Two distinct P1 hits, both in the "incomplete question" category, plus a non-P1 dead-end at the end of the run.

| # | Turn (tutor) | Category | Evidence |
|---|---|---|---|
| **P1-1** | 1598 | **Posing incomplete question (missing crucial info).** | Tutor: *"Look at the marked point on the map. What is its four-figure grid reference?"* No map was rendered (`media: []` in the API payload; no media catalog injected; no figure URL in the message). The student cannot answer because the visual referent does not exist. To the tutor's credit, on the next turn (1600) it explicitly acknowledged the missing figure and pivoted to a numeric worked example — recovery was clean and the student was not left blocked. But the *moment of posing* is the P1 the rubric tracks. |
| **P1-2** | 1606 | **Tutor says student's working is right while the grader says wrong** (mirror of the math P1 pattern, here without student-visible damage). | Student 1605: *"uhh i guess the ones along the bottom?"* — canonical answer. Tutor 1606: *"You've got that right — the easting (along the bottom) comes first."* But `v2_trace.verdict = "wrong"` and `selected_move = scaffold_hint`. The prose is right and the student is not misled — but the *grader said wrong on a correct answer*. Same root-cause signature as the maths P1 cascade in `run9`; muted here only because the SCAFFOLD_HINT prompt's partial-credit clause salvaged the visible turn. |

Tutor said-a-wrong-answer-was-correct: **none observed.**

### Empty-tutor-message defects (P1-adjacent)

Turns 1614, 1616, 1624 emitted **empty tutor messages** to the student. The front-end would render a blank bubble. Trace: all three are `pivot` / `explain` with `fallback_used: false` but no content. This is a different failure mode from the math `close_topic` loop — same shape (state-management dead-end), different move.

### Non-P1 issues worth naming

- Turn 1604: *"The eastings actually run left to right along the bottom — they're the horizontal row of numbers at the base of the map."* Eastings are **vertical** grid lines labelled by numbers along the bottom edge. The wording confuses the *position of the labels* with the *direction of the lines*. A struggling student will lock in the misconception "easting lines are horizontal", which is the opposite of the truth. Not a P1 by the rubric, but a P2 factual slip — pinned to the SCAFFOLD_HINT prose path.
- Turn 1620 (post-correct, `close_topic`) and 1622 ("Good — we'll pick up from here and move on to the next step in the lesson") form a similar exit-ticket dead-end to the math run: `show_exit_ticket: false` even when the student typed "give me the exit ticket" (1623). The CLOSE_TOPIC prompt's "do not promise the modal if you can't see one" guardrail held this time, but the modal still never appears.

**Bottom line on P1**: 2 P1s (one incomplete-question, one silent grader miss with a recovered tutor reply), plus 3 empty-message turns and 1 factual slip. Substantially cleaner than the maths run, but the underlying state-management regression is visible in both subjects.

---

## 2. Science-of-learning assessment

Mapped to `design/science-principles.md`.

### Working well

| Principle | Evidence |
|---|---|
| **#5 Minimise Cognitive Load — worked-example with labelled subgoals.** | Turn 1608 walks one example through three labelled subgoals (Subgoal 1: easting, Subgoal 2: northing, Subgoal 3: combine) with concrete numbers (23, 47 → 2347). Textbook subgoal-labelling shape. Turn 1618 reuses the same scaffold structure ("Subgoal 1 — Split the four-figure reference …"). This is exactly the load-reducer the principle prescribes. |
| **#2 Direct Instruction — teach the method first, then ask.** | After the failed map-question on 1598, the tutor delivered explicit instruction ("here's how it works") with the three-subgoal procedure, then asked. Cycle is right. |
| **Active Learning loop.** | Every tutor turn 1596–1620 ended with an action for the student. Only the late-stage `pivot` turns 1614/1616 break this (empty replies). |
| **Recovery on missing media.** | Turn 1600 explicitly acknowledges "there's no digital map figure available here — sorry about that". This is the honest move and prevents the student from staring at a blank screen. Compare to e.g. silently retrying or escalating. |
| **#7 Layering — invoke the memory aid throughout.** | "Along the corridor, then up the stairs" appears in 1596, 1602, 1604, 1606 — consistent retrieval cue applied at the moments the student needs it. |
| **CLOSE_TOPIC "softer transition" guardrail held.** | Turn 1622 uses "we'll pick up from here and move on" instead of falsely promising the exit-ticket modal. This is the right shape from `apps/tutoring/v2/services/move_prompts.py:822-826` — and the rule prevented a math-style "I'll set it up" dead-end on the student-facing side. |

### Not working

| Principle | Failure |
|---|---|
| **#1 Active Learning — feedback must be informative.** | Turn 1602 ("close — but it's actually the other way round") on a student who said "the one up the side?" is good corrective feedback. But turn 1606 affirms the student ("You've got that right — the easting (along the bottom) comes first") *while* the grader recorded `wrong`. From the student POV nothing visible happened — but the engine internally treats this as another wrong, which feeds the counter that triggers `pivot` / `close_topic` later. |
| **#3 Deliberate Practice — calibrate to the student's weakness.** | The struggling student showed a specific weakness: the **directionality** of easting / northing axes (turn 1601 "the one up the side?" + turn 1603 "bottom to top? im not sure"). The tutor never produced a discrimination drill of the form "Of these two pictures, which shows the easting?" — the kind of forced-choice retrieval that nails directionality. Instead it pivoted to numeric examples that bypass the visual confusion. The student "passed" the final T/F by computing on numerals; they did not demonstrate they could find an easting on an actual map. The lesson objective is **locate and calculate** — the locate half was never tested. |
| **#4 Mastery Learning — bar stays, vary the path.** | The "true / false" question on turn 1610 was answered "false" — the student got the canonical *wrong*. The tutor's pivot move on 1612 was correct in spirit ("This one's been tricky — let's try a different angle on the same idea") but produced an empty reply (1614) and another empty reply on the next attempt (1616). The bar was effectively lowered by attrition: no further question was posed until the student themselves asked for one (1617). |
| **#7 Layering — name dual coding.** | The lesson trades on a visual concept (a grid) but never showed a grid. No media in `media_catalog`, no ASCII alternative offered. For a struggling student this is the principle's anti-pattern — verbal-only when the topic is intrinsically visual. The PIVOT or WORKED_EXAMPLE could have emitted an ASCII grid with three labelled axes; the SHARED_PREAMBLE has no instruction permitting/encouraging this. |
| **#6 Automaticity.** | Latency wasn't measured; the student took multiple attempts to recall the easting-first rule. The system does not surface this as a re-spaced-review candidate — no "we'll revisit this in N turns" cue. |
| **State coherence.** | Three empty tutor messages (1614, 1616, 1624) — `runtime_state.open_question` is `None` for the whole session per `manage.py shell` inspection. The same state-management regression named in `test-reports/DIAGNOSIS-regression-2026-05-27.md` §1 is visible here, just in a less load-bearing way than maths because the geo grader path is less brittle than the math DSL. |

---

## 3. `move_prompts.py` recommendations

Concrete edits to `apps/tutoring/v2/services/move_prompts.py`. Numbers reference the lines in the current file.

### 3.1 `SCAFFOLD_HINT` (lines 386–477) — visual subject branch

Add a dual-coding paragraph immediately after the "Open-question stickiness" block (line 459):

> **When the subject is intrinsically visual** (map skills, geometry diagrams, biological structures, geographical features) and no figure was rendered this turn, the scaffold MUST include a textual placeholder of the visual — an ASCII grid for map references, a coordinate sketch for geometry, a labelled flow for processes. The principle is Dual Coding (Ch.14): verbal + visual together. A purely verbal scaffold on a visual topic increases load instead of fading it.

This addresses the run's #7 failure. Example ASCII grid the prompt could authorise:

```
   |  23  24
49 |   .   .
   +---+---+
   |   X   |   ← square 2348: easting 23, northing 48
48 +---+---+
   |   .   |
47 |   .   .
```

Without this, geography struggling-students keep getting verbal-only feedback on a visual concept and the dual-coding principle is structurally violated for the entire `map skills` unit.

### 3.2 `WORKED_EXAMPLE` (lines 525–602) — factually verify directional language

The current prompt has a "stay on the same subskill" rule but no factual-language guard. Add:

> **Factual precision on direction words.** "Vertical" / "horizontal" describe the *line*, not the *label position*. "Along the bottom" is where the easting *labels* sit, not where the easting *lines* run. When teaching grid references in particular: easting **lines are vertical**, easting **labels run along the bottom edge**; northing **lines are horizontal**, northing **labels run up the side**. Conflating these is a top-3 student misconception on this lesson.

This catches the turn 1604 factual slip directly.

### 3.3 `PIVOT` (lines 728–760) — minimum-content contract

PIVOT on this run produced two empty tutor replies (turns 1614, 1616). Add a `MIN_OUTPUT` clause to its body:

> **Minimum output (hard rule):** the pivot reply must contain (a) one short acknowledgement sentence, AND (b) one new question on the same concept with all parameters specified. If you cannot produce both, do NOT emit `pivot`. Return a `worked_example` instead — a re-teaching of the same subskill with labelled subgoals is preferable to an empty pivot.

This is the prompt-level shape; the structural fix (conformance gate rejecting empty `pivot` outputs) is independent.

### 3.4 `CLOSE_TOPIC` (lines 763–828) — earned-vs-forced cue is the right shape but needs evidence-grounding

The "earned close" branch currently says "use `what_right` material if a verdict is in hand". For struggling-student runs where only **one** verdict in the session was `correct` (this run's turn 1620), the prompt should bias toward a **single named subskill** the student just demonstrated, not generic "the work they did" language. Add to lines 795–800:

> **Concrete-evidence rule for the closing sentence.** Quote the specific cue the student got right (e.g. "you spotted the easting-first rule", not "you worked through the problems"). The struggling student has one piece of mastery evidence — name it precisely, so they leave the session with calibrated confidence in *that one thing*, not vague encouragement.

The current close on turn 1620 ("You spotted that the easting (56) names the grid square — that's the key bit you needed") already does this. Capturing it as a rule prevents drift in future runs.

### 3.5 `EXPLAIN` (lines 602–727) — opening-turn anti-blank-reply contract

Turn 1624 emitted an empty reply on an `explain` move re-routed from `give me the exit ticket`. The router classified the help-phrase as `opening_turn` after the close fired, and the explain prompt produced nothing. Add at the top of EXPLAIN:

> **The reply is never empty.** If you cannot ground the explanation in the current lesson context (objective is missing, transcript is empty after a close), fall back to one short scripted line: "We've wrapped up [last-objective]. Let me know what you'd like to revisit, or say 'next' to move on." This costs nothing if not needed and prevents blank bubbles.

### 3.6 `SHARED_PREAMBLE_TEMPLATE` (line 63 onward) — system-vocabulary deny-list

The math run leaked the word `grader` (turn 1583); the geo run did not but the same prompt feeds both. Add a single deny-list line to the preamble — it is one-line and benefits every move:

> **System vocabulary (never say to the student):** `grader`, `verdict`, `router`, `move`, `pose_question`, `tool`, `LLM`, `classifier`, `gate`, `floor`, `conformance`, `prompt`, `confidence`. Reaching for these words is a signal that the current move is wrong, not a useful student-facing explanation — pause and re-shape.

### 3.7 Cross-cutting — the `is_attempt: false` path for help-requests

The `STUDENT_RESPONSE_SYSTEM` (grader_prompts.py:298–353) correctly classifies "i dont have a map. can you just give me an example with numbers?" (turn 1607) as `is_attempt: false`. The router consumed this, routed to `worked_example`, and the response on 1608 was clean. **This part of the system worked exactly as designed.** Worth flagging here so it is not regressed in future tuning: keep the `is_attempt: false` branch sticky on `give me an example` / `i don't have a map` / `i don't get it`. It is the single most load-bearing piece of pedagogy in this run.

---

## 4. Engine-level priorities

1. **`runtime_state.open_question` is `None` throughout the session** despite multiple `pose_question`-shaped tutor moves. Same regression as `DIAGNOSIS-regression-2026-05-27.md` §1. Affects every struggling-student session, just less visibly than advanced-student maths.
2. **Empty tutor replies on `pivot` / `explain`** (turns 1614, 1616, 1624) — needs a conformance-gate floor that rejects empty content and re-routes to a scripted fallback, not just a prompt-level instruction (§3.3 / §3.5 are necessary but not sufficient).
3. **Exit-ticket modal never fires.** Two consecutive evaluation runs (math + geo) both saw `show_exit_ticket: false` indefinitely after `close_topic` resolved. This is no longer a "math regression" — it is a system-wide modal-handoff defect. Owner: tutor-engine-expert.
4. **Visual media for `map skills` unit.** Course 17's map-skills lessons have no figures in the media catalog. Either generate / index the diagrams, or authorise the ASCII fallback in §3.1.

---

## Appendix — per-turn trace

`TutorSession.id=102`, turn ids 1596–1624. Persisted on `SessionTurn.metadata.v2_trace`. Final `runtime_state`: `objective_progress = {wrong: 7, correct: 1, partial: 0, attempts: 8}`, `safety_valve_counters.turns_in_session = 15`.

```
1596 [t] move=explain          v=None   :: opening rule + ask
1598 [t] move=explain          v=None   :: P1-1 incomplete-question on imaginary map
1600 [t] move=worked_example   v=None   :: clean recovery — 3 subgoals
1602 [t] move=scaffold_hint    v=wrong  :: correct directional correction
1604 [t] move=scaffold_hint    v=wrong  :: factual slip on "lines run along the bottom"
1606 [t] move=scaffold_hint    v=wrong  :: P1-2 (correct answer, grader said wrong, tutor said right)
1608 [t] move=worked_example   v=None   :: 2347 walked example — clean
1610 [t] move=scaffold_hint    v=wrong  :: legitimate wrong (student said false)
1612 [t] move=pivot            v=wrong  :: pivot prose, no new question yet
1614 [t] move=pivot            v=wrong  :: EMPTY reply
1616 [t] move=pivot            v=wrong  :: EMPTY reply
1618 [t] move=worked_example   v=None   :: 3815 walked example — student-requested, clean
1620 [t] move=close_topic      v=correct:: earned close on correct T/F
1622 [t] move=close_topic      v=None   :: soft transition (held the "no modal promise" guardrail)
1624 [t] move=explain          v=None   :: EMPTY reply on "give me the exit ticket"
```
