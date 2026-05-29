# MATHS-S1 Tutor Evaluation — 2026-05-28 (run 11)

**Persona**: S1 (Cycle 4) struggling mathematics student, Seychelles. Consistently wrong answers, hesitant phrasing, occasional bare guesses.
**Engine**: v2 (`refactor/conversational-tutor-redesign` @ HEAD `e68263f` — *post-run-10 fix bundle*).
**Session**: `TutorSession.id=119`, lesson `1148` — *Solve One-Step Equations (unknown on one side) positive integer solutions only*. 12 student turns, **reached exit ticket**.
**Student account**: `student-run11` / Global / S3 (fresh account so the four MATHS lessons would not be in "review" mode; persona played at S1 register).
**Evaluator**: Roy Manzi (claude-opus-4-7).

**Purpose of this run**: validate the run-10 fix bundle in production. Specifically retest the two trap-distractor wrongs that previously laundered into CORRECT (commit `e68263f` — Math Fix 1c letter-disagreement guard + LLM-A/LLM-B checklists).

---

## 1. P1 unacceptable errors

| # | Category | Hits |
|---|----------|------|
| Tutor says student's correct answer is wrong | **0** |
| Tutor says student's wrong answer is correct | **0** |
| Posing incomplete questions (missing crucial info needed to answer) | **0** strict; 1 borderline (empty `confirm_and_advance` at T1874) |

**Bottom line**: **0 hard P1 hits** across 12 student turns. Both run-10 P1 retest cases (`"B because 23 + 8 = 31"` on x+8=23, `"D, x = 42 because thats the starting number"` on 42−x=17) **now grade WRONG** — the letter-disagreement guard fired and `name_misconception` ran. The fix bundle worked.

### Run-10 P1 retest results

| Turn (id) | Student input | Open question | Canonical | Old run-10 verdict | **Run-11 verdict** |
|---|---|---|---|---|---|
| 1860 | `"B because 23 + 8 = 31"` | x + 8 = 23 (MCQ A=15, B=31) | **A** | ❌ correct (P1-1) | ✅ **wrong** |
| 1862 | `"D, x = 42 because thats the starting number"` | 42 − x = 17 (MCQ B=25, D=42) | **B** | ❌ correct (P1-2) | ✅ **wrong** |
| 1868 | `"D because 4 times 32 = 128"` | 4x = 32 (MCQ A=8, D=128) | **A** | (not in run-10) | ✅ **wrong** |

Three wrong-graded-correct trap-distractor cases tested; three caught.

### One borderline observation — empty `confirm_and_advance` at T1874

After T1873's correct answer (`"20-6 = 14 so True"`), the engine selected `confirm_and_advance` with `verdict=correct` but emitted an **empty response body** (`message: ""`). The next student turn (`"ready for the next one"`) recovered via `close_topic` + exit-ticket dispatch, so the student wasn't stranded. This is the same empty-content failure documented in `MATHS-S1-evaluation-2026-05-27-run9.md` §1 P1-4, and the conformance-gate that previously caught it was deleted in the prune. **Not a regression introduced by run-10 fixes** — pre-existing. Classified as borderline P1 (one occurrence, recovered next turn).

---

## 2. Full transcript

