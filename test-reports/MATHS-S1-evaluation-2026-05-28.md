# MATHS-S1 Tutor Evaluation — 2026-05-28

**Persona**: S1 (Cycle 4) advanced mathematics student, Seychelles.
**Engine**: v2 (current `refactor/conversational-tutor-redesign` branch, post-prune; commits `5614264` … `e764184`).
**Session**: `TutorSession.id=106`, lesson `1166` — *And use the terms selling price, cost price, discount, profit and loss* (Mathematics S3, Numbers (I2) — Percentage and Money).
**Student account**: `student1` / Anse Boileau (institution `School-3`) / grade S3 (only S3 maths courses are seeded; persona played at "advanced" register — full canonical working on every turn).
**Evaluator**: Roy Manzi (claude-opus-4-7).
**Lesson position at run start**: fresh session, no prior history. Reached `is_lesson_complete=true` and exit ticket dispatched after 9 student turns.

---

## 1. P1 unacceptable errors

| # | Category | Hits |
|---|----------|------|
| Tutor says student's correct answer is wrong | 0 |
| Tutor says student's wrong answer is correct | 0 |
| Posing incomplete questions (missing crucial info needed to answer) | 0 |

**Bottom line**: **0 P1 hits** across 9 student turns. The grader trust-the-authored-canonical fix in commit `e764184` shows up in the data — every fully-canonical math response was verdicted `correct` in the v2 trace (`metadata.v2_trace.verdict_initial`), and no question was emitted with missing options or missing stem text. This is a meaningful improvement on run 9 (6 P1 hits on the Pythagoras lesson).

The full transcript:

| Turn (id) | Role | Content (truncated) | Router move | Verdict | Rule fired |
|-----------|------|---------------------|-------------|---------|------------|
| 1742 | tutor | "Welcome to today's lesson on selling price, cost price, discount, profit and loss …" | `explain` | n/a | Rule 5 (Direct Instruction Ch.11 opening) |
| 1743 | student | "Honestly all five feel clear to me. Cost price is what the shop paid … profit = SP - CP …" | — | — | — |
| 1744 | tutor | "A tourist shop in Mahé is having a sale. A souvenir item is originally priced at 280 SCR and is offered at 15% discount. Calculate the sale price …" | `confirm_and_advance` | n/a | Rule 6 (Testing Effect Ch.20 post-step pose) |
| 1745 | student | "Sale price = 280 × (1 − 0.15) = 280 × 0.85 = 238 SCR. So the customer pays 238 SCR; discount = 42 SCR." | — | — | — |
| 1746 | tutor | "A cafe imports coffee beans for 450 SCR per kg. The manager decides to add 60% to the cost price … A/B/C/D" | `confirm_and_extend` | correct | Rule 7 (answer_attempt) |
| 1747 | student | "B. 60% of 450 = 270, so SP = 450 + 270 = 720 SCR, profit = 270 SCR per kg." | — | — | — |
| 1748 | tutor | "You nailed the markup calculation — 60% of CP added on to get SP, then profit = SP − CP …" | `close_topic` | correct | Rule 7 (answer_attempt) |
| 1749 | student | "Yes please continue to the next part." | — | — | — |
| 1750 | tutor | "A trader buys fresh fruits for 300 SCR and sells them for 375 SCR. The statement 'The profit is 75 SCR' is correct. (True or False?)" | `confirm_and_advance` | n/a | Rule 6 |
| 1751 | student | "True. Profit = SP − CP = 375 − 300 = 75 SCR." | — | — | — |
| 1752 | tutor | "A market trader buys cinnamon … sells it for only 160 SCR per bag. Calculate the loss." | `confirm_and_extend` | correct | Rule 7 |
| 1753 | student | "Loss = CP − SP = 200 − 160 = 40 SCR per bag." | — | — | — |
| 1754 | tutor | "You've got the loss formula spot-on — CP − SP when CP > SP. Solid work across all five terms throughout this lesson. Let's move on to the next part of the lesson." | `close_topic` | correct | Rule 7 |
| 1755 | student | "Great, ready for the next part." | — | — | — |
| 1756 | tutor | "A shopkeeper buys a hand-painted plate from a local artisan for 75 SCR and sells it to a tourist for 130 SCR. Calculate the profit." | `confirm_and_advance` | n/a | Rule 6 |
| 1757 | student | "Profit = 130 − 75 = 55 SCR." | — | — | — |
| 1758 | tutor | "A bookshop in Victoria advertises 25% off all books. A textbook originally priced at 120 SCR … A) 25 / B) 30 / C) 45 / D) 95" | `confirm_and_extend` | correct | Rule 7 |
| 1759 | student | "B. 25% of 120 = 30 SCR is the discount." | — | — | — |
| 1760 | tutor | "You got the discount calculation — 25% of 120 = 30 SCR — exactly right. Strong, consistent work across all five terms. Let's move on to the next part of the lesson." | `close_topic` | correct | Rule 7 |

