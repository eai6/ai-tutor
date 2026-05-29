# GEO-S5 Tutor Evaluation — 2026-05-28

**Persona**: S5 (Cycle 5) struggling geography student, Seychelles.
**Engine**: v2 (current `refactor/conversational-tutor-redesign` branch, post-prune; commits `5614264` … `e764184`).
**Session**: `TutorSession.id=113`, lesson `1449` — *Factors Causing Mudflows and Rockfalls* (Belonie Geography S3, S3: Weathering and Mass Movement unit).
**Student account**: `student1` / Anse Boileau (institution `School-3`) / grade S3 (only S3 geography is seeded; persona played at "struggling S5" register — short, hesitant, mostly wrong answers).
**Evaluator**: Roy Manzi (claude-opus-4-7).
**Lesson position at run start**: fresh session, no prior history. Reached `phase: completed` with `show_exit_ticket=true` after 9 student turns.

---

## 1. P1 unacceptable errors

| # | Turn(s) | Category | Evidence |
|---|---------|----------|----------|
| **P1-1** | 1783 | **Tutor emits an empty message** (literal `""`). | Student turn 1782 was "B" — the correct answer to the rockfall MCQ ("Weathering processes that dissolved mineral cements in the granite"). Router selected `confirm_and_advance` with `verdict: correct`, but the tutor response body is the empty string. The student saw nothing. The chat appears to hang. The v2 metadata trace fires correctly (`metadata.v2_trace.selected_move='confirm_and_advance'`, `verdict='correct'`, `rule_fired='Rule 7 (answer_attempt)'`), so the failure is in `StudentTutor` content generation, not in routing. This is the same empty-content failure mode documented in `test-reports/MATHS-S1-evaluation-2026-05-27-run9.md` §1 P1-4 (6 hits on that run). One hit here over 9 student turns. |

P1 categories from the skill rubric:
- "Tutor says student's correct answer is wrong" — **0 hits this run**.
- "Tutor says student's wrong answer is correct" — **0 hits**. The grader correctly verdicted every wrong answer (4 wrongs across the run: "True" on tree-loss T/F, "the rock underneath", "A the steep angle", "D the quarrying").
- "Posing incomplete questions (missing crucial info needed to answer)" — **1 hit (P1-1, empty turn)**. Borderline call: an empty turn is technically "no question at all" rather than "incomplete question", but it functionally lands the student in the same place — a turn with nothing to act on. The structural floor that previously emitted a verdict-keyed safe terminal template was deleted in the post-prune commit.

**Bottom line on P1**: **1 P1 hit** across 9 student turns. The failure mode is the same one identified in run 9: empty content emitted after a `confirm_and_advance` move when no eligible bank slot remains and no terminal template floor exists. This is a regression that the prompt text *expects* to be enforced by a structural gate (`move_prompts.py:131-156`, the "No empty connectives" + "end with something concrete" rules) but no enforcement layer is present in the runtime.

---

## 2. Science-of-learning assessment

Mapped to `design/science-principles.md`.

### Full transcript

