# MATHS-S1 Tutor Evaluation — Run 6 (2026-05-26)

**Persona**: Advanced S1 / Cycle-4 Mathematics student. Provides consistently correct, full-working answers.
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on`).
**Session ID**: 90.
**Lesson evaluated**: L1177 — *Use Pythagoras' Theorem to Prove Whether a Triangle is a Right-Angled Triangle*
(Mathematics S3). New lesson published for this run; prior runs used L1148 / L1166 / L1167.
**Turns**: 11 student turns → exit ticket fetched.

---

## Transcript summary (engine annotations: `move / verdict / fallback_used`)

| Turn | Role | Content (abridged) | Engine annotation |
|------|------|--------------------|-------------------|
| 0 | tutor | Direct-instruction opener naming a² + b² = c² and the boat-ramp anchor; ends with engage check ("which part feels least clear"). | `explain / — / no` ✅ clean opener |
| 1 | student | "Both parts are clear. I'd square all three sides, identify the longest as c, then check whether a² + b² = c²." | — |
| 2 | tutor | "Let's make sure that's right together. Holding on the same idea … Pick the part you feel most confident about and start there." | `pose_question / — / **yes**` ⚠ verdict-blind safe template; never poses a problem |
| 3 | student | "Give me a practice problem please." | — |
| 4 | tutor | Tool-posed bank stem: *Triangle 5/12/13 — prove right-angled. Show all working.* | `pose_question / — / no` ✅ clean MCQ-style pose via tool |
| 5 | student | **Full correct working**: c=13; 5²+12²=25+144=169; 13²=169; ∴ right-angled. | — |
| 6 | tutor | "Let me check that with you before we go further … Tell me what you'd do first." | `scaffold_hint / **unverified** / yes` ❌ **P1 candidate — see §1** |
| 7 | student | Restates full step-by-step working with explicit subgoals 1-4. | — |
| 8 | tutor | "Quick check on that — pick one piece that feels closest to something you can answer …" | `scaffold_hint / unverified / yes` ❌ same fallback |
| 9 | student | "Yes, the triangle is right-angled because 25 + 144 = 169." | — |
| 10 | tutor | Re-engagement frame (boat-ramp anchor) + "now you try" on the SAME problem. | `worked_example / partial / yes` ⚠ wrong move dispatch — fallback delivers no actual subgoal walkthrough |
| 11 | student | "5 squared is 25. 12 squared is 144. 25+144=169. 13²=169. So a²+b²=c². Right-angled." | — |
| 12 | tutor | Same scaffold-hint fallback (verbatim opener varied). | `scaffold_hint / unverified / yes` ❌ third consecutive fallback |
| 13 | student | Canonical-matching phrasing: *"Yes, the triangle is right-angled. Working: c=13. a=5, b=12. a²+b²=25+144=169. c²=169. Since 169=169, right-angled."* | — |
| 14 | tutor | Same scaffold-hint fallback. | `scaffold_hint / unverified / yes` ❌ fourth consecutive unverified |
| (signal) | student | `too_easy` difficulty signal sent. | — |
| 15 | tutor | Bank-stem MCQ: *"What is the sum 12² + 10²?  A) 120  B) 244  C) 484  D) 22"*. | `pose_question / — / no` ✅ pose clean |
| 16 | student | **"B) 244. Because 12² = 144 and 10² = 100, and 144 + 100 = 244."** | — |
| 17 | tutor | **"Almost — the value doesn't line up with any of the quantities asked for — check which operation you applied and which slot you're answering."** Then PIVOT back to the 5/12/13 problem. | `pivot / **wrong** / yes` ❌❌❌ **P1: correct MCQ answer graded WRONG.** See §1. |
| 18 | student | "169 = 169 so it is right-angled." | — |
| 19 | tutor | Same scaffold-hint fallback. | `scaffold_hint / unverified / yes` |
| 20 | student | Full working + explicit "please advance to the next question". | — |
| 21 | tutor | Same scaffold-hint fallback ("Let me check that with you before we go further…"). | `scaffold_hint / unverified / yes` |
| 22 | student | "First I square each side … 25+144=169 which equals 169. So yes, right-angled." | — |
| 23 | tutor | Same scaffold-hint fallback. | `scaffold_hint / unverified / yes` ❌ session deadlocked |
| (close) | system | Exit-ticket fetched via `chat_exit_ticket` endpoint — 10 items returned; `current_step_idx=None` at close, `state=None`. | — |

**Runtime state at close** (from `TutorSession.runtime_state` dump):
- `state: None`, `current_step_idx: None`, `asked_questions: 0 items` — runtime never bound a step or populated the asked-ledger despite an `open_question` being live.
- `open_question.id = 10105` (LessonStep slot), `canonical` field stores the **full prose proof** (`"Yes, the triangle is right-angled. Working: The longest side is 13 cm, so c = 13…"`), `text` field is **empty**.
- Persisted `v2_trace` on rejected turns: `conformance_violations = ["open_question_stickiness: scaffold_hint posed a new item (QuestionSource.LESSON_STEP:10111) while the open question (QuestionSource.LESSON_STEP:10105) is still live; stay on the same item or use the pivot move"]` → forces single-retry → fallback template.

---

## §1 — P1 (unacceptable) errors

| Category | Count | Where | Notes |
|----------|------:|-------|-------|
| Tutor says correct answer is wrong | **1 hard, 7 soft** | T17 (hard); T6, T8, T10, T12, T14, T19, T21, T23 (soft: graded `unverified` on a fully-shown correct proof) | T17 is unambiguous: bank stem "12² + 10²", choices include B) 244, student answered "B) 244", engine returned **verdict=wrong** and pivoted away. The 7 `unverified` turns are softer — the engine refuses to *confirm* a correct full proof rather than affirmatively contradicting it — but the user-visible effect is the same: the student's correct work is treated as not-correct and re-asked indefinitely. |
| Tutor says wrong answer is correct | 0 | — | Not observed this run. |
| Incomplete question | 0 | — | All bank-posed stems were complete; no MCQ-without-options observed. |

**Root cause (Maths run)**: the grader cannot reconcile the student's natural-language proof against the `canonical` payload, which itself is a *prose* canonical (`"Yes, the triangle is right-angled. Working: …"`) rather than a structured assertion. Math-DSL grading needs an expected-value or structural matcher; here it falls back to grounded adjudication, which returns `unverified` on every shape of restatement (terse, verbose, canonical-mimic, step-by-step). Conformance then rejects `scaffold_hint`'s attempt to pose a sub-step from a *different* lesson-step bank, the retry fires the verdict-keyed safe template, and the session loops. The whole flow is the failure pattern described in `auto-memory/feedback_math_tutoring.md` and the run-5 report's "unverified trap", but **on a different lesson and persona**.

---

## §2 — Science of Learning evaluation

Scored against the 13 principles distilled in `design/science-principles.md` (Math Academy Way Ch.10–22). `n/a` = principle isn't testable in a single-session, single-topic dialogue.

| # | Principle (Ch.) | Verdict | Evidence |
|---|------------------|---------|----------|
| 1 | **Active Learning** (10) | ⚠ partial | Student is *doing* on every turn (correct full work, varied restatement, explicit step-by-step). But the engine treats every action as ungraded — there's no feedback closing the loop, so the practice-with-feedback cycle is broken even though the student is active. |
| 2 | **Direct Instruction** (11) | ✅ on T0 | Opening turn clearly states the theorem + the *method* ("square all three sides, check whether the sum of the two smaller squares = the largest square") *before* asking the student to do anything. ❌ on T10 — when the student is "stuck" (engine's view), `worked_example` fires but the fallback template delivers a re-engagement frame, NOT the labelled-subgoals walkthrough the move prompt specifies. |
| 3 | **Deliberate Practice** (12) | ❌ | No diagnostic ever runs — the same prompt repeats. The persona signalled "advanced" verbally (turn 1) and via `too_easy` difficulty signal, neither moved the engine off the 5/12/13 stem. No edge-of-ability calibration. |
| 4 | **Mastery Learning** (13) | ❌ | Bar held constant in the wrong direction — the engine *refuses* to advance even after the student demonstrates mastery 6+ times. Mastery learning says "vary the path, not the standard"; this session held the *path* constant (same stem) AND failed to recognize the standard had been met. |
| 5 | **Minimising Cognitive Load** (14) | ⚠ partial | ✅ T0 used dual coding (verbal + a concrete anchor — boat ramp). ✅ One-idea-per-turn rule is respected by the fallback templates. ❌ Worked example with labelled subgoals (the explicit Ch.14 prescription) was selected as a move at T10 but never rendered — the fallback ate it. |
| 6 | **Automaticity** (15) | n/a | Single-session, no latency tracking surfaced in trace. |
| 7 | **Layering** (16) | n/a | One topic this session. |
| 8 | **Non-Interference** (17) | n/a | One topic this session. |
| 9 | **Spaced Repetition** (18) | n/a | OUT OF MVP scope per move_prompts.py preamble. |
| 10 | **Interleaving** (19) | n/a | OUT OF MVP scope per move_prompts.py preamble. |
| 11 | **Testing Effect / Retrieval Practice** (20) | ❌ | "Attempt retrieval first, hints later" — the engine flipped this: every retrieval attempt is treated as ungraded, then a hint loop fires. The student's *retrievals were correct*, but the system can't tell. |
| 12 | **Targeted Remediation** (21) | ❌ | Remediation engaged when no remediation is needed — and when it did engage, the canonical re-asked the same wrong-shaped question instead of decomposing. The `scaffold_hint` move prompt explicitly forbids "Stack two questions" and "Pivot to a new problem while the open question is still live" — both rules were followed; what failed was the *premise* (the open question was already answered). |
| 13 | **Gamification** (22) | n/a | OUT OF MVP scope. |

---

## §3 — Recommendations for `apps/tutoring/v2/services/move_prompts.py`

The pattern across this run, run-5, and the auto-memory's `feedback_math_tutoring.md` is: **the engine has no recovery from an `unverified` verdict on a correct answer**. The move-prompts file is downstream of grader output, but a few prompt-level changes can reduce the dwell time in the loop and make the failure mode visible to the student instead of silent.

### R1. `SCAFFOLD_HINT` — recognize "the student has restated a full proof"
Lines 458–470 (the `UNVERIFIED` branch) currently instruct: *"Do NOT refute the student's claim. Do NOT call it wrong. Do NOT state a fact about the topic — the tutor-claim adjudicator will reject any prose factual claim made on an unverified verdict. End with a tool call to a question that re-tests one sub-skill of the open question."*

This is the rule that produced "Pick one piece that feels closest to something you can answer" SIX consecutive times. Add a sub-branch:

> **When the unverified turn-count on a single open question reaches 3+, switch from "re-test one sub-skill" to "verify by re-deriving."** Ask the student to *plug their own answer back into the original constraint* (here: "Does 5² + 12² equal 13² with the numbers you computed? Say yes/no.") — a yes/no retrieval the grounded adjudicator CAN verify, instead of another open-ended sub-skill pose. This converts an unverifiable proof into a single-bit assertion the grader can adjudicate.

### R2. `WORKED_EXAMPLE` — guard against fallback template displacing the labelled subgoals
Lines 552–566 describe the structure (2-4 labelled subgoals, anchored to the visible problem). The session traces show that when `worked_example` is selected but conformance rejects the rendered output, the verdict-keyed safe template fires and delivers a re-engagement frame ("Imagine you're building a boat ramp …") **with no subgoals**. The student sees `selected_move='worked_example'` in the API response but receives prose that violates the move's own contract.

Add to the "What NOT to do" section:

> **The fallback for this move must NOT be a re-engagement frame.** If conformance rejects the labelled-subgoals attempt and a retry is unavailable, the safe template for `worked_example` should be a *minimal labelled subgoals walkthrough authored against the canonical* (or, if the canonical is prose-only, a 2-line "Step 1: … / Step 2: …" extraction from the canonical). A worked-example move that produces zero labelled subgoals fails its own audit (Phase 2 §Tests "Move-prompt provenance audit" — principle 5).

### R3. `POSE_QUESTION` — surface the `too_easy` signal back to move selection
Not a prompt change per se, but the move-prompt file is the contract surface. Lines 281–292 describe verdict-shaped `lead_in` behaviour. Add the difficulty-signal case explicitly:

> **When the student has sent a `too_easy` signal in the current session and the engine is still on the original open question**, the lead_in MAY (and SHOULD) acknowledge the level-up — phrased as a teacher would ("alright, let's bump it up"). The pose itself MUST be at a higher rung from the complexity ladder; do not pose the same-difficulty bank slot. (T15 in this session correctly raised difficulty, but T17's grader then snapped back to the prior canonical — see R4.)

### R4. New cross-cutting note in `SHARED_PREAMBLE_TEMPLATE` — the bank-slot pivot rule
The "Voice (every turn)" block (lines ~70+) governs tone but not state. Add a structural rule:

> **Pivoting between bank slots: when the engine has just posed a new bank slot via `pose_question` and the student's response is on THE NEW SLOT (e.g. answering an MCQ posed seconds ago), the grader MUST resolve the new slot first.** Adjudicating the new-slot answer against the *prior* `open_question.canonical` is the run-6 T17 failure mode (student answered "B) 244" to "12² + 10²"; engine graded against "is the 5/12/13 triangle right-angled?"). This isn't a move-prompt rule, but flagging it in the preamble makes the contract visible at the surface every move shares.

R4 is the **highest-impact** of the four — T17 is the only hard P1 in this session, and it's a state-management bug surfacing as a prompting failure.

---

## §4 — Engine-level (out of scope for prompts, but flagged for the next layer)

These are upstream of `move_prompts.py` and must be fixed at the grader/state layer for any prompt change to land:

1. **Canonical-as-prose vs grader-as-structured-matcher mismatch.** L1177 step 3's canonical (10105) is a 60-word natural-language proof. The math grader needs either (a) a structured `expected_value: true` flag plus a "must mention each side squared" rubric, or (b) the grounded adjudicator must be willing to return `correct` (not just `unverified`) when the student's prose covers the canonical's steps. Right now it returns `unverified` indefinitely. This is the same failure surfaced in run-5's "unverified trap" memory note, on a fresh lesson — meaning the prior fix didn't generalize.
2. **`asked_questions` ledger never populates.** Despite an `open_question` being live for 22 turns, the ledger stayed at 0 items. The Phase 3 §3.5 two-phase-commit description in CLAUDE.md says the ledger is committed only after conformance approves — every conformance-approved turn in this session that posed a question (T4, T15) didn't increment it. This is the source of repeat-guards never firing.
3. **`state` and `current_step_idx` stayed `None`.** The session opened, ran 22 turns, fetched the exit ticket — and runtime_state never moved past initialization. The exit ticket should not have been deliverable (10/10 score-eligible items returned on a session with no committed step advance). The exit-ticket endpoint isn't reading the runtime contract.

---

## §5 — Comparison to prior MATHS-S1 runs

| Run | Lesson | P1 errors | Move-prompt issue surfaced |
|-----|--------|-----------|-----------------------------|
| run-2 | L1148 (one-step equations) | unverified-trap (multiple) | scaffold_hint stickiness |
| run-3 | L1166 (cost/selling price) | topic switching mid-turn | cognitive-load violations |
| run-4 | L1167 (cost & selling problems) | give-up → close_topic failure | exit-ticket envelope gap |
| run-5 | L1167 (repeat) | help-request not routed to worked_example | Direct Instruction violation |
| **run-6** | **L1177 (Pythagoras, NEW)** | **1 hard P1 (correct MCQ graded wrong on too_easy signal pivot)** + **persistent unverified-trap on full correct proof** | **(R1) unverified-trap recovery; (R2) worked_example fallback degenerates; (R3) difficulty-signal mid-conversation pose-state; (R4) cross-slot grader binding** |

The recurring "unverified trap on a correct answer" pattern is now observed across three lessons (L1148, L1167, L1177). The move-prompts changes alone won't fix it; the grader-side change (R1's yes/no re-derivation pose) is the move-prompt-only mitigation that has the best chance of breaking the loop in the next pilot pass.