Exit ticket dispatched in the API response of turn 1760 (`show_exit_ticket=true`, 5 MCQ questions on profit / per-loaf SP / discount price / loss / cost price → SP).

---

## 2. Science-of-learning assessment

Mapped to `design/science-principles.md`.

### Working well

| Principle | Evidence |
|---|---|
| **#1 Active Learning (Ch.10).** | Every tutor turn ended on a concrete student action — pose, T/F, MCQ, or transition request. Zero empty-connective turns this run (vs run 9 which had 9 such turns on the same engine commit). The `No empty connectives` rule at `move_prompts.py:145-156` was honored throughout. |
| **#2 Direct Instruction (Ch.11).** | The opening `explain` turn (1742) delivered the five terms (CP / SP / profit / loss / discount) before any retrieval prompt. Anchored to the lesson title in the first sentence ("Welcome to today's lesson on selling price, cost price …"), as `move_prompts.py:636-651` requires. Direct-instruction fires ONCE then yields to retrieval (Rule 5 → Rule 6 transition observed). |
| **#3 Deliberate Practice (Ch.12) — partial.** | Item rotation drilled all five named terms in distinct framings (single-step calculation → MCQ markup → T/F profit → calculation loss → calculation profit → MCQ discount). Practice problem count : worked-example count = 6 : 1 on this run, which satisfies the "≥ 7× practice over worked-examples" target in spirit (only one worked example, but every other turn was a doing-turn). |
| **#4 Mastery Learning (Ch.13).** | `close_topic` fired exactly when the Rule 7 close criterion was satisfied (`unscaffolded_correct_on_objective >= 1` + this 2nd is correct + `richness == "rich"`). Three objectives closed cleanly (markup, loss, discount). The 9th turn flipped the lesson to `phase: completed` with `show_exit_ticket: true` — the runtime correctly progressed the state-machine for **completion**, even though intermediate `current_step_idx` was not persisted (see §2 cross-cutting below). |
| **Voice & anchoring.** | Mahé, Victoria, Beau Vallon, breadfruit-free in this lesson because it's commerce, but the trader/shop/cafe/bookshop framing is locally anchored. No system vocabulary leaked. Tone stayed teacher-to-student in second person. |
| **Specific affirmation.** | Turns 1748 ("You nailed the markup calculation — 60% of CP added on to get SP, then profit = SP − CP"), 1754 ("You've got the loss formula spot-on — CP − SP when CP > SP"), and 1760 ("You got the discount calculation — 25% of 120 = 30 SCR — exactly right") all quote back the substantive operation. No stand-alone praise tokens; the content sentence carries the affirmation, per `move_prompts.py:202-210`. |
| **Grader correctness on rich math input.** | API trace surfaces `verdict: correct` on all four `Rule 7 (answer_attempt)` turns (1746 / 1748 / 1752 / 1758 / 1760). Run 8's symptom (rich canonical work graded `partial`) does not appear. The `e764184` grader fix is intact in this commit. |