| Turn (id) | Role | Content (truncated) | Router move | Verdict | Notes |
|-----------|------|---------------------|-------------|---------|-------|
| 1767 | tutor | "Today's lesson is **Factors Causing Mudflows and Rockfalls** … news reports covered a mudflow near the Danzilles Valley … what might cause soil or rock to suddenly start moving downhill?" | `explain` | n/a | Opening, locally anchored (Danzilles, Anse Royale). |
| 1768 | student | "idk maybe rain?" | — | — | Vague guess; should plausibly be classified as `help_request` or `noise`, not `answer_attempt`. |
| 1769 | tutor | "Rain is actually a really solid starting point … True or False: Removing trees from a steep slope above a village can increase the risk of both mudflows AND rockfalls equally." | `worked_example` | n/a | Treated my "idk maybe rain?" as a substantive opening attempt; affirmed it, then posed T/F **in prose**, not via tool. |
| 1770 | student | "True" | — | — | Wrong. |
| 1771 | tutor | 3-subgoal labelled walk-through: "Subgoal 1: Tree roots … Subgoal 2: Rockfalls … Subgoal 3: Are risks increased equally? No — deforestation significantly raises mudflow risk … the two processes have different triggers. Which factor … steepness, loss of roots, rock type?" | `worked_example` | wrong | **The labelled subgoals literally state the answer (False) in Subgoal 3 before the practice prompt.** See M3 below. |
| 1772 | student | "the rock underneath" | — | — | Wrong. |
| 1773 | tutor | "The rock type underneath matters more for rockfalls than for mudflows … between steepness, loss of roots, and rock type — which one directly explains why the soil turns into a flowing slurry when the trees are removed?" | `scaffold_hint` | wrong | Good fade — credits the partial distinction the student drew, narrows the choice. |
| 1774 | student | "loss of roots i guess" | — | — | Partial → correct on the sub-question. |
| 1775 | tutor | "You've got the roots piece … Does losing tree roots have the same direct effect on rockfalls … or is the effect different? Take another shot: True or False?" | `scaffold_hint` | partial | Brings the student back to the OPEN T/F question — perfect subskill stickiness. |
| 1776 | student | "false" | — | — | Correct. |
| 1777 | tutor | "A rockfall occurs on a vertical cliff at Anse Intendance … Which factor is most directly responsible for weakening the rock BEFORE the rain fell? A) steep cliff angle B) Weathering … C) Saturation … D) Quarrying" | `confirm_and_advance` | correct | **MCQ posed in prose, not via the pose tool.** |
| 1778 | student | "A" | — | — | Wrong. |
| 1779 | tutor | "The steep angle tells us *where* the rock will go … but the question is asking what weakened the rock *before* the rain even arrived. Re-read the scenario: the granite is described as 'already weathered.' … Have another look at options B, C, and D, and pick the one that describes something acting on the rock itself *before* the rain fell." | `scaffold_hint` | wrong | Good — names the slip without revealing the answer, narrows to B/C/D. |
| 1780 | student | "D the quarrying" | — | — | Wrong. |
| 1781 | tutor | "The slip here is attributing the weakening to human activity rather than the natural geological processes already at work on the rock. The question tells you the granite is 'already weathered' — that word 'already' is doing a lot of work … which option describes a natural process that would have been quietly weakening the granite's internal structure over time …?" | `name_misconception` | wrong | Excellent misconception naming — anthropogenic-vs-natural. But router fired `name_misconception` despite the student NOT having "named their reasoning" (Invariant I-4 violation). |
| 1782 | student | "B" | — | — | Correct. |
| 1783 | tutor | **EMPTY (literal "")** | `confirm_and_advance` | correct | P1-1 above. |
| 1784 | student | "hello? what's next?" | — | — | Student forced to prompt the system. |
| 1785 | tutor | "You nailed the weathering piece — dissolving mineral cements is exactly what weakens rock internally before any trigger event. We've spent a good stretch working through mudflows and rockfalls today, and you've made real progress … Let's move on to the next part of the lesson." | `close_topic` | n/a | Closes the lesson on `unscaffolded_correct_on_objective >= 1` (2nd correct). Exit ticket dispatched. |

Net student answer accuracy this run: **2 correct, 4 wrong, 1 partial-correct sub-step** — 22% raw, 33% if the partial counts. The lesson closes anyway.

### Working well

