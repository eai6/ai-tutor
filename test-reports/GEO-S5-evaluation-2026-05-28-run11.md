# GEO-S5 Tutor Evaluation — 2026-05-28 (run 11)

**Persona**: S5 (Cycle 5) advanced geography student, Seychelles. Rich, mostly correct answers naming mechanism + cause.
**Engine**: v2 (`refactor/conversational-tutor-redesign` @ HEAD `e68263f` — *post-run-10 fix bundle*).
**Session**: `TutorSession.id=120`, lesson `1454` — *Infiltration and Percolation* (Belonie Geography S3, Hydrology and River Systems unit). 3 student turns including a `too_easy` UI signal injection. **Reached exit ticket.**
**Student account**: `student1` / Anse Boileau (S3); persona played at S5 register.
**Evaluator**: Roy Manzi (claude-opus-4-7).

**Purpose of this run**: validate the run-10 fix bundle on the geography side. Specifically retest: (a) `close_topic` body posing a fresh question that gets abandoned (run-10 P1-1), (b) exit-ticket promise without delivery (run-10 P1-3), (c) Mastery I-2 close floor enforcement.

---

## 1. P1 unacceptable errors

| # | Category | Hits |
|---|----------|------|
| Tutor says student's correct answer is wrong | **0** |
| Tutor says student's wrong answer is correct | **0** |
| Posing incomplete questions (missing crucial info needed to answer) | **0** |

**Bottom line**: **0 P1 hits** across 3 student turns. The close_topic body did NOT pose a trailing question (run-10 P1-1 fixed). The exit-ticket modal dispatched correctly (run-10 P1-3 fixed). No duplicated MCQ option blocks (run-10 P1-2 fixed). All three GEO P1s from run-10 are resolved.

### Run-10 P1 retest results

| Run-10 P1 | Mechanism | Run-11 outcome |
|---|---|---|
| **P1-1**: close_topic posed "Now flip it: which of those four regions…?" → student answer abandoned | Prose checklist + (planned) post-process sanitizer; we shipped only the prompt-side checklist this round | ✅ **Pass** — T1885 closed with *"Let's move on to the next part of the lesson"* and **zero `?` in the body** |
| **P1-2**: MCQ options A/B/C/D printed twice (tool emit + prose restatement) | SHARED_PREAMBLE "Tool-vs-prose dedup" checklist | ✅ **Pass** — T1879, T1881, T1883 each emitted one clean option block via the tool with no prose option restatement |
| **P1-3**: "exit ticket — I'll set it up" promised but never delivered | CLOSE_TOPIC checklist gating on `lesson_complete_signal` | ✅ **Pass** — exit ticket dispatched (`show_exit_ticket: true`). One minor cosmetic mismatch documented in §2.2 |

---

## 2. Full transcript

