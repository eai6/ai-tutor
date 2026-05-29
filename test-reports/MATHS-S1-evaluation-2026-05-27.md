# MATHS-S1 Tutor Evaluation — 2026-05-27

**Persona**: S1 (Cycle 4) advanced mathematics student, Seychelles.
**Engine**: v2 (PR#12 cutover, `apps/tutoring/v2/`).
**Session**: `TutorSession.id=96`, lesson `1177` — *Use Pythagoras theorem to prove whether a triangle is a right-angled triangle*.
**Turns**: 25 (12 tutor / 13 student). 4 graded student attempts, all returning `verdict=correct`.
**Conformance**: 6 violations across 12 tutor turns (50%); 6 fallback templates fired.
**Outcome**: Lesson ended verbally; exit-ticket modal never opened (`show_exit_ticket=False` throughout).

---

## 1. P1 Unacceptable Errors

| P1 type | Found? | Evidence |
|---|---|---|
| Wrong answer marked correct | **No** | All four graded responses were genuinely correct. |
| Correct answer marked wrong | **No** | Grader produced `correct` on every assessed turn. |
| Incomplete question posed | **YES — 1 instance** | Turn 1442 (see below). |

### P1.1 — Incomplete question (Turn 1442)

The tutor labelled this turn the "exit ticket" and asked the student to *decide*, but presented no choice options:

> *"Here's your exit ticket — read carefully and decide*
>
> *A roof frame has sides 7 m, 24 m, and 25 m. This frame contains a right angle because 7² + 24² = 25²."*

`LessonStep.id=10107` (step 7) is a True/False item (`expected_answer="True"`); the True/False options are missing from the rendered prompt. The student happened to infer the format and answered "True", but the question as posed has no surface signal that it is binary, and no answer set. Failure mode: the `pose_question` move emitted a bare statement stem without the answer-choice block the bank step carries.

---

## 2. Other Quality Failures (sub-P1, still serious)

### 2.1 Confirm-after-correct is a non-answer fallback

The student gave a **full, mechanism-rich** correct answer at T1431:

> *"The longest side is 13 cm, so c = 13. The other two sides are a = 5 and b = 12. a² + b² = 25 + 144 = 169. c² = 13² = 169. Since 169 = 169, the triangle is right-angled."*

The tutor replied at T1432 with the **safe terminal template**:

> *"Got it — you matched the answer. Let's take one small step on what we're working on. Tell me the first thing that comes to mind, and we'll build from there."*

This was a `pose_question` move with `verdict=correct` and `fallback_used=True`. Conformance fired `state_coherence: verdict produced without open_question in runtime_state` — the engine graded the answer but the prior pose did not register an `open_question` in `runtime_state`. Because the safe template is content-free, the student receives **no informative feedback** on the correct working they just produced (Active Learning Ch.10 — feedback must be informative). The exact same template fires again at T1444 and T1448 — three correct rich answers in a row receive no acknowledgement of what was correct.

### 2.2 Engage-prompt regression on student "give me a problem"

After T1432's empty affirm, the student says *"Give me the next practice problem please."* The tutor (T1434, `explain` move, fallback) responds with the **lesson opener verbatim** ("Imagine you're building a boat ramp at the Seychelles Fishing Port…"), ending *"Tell me which part of that you'd like to start with."* The student tries again with *"Squaring the sides — and I am ready, ask me a problem"* and the tutor (T1436) returns the **identical opener again**. Two consecutive turns of the same canned engage block in response to clear student "ready for practice" signals. The student had to explicitly request a worked example before the engine advanced.

### 2.3 Premature "you're ready for the exit ticket" (after 1 practice item)

T1440 fires `close_topic` after exactly ONE practice item in the live session (T1439, item B from step 5). With only one assessed answer on the lesson objective the engine declares completion. The closed-topic line is *itself* the safe fallback ("Nice work on this one. You're ready for the exit ticket…"), and it fires again at T1448 after a second correct answer. The actual exit-ticket modal never opens (`show_exit_ticket=False` on every response, `exit_ticket=None`), so the verbal "I'll set it up" is a broken promise.

### 2.4 Final two turns are non-action endings

T1450 says "That's the lesson done. Let's move on to the next objective." T1452 says "The next part of the lesson is coming right up — your teacher or the next screen will have it for you." Neither closes on an action the student can take, and neither hands off to a real UI transition. (Active Learning — turn must end on a student action.)

### 2.5 Bonus / advanced-student adaptation: zero

The student demonstrated mastery on the 5/12/13 case with full working in one shot. The engine then asked an **easier** 6/8/10 multiple-choice version. There is no `confirm_and_extend` (twist the parameter, push to discrimination) anywhere in the trace; every correct answer routed to `confirm_and_advance` or `close_topic` with the same template, then the bank exhausted. An advanced student got asked nothing harder than the worked example they were already shown. (Deliberate Practice Ch.12 — keep the next problem at the edge of *this* student's ability.)

---

## 3. Science-of-Learning Adherence

Scoring conventions: **Green** = imperative honored. **Amber** = partial. **Red** = violated this session.

| # | Principle | Status | Observation |
|---|-----------|--------|-------------|
| 1 | Active Learning | 🟡 Amber | Student attempted on most turns, but 3 tutor turns were empty action-handoffs (T1432, T1440, T1448 — generic "tell me the first thing that comes to mind"). The intended ≥60% doing-rate held only because the student kept pushing — not because the engine kept demanding action. |
| 2 | Direct Instruction | 🟢 Green | Worked example (T1438) was anchored, labelled in 4 subgoals, followed by practice. |
| 3 | Deliberate Practice | 🔴 Red | The two practice items (5/12/13 and 6/8/10) sat at or below the worked-example difficulty. No edge-of-ability push despite rich correct answers. No `confirm_and_extend` ever fired. |
| 4 | Mastery Learning | 🟡 Amber | The bar held (problems were on-objective), but evidence threshold for closing the objective was suspiciously low — *one* practice item triggered T1440's close. |
| 5 | Minimise Cognitive Load | 🟢/🔴 Mixed | Worked example was correctly chunked into labelled subgoals (green). But T1434/T1436 emitted the engage opener with a generic "tell me what to start with", and T1432 piled a "tell me the first thing that comes to mind" on top of a verdict — conformance flagged `one_question_per_turn` on T1436 (2 action prompts in one turn). |
| 6 | Automaticity | ⚪ N/A | Not in MVP scope per `move_prompts.py` docstring. |
| 7 | Layering | ⚪ N/A | Single-objective session. |
| 8 | Non-Interference | ⚪ N/A | Single-topic session. |
| 9 | Spaced Repetition | ⚪ N/A | Out of MVP scope. |
| 10 | Interleaving | ⚪ N/A | Out of MVP scope. |
| 11 | Testing Effect / Retrieval | 🟡 Amber | Retrieval was offered, but the immediate feedback that consolidates the retrieval (Ch.20) was twice the empty "Got it — you matched the answer" template — that does not consolidate the right pattern. |
| 12 | Targeted Remediation | ⚪ N/A | No wrong attempts to remediate. |
| 13 | Gamification | ⚪ N/A | Out of MVP scope. |

**Net read**: structural conformance is doing its job (it correctly flagged the `state_coherence` and `active_end_required` defects), but the safe **fallback template** is so content-free that it strips the very feedback an advanced student needs. Six of twelve tutor turns landed on a fallback — half the session was a safety floor, not teaching.

---

## 4. Root Cause: `state_coherence` open_question missing

Four of six conformance violations were `state_coherence: verdict produced without open_question in runtime_state`. The grader is matching the student answer to a question (and getting it right — the verdicts `correct` were accurate), but `runtime_state.open_question` is `None` at adjudication time. Inspection of `TutorSession.runtime_state`:

```
rt.state            : None
rt.current_step_idx : None
rt.open_question    : None
rt.last_move        : None
```

These top-level keys never populated for the session, even though `objective_progress`, `posed_question_ledger`, and `safety_valve_counters` were updated normally. That suggests the pose path is writing to the ledger but **not** to the open_question slot — or the open_question is being cleared too eagerly after verdict, before the next pose. Either way, every subsequent verdict appears to conformance as "verdict without prior pose", triggering the safe terminal template — which is the dominant negative signal in the session.

This is a **v2 engine bug**, not a prompt bug. Worth filing separately with the v2 observability dashboard team.

---

## 5. Specific Recommendations for `apps/tutoring/v2/services/move_prompts.py`

The prompt file itself is well-organised and most directives are already aligned with `science-principles.md`. The gaps observed in this run are mostly downstream (engine state + fallback template content), but a few prompt changes would harden the failure modes that surfaced:

### R1 — `EXPLAIN` (lines 715–821): forbid verbatim re-emission of the lesson opener.

Symptom: T1434 and T1436 re-emit the *exact* engage paragraph back-to-back when the student says "ask me a problem". Today the prompt covers help-requests well, but it does not cover the case where the student is signalling they're *ready* (the inverse of a help-request). Add a directive in the "How (no verdict / opening turn)" block:

> *If the prior student turn signalled readiness ("I'm ready", "ask me a question", "give me a problem", "let's go"), do NOT re-emit the engage framing. The student already heard it. Either (a) hand off to `pose_question` on the next eligible slot, or (b) emit one transitional sentence and end the turn — never repeat the previous engage paragraph.*

### R2 — `CONFIRM_AND_ADVANCE` (lines 374–417): block the empty terminal-template shape.

Symptom: T1432, T1444 — the verdict-keyed safe template "Got it — you matched the answer. Let's take one small step on what we're working on. Tell me the first thing that comes to mind, and we'll build from there." is the LLM's chosen surface output when it can't anchor to an `open_question`. The prompt already says "do not open with a stand-alone praise token", but does not forbid the *trailing* generic ask. Add:

> *NEVER end a CONFIRM_AND_ADVANCE turn with a content-free invitation ("tell me the first thing that comes to mind", "what would you like to try next", "where would you like to start"). Either pose the next slot via the tool, or — if no slot is eligible — close the topic explicitly via `close_topic`.*

This won't *fix* the state_coherence bug, but it ensures that when the model is forced into this terminal shape it picks the explicit close move, not the empty filler.

### R3 — `CLOSE_TOPIC` (lines 859–888): gate on objective evidence threshold.

Symptom: T1440 fires close after **one** practice item on the active objective. The current prompt says "Objective evidence is sufficient — close this topic" but gives the model no rule about *how much* evidence. Add:

> *Do not close an objective on a single correct attempt. The minimum evidence is two distinct correct retrievals on the same objective — one is a coin flip, two is a signal. If only one correct attempt is in hand, pose a second item via `confirm_and_extend`.*

(Move selection is in `TutorEngine.pick_move`, not in the prompt, so this is belt-and-braces — but the prompt is the last line of defence when the engine routes prematurely.)

### R4 — `POSE_QUESTION` (lines 314–371): explicit answer-choice fidelity.

Symptom: T1442 emitted a True/False statement without the True/False options block. The `pose_question` tool description (not inspected here) presumably renders the bank's `choices` field — but the prompt body does not call this out. Add to "What NOT to put in this turn":

> *Do not abridge the bank stem. If the bank entry carries answer choices (A/B/C/D, True/False, multi-select), the rendered question must include them. A pose that omits the choice set is an incomplete question (P1).*

### R5 — `SHARED_PREAMBLE_TEMPLATE` (lines 63–227): add an "advanced learner" branch.

Symptom: an advanced student received the same problem difficulty throughout. The preamble has the doing-rate sized-down branch ("hedging → smaller and easier"), but no sized-up branch. The student's doing-rate window held `[true, true, ...]` and the streak was 4-of-4 correct, yet nothing in the preamble tells the model to push harder. Add (mirrors the existing hedging branch on lines 184–192):

> *When the doing-rate signal says the student is at 4/4 or 5/5 attempted-and-correct, size the next ask LARGER and HARDER than the open question — a twist on the parameter, a transfer to a new context, or a discrimination pair. Match the bar to demonstrated competence, not to the lesson floor.*

### R6 — `CLOSE_TOPIC`: outlaw the broken "I'll set it up" handoff.

Symptom: T1440, T1448, T1479, T1481 all use the line *"You're ready for the exit ticket — I'll set it up"*, but the exit-ticket modal never opens. The prompt currently says *"The frontend listens for these cues; do not bury the transition"* — which is the directive that **caused** this line to be emitted. If the frontend isn't honoring it, that is a frontend or engine bug, but the prompt should fall back to a safer phrasing when the close is itself a fallback. Add:

> *If this CLOSE_TOPIC turn is itself a fallback (i.e. it was generated after a conformance rejection), end on "the lesson is complete" rather than "I'll set it up" — making a promise the frontend can't keep is worse than no promise.*

---

## 6. Summary

- **One P1 violation** — incomplete True/False question at T1442 (the "exit ticket" turn).
- **No P1 grading errors** — the grader was accurate on all four correct answers.
- **The dominant quality problem is the safe terminal template** firing in place of `confirm_and_advance`, driven by an upstream engine bug (`runtime_state.open_question` never populating).
- **The advanced learner persona was wasted** — the engine asked nothing harder than the worked example, never extended a correct answer, and closed the objective after a single practice item.
- **Recommended prompt edits**: R1–R6 above. The biggest wins are R1 (kill the engage-paragraph re-emission), R2 (kill the empty terminal template), and R5 (size up for advanced doing-rate).
- **Recommended engine investigation**: why `runtime_state.open_question` is `None` after a `pose_question` move in a v2 session. This is the upstream driver of half the failures observed.