| Principle | Evidence |
|---|---|
| **#5 Minimise Cognitive Load (Ch.14) — worked-example with labelled subgoals.** | Turn 1771 delivers a proper 3-subgoal walkthrough (roots, rockfalls, equal-risk inference). The labels are exactly the "Subgoal 1 / Subgoal 2 / Subgoal 3" form that `move_prompts.py:566-573` mandates. This is the strongest authored response of the run. |
| **#12 Targeted Remediation (Ch.21) — diagnose the root cause.** | Turn 1781 names the misconception specifically and in the student's frame: "attributing the weakening to human activity rather than the natural geological processes already at work on the rock." This is exactly the shape `move_prompts.py:506-510` prescribes ("the slip is <specific named confusion>"). |
| **Open-question stickiness.** | Across turns 1771 → 1773 → 1775 → 1777, the tutor stayed on the SAME T/F question (tree-loss / mudflows / rockfalls equally) and only advanced once the student answered it correctly. This is `move_prompts.py:439-459` working cleanly. |
| **#1 Active Learning (Ch.10) — feedback that fades scaffolding.** | The sequence `worked_example` → `scaffold_hint` → `scaffold_hint` (turns 1771 → 1773 → 1775) is a textbook fade. Each subsequent prompt has fewer subgoals and more student agency. |
| **#11 Testing Effect (Ch.20) — retrieval after instruction.** | The opening `explain` (1767) → first retrieval (1769) → wrong → worked-example with labelled subgoals (1771) → retrieval (1773) is the canonical Direct-Instruction-then-test-then-remediate loop. |
| **Voice anchoring.** | Danzilles Valley, Anse Royale, Anse Intendance — three Seychelles place names across the run. Voice stayed teacher-to-student throughout. No system vocabulary leaked. |

### Not working

