# MATHS-S1 Tutor Evaluation — 2026-05-27 (run 9)

**Persona**: S1 (Cycle 4) advanced mathematics student, Seychelles. Consistently provided full-working correct answers.
**Engine**: v2 (`apps/tutoring/v2/`, PR#12 cutover).
**Session**: `TutorSession.id=101`, lesson `1177` — *Use Pythagoras' theorem to prove whether a triangle is a right-angled triangle*.
**Student account**: `student1` / Anse Boileau / S3 (only S3 courses seeded; played at advanced register).
**Turns observed**: 14 student turns (turn ids 1567–1595), exit ticket never fired.
**Evaluator**: Roy Manzi.

---

## 1. P1 unacceptable errors

The grader misclassified **7 of 8** fully-worked-out correct answers as `wrong` or `partial`. The tutor's *prose* repeatedly conceded the student's working was right ("Your working is spot on", "Your arithmetic is genuinely correct"), but the closed move set then sent the turn into scaffold/misconception/pivot territory anyway because the verdict had already been written. From the student's seat this reads as the tutor admitting the answer is correct, then demanding more work.

| # | Turn (tutor) | Reply to (student) | Grader verdict | What the student wrote | Category |
|---|---|---|---|---|---|
| **P1-1** | 1571 | 1570 | **`wrong`** | "c=13. 5²+12² = 25+144 = 169. 13² = 169. Since 169=169, the triangle IS right-angled." Canonical, full method. | Tutor says correct answer is wrong (grader). |
| **P1-2** | 1573 | 1572 | **`partial`** | "c² = 9²+40² = 81+1600 = 1681. c = √1681 = 41 m. Check: 9²+40² = 1681 = 41² ✓." All slots, fully verified. | Tutor says correct answer is partial. |
| **P1-3** | 1575 | 1574 | **`wrong`** | MCQ pick "B (6,8,10)" with full discriminating working ("A: 41≠36 fails, C: 113≠81 fails, B: 36+64=100=10² works"). B is canonical. | Tutor says correct answer is wrong. |
| **P1-4** | 1577 | 1576 | **`wrong`** | "A: a²+b² = 36+64 = 100 and c² = 100, so yes." Option A literally reads "a²+b²=100, c²=100, yes". Exact match. | Tutor says correct answer is wrong; fabricates a non-existent "option letter mismatch". |
| **P1-5** | 1579 | 1578 | **`wrong`** | Student doubled down — "Option A is exactly … A matches my computation." Tutor responded with `pivot`, abandoning the question without crediting. | Same as P1-4 + silent abandonment. |
| **P1-6** | 1581 | 1580 | **`wrong`** | "B. 8²+15² = 64+225 = 289 = 17², so right-angled." Canonical. | Tutor says correct answer is wrong; asks student to recompute 17×17 as if 289 was suspect. |
| **P1-7** | 1583 | 1582 | **`wrong`** | "17×17 = 289 (10·17 + 7·17 = 170+119 = 289). 8²+15² = 289. So 8²+15² = 17². Answer B." Even more explicit. | Tutor says correct answer is wrong; in the reply leaks the word "grader" ("the grader is checking the *exact wording* of each option…"). System-vocabulary leak + correctness denial. |

Only **turn 1584** ("True. 7²+24² = 49+576 = 625 and 25² = 625") was finally graded `correct`. At that point the safety-valve fired (`turns_in_session: 15` per `runtime_state.safety_valve_counters`), forcing `close_topic`, and the session deadlocked: turns 1591/1593/1595 all returned `close_topic` with `show_exit_ticket: false` and no exit-ticket payload. Two of those turns (1589 and at the very end) returned **empty tutor messages**.

**Bottom line**: 7 P1 grader errors on a textbook-perfect transcript + 1 system-vocabulary leak + 3-turn `close_topic` dead-end with no exit ticket. Exit ticket was never reached.

### Direct continuity with prior diagnoses

The same fingerprint as `test-reports/DIAGNOSIS-regression-2026-05-27.md` and run 8: `runtime_state.open_question` stays `None` through every turn even after the tutor poses questions, so the grader has nothing to grade against. In run 9 the trace went one step further — the LLM-routed tutor wrote *prose* that affirmed the working but the closed verdict→move table then steered the turn into a scaffold / misconception / pivot reply. The `STUDENT_RESPONSE_SYSTEM` / `MATH_DSL_SYSTEM` grader contract is silently degrading every full-working correct answer to `wrong`.

---

## 2. Science-of-learning assessment

Mapped to `design/science-principles.md`.

### Working

| Principle | Evidence |
|---|---|
| Voice & framing | Seychelles-anchored framing (Mahé, boat ramp at Victoria fishing port) used in turn 1567. No system vocabulary in the *opening* turn. |
| Dual coding cue | Memory aid "along the corridor, then up the stairs" (geo session, but same engine voice) is the right cognitive-load shape for a procedural rule. |
| Cognitive Load on the *first* turn | Turn 1567 opens with a 3-sentence rule + a single ask — clean one-idea-per-turn opener. |

### Not working

| Principle | Failure |
|---|---|
| **#1 Active Learning (Ch.10) — feedback must be informative.** | The most expensive failure of the run. Seven consecutive correct answers got `wrong` / `partial` feedback. The student is told they are wrong by the *system action* (next-question pivot, scaffold) while the *prose* concedes they are right. This is anti-feedback — strictly worse than absent feedback because it actively miscalibrates the student's self-assessment. The Math Academy Way explicitly names this as the textbook pathology to avoid. |
| **#3 Deliberate Practice (Ch.12).** | An advanced student who has already nailed 5-12-13, 9-40-41 hypotenuse, and the discrimination set deserves a *parameter twist* via `confirm_and_extend` — different surface, same rigor. The router did select `confirm_and_extend` once (after turn 1572 internally — see turn 1573's "Let's push that further with a trickier set of sides") but then the grader verdict-flip cancelled it. Net deliberate-practice output for the run: **zero new problems beyond the level of the opener**. |
| **#4 Mastery Learning (Ch.13).** | `close_topic` fired on `safety_valve_counters.turns_in_session = 15` — i.e. forced close. The objective progress was `{wrong: 6, correct: 1, partial: 1}` after the run, but **every single "wrong" was a graded P1**. The runtime closed on a saturated-difficulty signal that does not correspond to actual evidence. The CLOSE_TOPIC prompt's "forced close" branch (`apps/tutoring/v2/services/move_prompts.py:806-815`) correctly avoids praise, but the message it picked still uses "We've spent a solid stretch" — which on the actual transcript is dishonest in the opposite direction: the student wasn't stuck, the grader was. |
| **#5 Minimise Cognitive Load — expertise reversal.** | Turns 1573 ("let me ask you to state the conclusion one more time"), 1581 ("let's check that last step carefully … compute 17 × 17"), 1583 ("the grader is checking the *exact wording* of each option") all *increase* scaffolding on a student who has demonstrated mastery. Strict violation of the expertise-reversal effect. |
| **#11 Testing Effect.** | Retrieval was attempted but every successful retrieval was treated as a failure. Spaced retrieval practice depends on the system recognising the retrieval — when 7 of 8 retrievals are scored as misses, the principle inverts: the student is taught to distrust their own memory. |
| **One question per turn.** | Turn 1573 stacked two questions: (a) "what does the fact that 9² + 40² = 41² tell you about this triangle?" + (b) the discrimination MCQ. Conformance let this through. The cognitive-load floor is loose on `confirm_and_extend`. |
| **System-vocabulary leak.** | Turn 1583: "Here's the thing: the grader is checking the *exact wording* of each option, not just the number." `grader` is a system-internal word. The CLOSE_TOPIC / SCAFFOLD_HINT prompts explicitly forbid this register but no per-turn extractor catches the word. |
| **Exit-ticket transition.** | Turns 1591, 1593, 1595 all emit `close_topic` with `show_exit_ticket: false`. The CLOSE_TOPIC prompt (`apps/tutoring/v2/services/move_prompts.py:822-826`) already warns "do NOT promise the exit-ticket modal when you can't see whether one exists … use a softer transition" — turn 1591 ignored this and said "I'll set it up". The runtime never sets up anything. This is the same dead-end documented in `MATHS-S1-evaluation-2026-05-27-run8.md` P1-5. |

---

## 3. Router + move-prompt recommendations

These are scoped to the **router and tutor move prompts** that the run touched. The deepest fix (grader) lives outside this section but it is named so the router-level patches are not asked to compensate for a grader bug.

### 3.1 Stop the grader from degrading full-working correct math

This is the root cause of every P1 in the run. The grader's `MATH_DSL` path needs `runtime_state.open_question` populated to grade against, and `open_question` is `None` for every turn in session 101. Two evident upstream paths produce this:

- The `pose_question` tool call from the tutor is not committing to `runtime_state.open_question` even when conformance accepts the turn. The DIAGNOSIS docs from 2026-05-27 already pin this on a two-phase commit + tool-XML-leak interaction. Until that is closed, no router or move-prompt edit will recover.
- Independently, when the math DSL grader can't find a stem, it should fall through to `STUDENT_RESPONSE_SYSTEM` + `STUDENT_CLAIMS_SYSTEM` rather than emitting `wrong`. The current "wrong by default" path is the silent miscalibration.

I am **not** prescribing the grader fix from the router file — but every router-level fix below assumes this is repaired.

### 3.2 Router — add a `correctness_belief_in_prose` cross-check before sending `wrong`-verdict moves

`apps/tutoring/v2/services/router_prompts.py:120-178` routes verdict→move purely on counters. Add a sanity gate: if the router's own `reason` field, or the tutor's drafted prose for the upcoming move, contains explicit-correctness markers ("Your working is spot on", "Your arithmetic is correct", "Both slots are right"), the router should refuse to ship a `scaffold_hint` / `name_misconception` / `pivot` move on this turn — emit a one-line `router.verdict_inconsistency_floor` span and re-route to `confirm_and_advance` with a half-credit framing. Concretely:

> Add a new safety floor in `tutor_engine.pick_move`, after grader → before move execution:
> If `grader.verdict ∈ {wrong, partial}` AND the latest tutor *draft* (from `student_tutor.draft`) opens with one of the canonical "you got it right" / "your arithmetic is correct" markers, **override the verdict to `correct` with `confirm_and_advance`** and log the conflict. The grader and the tutor LLM disagreeing in this direction is *prima facie* a grader bug. A safety floor catches it without waiting for the grader fix.

This is the same shape as the existing 5 safety floors and would have caught all 7 P1s in this run.

### 3.3 Router — kill the "objective_turn_count ≥ 12 AND correct_on_objective = 0" forced close when the conflict floor above has fired ≥ 2 times

`router_prompts.py:136-139` currently fires `close_topic` on raw counters. If the verdict-inconsistency floor above has fired this session, `correct_on_objective` is **not trustworthy** — those `wrong` verdicts are P1s, not real wrongs. Until the run terminates cleanly, gate the forced-close on `correct_on_objective + verdict_inconsistency_overrides ≥ 1`. This stops the deadlock loop at turns 1591/1593/1595.

### 3.4 `CLOSE_TOPIC` — exit-ticket promise must be runtime-grounded

`apps/tutoring/v2/services/move_prompts.py:822-826` warns the LLM not to promise the exit-ticket modal if it can't see one exists. The constraint is the right shape but the prompt has no per-turn cue telling the LLM whether the exit ticket *is* wired in. Pass `exit_ticket_ready: bool` into the close-topic prompt rendering and template-gate the line: only emit "I'll set it up" if `exit_ticket_ready=True`, otherwise fall through to "we'll wrap here for now". On turns 1591/1593 the LLM had no way to know it was lying.

### 3.5 `SCAFFOLD_HINT` and `NAME_MISCONCEPTION` — system-vocabulary deny-list

The `grader` leak in turn 1583 came through `name_misconception` / `pivot` after the LLM started reasoning about its own internal disagreement with the verdict. Add an explicit per-prompt deny-list at the bottom of every move prompt body:

> **System-vocabulary deny-list (hard rule):** Never use the words `grader`, `verdict`, `router`, `move`, `pose_question`, `tool`, `prompt`, `LLM`, `classifier`, `confidence`, `gate`, `floor` in the visible reply. These are internal terms; reaching for them is a structural signal that the move is wrong, not a useful student-facing explanation.

A single tokenized regex in `conformance/check.py` would also catch this without prompt-time pressure, but the prompt-level rule disciplines the upstream draft.

### 3.6 `CONFIRM_AND_EXTEND` — enforce one question per turn

Turn 1573 stacked a state-the-conclusion ask + an MCQ. The CONFIRM_AND_EXTEND prompt (lines 330–385) emphasises "one twist" but does not say "exactly one question". Add the line already present in `WORKED_EXAMPLE` (538-540):

> CRITICAL: End the turn with EXACTLY ONE question. Do NOT also append a state-the-rule prompt. One ask, end of turn.

### 3.7 Router — add explicit `OPENING_TURN` re-classification on advance-without-pose

Turns 1587/1589 routed back to `explain` after the `close_topic` from 1585. The LLM-drafted explain reply ("The next step is about squaring each side length…") on 1587 was correct, but turn 1589 returned an *empty* message and 1591 reverted to `close_topic`. The router classified 1589 as `opening_turn` (`prior_answer_attempts_on_objective = 0` after the objective close), but the engine then immediately re-opened the same objective without resetting `safety_valve_counters.turns_on_current_objective`. Symptom: empty reply on 1589, then immediate forced close.

Fix: when the router emits `case: opening_turn` *and* `safety_valve_counters.turns_on_current_objective ≥ 12`, the engine should treat this as a true objective transition and reset the per-objective counters before the tutor LLM is called. Otherwise the conformance gate kills the explain message on stale counters and the student gets an empty turn.

---

## 4. Engine-level priorities (named, not fixed in this report)

1. **Grader contract repair.** `runtime_state.open_question` is `None` for every turn in this session. Until this is fixed, every advanced-student session repeats this exact P1 cascade. Owner: tutor-engine-expert.
2. **Verdict-inconsistency safety floor** (§3.2) — short-term mitigation while #1 is fixed.
3. **Exit-ticket transition contract** (§3.4) — independently broken; observable in two consecutive run reports.
4. **System-vocabulary regex on conformance** — strict-mode rule, not a prompt-only nudge.

---

## Appendix — full transcript

Captured in `/tmp/math_session.log` and persisted on `SessionTurn.metadata.v2_trace` for `TutorSession.id=101`, turn ids 1567–1595. Per-turn verdict / move / fallback table:

```
1567 [t] move=explain                v=None      :: opening rule + ask
1569 [t] move=explain                v=None      :: pose 5-12-13
1571 [t] move=scaffold_hint          v=wrong     :: P1-1 (correct full-working)
1573 [t] move=scaffold_hint          v=partial   :: P1-2 (correct 9-40-41)
1575 [t] move=scaffold_hint          v=wrong     :: P1-3 (correct discrimination B)
1577 [t] move=name_misconception     v=wrong     :: P1-4 (correct A, fabricated mismatch)
1579 [t] move=pivot                  v=wrong     :: P1-5 (correct A doubled-down; tutor abandoned)
1581 [t] move=pivot                  v=wrong     :: P1-6 (correct 8-15-17)
1583 [t] move=pivot                  v=wrong     :: P1-7 (correct + "grader" leak)
1585 [t] move=close_topic            v=correct   :: 7-24-25 finally graded right; forced close
1587 [t] move=explain                v=None      :: re-opening describe-the-method
1589 [t] move=explain                v=None      :: EMPTY tutor message
1591 [t] move=close_topic            v=None      :: promised exit ticket; show_exit_ticket=false
1593 [t] move=close_topic            v=None      :: repeated promise
1595 [t] move=close_topic            v=None      :: stuck
```
