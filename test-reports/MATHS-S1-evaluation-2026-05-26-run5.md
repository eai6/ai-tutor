# MATHS-S1 Tutor Evaluation — 2026-05-26 (run 5)

**Persona**: S1 (Cycle 4) struggling student in Seychelles. Consistently wrong
answers, mixed with help-requests ("i dont know how to do percentages",
"can you teach me?") and one *partial-correct* answer in the middle.
**Lesson evaluated**: L1167 — *Analyse and solve problems involving cost and
selling price* (Mathematics S3).
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on`).
**Student**: `student1` at School-3.
**Session ID**: 87. 8 student turns T1–T8.

> Comparison anchor: same lesson as run-4 (`MATHS-S1-evaluation-2026-05-26-run4.md`).
> Run-4 used a fresh student account (`mathprobe4`) and reported the per-move
> safety floor improvements were landing. This run re-tested on the production
> codebase post-merge and observed **a different and more severe regression
> pattern**: every tutor turn after T0 hit the fallback safety floor, the
> grader frequently refused to verify gradable answers, and the
> `confirm_and_extend` move was structurally blocked from advancing the
> student.

---

## 1. Transcript

| # | Role | Content | move / verdict / fallback / conformance violations |
|---|------|---------|----------------------------------------------------|
| 0 | tutor | Today we're looking at **analyse and solve problems involving cost and selling price**… *(direct-instruction frame: cost price, selling price, Profit = SP − CP, Loss = CP − SP, coconut 15→22 example)* *(opening Q in prose: "what is the profit the shop makes on each coconut?" — correct = 7 SCR)* | `explain / — / no / —` ⚠ verifiable-answer Q posed in **prose**, not via `pose_question` tool — violates SHARED_PREAMBLE structural rule but conformance let it through |
| 1 | student | is it 37 SCR? | (wrong, expected 7) |
| 1 | tutor | Let me check where you're at with a similar one first. A vendor at the market in Victoria buys breadfruit for 18 SCR each and sells them for 27 SCR each. **Calculate the profit per breadfruit and the profit percentage.** | `pose_question / **None** / no / passed (no violations)` ❌ **P1-RISK** Tutor jumped to a brand-new item without ever telling the student their "37 SCR" was wrong. Grader returned no verdict — the opening prose-question was not registered as an `open_question`, so T1 had nothing gradable. Worse, the new question now bundles **two slots** (profit + profit %) when only profit was taught in T0 (the latter requires the percentage formula which has not been introduced). |
| 2 | student | profit is 45 SCR and profit percentage is 60% | (wrong; correct = 9 SCR profit / 50% profit) |
| 2 | tutor | Quick check on that — let's pin it down together. Looking at the question one more time: A vendor… buys breadfruit for 18 SCR each and sells them for 27 SCR each. Calculate the profit per breadfruit and the profit percentage. **Try just one step of it and I'll guide you from there.** | `scaffold_hint / unverified / **yes** / [no_assessment_in_prose; unverified__no_refute; unverified__surfaces_uncertainty]` ❌ The grader returned `unverified` on an obviously verifiable wrong answer ("45" vs 9, "60%" vs 50%). Conformance then rejected the LLM's first and retry attempts; the verdict-keyed safe-template fired. |
| 3 | student | i dont know how to do percentages | (explicit help-request — Direct Instruction trigger) |
| 3 | tutor | I want to make sure we're on the same page here. Here's the question again, in plain words: A vendor… Calculate the profit per breadfruit and the profit percentage. **Pick one piece of it that feels closest to something you can answer, and try just that part.** | `scaffold_hint / unverified / **yes** / [no_assessment_in_prose; unverified__surfaces_uncertainty]` ❌ **Direct Instruction violation.** The student said "I don't know how to do percentages" — an explicit help-request. The help-request override in `move_selection.detect_help_request` did not trigger; the engine stayed on scaffold_hint. The fallback template refuses to *teach* and instead asks the student to pick a piece of an unknown topic. |
| 4 | student | maybe profit is 27 + 18 = 45 | (wrong; misconception: added instead of subtracted) |
| 4 | tutor | Almost — the value doesn't line up with any of the quantities asked for — check which operation you applied and which slot you're answering. Here's the question again, in plain words: …Pick one piece of it that feels closest to something you can answer, and try just that part. | `name_misconception / wrong / **yes** / [open_question_stickiness: new item 10026 vs open 10025]` ✅ wrong is correctly identified; ⚠ "almost" is overly generous (the student added instead of subtracted, that's not "almost"). |
| 5 | student | i think profit is 27 - 18 = 9 SCR but i dont know the percentage | (**partial correct**: profit = 9 ✓, percentage unknown) |
| 5 | tutor | Let me check that with you before we go further. Here's the question again, in plain words: A vendor… Pick one piece of it that feels closest to something you can answer, and try just that part. | `scaffold_hint / unverified / **yes** / [praise_filter: bare praise opener "yes!"]` ❌ **P1-CLASS**: student gave a CORRECT partial ("27 - 18 = 9 SCR"). The LLM's first draft began with "Yes!" (presumably it was about to confirm the correct part). The praise-filter rejected it under verdict=unverified; the retry also failed; the fallback shipped a template that *refuses to acknowledge the correct subtraction*. Functionally identical to "Tutor says a student's correct answer is wrong." |
| 6 | student | profit is 9 | (correct bare answer) |
| 6 | tutor | Let me walk you through the idea one step at a time:\n\nToday we're going to solve real problems about cost and selling price — the exact calculations shop owners use in Victoria and across Seychelles. Imagine a local shop buys coconuts for 15 SCR each and sells them for 22 SCR. How much profit do they make on each coconut? That's what we're learning today.\n\nNow you try it — apply the same thinking to: A vendor… **Calculate the profit per breadfruit and the profit percentage.** | `worked_example / partial / **yes** / [praise_filter: bare praise opener "spot on"]` ❌ **P1-CLASS**: the bare correct answer "profit is 9" got marked **partial** (the grader is now refusing to confirm the same correct answer the student gave at T5). LLM started "Spot on" → praise_filter rejected → fallback fires the **teacher_script worked-example** terminal. The worked example doesn't teach the *percentage* — the very thing the student is stuck on. The "now you try it" prompt re-bundles profit + profit %. |
| 7 | student | i still dont understand how to find the percentage. can you teach me? | (explicit help-request, second time) |
| 7 | tutor | I want to make sure we're on the same page here. Let's slow down on the same question: A vendor… Calculate the profit per breadfruit and the profit percentage. **Tell me what you'd do first, even if you're not sure about the rest.** | `scaffold_hint / unverified / **yes** / [open_question_stickiness: new item 10026 vs open 10025]` ❌ **Direct Instruction violation, second time**. Same student request, same refusal-to-teach. |
| 8 | student | is the percentage 9 percent? | (wrong — correct = 50%) |
| 8 | tutor | I want to make sure we're on the same page here. Here's the question again, in plain words: A vendor… Pick one piece of it that feels closest to something you can answer, and try just that part. | `scaffold_hint / unverified / **yes** / [open_question_stickiness: new item 10026 vs open 10025]` ❌ Same fallback template, identical phrasing as T3 — fourth identical fallback in 8 turns. Grader marked a clearly-wrong numeric answer as `unverified`. |

---

## 2. P1 Unacceptable Errors

| # | P1 type | Evidence | Severity |
|---|---------|----------|----------|
| **P1-A** | Tutor effectively says correct answer is wrong | **T5/T6**: student gives a correct partial ("27 - 18 = 9 SCR") and a correct bare answer ("profit is 9"). Both turns end with the safety-floor template asking the student to *try again* on the same question. The grader returns `unverified` (T5) and `partial` (T6) on what is unambiguously correct math. | **HIGH** — single-turn confidence loss for a struggling student. |
| **P1-B** | Posing incomplete questions | **T1**: "Calculate the profit per breadfruit **and the profit percentage**." The T0 direct-instruction segment defined Profit / Loss only; the **percentage** concept (% = (profit ÷ cost) × 100) was never introduced. The student is asked to apply a method they have not been taught. Every subsequent re-pose (T2, T3, T4, T5, T7, T8) repeats the same incomplete prompt. | **HIGH** — drives the cascade of unverified/wrong verdicts because the student lacks the prerequisite. |
| **P1-C-adjacent** | Wrong answer not flagged | **T1**: student's "is it 37 SCR?" (wrong, expected 7) gets no verdict and no feedback — tutor advances to the breadfruit problem as if the answer didn't matter. | **MEDIUM** — the answer was never told "wrong"; it was simply skipped. |

No instance of *"Tutor says a wrong answer is correct"* was observed.

---

## 3. Science of Learning Evaluation

| Principle | Working / Failing | Evidence |
|-----------|-------------------|----------|
| **Active Learning** (Ch.10) | ⚠ Partial. Student attempted on 8/8 turns, but **6/8 tutor turns were the same fallback template** — they were not real teaching turns. | T2, T3, T5, T7, T8 all shipped near-identical "Pick one piece…" / "Let's slow down…" templates. |
| **Direct Instruction** (Ch.11) | ❌ **Fundamentally broken in this run.** Two explicit "can you teach me?" requests (T3, T7) were both routed to `scaffold_hint`, not `explain` or `worked_example`. The help-request override either did not fire or was overridden by the open-question state. | T3 student message "i dont know how to do percentages" → tutor: "Pick one piece…" T7 "can you teach me?" → tutor: "Tell me what you'd do first." |
| **Deliberate Practice** (Ch.12) | ❌ The student is *stuck on the wrong rung*: the open question requires profit + profit % when only Profit = SP − CP has been taught. No re-calibration to the student's actual edge of ability happened across 8 turns. | T0 teaches subtraction-only; T1 immediately escalates to subtraction + percentage. |
| **Mastery Learning** (Ch.13) | n/a — no prerequisite gating in this session. |
| **Minimise Cognitive Load** (Ch.14) | ❌ The opening worked example (T0) does NOT label subgoals and ends with a verifiable-answer question posed in prose rather than via the tool. The T6 worked-example fallback re-uses the *teacher_script* but ends asking for the *same* multi-slot stuck problem — adding load instead of reducing it. | T0 ends with a Q in prose; T6 worked example asks for profit % which was never modeled. |
| **Automaticity** (Ch.15) | n/a — no latency tracking; not applicable to single-session quality. |
| **Layering** (Ch.16) | ❌ Profit % requires Profit calculation as a prerequisite. The lesson author put both into one question without scaffolding the prerequisite first. The tutor never decomposed. | The multi-slot pose in T1 stays intact for 8 turns. |
| **Targeted Remediation** (Ch.21) | ❌ The student literally named the slip ("i dont know how to do percentages") and the tutor still did not decompose, did not switch to teaching percentages, and did not offer a profit-only sub-question that did not require percentage. | T3, T7 = clear remediation triggers, ignored. |
| **Testing Effect** (Ch.20) | ⚠ Retrieval is attempted but the underlying item is unanswerable for this student — retrieval without prerequisite knowledge produces noise, not learning. | T1–T8 same multi-slot Q. |

**One-line summary**: the v2 engine kept the student talking but did not teach
them anything they did not already know, and it twice rejected work they did
correctly. The pedagogy floor *worked* (every turn ended with a concrete
action ask) but the *content* floor failed (the action ask repeated an
unanswerable multi-slot question and refused the student's correct subtraction).

---

## 4. Root-Cause Map (engine + conformance)

Two structural issues drive **most** of the failures above. Fixing them is
a higher-leverage change than tweaking the per-move prompts.

### 4.1 Grader returns `unverified` on verifiable numeric wrongs

`T2` ("profit is 45 SCR and profit percentage is 60%" vs canonical 9/50%),
`T3` ("i dont know how to do percentages"), `T5` ("27-18=9 SCR but i don't
know the percentage" — partially correct), `T7` ("can you teach me?"), and
`T8` ("is the percentage 9 percent?" — wrong, expected 50%) all returned
verdict=`unverified`. Only T2 and T8 should be `unverified` in any defensible
reading; T2's 45/60% and T8's 9% are both numerically wrong and gradable.

The grader appears to be treating any answer to the bundled
"profit + profit %" question as `unverified` because **one of the two slots
is unknown to the student**. The math-DSL grader is being applied to the
joint answer when it should be applied per slot.

**Implication for prompts**: even perfect move-prompts can't recover from
`unverified` cascades — the LLM is forced into uncertainty-language and the
praise filter blocks any positive confirmation. The fix is downstream of
`move_prompts.py` (in `apps/tutoring/v2/services/grader.py` or the bank
schema) — but the prompt can defend against this; see §5.

### 4.2 `confirm_and_extend` blocked by `open_question_stickiness` gate

`gates.py:502` lists `confirm_and_extend` as a probe-shaped move that must
stay on the open question. But `confirm_and_extend`'s **explicit contract**
(`move_prompts.py:330` and per-move docstring) is "pose a single follow-up
that varies one parameter (different numbers, different units, an edge case,
a mechanism step, a transfer to a new context) — same concept, slight twist."
That is, by definition, a NEW item.

Every time the engine picks `confirm_and_extend` and the LLM follows the
prompt, the gate fails it. The retry also fails. The terminal template fires
the `Let's slow down on the same question` floor — which contradicts what the
prompt promised and confuses a student who just answered correctly.