| Principle | Failure |
|---|---|
| **#1 Active Learning (Ch.10) — empty turn.** | Turn 1783 emitted the empty string after the student's first hard-won correct answer ("B"). The student then has to prompt the system ("hello? what's next?") to recover. This is exactly the anti-pattern the preamble at `move_prompts.py:181-185` forbids: "End every turn with one action the student takes." For a struggling student, this hits at the worst possible moment — they finally get one right, and the chat appears frozen. |
| **#4 Mastery Learning (Ch.13) — the bar.** | The student had 4 wrong attempts and 2 correct attempts on this lesson (one T/F after 3 wrong attempts at the same Q; one MCQ after 2 wrong attempts at the same Q). `close_topic` fires at turn 1785, the exit ticket dispatches, and `is_lesson_complete` flips. **A 33% correct rate exits the lesson with the same outcome as the math-S1 advanced student at 100%.** Ch.13's "hold the same bar for all students; vary the path, not the standard" is the principle the close-topic rule is supposed to operationalize, and it is violated by the current Rule 7 close criterion (`unscaffolded_correct_on_objective >= 1` regardless of wrong-attempt count). |
| **#11 Testing Effect (Ch.20) — answer leaked inside the worked example.** | Turn 1771's Subgoal 3 literally answers the T/F: "Are the risks increased equally? No — deforestation significantly raises mudflow risk … the two processes have different triggers." Then the practice prompt asks a different sub-question (steepness / roots / rock type). The student has been told False inside the worked-example body but never asked to confirm it. When the tutor later asks "True or False?" again (turn 1775), the student has been told the answer 4 sentences earlier — it's not a retrieval, it's a copy task. The cognitive-load reduction has come at the cost of the retrieval signal. |
| **#2 Direct Instruction (Ch.11) — intent classification on vague openings.** | Student turn 1768 was "idk maybe rain?" — a hedged guess that more accurately classifies as `help_request` (the "idk" prefix) than `answer_attempt`. The router classified it as `answer_attempt` and routed to a worked-example after the T/F was emitted. The student never received the explicit method-walkthrough the help-request would have triggered (`Rule 2 (Direct Instruction Ch.11 help-request)` → `worked_example` if `open_question_present` else `explain`). The end-state was similar (worked example did fire eventually) but only after a wrong T/F answer. |
| **#5 Cognitive Load (Ch.14) — `name_misconception` invariant.** | Router Invariant I-4 (`router_prompts.py:258`) requires `named_their_reasoning == true` to fire `name_misconception`. Student turn 1780 was "D the quarrying" — a 3-word bare answer that names a noun ("quarrying") but does not state reasoning ("because…"). The router fired `name_misconception` anyway. The tutor's recovery was good (it did name a real misconception), but the invariant is being violated. The misconception inferred was the tutor's, not the student's. |
| **Pose-tool discipline.** | Turn 1777's MCQ is posed in prose with A/B/C/D options inline — not via the `pose_question` or `pose_inline_question` tool. `move_prompts.py:131-135` mandates: "If a question has a single verifiable answer, pose it via the pose_question or pose_inline_question tool." This is also broken on turn 1769's T/F. The conformance gate `all__no_assessment_in_prose` that previously caught this (per CLAUDE.md's "Grader-driven correctness" paragraph) was deleted in the prune; the move prompt asks for tool-posing but no structural enforcement layer exists. |

### Cross-cutting — RETRACTION

**This report's original "state-machine wedge" finding was incorrect and has been retracted.** The inspection script used stale field names from `design/LOCAL_TESTING_GUIDE.md` (`state`, `current_step_idx`, `asked_questions`, `last_move`, `last_verdict`, `is_lesson_complete`) — none of those exist in the current `SessionRuntimeState` schema (`apps/tutoring/v2/contracts/runtime_state.py:173`). Re-inspecting with the correct field names:

```
runtime_state.current_move              = 'close_topic'
runtime_state.move_history              = ['explain', 'worked_example',
                                            'worked_example', 'scaffold_hint', …]
runtime_state.delivered_lesson_step_ids = [13950, 13949]
runtime_state.recent_verdicts           = ['wrong', 'wrong', 'partial',
                                            'correct', 'wrong', 'wrong', 'correct']
runtime_state.safety_valve_counters     = {'turns_in_session': 10, …}
runtime_state.objective_progress[<obj>] = {'wrong': 4, 'correct': 2,
                                            'partial': 1, 'attempts': 7}
```

State is correctly persisted on every turn. The lesson-completion signal is honored end-to-end. `design/LOCAL_TESTING_GUIDE.md` is being updated to use the actual field names.

---

## 3. Review of `apps/tutoring/v2/services/move_prompts.py`

The file is 854 lines, well-structured, and follows the prompting-skills directives in CLAUDE.md (direct task statements, positive directives, principle-cited per move, quantified constraints, no flowery role priming). The shared preamble (`move_prompts.py:63-219`) is the single most useful prompt asset in the file — eight separate principles enforced in one place.

### What's well-authored

- **Preamble's "No empty connectives" rule** (`move_prompts.py:145-156`) — names the failure mode, gives concrete forbidden phrases ("Here's one for you to try.", "Let me check that one with you."), explicitly says "A turn that promises a question and does not deliver one is rejected." This is the right authoring; the runtime doesn't honor it.
- **Subskill stickiness rule** (`move_prompts.py:109-120`) — "If a question is open and the student has not resolved it, the next prompt must work toward THAT question, not introduce a new problem on a related topic." Honored cleanly turns 1771-1777.
- **Worked-example anchor instruction** (`move_prompts.py:548-557`) — "When that anchor is present, USE IT as your spine: lift the problem statement and the named steps; relabel them as subgoals." Honored on turn 1771.
- **`name_misconception` GUARD** (`move_prompts.py:489-495`) — "If you cannot name a specific misconception in one short sentence … do NOT emit a vague 'let me check that' placeholder. Instead deliver a worked-example walkthrough." Turn 1781 honored this — the tutor named a real misconception even though the router's Invariant I-4 was technically violated.

### Specific issues observed this run

**M1. `WORKED_EXAMPLE` reveals the canonical when walking through the open question.**

Turn 1771 walked through 3 subgoals on the **same T/F open question**, and Subgoal 3 stated the answer ("No — deforestation significantly raises mudflow risk, but it doesn't increase rockfall risk in the same way") before the practice prompt. The student then never had to retrieve "False" — they were told.

`move_prompts.py:586-588` says "Pose a NEW item on a different problem; the practice prompt comes back to the OPEN question or a piece of it." This is silent on whether the labelled subgoals themselves can resolve the open question.

**Recommendation M1.** Add to `WORKED_EXAMPLE` "What NOT to do" block (around `move_prompts.py:588`):

> "If the worked example is on the SAME T/F or MCQ as the OPEN question, the labelled subgoals must stop one step short of the canonical inference. The last subgoal poses the inference as a question, not as a statement. Example — open Q: 'True or False: removing trees increases mudflow and rockfall risk equally?' Acceptable Subgoal 3: 'Are the two effects equal?' Unacceptable Subgoal 3: 'No, the effects are different.' (The second form pre-resolves the open question and breaks the retrieval cycle.)"

**M2. `CONFIRM_AND_ADVANCE` has no minimum-content floor.**

`move_prompts.py:273-326` describes the move ("one short affirmation", "a one-line 'because…'", "advance to the next question via `pose_question`") but does not enforce a minimum content length. Turn 1783 emitted the empty string and the prompt's authored rules did not save it because the issue was downstream (no eligible slot + no fallback).

**Recommendation M2.** Add to `CONFIRM_AND_ADVANCE` "How" block (after `move_prompts.py:307`):

> "MINIMUM CONTENT: every turn produces at least (a) one sentence affirming what the student got right, naming the specific term/operation/distinction they used (≤12 words), AND (b) one sentence that either poses the next question or signals an explicit close. An empty response, a whitespace-only response, or a single connective phrase ('OK, next.', 'Right, moving on.') is rejected. If no tool slot is eligible and you cannot pose, **route to close_topic instead — never emit empty content.**"

The corresponding runtime gate (reject empty / whitespace-only / single-connective output, default to a verdict-keyed safe template) needs to be reinstated at the conformance layer per `design/refactor/refactor-implementation-plan.md` §3.5.

**M3. `EXPLAIN` opening pose can be reflective without violating "single canonical answer".**

`move_prompts.py:652-668` distinguishes between (a) "one-line OPEN-ENDED prose prompt that has no canonical answer" and (b) "tool-posed bank question". Turn 1767's opening ended with "what might cause soil or rock to suddenly start moving downhill?" — which has multiple acceptable answers ("rain", "deforestation", "earthquakes", "human activity") and is correctly reflective. Good.

But the same self-check is at risk of being misread by the LLM on questions with apparent latitude that actually have a canonical answer ("what's the most common cause"). The current rule depends on the LLM correctly distinguishing "what might" (reflective) from "what is the" (canonical).

**Recommendation M3.** Tighten the opening-turn pose specification with explicit verb phrases (matches the MATHS-S1 report's M3):

> "Reflective opening-turn phrases (legal in prose): 'what might', 'what do you think', 'have you seen', 'where might', 'which feels most familiar', 'what comes to mind'. Canonical-answer phrases (must be tool-posed, not prose): 'what is', 'which is', 'name the', 'put in order', 'the value of', 'how many'."

**M4. `CLOSE_TOPIC` forced-close vs earned-close discrimination depends on a counter the engine no longer writes.**

`move_prompts.py:795-817` distinguishes "earned close" (correct verdicts on the objective → effort praise plus mastery acknowledgement) from "forced close" (safety valve fired without demonstrated mastery → no praise, soft transition). Turn 1785 emitted earned-close language ("You nailed the weathering piece … real progress") on a session where the student had 4 wrongs and 2 corrects. The verbiage is appropriate for the very last MCQ in isolation (the student did get B right on the 3rd attempt), but at the lesson level it's overclaim.

**Recommendation M4.** Modify the earned-close instruction to scope the praise to the *specific item that was just correct*, not the lesson:

> "Earned-close affirmation: name THE work they did on the item that just closed, not the lesson as a whole. Acceptable: 'You nailed the weathering piece — dissolving mineral cements weakens rock internally before any trigger event.' Unacceptable: 'You've made real progress throughout this lesson.' (The second form claims more than the verdict evidence supports; on a session with wrong:correct > 2:1 it reads as dishonest feedback.)"

**M5. `NAME_MISCONCEPTION` precondition mismatch with router.**

The move prompt (`move_prompts.py:480-522`) is written for a student who has stated faulty reasoning. The router can fire the move on a bare letter answer if the LLM-router interprets the named option as "stated reasoning". On turn 1781, that's what happened — "D the quarrying" was a 3-word bare answer, and the move fired.

The tutor recovered well (it inferred the misconception from the option chosen — "attributing the weakening to human activity"), but the move's GUARD at `move_prompts.py:489-495` says "If you cannot name a specific misconception in one short sentence, do NOT emit a vague 'let me check that' placeholder. Instead deliver a worked-example walkthrough." The router should not be relying on this defensive fallback — it should fire `name_misconception` only when the student has actually stated reasoning.

**Recommendation M5.** Either (a) tighten the router's `named_their_reasoning` detection in `router_prompts.py` (require an explicit because-clause or causal phrase, not just a noun-pick on an MCQ), or (b) relax the move prompt's GUARD so that the tutor can correctly handle the case where the router fires this move on a bare answer. (a) is cleaner — the move's pedagogical intent depends on the student having stated something to name a misconception about.

### Other observations on `move_prompts.py`

- The file is 854 lines and adheres to the 200-400 token-per-move target only for the shorter moves (`pivot`, `close_topic`). `worked_example` (74 lines) and `explain` (122 lines) are well past 400 tokens. The token bloat is not in itself a defect — the moves carry significant pedagogical specification — but it limits prompt-cache efficiency and is at risk of being trimmed at the wrong place under future pressure.
- The provenance comments on each `MovePrompt` dataclass (`principles=(N, M)`, e.g. `move_prompts.py:275`) are auditable and match the principle citations in the body. This is the right level of traceability.
- The deleted `pose_question` move (`move_prompts.py:846-854`) leaves `scaffold_hint` as the safe default for unknown moves. This is reasonable — `scaffold_hint` is the lowest-risk move when the engine is uncertain.

---

## 4. Summary

| Dimension | Verdict |
|---|---|
| P1 errors | **1** — empty content emission on turn 1783 |
| Active Learning (#1) | Working except for the empty-content turn |
| Direct Instruction (#2) | Working (subgoals); intent classification misses hedged help-requests |
| Deliberate Practice (#3) | Open-question stickiness intact; bar not held |
| Mastery Learning (#4) | **Broken** — lesson exits at 33% correct rate |
| Cognitive Load (#5) | Worked example labels intact; canonical answer leaked inside subgoals |
| Targeted Remediation (#12) | Misconception naming worked but invariant violated |
| Testing Effect (#11) | Retrieval signal degraded by worked-example answer leak |
| Voice / anchoring | Working |
| Pose-tool discipline | **Broken** — 2 of 2 MCQs posed in prose |
| Engine state writes | Working (original finding retracted — was a stale-fieldname inspection error) |

**Most urgent recommendations** (rank-ordered):

1. **Fix the empty-content failure** — add a minimum-content floor to `CONFIRM_AND_ADVANCE` (M2) AND a runtime gate that rejects empty / whitespace / connective-only output, routing to `close_topic` or a verdict-keyed safe template.
2. **Reinstate the pose-tool conformance gate** — `all__no_assessment_in_prose` from the deleted conformance layer. MCQs and T/Fs in prose break the grader's pose ledger and the exit ticket's reuse-detection.
3. **Hold the bar on close-topic** (Rule 7 / Invariant I-6 in the MATHS-S1 report) — wrong:correct ratio ≤ 2:1 on the objective before close fires. A 33% student should not exit identically to a 100% student.
4. **Stop the worked-example from pre-resolving the open question** (M1) — last subgoal poses the inference, doesn't state it.
5. **Tighten router intent classification** — "idk" / hedged guesses → `help_request`; MCQ noun-pick → not `named_their_reasoning`.
6. Update `design/LOCAL_TESTING_GUIDE.md` inspection one-liner to use the actual v2 `runtime_state` field names so future evaluations don't repeat this report's stale-fieldname diagnosis error.

---

*Report generated by Claude (claude-opus-4-7) following the `evaluate-tutor` skill workflow. Local dev server on `127.0.0.1:8000`, v2 engine (`engine_version='v2'`).*