| Turn (id) | Role | Content (truncated) | Move | Verdict | Note |
|-----------|------|---------------------|------|---------|------|
| 1847 | tutor | "Welcome to **Solve One-Step Equations**! … balance scale … Which part of this feels clearest?" | `explain` | n/a | Rule 5 (opening). Ends with open-ended reflective prompt (no canonical) — matches the updated `EXPLAIN` checklist. |
| 1848 | student | "i think you add 18 to both sides so x = 21" | — | — | Wrong reasoning + wrong arithmetic. |
| 1849 | tutor | "Those numbers (18 and 21) aren't from our coconut problem … 25x = 125. **Subgoal 1** … **Subgoal 2** … **Subgoal 3** …" | `worked_example` | wrong (implicit) | Labelled subgoals on a different problem (25x=125), each subgoal is ONE step, final subgoal POSES the inference — `WORKED_EXAMPLE` checklist satisfied. |
| 1850 | student | "umm i dont know how to divide, can you ask me an easier one?" | — | — | Help request. |
| 1851 | tutor | "Division is just asking … Subgoal 1 (2x = 6) … Subgoal 2 … Subgoal 3 … What does 18 ÷ 3 give you?" | `worked_example` | — | Help-request honored: method delivered in labelled steps, returns to open question. |
| (system) | inject | `POST /difficulty-signal/ {"signal": "too_hard"}` → `difficulty_level: -1` | — | — | Runtime signal recorded. |
| 1852 | (synthetic) | "This is too hard for me — could you go simpler?" | — | — | System-event turn. |
| 1853 | tutor | "Totally fair — let's strip it right back. **Subgoal 1** What does 3x = 18 mean? … **Subgoal 4 — Your turn:** what is 18 ÷ 3? **You are distributing 60 copies …**" | `worked_example` | — | **Two-question stacking**: prose practice prompt ("what is 18 ÷ 3?") AND a tool-posed new question (60 leaflets / 5x = 60). Same shape as run-10 P1-3 — the new SCAFFOLD_HINT / WORKED_EXAMPLE checklists didn't fully block it here. Borderline (the new pose is on-topic and the student CAN answer it). |
| 1854 | student | "65 leaflets each because 60 + 5 = 65" | — | — | Wrong (added instead of divided). |
| 1855 | tutor | "The slip here is adding 5 to 60 instead of dividing — but the equation 5x = 60 is asking you to *share* 60 equally into 5 groups … So what is 60 ÷ 5?" | `name_misconception` | wrong | Specific misconception named (add-instead-of-divide). `NAME_MISCONCEPTION` checklist satisfied. |
| 1856 | student | "60 divided by 5 is 11" | — | — | Wrong arithmetic. |
| 1857 | tutor | "The division step is right — 60 ÷ 5 is the move you need — but the arithmetic is a little off. Try counting up in 5s … 5 × ? = 60." | `scaffold_hint` | wrong | Credits the partial (division step correct), drops to a prerequisite (count up in 5s). |
| 1858 | student | "12 because 5 times 12 is 60" | — | — | **Correct.** |
| 1859 | tutor | "Solve the equation x + 8 = 23. … A) x = 15 / B) x = 31 / C) x = 8 / D) x = 23" | `confirm_and_advance` | n/a | Pose-tool MCQ, options not duplicated in prose — `SHARED_PREAMBLE` dedup checklist satisfied. |
| 1860 | student | **`"B because 23 + 8 = 31"`** ← run-10 P1-1 retest | — | — | Wrong (added instead of subtracted). |
| 1861 | tutor | "The slip is adding 8 to 23 instead of doing the opposite — but x + 8 = 23 means 8 has been *added* to x, so the inverse is to subtract … A fisherman in Seychelles starts with 42 fish …" | `name_misconception` | **wrong** ✓ | **P1-1 FIXED.** The letter-disagreement guard fired (student typed "B", canonical "A"); verdict flipped CORRECT → WRONG. Two-question stacking still present (misconception body + new tool-posed bank slot). |
| 1862 | student | **`"D, x = 42 because thats the starting number"`** ← run-10 P1-2 retest | — | — | Wrong (trap distractor — picks the starting number). |
| 1863 | tutor | "The slip is picking the starting number (42) as the answer instead of working out how many fish were actually used. The equation 42 − x = 17 is asking: '42 take away *what number* leaves 17?' … x = 42 − 17. What is 42 − 17?" | `name_misconception` | **wrong** ✓ | **P1-2 FIXED.** Letter-disagreement guard fired (student typed "D", canonical "B"). Specific misconception named. |
| 1864 | student | "42 - 17 = 35" | — | — | Wrong arithmetic. |
| 1865 | tutor | "The subtraction step is the right move … Try counting up from 17 to 42 … What is 42 − 17?" | `scaffold_hint` | wrong | Credits the subtraction move; drops to a counting-up scaffold. |
| 1866 | student | "oh ok 25 then" | — | — | **Correct.** |
| 1867 | tutor | "Solve the equation 4x = 32. … A) x = 8 / B) x = 36 / C) x = 28 / D) x = 128" | `confirm_and_advance` | n/a | Pose-tool MCQ; options not duplicated in prose. |
| 1868 | student | `"D because 4 times 32 = 128"` ← new trap retest | — | — | Wrong (multiplied instead of divided). |
| (1869) | tutor | (transient router-reason length validation error — pre-existing, unrelated to my fixes — retry recovered) | — | — | See §3 below. |
| 1870 | tutor | "The slip here is multiplying 4 by 32 instead of dividing — but 4x = 32 means '4 times something equals 32,' so you need to *undo* that multiplication by dividing both sides by 4. … 32 ÷ 4 = ? A student solved the equation x − 6 = 14 and got x = 20. Verify whether this solution is correct … (True or False?)" | `name_misconception` | **wrong** ✓ | Letter-disagreement guard catches it; specific misconception named. |
| 1871 | student | "False, idk how to check" | — | — | Wrong + help signal. |
| 1872 | tutor | "Here's how to verify a solution — it's a three-step check: **Subgoal 1** … **Subgoal 2** … **Subgoal 3** … So — what does 20 − 6 equal, and does that match 14?" | `worked_example` | — | Help-request handled with labelled subgoals; final subgoal POSES the inference — checklist satisfied. |
| 1873 | student | "20-6 = 14 so True" | — | — | **Correct.** |
| 1874 | tutor | `""` ← **empty message** | `confirm_and_advance` | correct | **Borderline P1**: empty body. Next turn recovers. |
| 1875 | student | "ready for the next one" | — | — | Forward signal. |
| 1876 | tutor | "You nailed the verification — substituting x = 20 back in and confirming both sides equal 14 is exactly the check. **Let's move on to the next part of the lesson.**" | `close_topic` | correct | Phase=`completed`, `show_exit_ticket=true`. Affirmation is specific (`CLOSE_TOPIC` checklist). **Cosmetic mismatch**: said "next part of the lesson" while exit ticket modal fires — see §2.2. |