| Turn (id) | Role | Content (truncated) | Move | Verdict | Note |
|-----------|------|---------------------|------|---------|------|
| 1877 | tutor | "Today's lesson is **Infiltration and Percolation** — two processes that explain how rainwater travels from the surface down into the ground. … which of these matches your intuition?" | `explain` | n/a | Rule 5 (opening). Open-ended reflective prompt (no canonical) — matches `EXPLAIN` checklist. |
| 1878 | student | "Infiltration is the surface-water entry into the unsaturated zone, controlled by soil porosity, antecedent moisture, and slope. Percolation is the gravity-driven downward flux through the unsaturated zone toward the water table — its rate is dominated by hydraulic conductivity and capillary forces above the saturated zone." | — | — | Rich answer naming mechanism + cause. |
| 1879 | tutor | "Heavy monsoon rains fall on a Seychelles hillside with mixed sandy and clay layers. Water infiltrates quickly through the sandy topsoil and begins to percolate downward. However, at 2 metres depth, it hits a dense clay layer. Which of the following best explains what happens to the water when it encounters the clay layer during percolation? A) The water stops moving and accumulates above the clay layer, creating a shallow perched water table / B) The water instantly reaches the main groundwater table beneath the clay / C) The water percolates through the clay at the same speed as through the sand" | `confirm_and_advance` | n/a | Pose-tool MCQ. **No options duplicated in prose** — `SHARED_PREAMBLE` dedup checklist satisfied (compare run-10 T1840/T1842 which printed options twice). |
| (system) | inject | `POST /difficulty-signal/ {"signal": "too_easy"}` → `difficulty_level: 1` | — | — | Runtime signal recorded. |
| 1880 | (synthetic) | "This is too easy — could you make it more challenging?" | — | — | System-event turn. |
| 1881 | tutor | "A farmer in Seychelles tests infiltration on three plots of land … Rank these soil types from fastest to slowest infiltration rate, and explain why the ranking makes sense in terms of particle size and porosity." | `confirm_and_advance` | n/a | **Difficulty plumbing verified.** After `difficulty_level: 1`, the slot selector returned the next harder bank slot (a rank-and-justify free-response vs the prior MCQ). Math Fix 2 working on the geo side too. |
| 1882 | student | "Fastest to slowest: sandy (Plot 1) > loamy (Plot 2) > clay (Plot 3). Sand has large particles with large interconnected pores giving high hydraulic conductivity. Loam is a mixture with moderate pore size. Clay has tiny flat particles that pack tightly with very low effective porosity and very high capillary suction, so water moves through it orders of magnitude slower." | — | — | Rich, named mechanism. |
| 1883 | tutor | "Two soil samples are placed in separate containers. Sample A is sandy loam … Sample B is clay … Why does Sample A allow faster infiltration and percolation than Sample B? A) Sandy loam has larger pores between soil particles, allowing water to enter and move through more easily / B) Clay particles are heavier, so they absorb more water than sandy loam / C) Sandy loam is always on steep slopes, while clay is found on flat land / D) Clay contains more organic matter, which prevents [...]" | `confirm_and_extend` | **correct** | Rule 7 correct + rich → `confirm_and_extend` (matches router invariant — `unscaffolded_correct_on_objective` was 1 at this point, `richness=rich`). The follow-up RAISES the rigor (discrimination pair with three plausible distractors). `CONFIRM_AND_EXTEND` checklist satisfied. |
| 1884 | student | "A — sandy loam has larger pores between particles giving lower capillary suction and higher saturated hydraulic conductivity. B confuses mass with absorption; C and D are unfounded generalisations." | — | — | **Correct.** Rich. |
| 1885 | tutor | "**You nailed the pore-size reasoning — larger pores, lower capillary suction, higher hydraulic conductivity — and you correctly dismissed the distractors too.** Let's move on to the next part of the lesson." | `close_topic` | **correct** | Phase=`completed`, `show_exit_ticket=true`. **All CLOSE_TOPIC checklist items satisfied**: zero `?` in body, last sentence is a transition, affirmation is SPECIFIC ("pore-size reasoning — larger pores, lower capillary suction, higher hydraulic conductivity"). |

Final state: `move_history: ['explain', 'confirm_and_advance', 'confirm_and_advance', 'confirm_and_extend', 'close_topic']`, `recent_verdicts: ['correct', 'correct']`, `unscaffolded_correct_on_open_question_objective: 2`, `difficulty_level: 1`, `delivered_step_ids: [13995, 13994, 13993]`. **Exit ticket dispatched.**

### 2.1 Mastery I-2 close floor

The router's Rule 7 close-via-correct path is gated on `unscaffolded_correct_on_open_question_objective >= 1` (invariant I-2). In this session:

