# GEO-S5 Tutor Evaluation — 2026-05-27

**Persona**: S5 (Cycle 5) struggling geography student, Seychelles.
**Engine**: v2 (PR#12 cutover, `apps/tutoring/v2/`).
**Session**: `TutorSession.id=97`, lesson `1451` — *The Hydrological Cycle Overview* (Belonie Geography S3).
**Turns**: 31 (16 tutor / 15 student). 6 graded attempts (2 correct, 4 wrong).
**Conformance**: 6 violations across 16 tutor turns (38%); 6 fallback templates fired.
**Outcome**: Lesson ended verbally; exit-ticket modal never opened (`show_exit_ticket=False` throughout).

---

## 1. P1 Unacceptable Errors

| P1 type | Found? | Evidence |
|---|---|---|
| Wrong answer marked correct | **YES** | T1479 (and T1481 reinforcement). |
| Correct answer marked wrong | **No** | The two `verdict=correct` calls were both genuine. |
| Incomplete question posed | **Marginal — 1 instance** | T1465 — True/False stem with no explicit answer choices block, but the "True or False:" lead makes the format unambiguous. Flagged as borderline. |

### P1.1 — Wrong answer treated as correct (Turn 1479)

**Sequence:**
- T1476 student: *"is it precipitation then condensation then evaporation then collection?"* (incorrect ordering — should be evaporation → condensation → precipitation → collection)
- T1477 tutor: *"Let's get that order nailed down properly."* (close-on-colon, no scaffold, no correction — caught by conformance later)
- T1478 student: *"can you tell me the right order?"* (an **explicit help-request**, not an attempt)
- **T1479 tutor: *"Nice work on this one. You're ready for the exit ticket — I'll set it up."*** ← P1

The engine routed `close_topic` with `fallback_used=True` on T1479, and the safe template said the student had done "nice work" and was ready for the exit ticket — **on a turn where the student had explicitly stated they did not know the answer and asked for the right order**. This is a textbook P1: the tutor signalled correctness on a non-attempt that contained no correct content. The student would reasonably leave the session believing they had mastered the ordering — they had not.

T1481 then doubles down on the same misclassification, reciting an inaccurate summary of what the student had supposedly demonstrated:

> *"You've worked through condensation, the cycle being continuous, and why 'False' was the right call on that True/False — solid effort getting there."*

The student had not worked through "the cycle being continuous" — they had asked the tutor what the answer was, and the tutor had told them.

### P1.2 — Borderline incomplete question (Turn 1465)

> *"True or False: During the hydrological cycle in Seychelles, water can only move from the ocean to the atmosphere through evaporation, and once it returns as precipitation, the cycle ends because the water stays on the islands."*

The "True or False:" prefix carries the answer format, so a charitable read says this is not a P1 incomplete-question. But compared to T1455 — which rendered the same kind of question with an explicit `A) Evaporation / B) Condensation / C) Precipitation / D) Collection` block — the T1465 pose lacks the A/B answer block the lesson step likely carries. Same defect as MATHS-S1 T1442. Worth flagging as a consistency gap.

---

## 2. Other Quality Failures (sub-P1, still serious)

### 2.1 Wrong answer affirmed (caught by conformance, replaced by fallback)

T1466 student answered the True/False with *"true... i think? because the rain stays on the islands and we drink it"* — the correct answer is False. The `worked_example` move produced a response that affirmed correctness; conformance fired `wrong__no_affirmation: verdict=wrong: response must not affirm correctness` and replaced the output with the safe template (T1467 — the lesson engage paragraph verbatim). The conformance defence prevented the P1 from reaching the student, but the **fallback content is itself broken** — it dumps the lesson opener back at a confused student who had just answered wrong, with no acknowledgement of their attempt and no scaffold on the True/False question they are still trying to answer. The student then guesses "true again" on T1468.

### 2.2 Verbatim engage-paragraph regurgitation, twice in one session

T1467 and T1473 emit the **identical** lesson opener ("Every drop of water you drink, every cloud you see, and every raindrop that falls on Mahé…") to a struggling student who has just (a) guessed wrong on True/False, (b) followed up asking for a confirmation of their answer. The student is trying to converge; the tutor is resetting them to t=0 of the lesson narrative. Same defect pattern as MATHS-S1 T1434/T1436 — fallback templates reach for the engage paragraph as a safe surface, and it is **anti-pedagogical** for a struggling student because it strips the conversational thread and re-loads the framing they have already heard.

### 2.3 Praise-without-content on T1461

Student answered "B condensation?" correctly. Tutor (T1461, `pose_question` with `verdict=correct`, fallback): *"Right — correctly identified condensation. Let's take one small step on what we're working on. Tell me the first thing that comes to mind, and we'll build from there."*

For a struggling student this is worse than the same template hitting the advanced student. The struggling student needs the *"because…"* clause — *because condensation is when water vapor cools and turns to droplets, which matches Box 2's description*. They got the right answer by elimination (after the tutor's hint at T1459 ruled out three options); they need confirmation of *why* it is right to anchor the concept. The fallback strips that.