Final state: `objective_progress: {wrong: 3, correct: 3, partial: 0, attempts: 6}`, `difficulty_level: -1`, `delivered_step_ids: [9835, 9839, 9836, 9837, 9841, 9840]`. **Exit ticket dispatched.**

### 2.1 What changed vs run-10

| Failure | Run-10 | Run-11 | Mechanism |
|---|---|---|---|
| P1-1 "B because 23+8=31" | graded correct | **graded wrong** | Math Fix 1c letter-disagreement guard fires. |
| P1-2 "D x=42 starting number" | graded correct | **graded wrong** | Same guard. |
| New trap: "D because 4×32=128" | n/a | **graded wrong** | Same guard. |
| Difficulty signal not feeding tool | level dropped to -1 but next question was 2-part composite | level dropped to -1; subsequent items were single-part (5x = 60, x + 8 = 23, 4x = 32) — `difficulty_hint="easier"` flowed into the tool | Math Fix 2 plumbing. |
| Stacked question on `scaffold_hint` | 1 stacking case (T1792 in run-10) | Stacking happened on `worked_example`/`name_misconception` turns (T1853, T1861, T1863, T1870) — the LLM authored a prose practice prompt AND the engine posed a fresh slot via the tool | **Not fully fixed.** SCAFFOLD_HINT checklist is in place; the checklist needs to apply to other moves that post both a diagnostic and a fresh pose. See §3 R1. |
| Worked-example answer leak in subgoal 1 | T1788 collapsed subgoals and stated `x = 5` inside Subgoal 1 | T1849, T1851, T1853, T1872 each used clean labelled subgoals with the final subgoal POSING the inference, not stating it | Checklist working as intended. |

### 2.2 New observation — `lesson_complete_signal` plumbing edge case

T1876's close_topic body said *"Let's move on to the next part of the lesson"* (i.e. the prompt read `lesson_complete_signal: false`) but the API returned `phase: completed, show_exit_ticket: true`. The mismatch comes from the gap between two completion paths:

- The prompt's `lesson_complete_signal` is currently derived from `context.is_final_step` (a step-index check).
- The engine's `is_lesson_complete` (which actually fires the exit ticket) is computed from BOTH `is_final_step` AND `_assessable_slots_remaining == 0 AND open_question is None`.

When all assessable slots on intermediate steps are exhausted (instruction-only steps remain), `_assessable_slots_remaining == 0` triggers the exit ticket, but the prompt's `lesson_complete_signal` is still `false`. The LLM picks "next part of the lesson", the engine fires the exit ticket. The student sees the transition phrase then the exit-ticket modal — mild dissonance, no functional break.

**Fix**: thread `assessable_slots_remaining == 0` into the `lesson_complete_signal` computation in `_render_objective_block`. One-line change.

---

## 3. Router / prompt review

### Recommendations

#### R1 — Extend the no-stacking checklist to `name_misconception` and `worked_example`