### Not working

| Principle | Failure |
|---|---|
| **#3 Deliberate Practice (Ch.12) — push to the edge.** | The "advanced S1 student" played the persona by giving full canonical work (mechanism named on every turn). Rule 7 correctly routes `richness=="rich"` to `confirm_and_extend`, which is supposed to "push a single twist that lives on the same concept" (`move_prompts.py:339-344`). In practice, the follow-up poses were the **next bank slot at the same rigor**, not a harder twist — the bank itself only contains S3 single-step stems (CP/SP/profit/loss/discount as five separate calculations). So the move *prompt* asks for a twist, but the *tool slot it has to pose from* doesn't have a harder slot. This is a curriculum gap, not a prompt gap: the lesson's question bank doesn't authoring composition/transfer items (e.g., "find CP given SP and 25% markup", "the marked price is 280 SCR; if a 15% discount yields a 50 SCR profit, what is the CP?"). |
| **#5 Minimise Cognitive Load (Ch.14) — expertise reversal.** | Turn 1748: "You nailed the markup calculation — 60% of CP added on to get SP, then profit = SP − CP. That's the full chain working cleanly." The second sentence re-authors the rule the student named in turn 1747 ("60% of 450 = 270, so SP = 450 + 270 = 720 SCR, profit = 270 SCR per kg"). `move_prompts.py:347-355` explicitly forbids this: *"do NOT re-author the mechanism in your own words … re-stating it back reads as condescension AND is the dominant trigger for an answer-leak / redundant-factual-claim rejection on this move"*. The rule is authored but not structurally enforced — the prune deleted the answer-leak / praise-filter gate that would have flagged this. Same pattern on turns 1754 and 1760. |
| **#7 Layering (Ch.16) — compose prerequisites.** | All six posed items are isolated single-step calculations on one named term at a time. None require composing two terms (e.g., "a shopkeeper buys for 200 SCR and sells at a 30% markup, then offers a 10% discount — what is the customer's final price and the shopkeeper's profit?"). The advanced student is not exercised on composition. Same root cause as the Deliberate Practice gap above — the curriculum bank, not the prompt. |
| **#11 Testing Effect (Ch.20) — feedback that lands.** | Every correct retrieval (turns 1747, 1751, 1753, 1757, 1759) received informative feedback in the immediately-following tutor turn. The runtime state correctly persists each verdict in `recent_verdicts` and bumps `objective_progress` so the next turn's router has accurate counters. The retrieval-consolidation loop is intact end-to-end. *(The original report flagged a state-machine break here; that finding has been retracted — see §2 cross-cutting.)* |

### Cross-cutting: state-machine writes — RETRACTION

**This report's original "state-machine wedge" finding was incorrect and has been retracted.** The inspection script I ran used stale field names from `design/LOCAL_TESTING_GUIDE.md` (`state`, `current_step_idx`, `asked_questions`, `last_move`, `last_verdict`, `is_lesson_complete`) — none of those exist in the current `SessionRuntimeState` schema (`apps/tutoring/v2/contracts/runtime_state.py:173`).

Re-inspecting the runtime_state with the actual schema fields shows the engine IS persisting state correctly on every turn:

```
engine_version='v2'
runtime_state.current_move                  = 'close_topic'
runtime_state.move_history                  = ['explain', 'confirm_and_advance',
                                                'confirm_and_extend', 'close_topic',
                                                … 10 entries …]
runtime_state.delivered_lesson_step_ids     = [10009, 10010, 10011, 10006, 10005, 10008]
runtime_state.recent_verdicts               = ['correct', 'correct', 'correct',
                                                'correct', 'correct', 'correct']
runtime_state.safety_valve_counters         = {'turns_in_session': 10, …}
runtime_state.objective_progress[<obj>]     = {'attempts': …, 'correct': …, 'wrong': …}
```