### 2.4 Free-text ordering question posed in prose, not via the tool

T1463 ends with: *"can you put them in the order a water droplet from the Indian Ocean would experience them?"* — a question with a single canonical answer (E → C → P → C), posed in prose rather than via `pose_question`. Conformance caught the related issue downstream (T1479 fallback fired with `all__no_assessment_in_prose`). The `EXPLAIN` move's prompt explicitly forbids this (lines 753–763): *"Never end with a verifiable-answer question typed in prose."* The model emitted one anyway, and the consequence was that the student's wrong ordering at T1464 landed without a verdict — and then at T1476 the second wrong ordering also landed without a verdict.

### 2.5 No `name_misconception` ever fired

The student got the **same kind** of question wrong on T1456 (Evaporation instead of Condensation) and T1466 (True for "cycle ends on island"). They demonstrated a confusion about cycle direction (T1464: condensation-before-evaporation) and again on T1476 (precipitation-before-condensation). That is a stable, namable misconception: **the student does not have the directional flow of the cycle in their head**. Yet `name_misconception` was never selected. The engine picked `worked_example` twice and then routed straight to `close_topic` without ever giving the student a chance to confront the specific slip by name. (Targeted Remediation Ch.21 — diagnose the root cause; component-level pinpointing.)

### 2.6 Premature close on a struggling student

T1479 + T1481 close the lesson with `correct=2, wrong=4` on the objective (`objective_progress` snapshot). A student who answered wrong twice as often as right is **not ready for the exit ticket**. Compare to the MATHS-S1 report, where the same templates fired after one correct answer — the close logic is permissive in both directions.

---

## 3. Science-of-Learning Adherence