The `scaffold_hint` body now has the no-stacking checklist (run-10 fix). But this run shows the same failure mode firing on `name_misconception` (T1861, T1863, T1870) and `worked_example` (T1853): the move body prose contains a diagnostic ("what is 60 ÷ 5?"), and the engine ALSO posts a fresh bank slot via the tool. Two questions land in the same turn.

The shape of the fix: extend the existing checklist clause to those moves' bodies, OR move the rule into `SHARED_PREAMBLE` so all moves see it. The simplest path:

```
(add to SHARED_PREAMBLE, after "One question per turn — always" block)

Mid-move pose dedup — when your move body authors a prose practice
prompt or diagnostic AND the engine has called the pose_question
tool in the same turn:
- Drop the prose practice prompt. The tool's emitted stem IS the
  practice prompt for this turn.
- A prose "what is X ÷ Y?" plus a tool-posed bank slot = two
  questions in the same turn. Keep only the tool-posed slot.
- If you cannot ship the prose diagnostic without the tool pose,
  cut the prose diagnostic. The student answers the tool's stem.
```

#### R2 — Fix `lesson_complete_signal` to honor `assessable_slots_remaining`

Engine-side change in `apps/tutoring/v2/services/student_tutor.py::_render_objective_block`. Currently:

```python
lesson_complete_signal = "true" if context.is_final_step else "false"
```

Update to also fire on slot exhaustion:

```python
lesson_complete_signal = (
    "true" if context.is_final_step
    else "true" if getattr(context, "assessable_slots_remaining", -1) == 0
    else "false"
)
```

This requires plumbing `assessable_slots_remaining` into `TutoringContext` (already computed by the engine; just thread it through).

#### R3 — Address the empty `confirm_and_advance` regression

T1874 emitted `""` as the entire body on a CORRECT verdict. The router fired `confirm_and_advance` (pose-dominant move). The slot selector probably returned `exhausted=True` for the bank lookup but the `_apply_pose_dominant_floor` did NOT override to `close_topic` (otherwise the move history would show that override). Possible cause: `confirm_and_advance` was applied but the tool call returned no stem AND the LLM emitted no prose lead-in. Investigate — this is the same shape as the run-9 P1-4 cluster.

#### R4 — Router `reason` length validation error (pre-existing, observed twice this run)

The router LLM returned `reason` > 400 chars two consecutive times, both triggering `RouterDecision` validation failure and a retry. Eventually the engine emitted the safe-template fallback ("Something went wrong on my end. Let's try that again."). User-visible failure. Either:
- Raise the `reason` cap (the contract says ≤ 400 chars; the router LLM consistently overshoots when the routing decision involves multiple counter values).
- Truncate the `reason` field in `MoveRouter.route` before validation.

Not introduced by my changes; surfaced in this run.

---

## 4. Summary

| Dimension | Verdict |
|---|---|
| P1 errors | **0 hard P1 hits.** Both run-10 trap-distractor P1s (P1-1, P1-2) now grade WRONG. A new trap test ("D 4×32=128") also caught. Math Fix 1c + LLM-B checklist working as intended. 1 borderline P1 (empty confirm_and_advance at T1874) is pre-existing, not introduced. |
| Science-of-learning adherence | **Strong.** Direct Instruction (worked example with labelled subgoals on a similar problem, then return to open question). Targeted Remediation (rung-drop from "60÷5" to "count up in 5s"). Active Learning (every turn except T1874 ended with a student action). Subskill stickiness held on all `scaffold_hint` turns. |
| Difficulty signal | Worked. `difficulty_level: -1` after `too_hard`; subsequent items were single-part rather than the multi-part composites observed in run-10. Math Fix 2 plumbing verified. |
| Mastery close floor (I-2) | Not directly exercised on this struggling-student session (close fired only at lesson completion). GEO run-11 exercised it — see paired report. |
| Worked-example answer leak | Fixed across 4 worked_example turns this run. Checklist working. |
| Stacked questions | **Still occurring** on `name_misconception` and `worked_example` (not just `scaffold_hint`). See R1. |
| Exit ticket | **Dispatched correctly.** Cosmetic phrasing mismatch — see R2. |
| Pre-existing issues observed | (a) Empty `confirm_and_advance` body at T1874 (R3); (b) router `reason` > 400 chars validation error twice this run (R4). |

Refs: `test-reports/MATHS-S1-evaluation-2026-05-28-run10.md`, commit `e68263f`.
