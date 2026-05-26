# GEO-S5 Tutor Evaluation — 2026-05-26 (run 5)

**Persona**: S5 (Cycle 5) advanced Geography student in Seychelles. Provides
correct answers with mechanism-level reasoning.
**Lessons evaluated**:
  - L1438 — *Define and Classify Weathering* (Geography S3 / Weathering unit).
  - L1439 — *Agents of Weathering* (continuation lesson).
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on`).
**Student**: `student1` at School-3.
**Session IDs**: 88 (L1438, 4 turns → exit ticket), 89 (L1439, 3 turns → exit
ticket).

> Why two lessons in one report: each lesson closed in 3–4 turns because
> `close_topic` fires after 2 correct answers on the current objective. The
> persona requires > 5 substantive turns, which only one lesson cannot
> deliver under the current engine threshold.

---

## 1. Transcripts

### 1.a Session 88 — L1438 *Define and Classify Weathering*

| # | Role | Content | move / verdict / fallback / conformance violations |
|---|------|---------|----------------------------------------------------|
| 0 | tutor | Today we're diving into **Define and Classify Weathering**… *(direct-instruction frame: weathering = breakdown of rock at/near surface; no transport; coral / volcanic stones examples)* *(opening prose Q: "what do you reckon might cause rocks to break down in different ways?")* | `explain / — / no / —` ✅ Opening Q is open-ended (no canonical answer) — appropriate for explain. |
| 1 | student | Several processes — physical weathering from temperature changes and frost, chemical weathering from rainwater acidity and oxidation, and biological weathering from plant roots and lichens. | (rich, complete) |
| 1 | tutor | Let's see how those ideas hold up with a real example. A student collects a basalt rock sample from a Seychelles beach. When she examines it under a magnifying glass, she sees that the mineral surfaces are pitted and have changed colour, and in some spots, the minerals look softer than in others. The internal structure of the rock has not cracked. Which type of weathering best explains these observations? | `pose_question / — / no / passed` ✅ Tool-posed bank question, anchored to a Seychelles context, single-slot. Open-ended T1 received no verdict (correct — open Qs are not graded). |
| 2 | student | Chemical weathering — the pitting and colour change without cracking points to mineral alteration through processes like hydrolysis and oxidation rather than mechanical breakdown. | (**correct + full mechanism reasoning**) |
| 2 | tutor | Got it — identifying chemical weathering and the roles of oxidation and hydrolysis. **Let's slow down on the same question:** A student collects a basalt rock sample from a Seychelles beach…[full Q restated]…Tell me what you'd do first, even if you're not sure about the rest. | `confirm_and_extend / correct / **yes** / [open_question_stickiness: posed new item 13915 vs open 13913]` ❌ **P1-CLASS**: student gave a fully correct expert-level answer. The tutor's first phrase ("Got it — identifying chemical weathering and the roles of oxidation and hydrolysis.") acknowledged it. But the rest of the turn shipped the verdict-keyed safe-template **for the partial/wrong/unverified path** — restating the same question and asking the student to "try just the first step." Functionally tells a correct answer it's wrong. |
| 3 | student | Chemical weathering. | (bare correct, re-confirming) |
| 3 | tutor | You nailed it — colour change, pitting, and softening with no cracking is the chemical weathering signature. You're ready for the exit ticket — I'll set it up. | `close_topic / correct / no / passed` ✅ Clean close. ⚠ But after only **ONE practiced item**, the 10-step lesson is closed and the student is shoved to the exit ticket. |

### 1.b Session 89 — L1439 *Agents of Weathering*

| # | Role | Content | move / verdict / fallback / conformance violations |
|---|------|---------|----------------------------------------------------|
| 0 | tutor | Today we're kicking off our lesson on **Agents of Weathering**… *(wind, water, ice, living things; Beau Vallon granite reference)* *(open Q: "can you name one agent of weathering you think you've already seen in action near where you live?")* | `explain / — / no / —` ✅ |
| 1 | student | Salt spray — at Beau Vallon I see how sea-salt crystals form in the cracks of granite, and as they expand they wedge the rock apart. That is a form of salt weathering driven by water as an agent. | (rich, correct, with mechanism) |
| 1 | tutor | Salt crystal expansion is a great real-world observation — let's see how you apply that thinking here. A geographer visits a cliff face near Anse Intendance in June and observes three different features: (1) Large cracks radiating outward from a boulder exposed to intense daytime sun; (2) A reddish-brown stain on the rock surface where moisture seeps through; (3) A mango tree's roots pushing through cracks in the rock. Which of these is an example of chemical weathering? | `pose_question / — / no / passed` ✅ ⚠ Note: lesson title is *Agents* of weathering but the posed question is about *types* — slight subject drift, but tolerable. |
| 2 | student | Feature 2 — the reddish-brown stain is oxidation, where iron-bearing minerals react with oxygen and water to form iron oxide. That is chemical weathering. | (**correct + mechanism**) |
| 2 | tutor | Got it — correctly identifying Feature 2 and the process of oxidation. **Let's slow down on the same question:** A geographer visits a cliff face near Anse Intendance…[full Q restated]…Tell me what you'd do first, even if you're not sure about the rest. | `confirm_and_extend / correct / **yes** / (same stickiness violation pattern as L1438 T2)` ❌ **P1-CLASS — identical to L1438 T2.** The advanced answer is acknowledged in the first clause, then the fallback template tells the student to "slow down" on a question they already aced. |
| 3 | student | Feature 2. | (bare correct, re-confirming) |
| 3 | tutor | You nailed the oxidation identification. Let's move on to the next part of our lesson on Agents of Weathering. *(exit ticket payload attached)* | `close_topic / correct / no / passed` ✅ Clean close — but again **only one practice item** before the 10-step lesson terminates. |

---

## 2. P1 Unacceptable Errors

| # | P1 type | Evidence | Severity |
|---|---------|----------|----------|
| **P1-A** | Tutor effectively says correct answer is wrong | **L1438 T2 + L1439 T2**: in both sessions, the student gave a fully correct answer with mechanism, and the tutor responded by re-posing the same question and asking the student to "tell me what you'd do first, even if you're not sure about the rest." The first-clause affirmation ("Got it — …") is so brief it reads as a throwaway compared to the dominant "let's slow down" body. | **HIGH** — for an advanced student this reads as the tutor not understanding their answer. It also predicts an advanced student will disengage. |
| **P1-B** | Posing incomplete questions | None observed. Both posed questions were complete, single-slot, well-anchored to a Seychelles context. |
| **P1-C** | Tutor says wrong answer is correct | None observed. |

---

## 3. Science of Learning Evaluation

| Principle | Working / Failing | Evidence |
|-----------|-------------------|----------|
| **Active Learning** (Ch.10) | ❌ **One practice item per lesson** is far below the Ch.10 imperative of "practice problem count ≥ ~7× worked-example count per topic." 1 / 1 ratio across two consecutive lessons. | L1438 closed after 1 bank Q; L1439 closed after 1 bank Q. |
| **Direct Instruction** (Ch.11) | ✅ T0 of each session is clean DI — concept named, definition stated, Seychelles example, then open-ended invitation to engage. |  |
| **Deliberate Practice** (Ch.12) | ❌ "Edge of ability" calibration absent. The student answered the first bank Q with mechanism detail that overqualified the stem; the engine had two paths to push (a) `confirm_and_extend` with a harder discrimination, or (b) raise the rung. Neither happened — the tutor re-asked the *same* question, then closed the topic on a bare-correct repeat. |  |
| **Mastery Learning** (Ch.13) | ⚠ The "bar" was held (no item was watered down), but only ONE bar was tested. The lesson title says "Define and Classify" — *classify* was tested once; *define* never. |  |
| **Minimise Cognitive Load** (Ch.14) | ✅ Single-slot pose; reasonable opener; no over-teaching. ⚠ Expertise-reversal violation in T2 of both sessions — the fallback "slow down" template imposes scaffolding ON A STUDENT WHO JUST DEMONSTRATED MASTERY. This is the textbook expertise-reversal trap that `move_prompts.py:170` explicitly warns against. |  |
| **Layering** (Ch.16) | ❌ The lessons are sequenced (Define→Agents) but within a single lesson, no composition of subskills was exercised. The student never had to *compose* "physical + chemical" or "discriminate weathering from erosion." |  |
| **Targeted Remediation** (Ch.21) | n/a — no remediation triggers in a correct-answer session. |
| **Testing Effect** (Ch.20) | ❌ One retrieval per lesson is far below the Ch.20 imperative of "frequent, brief, low-stakes quizzes." Most of the testing is deferred to the exit ticket — but exit tickets are *summative* assessment, not formative retrieval. |  |
| **Interleaving** (Ch.19) | n/a — within-session feature. The 1-item lesson does not have enough items to interleave across. |  |

**One-line summary**: an advanced student gets ONE chance to demonstrate
mastery per 10-step lesson; the engine then either (a) confuses them with a
"slow down" template on their correct answer, or (b) ships them to the exit
ticket. Active Learning and Deliberate Practice are both functionally
absent. The advanced student is doing 1× retrieval and 1× summative
exit-ticket — no spaced practice, no edge-of-ability stretching.

---

## 4. Root-Cause Map

Two structural drivers — both **engine** issues, not prompt issues:

### 4.1 `open_question_stickiness` gate fails every `confirm_and_extend`

Verbatim trace evidence:
```
T1342 (L1438): viols=['open_question_stickiness: confirm_and_extend posed a new item
                       (QuestionSource.LESSON_STEP:13915) while the open question
                       (QuestionSource.LESSON_STEP:13913) is still live; stay on the
                       same item or use the pivot move']
```

`gates.py:502` defines `probe_moves = ("scaffold_hint", "name_misconception",
"confirm_and_extend", "pose_question")`. But `move_prompts.py:CONFIRM_AND_EXTEND`
explicitly instructs the LLM to "**pose a single follow-up that varies one
parameter** (different numbers, different units, an edge case, a mechanism
step, a transfer to a new context)". A new bank item with new numbers is
literally what the move-prompt asks for; the gate then fails it.

**Net effect**: every correct answer on an item that has < 2 cumulative
corrects gets the `confirm_and_extend` move, which is structurally
guaranteed to fail conformance and fall back to a wrong-template.

This is the single most expensive engine bug in the v2 system. Both
evaluations have hit it on every run.

### 4.2 `_OBJECTIVE_MIN_CORRECT = 2` closes lessons after 2 correct items

`move_selection.py:112` sets the close-topic threshold at 2 corrects /
attempts ≥ 0.5 ratio. The advanced student crosses this threshold in 2 turns.
But "objective sufficient" is being treated as "lesson sufficient" — the
engine ships the exit ticket payload as soon as the first objective closes.
On a 10-step lesson, this means **80 %+ of the lesson content is never
practiced**.

For a struggling student this may be fine (limit time on a hard lesson),
but for an advanced student who's still in the *desirable-difficulty*
window, the engine prematurely retires before any deliberate-practice
stretch occurs.

---

## 5. Recommendations for `apps/tutoring/v2/services/move_prompts.py`

The two §4 issues are engine-level, not prompt-level. Prompt changes can
mitigate but cannot fully resolve. Listing both kinds for completeness.

### R1 — `CONFIRM_AND_EXTEND` (`move_prompts.py:330`): teach the LLM to phrase a "twist" *within the same bank item*

**Current prompt** says "pose a single follow-up that varies one parameter
(different numbers, different units, an edge case…)". This makes the LLM
issue a NEW bank pose, which the stickiness gate rejects.

**Edit**: replace the "How" body with:

```
- One short affirmation (≤1 sentence) that NAMES the specific thing they
  got right — quote a key term from their answer ("you got the
  hydrolysis chain"). Do NOT re-author the mechanism in your own words.

- Then pose ONE follow-up that exercises the SAME bank item from a
  different angle. Permissible angles (pick exactly one):
    * Ask for the boundary condition: "what would you expect if there
      was ALSO mechanical cracking visible?"
    * Ask for a discrimination: "what feature would tell you this is
      chemical weathering and not biological weathering?"
    * Ask for transfer: "apply the same reasoning to a granite cliff
      at Anse Intendance — what would you look for?"
    * Ask the student to predict: "if this rock sits in the rain for
      ten more years, what would the surface look like?"
  These extensions DO NOT call pose_question again on a new bank slot.
  They are POSED IN PROSE as a single follow-up question (the
  `no_assessment_in_prose` gate skips this move because the follow-up
  has no single canonical answer — it is interpretive).

- If you cannot author a clean interpretive follow-up (e.g. the bank
  item has no obvious twist), then DO NOT emit confirm_and_extend at
  all — instead call close_topic. The engine handles the close.
```

This works *with* the stickiness gate by keeping the extend-turn in prose.
It still falls short of true Deliberate Practice (no new bank item is
posted), but it eliminates the P1-class fallback path.

### R2 — `CONFIRM_AND_EXTEND` + `CONFIRM_AND_ADVANCE`: add a no-praise-opener directive (mirror R5 of the MATHS-S1 report)

**Edit**: at the top of both move bodies, add:

```
NEVER open with "Yes", "Right", "Great", "Spot on", "Perfect", or any
single-word affirmation. The conformance praise filter rejects these
under any non-correct verdict. Open with an action-clause: "You got the
oxidation chain — the boundary case is…".
```

### R3 — Engine-side, not prompt: remove `confirm_and_extend` from `gates.py:502 probe_moves`

This is the **highest-leverage** fix. The move's contract is to pose a
*different* item; the gate's purpose is to prevent moves that should stay
on the open question from drifting. The two are in direct conflict. Remove
`confirm_and_extend` from the list:

```python
probe_moves = (
    "scaffold_hint",
    "name_misconception",
    "pose_question",  # only when the engine intends to stay on open Q
)
```

Then the L1438 T2 / L1439 T2 traces would pass conformance and the student
would see a genuine follow-up question rather than the "slow down" template.

### R4 — Engine-side, not prompt: raise `_OBJECTIVE_MIN_CORRECT` for advanced learners (or decouple objective-close from lesson-close)

`move_selection.py:112` ships `_OBJECTIVE_MIN_CORRECT = 2`. Two practiced
items per OBJECTIVE is fine; the bug is that the lesson closes as soon as
the first objective closes — but the lesson has multiple objectives, and
even within one objective, two items is below the Ch.10 active-learning
floor for a non-struggling student.

Options:
  (a) Make `_OBJECTIVE_MIN_CORRECT` dynamic on the student profile —
      struggling students keep the 2-item floor; non-struggling students
      get 3–4.
  (b) Ensure all enabling objectives are exercised before close_topic
      ships the exit-ticket payload (the current code closes the lesson
      when ANY objective closes, not when ALL objectives close).

Either change is a one-line edit. (b) is the safer one.

### R5 — `EXPLAIN` (`move_prompts.py:572`): the opening Q is open-ended *by design*; do not add a stickiness expectation

Both sessions handled this correctly: T0 ends with an open-ended prose Q,
T1 student answers, T2 tutor `pose_question` registers the first
formal open_question. **No change needed**, but the prompt's wording
("end with a single prompt that invites the next move") could be made
explicit that the opener's Q is *not* the open_question — that's set in
the subsequent pose_question turn.

**Optional clarifier**:

```
- The opening turn's framing question is OPEN-ENDED — it has no
  canonical answer and is not registered as the session's
  open_question. The first formal pose_question (next turn) is what
  registers the open_question. So do not stake the opening turn's
  question on any specific expected answer.
```

---

## 6. Priority Triage

| Priority | Fix | File | Why this first |
|----------|-----|------|----------------|
| **P0** | Remove `confirm_and_extend` from `probe_moves` in `open_question_stickiness` gate. | `apps/tutoring/v2/services/conformance/gates.py:502` | Single change; eliminates the P1-class fallback on **every** correct-answer turn (observed in both runs, both sessions). |
| **P0** | Defer lesson-close until ALL objectives are sufficient. | `apps/tutoring/v2/services/move_selection.py:_objective_evidence_sufficient` | Currently the lesson closes on the first objective close. Advanced students get ~1 practice item per lesson. |
| **P1** | CONFIRM_AND_EXTEND prompt edit (R1). | `apps/tutoring/v2/services/move_prompts.py:330` | Belt-and-suspenders against the gate fix in P0. Also produces a better extension shape (interpretive follow-up). |
| **P1** | No-praise-opener directive on CONFIRM_AND_* (R2). | `apps/tutoring/v2/services/move_prompts.py:302, 330` | Defends against praise_filter retry-cascade. |
| **P2** | EXPLAIN opener clarifier (R5). | `apps/tutoring/v2/services/move_prompts.py:572` | Cosmetic; not load-bearing in this run. |

---

## 7. Engine Health Indicator

`fallback_used = true` rate this session pair: **2 / 4 substantive tutor
turns = 50 %** (counting only confirm_and_extend and close_topic; if T0
explain and the pose_question T1 are counted, drops to 2/7 = 29 %).

The Phase 3 observability dashboard target is < 5 %. Even on an advanced
student giving all-correct answers, the engine cannot avoid the fallback
path for `confirm_and_extend`. This is the load-bearing symptom of the
§4.1 gate misconfiguration.