This bug fully drives §5 of the GEO-S5 evaluation too (see companion report).
**It is the single highest-leverage fix in this evaluation.**

---

## 5. Recommendations for `apps/tutoring/v2/services/move_prompts.py`

The two structural issues above (§4) cannot be fixed by prompt edits alone.
But the move prompts can be hardened against them, and several second-order
gains are available.

### R1 — `EXPLAIN` (`move_prompts.py:572`): forbid prose-posed assessment questions

**Symptom**: T0 ends with a verifiable-answer Q ("what is the profit the shop
makes on each coconut?") posed in prose. The conformance `no_assessment_in_prose`
gate caught this on later turns but not on the explain-opening. As a result,
the student's wrong answer in T1 was not gradable.

**Edit**: in EXPLAIN's "How (no verdict / opening turn)" block, replace

```
- End with a single prompt that invites the next move — typically
  ``pose_question`` next turn. If the opening question has a
  verifiable answer, pose it via the tool, not as prose.
```

with a stronger directive:

```
- End the turn with EITHER (a) a one-line prose framing prompt that has
  no canonical numeric/factual answer ("what do you think might cause…",
  "what's one example you've seen…"), OR (b) a tool-posed bank question
  via pose_question. NEVER end with a verifiable-answer question typed
  in prose — the grader cannot register it as an open_question, and any
  subsequent student answer to it will return verdict=None. This is the
  single most expensive failure mode for the opening turn.
```

### R2 — `EXPLAIN` + `WORKED_EXAMPLE`: forbid posing prerequisites the student has not been taught

**Symptom**: T1's pose ("Calculate the profit per breadfruit and the profit
**percentage**") asks for a calculation the T0 explain did not introduce.

**Edit**: in EXPLAIN's "How (no verdict / opening turn)" block, add:

```
- The opening question must require ONLY the rule(s) you just named.
  If the lesson-step bank item bundles multiple subskills (e.g. profit
  AND profit %), the opening pose must use a slot that exercises ONLY
  the subskill you've taught. If no such slot is available, close the
  explanation without a pose and let the next turn pose via the tool.
```

### R3 — `CONFIRM_AND_EXTEND` (`move_prompts.py:330`): clarify that the "twist" must reuse the open question's inputs

**Symptom**: the move's prompt says "vary one parameter… same concept, slight
twist" which the LLM (correctly) interprets as a new bank slot. The
`open_question_stickiness` gate then rejects it.

This is primarily a **gate-side bug** (gates.py should remove
`confirm_and_extend` from `probe_moves`). But the prompt can hedge:

```
- The "twist" stays on the SAME open question (same bank item). Vary the
  framing (ask for a comparison, ask the student to explain why the
  answer is what it is, ask for a generalisation, ask for a boundary
  case) — but do NOT post a new bank slot. If you need a new bank slot,
  the correct move is close_topic followed by pose_question on the next
  turn, NOT confirm_and_extend on this turn.
```

This is a workaround. The proper fix is **`gates.py:502` — remove
`confirm_and_extend` from `probe_moves`**.

### R4 — `SCAFFOLD_HINT` UNVERIFIED branch: handle the "I don't understand X" case as a help-request, not a hedge

**Symptom**: T3 ("i dont know how to do percentages") and T7 ("can you teach
me?") were graded `unverified`. `move_selection.detect_help_request` should
have caught these and routed to `worked_example` / `explain`. Since the help
request fired *inside* a wrong/unverified state, the engine stayed on
`scaffold_hint`.

**Edit**: in SCAFFOLD_HINT's UNVERIFIED block, add:

```
- Distinguish two unverified shapes:
  (a) the student gave a hedged or off-topic answer the grader couldn't
      parse → follow the existing "surface uncertainty + diagnostic
      pose" path.
  (b) the student EXPLICITLY says they don't know the topic, can't do
      the operation, or asks you to teach them ("I don't know how to
      do <X>", "can you teach me", "what is <X>"). In this case, do
      NOT pose another retrieval question — TEACH <X> in one short
      paragraph (Direct Instruction Ch.11), then ask one application
      question on <X> alone. The student named the gap; close it.
```

(The cleaner fix is to extend `detect_help_request()` to catch "I don't know
how to do <X>" patterns. But the prompt fix is a useful belt-and-suspenders.)

### R5 — `CONFIRM_AND_ADVANCE` (`move_prompts.py:302`): protect the praise-filter retry from re-rejection

**Symptom**: T5 LLM draft "Yes!" was rejected by `praise_filter` under
verdict=unverified. The retry presumably stayed too close to the original.
The fallback template fires.

The praise filter is correct *as a rule* but is firing because the verdict
itself is wrong (§4.1). The move prompt should make the retry land on
non-praise language deterministically:

**Edit**: at the top of CONFIRM_AND_ADVANCE add:

```
NEVER open with "Yes", "Right", "Great", "Spot on", "Perfect", or any
single-word affirmation. The conformance praise filter will reject the
turn if it can't verify the verdict, and the retry will land in the same
trap. Open with an action-clause: "You've got the subtraction step —
now the question is what fraction of the cost that represents."
```

(This is defensive; the deeper fix is to make praise-filter conditional
on `verdict.confidence < threshold` rather than blanket-reject.)

### R6 — Templates module (`templates.py:75`): make `_PARTIAL_TEMPLATE` and `_CORRECT_AFFIRMATIONS` content-bearing under poor verdict signal

**Symptom**: T6 (`verdict=partial`, fallback fires) the rendered template uses
`safe.what_right = "you've got part of the idea"` (default) because the
grader did not populate `what_right`. The student reads a generic line and
the rendered worked-example fallback re-attaches the same multi-slot Q.

**Edit**: in `templates._render_worked_example_terminal`, when the action
floor lands a verifiable-answer Q that includes "percentage" / "%" but the
`teacher_script` does not, **strip the percentage clause** from the open-Q
stem before re-presenting it. (Subject-agnostic: strip any "and the X" tail
where X is a noun the teacher_script does not name.)

This is a substantial change to `templates.py` rather than `move_prompts.py`.
Flag for a separate engineering pass; do not bundle into the prompt change.

### R7 — Lesson-content fix (data, not prompt)

L1167 step 1 bundles "profit + profit %" without teaching profit % first. The
`LessonStep.educational_content` for this step needs an authored worked-example
anchor that walks profit % calculation, OR the bank questions for step 1 should
be split into a profit-only slot and a profit %-only slot. This is in
`apps/curriculum/`, not `apps/tutoring/`.

---

## 6. Priority Triage (recommended next-steps)

| Priority | Fix | File | Why this first |
|----------|-----|------|----------------|
| **P0** | Remove `confirm_and_extend` from `probe_moves` in `open_question_stickiness` gate. | `apps/tutoring/v2/services/conformance/gates.py:502` | Single change. Unblocks the entire "correct answer → advance" path. Visible across BOTH MATHS-S1 and GEO-S5 evaluations every run. |
| **P0** | Fix grader to score per-slot on multi-slot bank items, not joint. | `apps/tutoring/v2/services/grader.py` | T2, T5, T6, T8 cascade comes from a joint-grading failure. |
| **P1** | Extend `detect_help_request()` to catch "I don't know how to do <X>" + "I don't understand <X>". | `apps/tutoring/v2/services/move_selection.py:detect_help_request` | T3 and T7 are textbook Direct-Instruction triggers and were missed. |
| **P1** | EXPLAIN-move prompt edit (R1, R2 above). | `apps/tutoring/v2/services/move_prompts.py:572` | Cheapest defense against the prose-pose failure mode. |
| **P2** | CONFIRM_AND_ADVANCE / CONFIRM_AND_EXTEND praise-opener directive (R3, R5). | `apps/tutoring/v2/services/move_prompts.py:302, 330` | Reduces praise-filter retry-cascade. Effect smaller than P0/P1. |
| **P2** | Lesson-content split for L1167 step 1. | curriculum DB | Off-engine fix; addresses one lesson, not the engine. |
| **P3** | Diversify the `_OPEN_Q_ACTION_FLOORS` pool (currently rotates 3 — 4 of 8 tutor turns landed on the same string). | `apps/tutoring/v2/services/templates.py:86` | Cosmetic; matters only if fallback-rate stays high. |

---

## 7. Engine Health Indicator

`fallback_used = true` rate this session: **7 / 9 tutor turns = 78 %**.

The Phase 3 observability dashboard target is < 5 %. The session is
catastrophically off the target. The two §4 root causes are the only
plausible explanation — every other path was structurally sound.
