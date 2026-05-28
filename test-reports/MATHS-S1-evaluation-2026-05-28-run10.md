# MATHS-S1 Tutor Evaluation — 2026-05-28 (run 10)

**Persona**: S1 (Cycle 4) **struggling** mathematics student, Seychelles. Per run-instructions: consistently wrong answers, hesitant phrasing, occasional bare guesses. The advanced register from prior 2026-05-28 runs is intentionally flipped here.
**Engine**: v2 (`refactor/conversational-tutor-redesign`, post-prune; HEAD `5d5b209`).
**Sessions covered**:
  - **Session 114** — lesson `1148` *Solve one-step equations (unknown on one side) positive integer solutions only* — full session, 11 student turns, **reached exit ticket**.
  - **Session 115** — lesson `1167` *Analyse and solve problems involving cost and selling price* — supplemental, 6 student turns including a `too_hard` UI signal injection.
**Student account**: `student1` / Anse Boileau (institution `School-3`) / grade S3 (only S3 maths is seeded; the S1 persona is acted at register level).
**Evaluator**: Roy Manzi (claude-opus-4-7).

---

## 1. P1 unacceptable errors

| # | Session.Turn | Category | Evidence |
|---|--------------|----------|----------|
| **P1-1** | 114 / turn 1794 | **Tutor says student's wrong answer is correct.** | Open question was *"Solve x + 8 = 23"* (MCQ A=15, B=31, C=8, D=23). Student replied: *"B because 23 + 8 = 31"*. The correct answer is **A (x = 15)**; the student added instead of subtracting (the canonical error this lesson is built to catch). Live API response on that turn: `selected_move: confirm_and_advance`, **`verdict: correct`**, `fallback_used: false`. The tutor then posed a fresh fish-net problem ("A fisherman starts with 42 fish, has 17 left, find x") as the next item, treating the previous wrong as if mastered. `runtime_state.recent_verdicts` confirms `correct` was recorded for this attempt. |
| **P1-2** | 114 / turn 1796 | **Tutor says student's wrong answer is correct.** | Open question was *"42 − x = 17"* (MCQ A=59, B=25, C=17, D=42). Student replied: *"D, x = 42 because thats the starting number"* — picked the trap distractor. The correct answer is **B (x = 25)**. Live API response: `selected_move: confirm_and_advance`, **`verdict: correct`**. The tutor immediately advanced to the leaflet-distribution problem (5x = 60). Two consecutive wrong-graded-correct verdicts. |
| **P1-3** (structural) | 114 / turn 1792 | **Posing a stacked / two-question turn — violates "one question per turn".** | Single tutor turn contains **(a)** a diagnostic sub-question ("what does 18 ÷ 3 give you?") aimed at the prior open question (3x = 18), AND **(b)** an entirely new MCQ with stem, options, and 4 distractors ("Solve x + 8 = 23. A) x = 15 / B) x = 31 / C) x = 8 / D) x = 23"). The shared preamble at `move_prompts.py:122-129` explicitly prohibits this: *"A turn contains AT MOST one thing for the student to attempt. Never stack a diagnostic sub-question on top of a tool-posed question."* This is a borderline P1 — not a literally "incomplete" question, but the student doesn't know which to answer, and answering the MCQ implicitly abandons the prior open question (the 3x=18 thread is never closed). |
| **P1-4** (structural) | 114 / turn 1788 | **Worked example pre-resolves the question in subgoal 1 — answer-leak via worked example.** | Turn 1788's `worked_example` move opens with: *"**Subgoal 1 — Spot the operation:** x is being multiplied by 25. **The right side becomes 5. So x = 5.** What operation is being done to x, and what would the inverse operation be?"* — Subgoal 1 labels itself "Spot the operation" but immediately states the answer (`x = 5`) inside the same subgoal, *before* the practice prompt. The body of the worked example collapses three subgoals into one declarative sentence AND states the canonical inside the labelled step. The open-question canonical guard at `move_prompts.py:640-678` is authored to prevent exactly this: *"the labelled subgoals must stop ONE STEP SHORT of stating the canonical answer."* |

