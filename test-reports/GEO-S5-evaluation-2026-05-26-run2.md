# GEO-S5 Tutor Evaluation — 2026-05-26 (run 2)

**Persona**: S5 (Cycle 5) advanced Geography student in Seychelles. Provides full,
correctly-reasoned answers with named processes, mechanisms, and locale-specific
references.
**Lesson evaluated**: L1451 — *The Hydrological Cycle Overview* (Geography S3 / Belonie
catalogue). Distinct from the earlier same-day GEO-S5 report on L1425.
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on`).
**Student**: `geoprobe` (freshly created S5 student at School-3).
**Session ID**: 79. 8 student turns (T1–T8).

---

## 1. Transcript

| # | Role | Content | move / verdict / fallback |
|---|------|---------|---------------------------|
| 0 | tutor | Welcome to today's lesson on **The Hydrological Cycle Overview**! … *(direct-instruction opener naming closed-system property and process names)* … *(I don't have a digital diagram for this lesson yet — ask your teacher for the printed diagram to follow along as we work through the stages.)* Let's check your starting knowledge straight away — can you name **one** process in the hydrological cycle? | `explain / — / —` ✅ honest figure-missing disclaimer |
| 1 | student | Evaporation - the process where solar energy converts liquid water into water vapour at ocean and lake surfaces | — |
| 1 | tutor | Let's check that together rather than guess. Let's try a question on this together. | `pose_question / — / yes` ⚠ safe template ignores rich correct answer |
| 2 | student | Condensation occurs when water vapour cools as it rises and changes back into tiny droplets that form clouds | — |
| 2 | tutor | Let's put that to the test with a diagram question. **Look at the diagram below showing the hydrological cycle with four blank boxes. Which label belongs in Box 2, where water vapor is cooling and forming visible droplets in the atmosphere?** | `pose_question / — / no` ⚠⚠ **P1: references a diagram that does not exist** (T0 explicitly said the lesson has no digital diagram) |
| 3 | student | Condensation belongs in Box 2 - that's the process you described where water vapour cools and forms droplets | — |
| 3 | tutor | Good effort putting your reasoning in writing! I couldn't verify that from the transcript alone, so let's pin it down with a focused question. Let's check one key idea about how water moves through the cycle — **True or False: During the hydrological cycle in Seychelles, water can only move from the ocean to the atmosphere through evaporation, and once it returns as precipitation, the cycle ends because the water stays on the islands.** | `scaffold_hint / unverified / no` ✅ surfaces uncertainty; clean T/F pose |
| 4 | student | Precipitation occurs when condensed water droplets in clouds grow too heavy to remain suspended, falling as rain, snow, sleet or hail back to Earth's surface | — |
| 4 | tutor | Good effort putting your reasoning in writing! I couldn't verify that from the transcript alone, so let's pin it down with a focused question. **Let's test one specific part of the cycle:** | `scaffold_hint / unverified / no` ⚠⚠ **P1: incomplete question — turn ends at the colon with no stem** |
| 5 | student | False - the cycle continues because water can leave the islands through evaporation, transpiration, surface runoff to the ocean, and groundwater flow; nothing 'stays' permanently | — |
| 5 | tutor | Yes — you matched the answer. Let's try a question on this together. | `confirm_and_extend / correct / yes` ⚠ safe template; no extension Q despite the move name |
| 6 | student | Transpiration is the release of water vapour from plant leaves through stomata - it contributes significantly to atmospheric moisture, especially in tropical regions like Seychelles | — |
| 6 | tutor | Good effort putting your reasoning in writing! I couldn't verify that from the transcript alone, so let's pin it down with a focused question. **Let's test the core idea directly:** | `scaffold_hint / unverified / no` ⚠⚠ **P1: incomplete question — turn ends at colon** |
| 7 | student | What is the question? I don't see one in your last message | — |
| 7 | tutor | You're right to flag that — I didn't actually post the question! Let me fix that now. | `scaffold_hint / wrong / no` ⚠⚠ tutor admits omission then **still does not pose a question** |
| 8 | student | Surface runoff returns precipitation to the oceans and lakes via streams and rivers, completing the cycle from land back to the hydrosphere | — |
| 8 | tutor | Good effort putting your reasoning in writing! I couldn't verify that from the transcript alone, so let's pin it down with a focused question. **Let's test one specific stage of the cycle directly:** | `scaffold_hint / unverified / no` ⚠⚠ **P1: incomplete question — turn ends at colon** |

---

## 2. P1 Unacceptable Errors

| Class | Count | Where | Notes |
|---|---|---|---|
| Tutor marks a correct answer wrong | **0** | — | The T5 True/False verdict was `correct`. Other rich answers were `unverified`, not `wrong`. |
| Tutor marks a wrong answer correct | **0** | — | No false-positive observed. |
| Incomplete question (missing crucial info) | **4** | T2, T4, T6, T8 | T2: references "the diagram below" / "four blank boxes" / "Box 2" — none of which exist in this lesson (the engine explicitly disclaimed the missing diagram in T0). T4, T6, T8: tutor turns end at a colon ("Let's test the core idea directly:") with no question stem. |

**P1 verdict: FAIL.** Four P1 incomplete-question violations in a single 8-turn
session. Three of them share an identical template pattern, indicating a systematic
bug rather than an LLM stochastic miss.

---

## 3. Science-of-Learning Evaluation

| # | Principle | Score | Evidence |
|---|---|---|---|
| 1 | **Active Learning** | 🟡 | The student stayed active, but T4, T6, T8 made activity impossible — there was literally no question to answer. The student was forced into a clarification meta-turn (T7) to surface this. |
| 2 | **Direct Instruction** | ✅ | T0 opened with a tight, well-scoped definition of the hydrological cycle and named the four key processes — exactly the Direct Instruction frame Ch.11 prescribes. |
| 3 | **Deliberate Practice** | ❌ | A real advanced student volunteered three correctly-named processes with mechanism-level detail (T1, T2, T4, T6, T8) and ONE T/F item (T3→T5) tested them. The session never raised the difficulty floor despite clear over-qualification on every turn. No `confirm_and_extend` with an extension question; no harder discrimination prompt; no application/transfer item. |
| 4 | **Mastery Learning** | n/a | One session. |
| 5 | **Minimising Cognitive Load** | 🟡 | Opener was tight (one idea per turn). But the figure-ref violation in T2 introduces a phantom diagram the student must mentally synthesise — *increases* load. |
| 6 | **Automaticity** | n/a | Not measured. |
| 7 | **Layering** | 🟡 | The T/F item in T3 layers evaporation/precipitation/runoff knowledge — but the student had to do the layering; the tutor never composed across the processes the student volunteered. |
| 8 | **Non-Interference** | n/a | Single-topic. |
| 9–10 | Spaced Repetition / Interleaving | n/a | Out of scope. |
| 11 | **Testing Effect / Retrieval Practice** | 🟡 | T3 was a clean retrieval pose. But T4/T6/T8 cannot test anything because they have no stem — that's three failed retrieval opportunities on a student who clearly had the content. |
| 12 | **Targeted Remediation** | n/a | Student wasn't stuck. |
| 13 | **Gamification** | n/a | UI. |

### What's working

- **Honest figure-missing disclaimer in the opener (T0).** "I don't have a digital
  diagram for this lesson yet — ask your teacher for the printed diagram" is the right
  posture for a curriculum with sparse media. This is the **one place** in the
  session the figure-ref discipline is correctly observed.
- **`surfaces_uncertainty` discipline.** T3, T4, T6, T8 all open with the exact
  phrasing the SCAFFOLD_HINT prompt prescribes for `unverified` verdicts ("Good
  effort putting your reasoning in writing! I couldn't verify that from the
  transcript alone, so let's pin it down with a focused question."). That part of the
  prompt is doing its job — the failure is downstream, where the actual question is
  supposed to follow.
- **The single correct-verdict turn (T5) graded properly.** Despite the student
  giving a nuanced answer beyond a bare "False", the grader correctly returned
  `correct`.
- **T3's True/False stem was excellent.** Locale-anchored (Seychelles islands),
  multi-process (evaporation, precipitation, runoff implied), and discriminating
  (the false claim "the cycle ends because the water stays on the islands" is a
  testable misconception).

### What's not working

1. **The four P1 incomplete-question failures (T2, T4, T6, T8).** Pattern:
   - T2 invents a diagram and refers to "Box 2 / four blank boxes". Figure-ref
     conformance gate failed to catch this — the conformance docstring lists
     `figure_ref` as a deterministic gate, and the lesson context unambiguously
     declares no diagram is available.
   - T4, T6, T8 all emit the SCAFFOLD_HINT prompt's prescribed unverified preamble
     followed by a colon-terminated transition ("Let's test the core idea
     directly:") with **no question stem**. This strongly suggests the model is
     authoring a tool call (per the `move_prompts.py` directive "End with a tool
     call to a SIMPLER bank slot") and either the tool call is being stripped, the
     bank has no eligible slot, or the conformance / answer-leak gate is stripping
     the stem on the assumption it would leak the open question. The lead_in
     survives; the stem is gone. The student then asks T7 "what's the question?"
     — the engine *acknowledges* it on T7 ("I didn't actually post the question!
     Let me fix that now.") but then fails to fix it.

2. **`confirm_and_extend` did not extend (T5).** The move was selected on the only
   correct-verdict turn of the session, but the response was the safe template
   "Yes — you matched the answer. Let's try a question on this together." with
   `fallback_used=true`. No extension item. For an advanced student this is the
   highest-value missed opportunity — the prompt explicitly says "Pose a single
   follow-up that varies one parameter (different numbers, different units, an edge
   case)" and it didn't.

3. **No difficulty escalation despite over-qualification.** Across T1, T2, T4, T6, T8
   the student volunteered mechanism-level detail beyond what the bank questions
   tested. There is no engine-side signal that maps "student answer materially
   exceeds bank-stem difficulty" → "raise the stake or apply/transfer". For an S5
   advanced student this is the exact wrong direction (cf. desirable-difficulty,
   Ch.14).

4. **Rich open-text answers default to `unverified`.** T1 — a textbook-perfect named
   process with mechanism explanation — was not graded at all (no open question yet,
   so verdict=None) and got a safe template back. T2, T4, T6, T8 were all
   `unverified`. The grader's grounded-adjudication pathway cannot ground these
   against the bank because the bank is item-scoped, not concept-scoped. Result:
   advanced free-response is systematically under-credited.

### Areas for improvement

- **B1.** Wire a hard deterministic check on the structural-conformance "rule" gate:
  *if `selected_move ∈ {pose_question, scaffold_hint, confirm_and_extend, pivot,
  worked_example, name_misconception}` AND the response has no tool call AND no
  question mark in prose, reject with a verdict-keyed safe template that is itself
  question-bearing* (currently the safe template is also question-less). See the
  Move-Prompt Review §G1.
- **B2.** Block any tutor turn whose prose references "the diagram", "below",
  "above", "Box N", or "image" when `lesson.has_media == False`. This is checkable
  cheaply with a regex pre-filter before conformance runs (avoids burning a retry
  cycle on the figure-ref gate).
- **B3.** Add a "student over-qualification" signal to the grader output: if the
  student's answer overlaps the bank's canonical AND contains ≥1 mechanism term not
  required by the canonical, set `over_qualifies=true` and route to
  `confirm_and_extend` with a "harder stake" extension prompt directive.
- **B4.** Allow grounded adjudication on `unverified` rich open-text by adding a
  concept-level (not item-level) KB rubric per lesson — even a bullet list of
  acceptable named processes + their accepted definitions would let an advanced
  answer like T1's evaporation reply be tagged `correct` instead of falling into
  the unverified bucket.

---

## 4. Move-Prompt Review (`apps/tutoring/v2/services/move_prompts.py`)

This review focuses on the specific prompts implicated in this session: `EXPLAIN`
(T0), `POSE_QUESTION` (T1, T2), `SCAFFOLD_HINT` (T3, T4, T6, T7, T8),
`CONFIRM_AND_EXTEND` (T5). See the parallel MATHS-S1 report for the broader review.

### Strengths

- The `SHARED_PREAMBLE_TEMPLATE` "Subject anchoring" block (lines 81–86) appears to
  be doing its job here — every turn correctly stayed inside Geography / hydrological
  cycle. No subject hallucination this session (contrast: the earlier same-day
  GEO-S5 report on L1425 logged "In S3 maths" hallucination at T0).
- `SCAFFOLD_HINT`'s UNVERIFIED section (lines 275–294) produces an internally
  consistent register every time — uncertainty surfaced, no factual claim made,
  effort-praised. The directive *is* obeyed; the failure is downstream.
- `EXPLAIN`'s "no new numerical examples" rule (lines 390–391) is appropriate for
  Geography and the T0 opener stayed within the closed-system framing.

### Gaps surfaced by this session

#### G6. `SCAFFOLD_HINT` UNVERIFIED branch does not require a posed question

Lines 285–286 say the turn must "End with a tool call to a SIMPLER bank slot that
re-tests one sub-skill of the open question." But the prompt does not say what to do
if (a) the bank has no eligible simpler slot, (b) the tool call fails the answer-leak
gate. Result: the model emits the prescribed lead_in ("…let's pin it down with a
focused question.") and then a colon-terminated transition with nothing after it.

Recommend adding:

> **Floor rule.** Every UNVERIFIED scaffold_hint turn must end with either a tool
> call (preferred) OR a fallback question stem typed in prose that has a
> verifiable answer. If neither is possible, switch move to `explain` and restate
> the OPEN QUESTION in simpler words instead of trailing off.

#### G7. `CONFIRM_AND_EXTEND` does not protect the extension against safe-template
collapse

Lines 232–251 require a follow-up pose — but T5 of this session emitted the move,
got `fallback_used=true`, and dropped the extension entirely. The prompt has no
"extension is mandatory; if you can't author one, switch to `close_topic`"
discipline. Recommend:

> **Extension floor.** If you cannot author a clean extension (different numbers,
> units, or edge case) on this same concept, do NOT emit a generic affirmation —
> switch move to `close_topic` so the engine advances rather than spinning.

#### G8. No figure-ref directive in `POSE_QUESTION` or `SCAFFOLD_HINT`

Neither move prompt instructs the model on when it may reference media. T2 of this
session invented a diagram. The shared preamble has no media-availability block
either. Recommend adding to `SHARED_PREAMBLE_TEMPLATE` (around the "Subject
anchoring" block):

> **Media availability.** This lesson's available media is: {media_catalog}.
> Do not reference any image, diagram, figure, box, or "the picture above/below"
> unless it appears in this list. If you need a visual to teach a point, say so in
> words and describe the relationship abstractly.

(`{media_catalog}` is rendered from the existing lesson media catalog used elsewhere
in the prompt builder.)

#### G9. `POSE_QUESTION` allows opening-turn ungraded student claim to drop on the floor

T1 of this session: the student gave a perfect mechanism-level answer to the open
question "name one process". The tutor responded with a safe template that did not
acknowledge or grade the answer. This is because at T1 there was no `open_question`
yet (the opener was prose-only, T0 ended with a question mark but no tool call).

Recommend in `EXPLAIN`'s "no verdict / opening turn" branch (lines 366–377):

> **Opening question pose.** If the opener ends with an assessment question (a
> question with a verifiable answer), it MUST be posed via the tool, not as prose.
> An opener that asks "name one process" without a tool call leaves the student's
> reply ungraded and forces an empty-handed next turn.

#### G10. No prompt-level handle for "advanced over-qualification"

None of the nine move prompts has a concept of "the student is exceeding the bank's
expected answer". An advanced learner who consistently over-qualifies should
trigger `confirm_and_extend` not `scaffold_hint`. The prompts assume the move
selector resolves this — but in this session the move selector kept returning
`scaffold_hint` because the grader couldn't verify the rich answers and `unverified`
verdicts route to scaffold_hint.

Recommendation pairs with §B3 (engine-side over-qualify signal): once the engine
sets `over_qualifies=true`, `CONFIRM_AND_EXTEND` should be invoked with an explicit
"raise the stake" directive in the prompt:

> **Over-qualified branch.** If the engine flags `over_qualifies=true`, your
> follow-up question must be harder than the open question (transfer, mechanism,
> or edge-case), not a parameter twist.

---

## 5. Summary

- **P1**: FAIL — four incomplete-question violations (T2 figure-ref + T4/T6/T8
  colon-terminated empty pose). The figure-ref one is one-off; the colon-terminated
  pattern is systematic and the same root cause produced all three instances.
- **Strongest turn**: T3 (clean locale-anchored True/False that the student answered
  perfectly on T5).
- **Weakest moment**: T7 — tutor admits the omission, then fails to fix it.
- **Top fix**: deterministic conformance-gate rule that rejects question-less
  scaffold_hint / pose_question / confirm_and_extend turns, and the safe-template
  for these moves must itself carry a question stem.
- **Second fix**: lesson-media-catalog block in `SHARED_PREAMBLE_TEMPLATE` to
  prevent diagram hallucination at the prompt level (cheaper than catching it in
  conformance every time).
- **Third fix**: over-qualification signal from grader → `confirm_and_extend`
  prompt branch so advanced students get harder, not safer.