| # | Principle | Status | Observation |
|---|-----------|--------|-------------|
| 1 | Active Learning | 🟡 Amber | Student attempted on most turns, but T1461, T1467, T1473, T1483 ended on filler with no concrete student action. The doing-rate window had 6/16 verdictless turns. |
| 2 | Direct Instruction | 🟡 Amber | T1459 (worked example after wrong attempt on Box 2) was a textbook subgoal-labelled walkthrough — green. But T1463 explained the term "hydrological" and then closed on a prose ordering question instead of teaching the cycle order explicitly with subgoals before retrieving it. |
| 3 | Deliberate Practice | 🔴 Red | The cycle-ordering question (the student's persistent slip) was never re-posed at the edge of their ability. The session moved past it without remediation. |
| 4 | Mastery Learning | 🔴 Red | Lesson closed with objective evidence at 2 correct / 4 wrong. The bar was effectively lowered to "we ran out of moves". |
| 5 | Minimise Cognitive Load | 🟢/🔴 Mixed | T1459's subgoal-labelled worked example was green — exactly the right move for the wrong answer on Box 2. But T1457, T1467, T1473 dumped the full lesson opener (~180 words) on a confused student — pure cognitive load with no labelling. Conformance flagged `one_question_per_turn` on T1457 (two action prompts in one turn). |
| 6 | Automaticity | ⚪ N/A | Out of MVP scope. |
| 7 | Layering | ⚪ N/A | Single-objective session. |
| 8 | Non-Interference | ⚪ N/A | Single-topic session. |
| 9 | Spaced Repetition | ⚪ N/A | Out of MVP scope. |
| 10 | Interleaving | ⚪ N/A | Out of MVP scope. |
| 11 | Testing Effect / Retrieval | 🟢 Green (for the box-labelling Q only) | T1459 → T1460 → T1461 was the correct shape: hint, retrieval, feedback. |
| 12 | Targeted Remediation | 🔴 Red | The cycle-direction misconception was never named, never component-skill-tested, never closed. The student's last interaction on it was the tutor telling them the answer (T1475), followed immediately by a `close_topic` (T1477) and then a wrong-marked-correct close (T1479). |
| 13 | Gamification | ⚪ N/A | Out of MVP scope. |

**Net read**: this session is a **net pedagogical loss** for a struggling student. The engine did one thing well — the Box-2 multiple-choice scaffold (T1459) was textbook good. Everything after that diverged from the playbook: wrong-affirmed (caught by conformance, replaced with engage-dump), help-request treated as success, premature close on majority-wrong evidence.

---

## 4. Conformance Telemetry

| Turn | Move | Verdict | Fallback? | Violation |
|---|---|---|---|---|
| T1457 | worked_example | wrong | ✓ | `one_question_per_turn` |
| T1461 | pose_question | correct | ✓ | `state_coherence: verdict produced without open_question` |
| T1467 | worked_example | wrong | ✓ | `wrong__no_affirmation` |
| T1471 | pose_question | correct | ✓ | `state_coherence: verdict produced without open_question` |
| T1473 | explain | (no verdict) | ✓ | `no_verdict_claim__no_affirm` |
| T1479 | close_topic | (no verdict) | ✓ | `all__no_assessment_in_prose` |

Six tutor turns out of sixteen tripped a conformance check. Of these, **all six** were replaced with fallback templates — meaning the actual surface output the student saw was not the model's chosen reply, it was a safety-floor template. **The dominant signal in this session was the safety floor, not the tutor.**

The two `state_coherence` violations mirror MATHS-S1: the grader produces a verdict but `runtime_state.open_question` is `None` at that point. Same suspected engine bug.

---

## 5. Specific Recommendations for `apps/tutoring/v2/services/move_prompts.py`

Three additions / two amendments. The R1, R2, R4 items from the MATHS-S1 report also apply here — not repeated.

### R7 — `CLOSE_TOPIC` (lines 859–888): require a verdict AND a competence threshold.

Symptom: T1479 closes a topic after a help-request, and the underlying objective state was 2-correct / 4-wrong. Add to the body:

> *Do not emit a CLOSE_TOPIC turn unless either (a) the most recent student turn carried a `verdict=correct` AND the objective's correct count is ≥2, OR (b) the engine explicitly handed off after a `pivot` exhaustion. Help-requests, verdictless turns, and "tell me the answer" requests must NOT close the topic — they route back to `worked_example` or `name_misconception`.*

The model should treat the fallback template *"Nice work on this one. You're ready for the exit ticket — I'll set it up"* as **never appropriate when there's no verdict on the immediately prior student turn**. The current prompt does not say that.

### R8 — Add explicit help-request → `worked_example` routing in `CLOSE_TOPIC` body.

When the student says *"can you tell me the right order?"* or *"what is the answer?"*, the closed-move-set choice is `worked_example` (or `explain` with method delivery), never `close_topic`. The `CLOSE_TOPIC` prompt should refuse to fire on a help-request:

> *DEFENSIVE: If the prior student turn is a help-request ("tell me the answer", "can you tell me", "what's the right order", "what's the answer", "I give up"), do NOT close the topic. Instead, fall back to a one-sentence handoff sentence ("Let's walk through this one together.") and stop — the engine will route the next turn to `worked_example`.*

### R9 — `NAME_MISCONCEPTION` (lines 584–635): broaden the trigger from "three wrong attempts on the same item" to "stable directional / categorical confusion across items in the same lesson".

The current move triggers on *"three wrong attempts on the same item or subskill"*. The geo session had wrong attempts on **different items** (T1456 evaporation/condensation swap, T1464 ordering, T1466 cycle-end T/F, T1476 ordering) that all share the same underlying confusion (cycle direction). Add to the body's trigger note:

> *Also fire when the student has produced ≥2 wrong answers across different items in the same lesson that share a common named misconception (cycle direction, units-conversion direction, sign-of-operation, etc.). The repeated wrong-across-items signal is the strongest evidence the student does not own the underlying distinction; one focused naming turn is worth more than another worked example.*

(The engine triggers `name_misconception`, not the prompt — but flagging this in the prompt body makes it clear to the LLM when the move is in flight that "I'm here because of cross-item evidence", which shapes the diagnostic.)

### R10 — Tighten the `EXPLAIN` ban on prose-posed verifiable questions.

The prompt already says *"Never end with a verifiable-answer question typed in prose"* (lines 753–763). It happened anyway on T1463 (*"can you put them in the order…"*). Add a more concrete check:

> *Self-check before emitting: if the question you are about to write ends in "what is …", "which is …", "put them in order", "what is the value of …", "name the …", that question has a canonical answer and MUST be posed via the tool. If no tool slot is available for it, do not pose it at all — emit a reflective open-ended prompt instead ("what surprises you about this?", "where might you see this in everyday life?"). A prose-posed verifiable Q is the dominant failure mode this move has.*

### R11 — `SCAFFOLD_HINT` (lines 476–581): credit-rule reinforcement.

The student named one stage correctly at T1454 ("the water dries up and disappears") and again at T1470 ("it goes back to the ocean"). Neither was credited as a partial — both were treated as wholly wrong / verdictless. The `SCAFFOLD_HINT` prompt has a strong partial-credit rule already (lines 488–497), but it only fires on `verdict=wrong/partial`. T1454 had no prior question (it was the opening response to the engage prompt), so no verdict, so no scaffold — the partial-credit didn't get a chance. This is a routing gap more than a prompt gap; mention to the engine owners.

---

## 6. Summary

- **One P1 violation** — wrong answer treated as correct at T1479 (help-request closed as success), with T1481 reinforcing the misclassification.
- **One borderline P1** — T1465's True/False stem rendered without an explicit answer-choice block.
- **Six conformance violations across sixteen tutor turns**: half the session was safety-floor output, not engine-chosen output.
- **The struggling persona was poorly served**: the engine did one move well (T1459's worked example with subgoals on Box 2), then degraded into engage-paragraph dumps, premature closes, and a wrong-marked-correct ending.
- **Recommended prompt edits**: R7, R8, R9, R10 above. The biggest single win is R7+R8 — kill the path where `close_topic` fires on a help-request.
- **Recommended engine investigation**: same `runtime_state.open_question` bug surfaced in MATHS-S1, plus the cycle-direction misconception was never named despite being a textbook trigger for `name_misconception` (cross-item recurrence in the same lesson).