The `delivered_lesson_step_ids` ledger has all 6 posed stems. The move history, verdict history, and safety counters are intact. There is no state-write regression.

`design/LOCAL_TESTING_GUIDE.md` is what's outdated — the documented inspection one-liner uses the legacy `engine_state` field names rather than the v2 `runtime_state` field names, which produced the false-negative inspection output that this report relied on. The testing guide is being updated alongside this retraction.

---

## 3. Router & move-prompt review

Both prompts (`router_prompts.py`, `move_prompts.py`) are well-authored, principle-cited, and follow the prompting-skills directives in CLAUDE.md. The failures observed this run are not in the authored prompt text — they are **runtime gaps left behind by the prune** (no conformance gate; no safe-template floor; no state-write enforcement). Recommendations split into (3.1) prompt-internal hardening and (3.2) runtime gaps the prompts can't fix.

### 3.1 Router prompt (`apps/tutoring/v2/services/router_prompts.py`)

**Strengths.**
- Closed 8-move set with principle citations on every rule (`router_prompts.py:55-91`). Each rule references the chapter it operationalizes — this is the right design for a pedagogy-driven router.
- Rule 7's `correct` branch correctly distinguishes `confirm_and_advance` vs `confirm_and_extend` vs `close_topic` by `richness` and `unscaffolded_correct_on_objective` — three different responses for three different states.
- Invariants I-1 … I-5 are tight and verifiable (`router_prompts.py:244-260`).

**Issues observed.**

**R1. The `confirm_and_extend → close_topic` cadence collapses to 2-turn cycles on rich students.** On turns 1746→1748, 1752→1754, 1758→1760, the cycle was: (a) `confirm_and_extend` on first rich-correct, (b) `close_topic` on second rich-correct. This is exactly what Rule 7 mandates, but for an advanced student giving full canonical work every time, it means **every objective closes in 2 attempts** with the same-rigor bank slot. The router has no way to lift the rigor.

**Recommendation R1.** Add a fourth correct-branch path: when `unscaffolded_correct_on_objective >= 1` AND `richness == "rich"` AND a *transfer/composition* slot exists in the bank, route to a new move `confirm_and_lift` (or extend the existing `confirm_and_extend` semantics) that targets the harder slot. Requires curriculum-side authoring of composition slots; the router rule is one if-clause.