**Bottom line**: **2 hard P1 hits** (wrong-graded-correct, the most damaging category — a struggling student gets two false confirmations of wrong arithmetic mental models) **+ 2 structural P1-adjacent issues** (stacked questions, worked-example answer-leak). The grader correctly handled the more transparent wrong answers (T1 "add 18", T6 "60+5=65", T7 "60÷5=11", T11 "4×32=128") but failed twice on **MCQ distractor traps where the student's stated reasoning was the canonical error** (`23 + 8 = 31`, `x = 42 starting number`). Both are exactly the misconceptions the bank distractors exist to detect; both slipped through.

P1 categories from the skill rubric:
- "Tutor says student's correct answer is wrong" — **0 hits**.
- "Tutor says student's wrong answer is correct" — **2 hits (P1-1, P1-2)**.
- "Posing incomplete questions (missing crucial info needed to answer)" — **0 strict hits**; 1 borderline (stacked turn — incompletely scoped rather than missing info).

---

## 2. Full transcript — session 114

| Turn (id) | Role | Content (truncated) | Router move | Verdict | Note |
|-----------|------|---------------------|-------------|---------|------|
| 1786 | tutor | "Welcome to **Solve One-Step Equations**! … balance scale … Solve 3x = 18." | `explain` | n/a | Rule 5 (opening). Pose was a free-response with single canonical answer — borderline prose-pose. |
| 1787 | student | "i think you add 18 to both sides so x = 21" | — | — | Wrong (added 18, canonical is divide). |
| 1788 | tutor | "Worked example: 25x = 125 … Subgoal 1 — Spot the operation: x is being multiplied by 25. The right side becomes 5. So x = 5. What operation is being done to x, and what would the inverse operation be?" | `worked_example` | wrong | **P1-4** — subgoal 1 leaks `x = 5` before any practice. |
| 1789 | student | "multiplied by 25, inverse is subtraction?" | — | — | Identified multiplication, wrong inverse. |
| 1790 | tutor | "You've nailed the first part — yes, x is being multiplied. But … if multiplication is the operation, what's the *opposite* of multiplying? … apply it to **3x = 18**" | `scaffold_hint` | partial | Defensible partial (named the operation correctly). Good subskill stickiness — stays on 3x = 18. |
| 1791 | student | "umm divide? so divide 3 from both sides? x = 18 - 3 = 15" | — | — | Correct inverse named, then applied subtraction. |
| 1792 | tutor | "You've got the right idea — divide is exactly the inverse … But look at your working: you wrote 18 − 3 = 15. … When you divide both sides by 3, what does 18 ÷ 3 give you? **Solve the equation x + 8 = 23. … A) x = 15 / B) x = 31 / C) x = 8 / D) x = 23**" | `scaffold_hint` | wrong | **P1-3** — stacked: diagnostic "what does 18÷3 give you?" + brand-new MCQ on x+8=23. Two open questions in one turn. |
| 1793 | student | "B because 23 + 8 = 31" | — | — | Wrong (canonical trap: added instead of subtract). |
| 1794 | tutor | "A fisherman in Seychelles starts with 42 fish … After using some, he has 17 fish left … 42 − x = 17. A) x = 59 / B) x = 25 / C) x = 17 / D) x = 42" | `confirm_and_advance` | **correct** | **P1-1** — wrong answer (B 31) graded correct; tutor advanced without addressing either the diagnostic or the wrong MCQ. |
| 1795 | student | "D, x = 42 because thats the starting number" | — | — | Wrong (trap distractor — picks the starting number). |
| 1796 | tutor | "You are distributing 60 copies of a leaflet … 5x = 60 …" | `confirm_and_advance` | **correct** | **P1-2** — wrong answer (D 42) graded correct again. Tutor advanced to a new item. |
| 1797 | student | "each one gets 65 leaflets, x = 65 because 60 + 5 = 65" | — | — | Wrong (added instead of divided). |
| 1798 | tutor | "The slip here is adding 5 to 60 instead of using the inverse of multiplication. In **5x = 60**, x is being *multiplied* by 5 … divide **both sides** by 5. What is 60 ÷ 5?" | `name_misconception` | wrong | Good — names the canonical "add-instead-of-divide" misconception (the move's purpose). Stays on the open question. |
| 1799 | student | "60 divided by 5 is 11?" | — | — | Wrong arithmetic. |
| 1800 | tutor | "60 ÷ 5 isn't quite 11 — try counting up in 5s: 5, 10, 15, 20 … how many steps does it take to reach 60?" | `scaffold_hint` | wrong | Excellent prerequisite-skill drop-down (Layering Ch.16) — gives the student a concrete enumeration strategy without lowering the bar. |
| 1801 | student | "oh ok i count 12 steps so x = 12" | — | — | Correct (well, 12 by counting in 5s = 60). Honest arithmetic recovery. |
| 1802 | tutor | "A student solved x − 6 = 14 and got x = 20. Verify whether this solution is correct … (True or False?)" | `confirm_and_advance` | correct | Good — verification item exercises a different subskill (substitute and check). |
| 1803 | student | "False, idk how to check it" | — | — | Wrong + explicit help signal. |
| 1804 | tutor | "Good instinct to flag that you're unsure — let's walk through exactly how to check. **Subgoal 1 — Write out the original equation:** x − 6 = 14. **Subgoal 2 — Swap x for the proposed answer:** 20 − 6 = ?. **Subgoal 3 — Check both sides match:** 20 − 6 = 14. … So using those three steps on x − 6 = 14 with x = 20: does the left side equal the right side, and what does that tell you — is the student's answer True or False?" | `worked_example` | n/a (help_request) | **Excellent.** Help-request honored: 3 labelled subgoals (Cognitive Load Ch.14), last subgoal POSES the inference rather than stating it (open-question canonical guard `move_prompts.py:640-678` honored), closes on the open question. Compare to the broken worked example at turn 1788 — same move, opposite execution. |
| 1805 | student | "ok so True then? since both sides are 14" | — | — | Correct, with reasoning. |
| 1806 | tutor | "Solve the equation 4x = 32. A) x = 8 / B) x = 36 / C) x = 28 / D) x = 128" | `confirm_and_advance` | correct | Pose-tool MCQ, clean. |
| 1807 | student | "D, x = 128 because 4 times 32 is 128" | — | — | Wrong (multiplied instead of divided — the canonical trap). |
| 1808 | tutor | "Hmm, wait — you said '4 times 32 is 128', but that's multiplying 4 by 32, not solving for x. In 4x = 32, x is what you're trying to find, so you need to *divide* both sides by 4, not multiply. What is 32 ÷ 4?" | `close_topic` | wrong | API also returned `phase: completed`, `show_exit_ticket: true`. **The close_topic body ends with a question** ("What is 32 ÷ 4?") — violates the move's "Close means close" directive (`move_prompts.py:918-921`). But exit ticket was correctly dispatched. |

Final state: `runtime_state.move_history`: `['explain', 'worked_example', 'scaffold_hint', 'scaffold_hint', 'confirm_and_advance', 'confirm_and_advance', 'name_misconception', 'scaffold_hint', 'confirm_and_advance', 'worked_example', 'confirm_and_advance', 'close_topic']`. `objective_progress.attempts: 10`, `correct: 5`, `wrong: 4`, `partial: 1`. Note the **objective_progress over-counts correct**: 2 of the 5 "correct" verdicts were the wrong-graded-correct P1s, so genuine `correct` was 3, genuine `wrong` was 6. This means the I-6 wrong-to-correct ratio invariant (`wrong ≤ 2 × correct` for close via correct branch) was satisfied **only because two wrongs were laundered into corrects**.

---

## 3. Supplemental session 115 — `too_hard` UI signal

Six student turns on lesson 1167 (cost / selling price), `too_hard` injected after turn 1. Selected observations:

| Turn id | Role | Content / API trace | Observation |
|---------|------|---------------------|-------------|
| 1811–1812 | tutor → student | Opening explain, then "profit is 22 because thats the bigger number" → `verdict: wrong`, move `worked_example`. | Grader correct. |
| (injection) | system | `POST /difficulty-signal/ {"signal": "too_hard"}` → `difficulty_level: -1`. System tutor message: *"No worries at all — let's slow it right down and walk through it together step by step."* | Difficulty level persisted (`runtime_state.difficulty_level: -1`, `last_system_event: difficulty_change:too_hard`). |
| 1817 | tutor | *"Totally fine — let's use a simpler set of numbers first, then come back to the breadfruit. The diagram below shows exactly these three steps laid out visually."* (no question, no diagram visible — `media: []` in API) | **No action ask.** Violates the Active Learning preamble rule at `move_prompts.py:181-185`. Promises a diagram that isn't returned. |
| 1819 | tutor | *"You divide by what you *paid*, not what you sold it for. The diagram below shows this exact coconut transaction so you can see where each number comes from."* | Same shape: principle restatement + diagram-promise + no action. Two consecutive turns with no concrete student ask — exactly the "empty connective" failure mode described in `move_prompts.py:145-156`. |
| 1821 | tutor | After student says "ok can you ask me something simple?" → *"A shop owner imports fish for 45 SCR per kg and sells it for 60 SCR per kg. Calculate **the profit per kg and the profit percentage**."* | **`too_hard=-1` did not reduce question composition.** The post-signal question is still a **two-part composite** (profit AND profit%). The signal moved the difficulty integer but did not change the bank-slot selection logic — the student-perceived difficulty stayed identical. Cognitive Load Ch.14 violation: "one idea per turn" preamble is overridden by a multi-part bank item. |

Net for session 115: difficulty signal is **cosmetic** — it updates `runtime_state.difficulty_level` and emits a soothing system line, but the slot selector does not consult the level when picking the next bank item. A struggling student who signals "too hard" gets identical-difficulty content with a marginally warmer tone.

---

## 4. Science-of-learning assessment

Mapped to `design/science-principles.md`. Mix of strong execution and the failure modes named in P1.

### Working well

| Principle | Evidence |
|---|---|
| **#3 Deliberate Practice (Ch.12) — calibrate to the edge.** | After the 60÷5=11 wrong (T1799), the tutor dropped to "count up in 5s" — a true prerequisite-skill scaffold that doesn't lower the bar (the student still has to produce `x = 12`). This is the canonical Targeted Remediation Ch.21 pattern: drop a rung, hold the bar. |
| **#5 Minimise Cognitive Load (Ch.14) — labelled subgoals.** | Turn 1804's worked_example (in response to *"False, idk how to check it"*) used three explicit subgoals ("Subgoal 1 — Write …", "Subgoal 2 — Swap …", "Subgoal 3 — Check …") and stopped one step short of the answer. Textbook execution of the open-question canonical guard. |
| **#11 Testing Effect (Ch.20) — retrieval first when method has been seen.** | Once method evidence was present (turns 1801 onward), the engine flipped from `worked_example` / `scaffold_hint` to `confirm_and_advance` (turns 1802, 1806). The router's Rule 6 (`method_evidence_present == true` → post_step_pose) fired correctly. |
| **#2 Direct Instruction (Ch.11) — help-request handling.** | The "False, idk how to check it" turn was correctly classified as a help-request and the response delivered the method explicitly (3 subgoals) before going back to retrieval. Compare to prior-run regressions where help-requests were treated as `answer_attempt`. |
| **Voice & subject anchoring.** | Seychelles examples throughout (fishermen, ocean-conservation leaflets, market traders) without system-vocabulary leak. The `move_prompts.py:63-179` preamble is well-applied. |

### Not working

| Principle | Failure |
|---|---|
| **Grader correctness — the central P1 cluster.** | Two wrong MCQ picks (T1793 "B because 23+8=31", T1795 "D x=42 because starting number") were both graded **correct**. Both contained the canonical error stated as the student's reasoning — exactly the signal the grader exists to catch. The grader trust-the-authored-canonical fix in commit `e764184` resolves the *false-negative* failure (full canonical denied as partial) but introduces / surfaces a *false-positive* failure mode: when the student writes a confident sentence with an MCQ letter and any arithmetic, the grader appears to accept it on confidence rather than checking the letter against the canonical. This is the worst-direction P1 — a struggling student gets two confirmations of broken mental models inside three turns. |
| **#5 Minimise Cognitive Load (Ch.14) — one idea per turn.** | Turn 1792 stacks a diagnostic ("what does 18÷3 give you?") AND a brand-new MCQ (x+8=23 with 4 options). Two open questions, two distinct subskills, one turn. The preamble at `move_prompts.py:122-129` explicitly bans this — the rule is authored, not enforced. |
| **#5 Minimise Cognitive Load (Ch.14) — worked example structure.** | Turn 1788 collapses three subgoals into one declarative sentence and states the canonical inside subgoal 1 (`"x = 5"`) *before* asking the student anything. The `worked_example` body section at `move_prompts.py:640-678` is explicitly authored against this. |
| **#1 Active Learning (Ch.10) — every turn ends in one student action.** | Session 115 turns 1817, 1819 both ended with a "diagram below" promise and no question. The Active Learning preamble at `move_prompts.py:181-185` requires every turn end on a student-action; two consecutive turns failed that. |
| **#3 Deliberate Practice (Ch.12) — calibration to the student's edge.** | The `too_hard` UI signal moved `difficulty_level` to -1 but did NOT change the bank-slot selection: the very next question was a 2-part composite (profit per kg AND profit %). For a struggling student, "too_hard → harder-or-equal item" is the opposite of what Deliberate Practice demands. |
| **#4 Mastery Learning (Ch.13) — close requires evidence; not laundered evidence.** | The close at T1808 satisfied I-6 (wrong-to-correct ≤ 2:1) **only because two wrongs were graded correct**. With true counts (correct=3, wrong=6), the close would have been blocked. The wedge between routed verdicts and actual student competence means the engine closed a lesson the student had not mastered. The exit ticket (5 MCQs + matching + fill-in) now tests a student whose retrieval signal is corrupted by two false confirmations. |
| **`close_topic` — body should not end with a question.** | T1808 closes with *"What is 32 ÷ 4?"* — a verifiable-answer question. The move's "What NOT to do" list at `move_prompts.py:918-921` is explicit: "Add another assessment question on this objective. Close means close." The rule is authored; the engine ignored it. |

---

## 5. Router & move-prompt review

### 5.1 Router (`apps/tutoring/v2/services/router_prompts.py`)

**Strengths.** Closed move set with named invariants (I-1..I-6), principle-cited rules, counter-passed-by-name pattern that defeats LLM transcript re-counting. Rule 7's `moves_by_verdict` shape is clean — the router pre-decides each verdict branch and the engine just looks up the row.

**Findings (this run).**

#### R1 — Add a "double-check" guard for grader output on MCQ wrong-as-correct

The router currently *trusts the grader* on the verdict label and just routes from it. When the grader mis-labels a wrong MCQ pick as `correct` (P1-1, P1-2), the router happily routes to `confirm_and_advance` and the failure compounds. Defense in depth: when `intent == "answer_attempt"` AND the open question is an MCQ AND the student's response contains an explicit MCQ letter (A/B/C/D) AND the named canonical letter differs from the student's letter, the router should emit a sanity flag in the `reason` field and add an invariant:

```
I-7 — answer_attempt MCQ disagreement guard
  When the open question is an MCQ and the student's response cites
  a letter (A/B/C/D), require the grader's verdict to be consistent
  with letter == canonical_letter. If the verdict the grader emitted
  contradicts the explicit letter pick (e.g. verdict=correct but the
  student's letter ≠ canonical letter), do NOT route to
  confirm_and_advance / confirm_and_extend / close_topic. Instead
  route to scaffold_hint and emit a router span
  `router.mcq_letter_disagreement` so it surfaces on the v2
  dashboard.
```

This is the defense-in-depth pattern the v2 design explicitly endorses for grader/router separation. The router's structural knowledge (MCQ letter) is sufficient to catch this class of grader miss without trusting the grader's adjudication.

#### R2 — Strengthen Rule 7 "answer_attempt" output on partial / multi-slot answers

In session 115 turn 1822 ("profit is 60 - 45 = 15 SCR, percentage is 15%"), the student got the profit RIGHT (15) but the percentage WRONG (computed as 15÷100, not 15÷CP). The grader returned `partial`, the router fired `scaffold_hint`, and the response correctly credited the partial. **This worked.** But the pattern is fragile: if the lesson bank had marked profit as a single slot, the partial would have been routed as wrong (Rule 7 wrong branch → re-teach). Add an explicit note to Rule 7's partial branch:

```
• partial branch — multi-slot answers:
    When the open question has N>1 canonical slots and the student's
    response names a strict subset correctly, return scaffold_hint
    with a slot-coverage note in `reason`. Never return
    confirm_and_advance via partial — that closes the open question
    before the uncovered slots are exercised.
```

#### R3 — Close_topic forbid-question invariant

T1808 closed the topic with a verifiable-answer question in the body ("What is 32 ÷ 4?"). The move prompt forbids this; the router could enforce it via an output-shape invariant:

```
I-8 — close_topic body shape (post-move LLM, not pre-route)
  When the router selects close_topic, the engine must check the
  generated body for a trailing question token (`?`). If present
  AND the sentence contains a numeric/letter/term answerable item,
  reject the turn and retry with an explicit "close means close —
  no trailing question" reminder.
```

This is a structural-conformance gate; it lives outside the router prompt but the router's `reason` field can name it (`"close_topic — body must not contain a verifiable-answer question"`) so the trailing gate has a hook.

#### R4 — Difficulty-level → slot-selection wiring

`runtime_state.difficulty_level` updates on UI signal but the slot selector does not appear to consult it. This is out of scope for prompt edits but the router's `reason` field should explicitly cite the difficulty level on every routing decision (it already names the counters; add `difficulty_level: -1`) so the dashboard can surface "signal sent, slot composition unchanged" as a measurable drift.

### 5.2 Move prompts (`apps/tutoring/v2/services/move_prompts.py`)

**Strengths.** Principle citations are precise. The open-question canonical guard in `worked_example` is authored against the exact failure mode at T1788. The `close_topic` move's help-request defensive section (`move_prompts.py:861-874`) reflects real prior-run learnings.

**Findings.**

#### M1 — `worked_example` opening turn: explicit "do not pre-state the answer in subgoal labels" reminder

The canonical guard at `move_prompts.py:640-678` already says "subgoals must stop ONE STEP SHORT of stating the canonical answer." But T1788's failure was subtler: the **labelled subgoals collapsed** ("Subgoal 1 — Spot the operation: x is being multiplied by 25. The right side becomes 5. So x = 5.") into a single declarative sentence that named the answer mid-clause. Tighten the rule with a positive directive:

```
Each labelled subgoal must be ONE step of the method (name the
operation, apply the inverse, verify). Do NOT combine two steps
into one labelled subgoal, and do NOT include the canonical answer
inside any subgoal body. The final subgoal POSES the inference;
none of the earlier subgoals reveal it. Each subgoal is at most
ONE short sentence.
```

This is positive ("each subgoal is one step") rather than negative ("do not combine steps"), matching the prompting-fundamentals positive-instruction directive.

#### M2 — `scaffold_hint` "stay on the open question" — make it lethal on stacking

T1792 violated the preamble's "one question per turn" + "subskill stickiness" rules by stacking a diagnostic ("what does 18÷3 give you?") with a brand-new MCQ ("x + 8 = 23 …"). The shared preamble (`move_prompts.py:109-129`) has both rules, but the move-specific `scaffold_hint` body should restate the constraint at point of use — this is the move most prone to the stacking failure:

```
HARD GUARD — scaffold_hint NEVER introduces a new problem:
- The scaffold replaces the prior probe on the SAME open question.
  It does NOT add a probe alongside a new problem.
- If you cannot generate a smaller-step diagnostic on the open
  question, return ONE plainer restatement of the open question
  itself and ask the student to attempt one named piece. Do NOT
  reach for a fresh bank item to "give them an easier example" —
  that is the worked_example move's job, and even there the
  practice prompt returns to the OPEN question.
```

The current `scaffold_hint` prompt is implicit on this; making it explicit at the point where the failure shows up reduces the load on the model.

#### M3 — `close_topic` — strengthen the "close means close" structural rule

The move prompt's "What NOT to do" at `move_prompts.py:918-921` says "Add another assessment question on this objective. Close means close." T1808 violated this by ending with "What is 32 ÷ 4?". Hoist the constraint to a hard-guard at the body's structural level:

```
HARD STRUCTURAL — close_topic body ends with a transition, never a
question:
- The LAST sentence is one of: "Let's move on to <next objective>.",
  "You're ready for the exit ticket — I'll set it up.", "We'll
  wrap here for now and pick this back up." NEVER a sentence
  ending in '?'. A question in a close turn confuses the student —
  the close announces an end, then asks for input on what they
  thought just ended.
```

This pairs with R3 above (router invariant) — the prompt makes the policy explicit; the structural gate catches drift.

#### M4 — `confirm_and_advance` — when the grader returned `correct` on an MCQ, double-check the letter before praising

This is the move that fires on T1794 and T1796 — both wrong-graded-correct. The move's body currently praises the answer and advances. Add a sanity gate at the head of the move body:

```
DEFENSIVE — MCQ verdict sanity check:
- If the open question is an MCQ AND the student's response cites
  a letter (A/B/C/D), AND the verdict is `correct`, the affirmation
  must NAME the letter the student picked AND the substantive
  reason it is correct: "B is right — markup adds to CP, so
  450 + 270 = 720." If you cannot author the substantive reason
  ("B because ___" where the blank is a content-bearing phrase, not
  a tautology), the verdict is suspect — do NOT advance. Pose ONE
  short diagnostic that exercises the same subskill (e.g. "walk me
  through why B not A?"); the engine will reroute on the response.
```

This is a model-side fallback for R1 (router invariant). The model sees what the router cannot — whether the student's stated reasoning actually supports the letter. If it cannot author a content-bearing affirmation, the verdict is probably wrong.

#### M5 — `explain` opening turn — pose tool always when single canonical

T1786's opening ends with *"Solve the equation 3x = 18. Show your working by identifying the operation, applying the inverse operation to both sides, and stating the solution."* — this is a free-response single-canonical question posed in prose, not via the `pose_question` tool. The `explain` body at `move_prompts.py:733-749` is explicit about this ("Never end with a verifiable-answer question typed in prose"). The rule is authored; the model is ignoring it on the opening turn because the bank slot may not match the "show your working" composite. Add to the body:

```
- When the lesson's first eligible bank slot is a procedural item
  (single canonical numeric answer), the opening pose IS the tool
  call. If no eligible bank slot exists, do NOT type the question
  in prose — close the explanation with an open-ended reflective
  prompt ("what would you try first?") and let the next move
  handle the retrieval pass.
```

This is a stricter version of the existing self-check; the failure mode at T1786 was the model substituting prose-pose when it could have either (a) used a bank slot or (b) used an open-ended reflective prompt.

---

## 6. Summary

| Dimension | Verdict |
|---|---|
| P1 errors | **2 hard P1 hits (wrong-graded-correct, T1794 + T1796)** + 2 structural P1-adjacent issues (stacked turn T1792, worked-example answer-leak T1788). The grader correctly handled transparent wrongs but failed twice on MCQ distractor traps where the student stated the canonical error as their reasoning. |
| Science-of-learning adherence | Direct Instruction (Ch.11) help-request handling, Cognitive Load (Ch.14) labelled subgoals in T1804, Targeted Remediation (Ch.21) rung-drop in T1800 — all strong. Active Learning (Ch.10) per-turn action rule violated by 2 consecutive empty-promise turns in session 115. Deliberate Practice (Ch.12) edge-of-ability calibration broken: `too_hard` signal does not reduce question composition. Mastery Learning (Ch.13) close was satisfied only via launder-corrupted verdict counts. |
| Router prompt | Sound structure. R1 (MCQ letter sanity invariant), R2 (multi-slot partial directive), R3 (close-topic no-question invariant), R4 (difficulty wiring) would close the holes this run surfaced. |
| Move prompts | Sound principle citations. M1 (worked_example one-step-per-subgoal), M2 (scaffold_hint no-stack guard), M3 (close_topic no-trailing-question), M4 (confirm_and_advance MCQ letter check), M5 (explain pose-tool-or-reflective) are direct fixes for the prompt-level failures. |
| Difficulty signal | Cosmetic — updates `runtime_state.difficulty_level` and `last_system_event` but does not feed the slot selector. The struggling student's `too_hard` resulted in a 2-part composite next question. Out-of-scope for prompts; flagged for engine work. |
| Exit ticket reached | **Yes** (session 114) — 5 MCQ + matching + fill-in items dispatched at T1808. However, the underlying competence the engine credited the student with is corrupted by the two false-correct verdicts; the exit ticket now stress-tests a student who was wrongly confirmed on two arithmetic mental models. |

Refs: `test-reports/MATHS-S1-evaluation-2026-05-28.md` (advanced register, 0 P1), `test-reports/MATHS-S1-evaluation-2026-05-27-run9.md` (Pythagoras lesson, different failure cluster), `test-reports/DIAGNOSIS-grader-2026-05-27.md`.
