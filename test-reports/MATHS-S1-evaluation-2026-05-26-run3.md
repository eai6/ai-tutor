# MATHS-S1 Tutor Evaluation — 2026-05-26 (run 3)

**Persona**: S1 (Cycle 4) struggling student in Seychelles. Consistently provides
wrong answers; eventually asks for an explanation and a worked example.
**Lesson evaluated**: L1166 — *Know and use the terms selling price, cost price,
discount, profit and loss* (Mathematics S3 / Percentages unit). Chosen for novelty
versus prior runs that used L1148 (one-step equations).
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on`, Sonnet 4.6 + Gemini 3 Flash
Preview per Phase 3 retune).
**Student**: `mathprobe` at School-3.
**Session ID**: 82 (9 student turns T1–T9 → exit ticket).

> **Context**: prior same-day runs sit at
> `test-reports/MATHS-S1-evaluation-2026-05-26.md` (session 75) and
> `test-reports/MATHS-S1-evaluation-2026-05-26-run2.md` (session 78). Both used
> L1148. This run switches lesson + repeats persona to test whether the
> regression patterns hold across topics.

---

## 1. Transcript

| # | Role | Content | move / verdict / fallback |
|---|------|---------|---------------------------|
| 0 | tutor | Today we're kicking off a lesson on **selling price, cost price, discount, profit and loss**… The £40 a shopkeeper paid for a jacket — *(MCQ Q with 4 options)* | `explain / — / no` ✅ clean opener |
| 1 | student | i think its D, discount | — (wrong: correct=B cost price) |
| 1 | tutor | Let's make sure that's right together. Here's one for you to try. | `pose_question / — / **yes**` ⚠⚠ **P1: no question posed — safe template only** |
| 2 | student | what is the question? | — (student forced to ask) |
| 2 | tutor | Here it is — read carefully and tell me if it's true or false. A trader buys fresh fruits for 300 SCR and sells them for 375 SCR. The statement 'The profit is 75 SCR' is correct. | `pose_question / — / no` ✅ clean T/F |
| 3 | student | is the cost price the same as the profit? | — (meta-question, not an answer) |
| 3 | tutor | Quick check on that — let's pin it down together. Here's one for you to try. | `scaffold_hint / unverified / **yes**` ⚠⚠ **P1: no question posed — safe template only** |
| 4 | student | i think profit is 95 | — (wrong: correct=75) |
| 4 | tutor | Good try showing your working — but the number isn't quite right. Profit is found by subtracting the cost price from the selling price… A shopkeeper buys a hand-painted plate for 75 SCR and sells it to a tourist for 130 SCR. Calculate the profit. | `scaffold_hint / wrong / no` ⚠ pivots to a NEW item instead of staying on the 75/300/375 open question (preamble subskill-stickiness rule violated) |
| 5 | student | discount is 20 | — (wrong: confuses term; correct=55) |
| 5 | tutor | It looks like there might be some mix-up between "discount" and "profit"…  Try this one — same idea, find the difference between selling price and cost price. A market trader buys cinnamon for 200 SCR but sells it for only 160 SCR. Calculate the loss. | `scaffold_hint / wrong / no` ⚠ misconception named well, but pivots AGAIN to a new item without resolving plate problem |
| 6 | student | is the loss 360? | — (wrong: added instead of subtracting; correct=40) |
| 6 | tutor | It looks like 360 came from adding the two prices together — but for a loss, we want to find the *difference*… A bookshop in Victoria advertises 25% off all books. A textbook originally priced at 120 SCR is on sale. What is the discount amount? | `scaffold_hint / wrong / no` ⚠ misconception named accurately, then pivots AGAIN to a *third unrelated* item (textbook discount, new subskill = percentage-of) |
| 7 | student | i dont understand can you explain how to do these? | — (explicit help-request) |
| 7 | tutor | No problem at all — let's slow it right down. A **discount** is simply the amount of money taken *off* the original price. To find it, you multiply the original price by the percentage, like this: discount = (percentage ÷ 100) × original price. So for the textbook, you'd work out (25 ÷ 100) × 120 SCR. Give that calculation a go — what do you get? | `explain / — / no` ✅ best turn of the session — direct instruction + concrete formula + brings them back to the open textbook question |
| 8 | student | is the loss 40 then? | — (answers the *prior* loss question, which is now stale; would be correct=40 for 200/160, but the active open Q has switched to the 120 SCR discount) |
| 8 | tutor | I want to make sure we're on the same page here. Here's one for you to try. | `scaffold_hint / unverified / **yes**` ⚠⚠ **P1: no question posed** — and the grader is right to surface uncertainty (answer doesn't match the open Q) but the response stalls |
| 9 | student | im totally lost, can you show me a full worked example step by step? | — (explicit worked-example request) |
| 9 | tutor | Let's keep going. Here's one for you to try. | `worked_example / — / **yes**` ⚠⚠⚠ **CRITICAL: student explicitly asked for a worked example; engine selected `worked_example`; safe-template fallback returned NO example and NO question** |

**Exit ticket (10 items, mixed type)**: scored **1/10** → `remediation`,
`mastery_level=in_progress` (working as designed for a struggling student).

---

## 2. P1 Unacceptable Errors

| Class | Count | Where | Notes |
|---|---|---|---|
| Tutor marks a correct answer wrong | **0** | — | No correct answer was ever flagged wrong. Grader correctness held. |
| Tutor marks a wrong answer correct | **0** | — | All wrong attempts (D, 95, 20, 360) returned `wrong`; the stale-question answer at T8 was returned `unverified`, which is also defensible. |
| Incomplete question (missing crucial info) | **4** | T1, T3, T8, T9 | All four are the structural-conformance safe-fallback template "*Here's one for you to try.*" / "*Let's keep going. Here's one for you to try.*" — emitted when the LLM-produced response was rejected by conformance and no clean retry landed. Every one of these turns ends with no question for the student to answer. |

**P1 verdict: FAIL.** Four incomplete-question violations in a 9-turn
session is a systemic conformance-vs-recovery problem, not a stochastic miss.
Three of the four (T1, T3, T8) follow the same shape: when the grader returns
`unverified` (or when the student turn is a clarification rather than an
attempt), the move-prompt LLM emits something the classifier rejects, the retry
also fails, and the safe template ships — but the safe template has no question
behind the connective tissue.

The most damaging single failure is T9: the student *explicitly* asks for a
worked example, the engine *correctly* selects `worked_example`, and the safe
template ships a 9-word non-answer. The `worked_example` move requires labelled
subgoals + a short practice prompt; the safe fallback delivers neither and
silently breaks the help-request override rule baked into `SHARED_PREAMBLE`.

---

## 3. Science-of-Learning Evaluation

Rated against the 13 principles in `design/science-principles.md`. Score key:
✅ delivered • 🟡 partial • ❌ missed • n/a out of scope for one session.

| # | Principle | Score | Evidence |
|---|---|---|---|
| 1 | **Active Learning** | 🟡 | Student stayed engaged (9 attempts in 9 turns), but T1, T3, T8 and T9 emitted no question, so on 4/9 turns the student literally had nothing to act on except a meta-question. |
| 2 | **Direct Instruction** | 🟡 | T0 opener is tight, well-scoped, names the five terms. T7 (`explain` after help-request) is excellent — explicit formula, anchored to the active item. But T9 (`worked_example` after help-request) silently dropped — Direct Instruction failed the moment the student needed it most. |
| 3 | **Deliberate Practice** | ❌ | Practice was not calibrated to *this student's* weakness. The same misconception (subtraction direction, confusing terms) was visible from T2 onwards; instead of staying on one item and exercising that subskill, the engine pivoted to a new item every turn (T4 plate, T5 cinnamon, T6 textbook). The student never got to retry the *same* problem to closure. |
| 4 | **Mastery Learning** | n/a | One session. Note: exit-ticket attempt logged 1/10 → mastery_level=in_progress, which is the correct path (path varies, bar holds — no auto-pass). |
| 5 | **Minimising Cognitive Load** | ❌ | Three topic switches in three turns (profit on fruits → profit on plate → loss on cinnamon → discount on textbook) is the opposite of "one idea per turn". By T6 the student is juggling four different unfinished prompts. The T4 scaffold prompt also stacks: "check that calculation carefully… Here's a similar one" — that's two things to do, and the second one displaces the first. |
| 6 | **Automaticity** | n/a | No latency measurement in this turn loop. |
| 7 | **Layering** | n/a | Single objective. |
| 8 | **Non-Interference** | ❌ | The 25%-off textbook problem (subskill: percentage-of) was injected at T6 while the student was still slipping on profit/loss subtraction. Percentage-of and subtract-to-find-difference are *exactly* the kind of confusable subskills Ch.17 warns about — interleaving these in three consecutive turns guarantees interference. |
| 9 | **Spaced Repetition** | n/a | Out of MVP scope per `move_prompts.py` docstring. |
| 10 | **Interleaving** | n/a | Same reason. |
| 11 | **Testing Effect / Retrieval Practice** | 🟡 | Retrieval-first was honoured (no answers leaked). But on 4 turns the "retrieval prompt" was empty — there was nothing to retrieve against. |
| 12 | **Targeted Remediation** | ❌ | The diagnosis was visible after T4 (student computes the wrong direction of subtraction; confuses *loss* / *profit* / *discount* lexically). The correct remediation is one component-skill drill on subtraction with two given numbers, repeated until the slip closes. Instead the engine kept generating new items, each with a new lexical wrapper. |
| 13 | **Gamification** | n/a | Out of MVP scope. |

**Aggregate**: 0 ✅ delivered • 3 🟡 partial • 5 ❌ missed (of 8 in-scope rows).

---

## 4. Cross-Cutting Issues Surfaced This Session

1. **Conformance-rejection → safe-template emit is too aggressive.** The "*Here's one for you to try.*" template ships *with no question*. That is itself a P1 incomplete-question violation. The template needs to either (a) restate the open question or (b) carry one fixed bank-slot pose. Saying "here's one" and not posing one is the worst of both worlds.

2. **`worked_example` fallback is the same broken safe template.** When the move is `worked_example` and the LLM output is rejected, the fallback ships "Let's keep going. Here's one for you to try." — completely absent the worked-example obligation. There needs to be a per-move fallback library: for `worked_example` the safe terminal should at minimum walk through *one* of the bank's already-stored worked examples (every lesson step has one in `LessonStep.worked_example_text`).

3. **Open-question drift.** The preamble has explicit "Stay on the open question — subskill stickiness" rules. They are being violated by the LLM at T4, T5, T6 — every wrong attempt pivots to a new item. Either the open-question state isn't actually being passed to the move-prompt LLM in the context block, or the LLM is ignoring it. Worth verifying via the per-turn v2_trace.

4. **Help-request override drops on fallback.** The preamble explicitly says: "When the student explicitly asks for an explanation, a worked example, 'show me how' … answer the *content* of that ask." This rule is enforced when the LLM response is accepted (T7 honoured it perfectly) but the safe fallback ignores it (T9). The fallback needs to know the trigger was a help-request and at minimum emit a static "Here is a worked example: …" sourced from the lesson step bank.

5. **Stale-answer / question-switch ambiguity (T8).** Once the engine pivots from item A to item B without resolving A, the student can legitimately answer either. T8 ("is the loss 40 then?") is correct for the cinnamon problem (200−160=40) but stale by the time it arrives. The grader returned `unverified` which is the right verdict — but the response then stalled. The engine should detect this pattern ("student answered a question that is now closed") and explicitly say so before pivoting.

---

## 5. Prompt Review — `apps/tutoring/v2/services/move_prompts.py`

Recommendations target the move prompts AND the fallback layer they implicitly
rely on. The prompts themselves are mostly well-engineered (concise, lifted
imperatives from `science-principles.md`, positive directives, principles
audit-trail in the dataclass); the failures are in (a) what happens when the
LLM output is rejected, and (b) one prompt where a directive is missing.

### 5.1. `SHARED_PREAMBLE` — add an "incomplete-turn forbidden" hard rule (HIGH)

The preamble's "End the turn with something concrete" paragraph (lines 128–134)
already says "never end at a colon, dash, or trailing phrase with no question
or action behind it." Add a sibling explicit rule:

> "If you cannot legally call a tool this turn AND cannot pose a written
> question, you must EXPLICITLY restate the open question in plainer words and
> ask the student to attempt one specific step of it. Do not produce a
> connective tissue line like 'Here's one for you to try.' followed by
> nothing — those phrases are reserved for legitimate posed-question turns
> only."

This raises the LLM's chance of passing conformance on first attempt, reducing
fallback emissions.

### 5.2. `SCAFFOLD_HINT` — strengthen open-question stickiness (HIGH)

The current body (lines 322–384) says "Do not introduce a new problem; the
open question stays the focus until it is resolved" but the model violated
this 3× in our session. Add a concrete *anti-pattern* example, since negative
instructions are weak on Gemini 3 (per `gemini-prompting-expert`):

> "DO this (subskill drill on the same open question): if the open question
> was 'compute 375 − 300' and the student answered 95, the next probe is
> 'compute 375 − 300 step by step — what is 375 − 200, then minus 100?' —
> same item, decomposed.
>
> DO NOT do this: do not invent a new item with different numbers
> ('compute 130 − 75'). A new item is a `pivot`, and `pivot` only fires
> after 4 attempts."

### 5.3. `WORKED_EXAMPLE` — make help-request the dominant trigger (HIGH)

Lines 458–464 mention the help-request path but it reads as an addendum. For
this session, the most damaging single failure was T9: a help-request landed,
the engine selected `worked_example`, the LLM output was rejected, and the
fallback shipped a non-example. Rewrite so the help-request path is the
*primary* shape of the prompt:

> "Trigger: this turn is `worked_example`. The single most common trigger is
> the student explicitly asking for one ('show me', 'walk me through it', 'I
> don't get it'). Treat that ask as the brief.
>
> Required structure (3 labelled subgoals, then one practice prompt):
> - Subgoal 1: [name the first step in the lesson's vocabulary]
> - Subgoal 2: [compute / decide / identify]
> - Subgoal 3: [confirm the answer / read it back / state the unit]
> - Practice: [pose_inline_question on a one-step component of the open Q]
>
> If you cannot author this in one turn, do not emit a 'let's try one'
> connective — instead, lift the worked example directly from the lesson
> step's `worked_example_text` field. That field is in your context block
> as KB-WE."

The KB-WE injection requires a small change in the `StudentTutor` context
assembly: surface `LessonStep.worked_example_text` for the current step as a
named context chunk. The engine already loads `LessonStep`; piping the field
is a one-line addition.

### 5.4. Fallback layer — per-move safe terminals (HIGHEST PRIORITY)

This is the highest-leverage fix and is NOT a prompt change — it's the
verdict-keyed-safe-terminal table referenced in CLAUDE.md but materially
under-spec'd for moves that *must* deliver content (worked_example, explain,
name_misconception). Today every rejected move falls back to a generic
"Here's one for you to try." that violates structural conformance on its own
terms (it claims to pose without posing).

Proposed per-move terminals:
- **pose_question** terminal: restate the open question in plainer language +
  add "Try just this one part: <smallest decomposition>".
- **scaffold_hint** terminal: "Looking at the question one more time — <restated
  open Q>. Try just <one subskill> first."
- **worked_example** terminal: ship `LessonStep.worked_example_text` verbatim
  with a 2-line "Subgoal 1 / Subgoal 2 / Subgoal 3" wrapper auto-generated
  from sentence-splitting.
- **explain** terminal: ship `LessonStep.direct_instruction_text` or
  `LessonStep.objective` as the explanation body.
- **name_misconception** terminal: "The slip looks like <generic from
  open-question type>; let's slow down on <subskill name>."
- **close_topic** terminal: existing wording is fine.

Wire these into `apps/tutoring/v2/services/conformance.py` (or wherever the
single rejection→fallback hop lives), keyed by `selected_move`. This change
makes the safe-fallback contractually deliver the move's pedagogy minimum,
not a meaningless connective.

### 5.5. `POSE_QUESTION` — `lead_in` variation (LOW)

Lines 218–220 say `lead_in` should vary turn to turn. The session repeatedly
shipped the same template "Here's one for you to try." across T1, T3, T8 — but
those are fallbacks, not LLM-authored leads, so this is symptomatic of issue
5.4 not the pose_question prompt. No change needed once the fallback layer is
fixed.

### 5.6. Subject-anchoring directive — already present, working (NONE)

`SHARED_PREAMBLE` lines 91–96 explicitly forbid subject drift. We saw zero
subject drift in this session — the engine stayed in "money, cost price,
profit, loss" the whole way. This directive is doing its job; no change.

---

## 6. Summary

| Dimension | Result |
|---|---|
| P1 false-positive (correct→wrong) | **0** — PASS |
| P1 false-negative (wrong→correct) | **0** — PASS |
| P1 incomplete question | **4** — FAIL (3 of 4 from the fallback safe-template; 1 of 4 the broken `worked_example` fallback on help-request) |
| Help-request handling | **1 of 2 honoured** (T7 ✅, T9 ❌) |
| Open-question stickiness | **violated 3×** (T4, T5, T6 all pivoted to new items) |
| Exit ticket transition | **WORKS** via legacy `/exit-ticket/` endpoint; v2 routing does emit `is_complete=true` on `close_topic` but does NOT emit `show_exit_ticket=true` + `exit_ticket={…}` payload, so the modal must be triggered by other means in the v2 flow |

**Primary recommendation**: implement per-move safe-fallback terminals
(§5.4). One change fixes 3 of 4 P1 violations observed this session.

**Secondary recommendation**: amplify the open-question-stickiness directive
in `SCAFFOLD_HINT` (§5.2) — three consecutive item-pivots on a struggling
student is the clearest signal that the existing prose isn't strong enough.

**Tertiary**: thread `LessonStep.worked_example_text` into the context block
(§5.3) so the `worked_example` move has authoritative content to fall back to
when the LLM's authored version is rejected.

Refs: design/science-principles.md, apps/tutoring/v2/services/move_prompts.py,
apps/tutoring/v2/routing.py