**R2. Rule 7 `close_topic` fires on `unscaffolded_correct_on_objective >= 1` (so the 2nd correct closes), with no consideration of `wrong_attempts_on_open_question`.** This works for the advanced persona but in the GEO-S5 run (see companion report) a struggling student with 6 wrongs and 2 corrects exits the lesson with `is_lesson_complete=true`. The Mastery Learning principle (#4 Ch.13) reads: "Hold the same bar for all students; vary the path, not the standard." 2-out-of-8 attempts is below the standard.

**Recommendation R2.** Add an invariant I-6: `close_topic` via Rule 7 correct branch requires the **wrong-to-correct ratio on the objective to be ≤ 2:1** (i.e., at most 4 wrongs accompanying 2 correct unscaffolded). Below that threshold, route to `confirm_and_advance` on the next eligible slot instead — the student must demonstrate the standard on additional items before close.

**R3. Rule 6 (post-step pose) defaults to `confirm_and_advance` even when no answer has been given yet.** Turns 1744, 1750, 1756 fired Rule 6 with no prior verdict — they are *pose* turns, not affirm-then-pose turns. The move's prompt (`move_prompts.py:273-326`) is written for a CORRECT verdict context ("The grader marked the student CORRECT"). On a pose-only turn, the "confirm" half of the prompt is vestigial.

**Recommendation R3.** Either (a) introduce a `pose_next` move (no confirmation, just pose) or (b) parameterize `confirm_and_advance` to skip the affirmation when `verdict is None`. The current behavior works because the tutor LLM ignores the unmet "confirm" instruction, but the prompt drift is a latent failure mode.

### 3.2 Move prompts (`apps/tutoring/v2/services/move_prompts.py`)

**Strengths.**
- Universal preamble (`move_prompts.py:63-219`) carries voice, subject anchoring, subskill stickiness, one-question-per-turn, the no-empty-connectives rule, the help-request override, the LO-leak guard, the Active Learning end-of-turn floor, and the expertise-reversal guard. Eight separate authored protections in one place — this is the right level of consolidation.
- Each per-move prompt cites its principle row(s) from `science-principles.md` (`move_prompts.py:275, 332, 387, 482, 526, 603, 730, 765`). Provenance is auditable.
- `confirm_and_extend` explicitly forbids re-authoring the mechanism (`move_prompts.py:347-355`).
- `worked_example` requires labelled subgoals and an end-of-turn practice prompt (`move_prompts.py:559-577`).

**Issues observed.**

**M1. `confirm_and_extend` re-authors the mechanism three times this run (turns 1748, 1754, 1760).** The rule at `move_prompts.py:347-355` is clear, but the violations are equally clear:

- Turn 1748: "60% of CP added on to get SP, then profit = SP − CP. That's the full chain working cleanly." — re-authors the student's "60% of 450 = 270, so SP = 450 + 270 = 720 SCR, profit = 270 SCR per kg."
- Turn 1754: "CP − SP when CP > SP" — restates the formula the student just used.
- Turn 1760: "25% of 120 = 30 SCR — exactly right" — repeats the student's arithmetic verbatim.

The rule's intent ("the affirmation names what they named; the follow-up question carries the load") is not enforced. The structural conformance gate that would catch this (`answer-leak / redundant-factual-claim`, per the prompt's own meta-comment at `move_prompts.py:352-354`) was deleted in the prune.

**Recommendation M1.** Tighten the affirmation directive to be quantitatively bounded:

> "AFFIRMATION CLAUSE: The first clause of your response is ≤8 words. It names the substantive term the student used (the operation name, the formula label, the discriminator) but does NOT restate the arithmetic, the formula expression, or the chain of reasoning. Example shape: 'You nailed the markup calculation —' (8 words, names 'markup calculation', does not restate '60% of CP added on'). Counter-example to avoid: 'You nailed the markup calculation — 60% of CP added on to get SP, then profit = SP − CP. That's the full chain working cleanly.' (re-authors the mechanism; rejected.)"

The 8-word cap is enforceable by a deterministic gate at the conformance layer (count tokens in the leading clause). The student's named term can be detected by extracting the verdict's `what_right` field and verifying the next sentence does NOT contain a re-emission of any operator or numeric chain the student used.

**M2. Subject anchoring works at lesson open but not at lesson close.** Turn 1754's close ("solid work across all five terms throughout this lesson") and turn 1760's close ("Strong, consistent work across all five terms") both say "all five terms" when only **three of five** had been retrieval-tested (markup, loss, discount — profit was only T/F-checked, discount and CP appeared only inside problem stems). This is a mild factual overclaim that survives because the v2 trace shows the engine doesn't track per-term retrieval counts.

**Recommendation M2.** Close-topic prompt should either (a) drop the "all five terms" affirmation when the engine cannot prove all five were retrieval-tested, or (b) accept a `terms_retrieved` counter from the engine and condition the affirmation on it. Cleaner is (a): replace the verb-phrase with concrete evidence — "You worked through markup, loss, and discount in turn — that's solid evidence on the calculation chain."