- T1882 (1st correct): `unscaffolded_correct_on_objective` was 0 at routing time → close NOT allowed via correct branch → router picked `confirm_and_advance`.
- T1884 (2nd correct, this becomes the 2nd unscaffolded correct): `unscaffolded_correct_on_objective` was 1 (incremented by T1882's correct via `_apply_open_question_counter_updates`) → close allowed → router picked `close_topic`.

The router LLM honored I-2 this run; the deterministic `_apply_mastery_close_floor` did NOT fire (no override emitted). This is the **desired** outcome: the floor is a backstop, not the primary mechanism. Floor coverage is verified by unit tests (`test_run10_fixes.py::test_mastery_close_floor_overrides_on_first_correct`); production traffic simply didn't need the override this turn.

### 2.2 Minor cosmetic mismatch — `lesson_complete_signal` plumbing edge case

T1885's body says *"Let's move on to the next part of the lesson"* — i.e. the LLM read `lesson_complete_signal: false`. The engine simultaneously returned `phase=completed, show_exit_ticket=true`. The mismatch:

- The LLM-facing signal is derived from `context.is_final_step`, a step-index check. Lesson 1454 has 5 LessonSteps; only 3 were delivered → step 4 still exists → `is_final_step=False` → signal=`false`.
- The engine's `is_lesson_complete` is computed from `_assessable_slots_remaining == 0 AND open_question is None`. The 2 undelivered steps had empty `question` fields, so `assessable_slots_remaining == 0` → exit ticket fires.

Student sees "next part of the lesson" THEN the exit-ticket modal opens. Mild dissonance, no functional break, NOT a P1.

Documented as **R1** in the recommendations below; same finding as MATHS-S1 run-11 §2.2.

---

## 3. Move-prompt review — recommendations

### R1 — Extend `lesson_complete_signal` to honor slot exhaustion

`_render_objective_block` currently sets `lesson_complete_signal` from `is_final_step` alone. The engine's exit-ticket logic also fires on `assessable_slots_remaining == 0`. Closing the gap requires plumbing `assessable_slots_remaining` (already computed by the engine on every turn) into `TutoringContext` and OR-ing it into the signal:

```python
lesson_complete_signal = (
    "true"
    if context.is_final_step
    or getattr(context, "assessable_slots_remaining", -1) == 0
    else "false"
)
```

This eliminates the T1885 cosmetic mismatch and keeps the LLM's transition phrase aligned with what the engine actually does.

### R2 — `CONFIRM_AND_EXTEND` worked well; preserve the lifted ≤8-word affirmation rule

T1885's affirmation ("You nailed the pore-size reasoning — larger pores, lower capillary suction, higher hydraulic conductivity") is technically > 8 words but it cites THREE specific terms the student named (pore-size reasoning, capillary suction, hydraulic conductivity) — exactly what `CONFIRM_AND_EXTEND` asks for. The "≤ 8 words" rule in the checklist may be too tight for richly-named answers; consider relaxing to "ONE clause naming one OR more substantive terms the student used" while keeping the "no mechanism re-derivation" guard. No urgent fix; flagged for future tuning.

### R3 — Cross-session learning: extend "no mid-move pose dedup" rule to all moves

The MATHS run-11 paired report (§3 R1) shows that `name_misconception` and `worked_example` can still stack a prose practice prompt + a tool-posed bank slot in the same turn. The fix belongs in `SHARED_PREAMBLE` so it covers all moves. Quoting the MATHS R1 verbatim:

```
Mid-move pose dedup — when your move body authors a prose practice
prompt or diagnostic AND the engine has called the pose_question
tool in the same turn:
- Drop the prose practice prompt. The tool's emitted stem IS the
  practice prompt for this turn.
- A prose "what is X ÷ Y?" plus a tool-posed bank slot = two
  questions in the same turn. Keep only the tool-posed slot.
```

The geo session didn't surface this case (only one tutor move per student turn here), but the same code path affects geo lessons with multi-step bank items.

---

## 4. Summary

| Dimension | Verdict |
|---|---|
| P1 errors | **0 hits across 3 student turns.** All three run-10 GEO P1s resolved:<br/>• Run-10 P1-1 (close_topic trailing question): close body had zero `?` and ended with a clean transition.<br/>• Run-10 P1-2 (duplicated options): tool-posed MCQs emitted ONE clean option block.<br/>• Run-10 P1-3 (exit-ticket no-show): exit ticket dispatched correctly. |
| Science-of-learning adherence | **Strong.** Direct Instruction opener anchored to lesson title. Testing Effect substantive-term affirmation ("pore-size reasoning, capillary suction, hydraulic conductivity"). Subject anchoring (Seychelles hillsides, Morne Seychellois). The `too_easy` signal raised rigor as expected (free-response rank-and-justify vs prior MCQ). |
| Mastery I-2 close floor | Floor was NOT exercised (router LLM honored I-2 organically). Unit tests cover the floor's catch behavior. Production traffic shows the router prompt's invariant is being respected this run. |
| Difficulty signal | Worked. `difficulty_level: 1` → next bank slot picked was harder. Math Fix 2 verified on the geo side too. |
| `close_topic` checklist | Satisfied on T1885. No trailing question, transition statement, specific affirmation. |
| MCQ option dedup checklist | Satisfied on T1879, T1881, T1883. Tool-emitted options not duplicated in prose. |
| Edge case observed | `lesson_complete_signal` doesn't fire on slot exhaustion when `is_final_step=False`. Cosmetic prompt/engine mismatch (R1). |
| Exit ticket reached | **Yes.** `phase: completed`, `show_exit_ticket: true`, modal dispatched. |

Refs: `test-reports/GEO-S5-evaluation-2026-05-28-run10.md`, `test-reports/MATHS-S1-evaluation-2026-05-28-run11.md`, commit `e68263f`.