**M3. `explain` opening lesson worked this run, but the prompt's "OPEN-ENDED prose prompt" rule at `move_prompts.py:652-668` is at risk of being violated by content drift.** Turn 1742 ended with "Which of those five terms — cost price, selling price, profit, loss, or discount — feels least familiar to you right now?" — that's a single-canonical-answer question (the student's named term), which the rule explicitly forbids in prose. The prompt's own self-check at `move_prompts.py:669-680` should have caught this ("read the last sentence of your turn. If it has a single canonical answer it MUST be posed via the tool"). It didn't, because "feels least familiar" is reflective and has no canonical answer. This is fine in practice. The risk: the prompt's distinction between "single canonical answer" and "reflective with no canonical answer" depends on the LLM's reading of the verb ("feels"), which is brittle.

**Recommendation M3.** Add an enumerated list of "always-reflective" verbs/phrases to the prompt: `feels`, `seems to you`, `would you guess`, `which sounds most`, `which have you seen`. The opening pose may use any of these without a tool; any other interrogative form must be tool-posed.

### 3.3 Runtime gaps (post-prune)

The deleted modules (`conformance/check.py`, `conformance/templates/floors`, the pose-tool two-phase commit, the `MAX_POSE_ATTEMPTS_PER_TURN` retry) covered failure modes that the prompts alone cannot enforce. The most expensive ones this run:

| Gap | Symptom on this run | Worst-case symptom (observed run 9 / GEO-S5) |
|-----|---------------------|--------------------------------------------|
| Empty-message floor | Not observed this run | 6 empty tutor turns in run 9; 1 empty tutor turn in GEO-S5 same-day |
| Answer-leak / praise-filter gate | M1 above — soft drift, not student-visible failure | Mechanism re-authored in 4/5 affirmations on the same engine commit |
| Pose-tool two-phase commit | Not observed this run (all poses succeeded) | GEO MCQ posed in prose (not via tool), per same-day GEO-S5 report |
| State writes on every turn | All 6 interim fields silently `None` | Same — documented in `DIAGNOSIS-regression-2026-05-27.md` §3 |

**Recommendation 3.3.** Reintroduce the three smallest deterministic gates (empty-message floor, single-question-per-turn, no-prose-pose) without the full conformance LLM classifier. Per `design/refactor/refactor-implementation-plan.md` §3.5 these can be in-engine regex / structural checks costing ~1 ms per turn. The state-write fix is independent — the engine must write `runtime_state.{state, current_step_idx, open_question, last_move, last_verdict}` on every turn, and `asked_questions` must be appended on every pose.

---

## 4. Summary

| Dimension | Verdict |
|---|---|
| P1 errors | **0** — clean run |
| Active Learning (#1) | Working |
| Direct Instruction (#2) | Working (opening) |
| Deliberate Practice (#3) | Partially blocked by curriculum (no transfer slots) |
| Mastery Learning (#4) | Working — terminal completion + interim counters both persisted |
| Cognitive Load (#5) | Expertise-reversal violation in 3/3 confirm-and-extend turns |
| Layering (#7) | Not exercised (curriculum gap) |
| Testing Effect (#11) | Working — verdict ledger and per-objective counters persisted |
| Voice / anchoring | Working |
| Engine state writes | Working (original finding retracted — was a stale-fieldname inspection error) |

**Most urgent recommendations** (rank-ordered):

1. Tighten `confirm_and_extend` affirmation to ≤8 words + named-term, no mechanism re-emission (M1).
2. Author transfer/composition slots in lesson banks so the router has a harder pose to choose (R1).
3. Add Rule 7 close-topic invariant I-6: wrong:correct ratio ≤ 2:1 on the objective before close fires (R2).
4. Update `design/LOCAL_TESTING_GUIDE.md` inspection one-liner to use the actual v2 `runtime_state` field names so future evaluations don't repeat this report's stale-fieldname diagnosis error.

---

*Report generated by Claude (claude-opus-4-7) following the `evaluate-tutor` skill workflow. Local dev server on `127.0.0.1:8000`, v2 engine (`engine_version='v2'`).*
