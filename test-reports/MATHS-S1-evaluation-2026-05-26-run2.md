# MATHS-S1 Tutor Evaluation — 2026-05-26 (run 2)

**Persona**: S1 (Cycle 4) struggling student in Seychelles. Consistently provides wrong
answers; eventually asks for explanation / worked example.
**Lesson evaluated**: L1148 — *Solve One-Step Equations (Unknown on One Side, Positive
Integers)* (Mathematics S3; the closest published math lesson in this dataset since
Cycle-4 specific lessons aren't seeded).
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on`).
**Student**: `mathprobe` (freshly created S1 student at School-3).
**Session ID**: 78. 9 student turns (T1–T9).

> **Context**: an earlier same-day report exists at
> `test-reports/MATHS-S1-evaluation-2026-05-26.md` covering session 75 with `student1`.
> This re-run uses a fresh student so the transcript is uncontaminated by prior
> sessions and the asked-question ledger starts empty.

---

## 1. Transcript

| # | Role | Content | move / verdict / fallback |
|---|------|---------|---------------------------|
| 0 | tutor | Welcome to today's lesson: **Solve One-Step Equations (Unknown on One Side)**! The goal is straightforward: when you see an equation like *x + 3 = 7* … *(short direct-instruction frame)* | `explain / — / —` |
| 1 | student | yes ready | — |
| 1 | tutor | Try this one — show each step! **Solve the equation x + 8 = 23.** Show your working by identifying the operation, applying the inverse operation, and writing the final answer. | `pose_question / — / no` ✅ |
| 2 | student | is it x = 21? | — |
| 2 | tutor | Good effort showing your reasoning — you're on the right track with the method! The slip is in the arithmetic when you applied the inverse operation. Before I give more away, can you tell me: **what is 23 − 8?** Let's warm up the inverse-operation step with a similar equation — **Solve the equation 4x = 32.** Which of the following is the correct solution? | `scaffold_hint / wrong / no` ✅ best turn of the session |
| 3 | student | 23 - 8 is 14 | — |
| 3 | tutor | Almost there — the subtraction step is the key, so let's double-check that arithmetic carefully. Let's practise the inverse-operation step with a clear example — **Solve the equation 3x = 18.** Show your working … | `scaffold_hint / wrong / no` ⚠ pivot away from open Q without resolution |
| 4 | student | x = 36 | — |
| 4 | tutor | Not quite. the working ends at the wrong value — re-check the operation you applied. Let's try a question on this together. | `scaffold_hint / wrong / yes` ⚠ safe template; no new Q posed |
| 5 | student | i dont know how to do this can you explain? | — |
| 5 | tutor | Good effort for asking — that's exactly the right move when you're stuck! … Try this simpler one first — it targets the exact step you need: **You are distributing 60 copies of a leaflet about ocean conservation equally among 5 students.** … Write the equation 5x = 60 and solve it. | `scaffold_hint / unverified / no` ⚠ student asked to *explain*, got another problem |
| 6 | student | do i add 3 to both sides? | — |
| 6 | tutor | I want to check that with you before I'm sure either way. Let's try a question on this together. | `scaffold_hint / unverified / yes` |
| 7 | student | so x = 21 | — |
| 7 | tutor | Good effort putting your reasoning in writing — I can see you're trying to work through the steps. I couldn't verify that from the transcript alone … **A fisherman in Seychelles starts with 42 fish in his net.** … 42 − x = 17. Solve for x. | `scaffold_hint / wrong / no` |
| 8 | student | x = 59 | — |
| 8 | tutor | Not quite. the working ends at the wrong value — re-check the operation you applied. Let's try a question on this together. | `scaffold_hint / wrong / yes` |
| 9 | student | im totally lost can you show me a worked example? | — |
| 9 | tutor | I want to check that with you before I'm sure either way. Let's try a question on this together. | `scaffold_hint / unverified / yes` ⚠⚠ explicit worked-example request → safe template |

---

## 2. P1 Unacceptable Errors

| Class | Count | Where | Notes |
|---|---|---|---|
| Tutor marks a correct answer wrong | **0** | — | Grader correctness held across all 9 turns. |
| Tutor marks a wrong answer correct | **0** | — | x=21, x=36, x=59 all flagged `wrong`. |
| Incomplete question (missing crucial info) | **0** | — | Every posed question had a complete stem with given numbers. |

**P1 verdict: PASS.** No P1 violations.

---

## 3. Science-of-Learning Evaluation

Rated against the 13 distilled principles in `design/science-principles.md`. Score key:
✅ delivered • 🟡 partial • ❌ missed • n/a out of scope for one session.

| # | Principle | Score | Evidence |
|---|---|---|---|
| 1 | **Active Learning** | ✅ | Student computed/answered on every turn. Tutor never lectured beyond one short opening frame. |
| 2 | **Direct Instruction** | ❌ | Turn 0 had a tight direct-instruction opener, but when the student explicitly asked "can you explain?" (T5) and "show me a worked example?" (T9) the engine did **not** select `explain` or `worked_example`. It stayed in `scaffold_hint`. Direct instruction was therefore never delivered after the opener despite repeated explicit asks. |
| 3 | **Deliberate Practice** | 🟡 | Edge-of-ability was attempted (5x = 60 picked as a "simpler" anchor after the student stumbled on x+8=23), but the path drifted — see Cognitive Load below. |
| 4 | **Mastery Learning** | n/a | One session; no advancement-gating signal triggered. |
| 5 | **Minimising Cognitive Load** | ❌ | **Three distinct equations were left open simultaneously** by turn 5 (x+8=23 from T1, 4x=32 from T2, 3x=18 from T3, 5x=60 from T5). The student never resolved any of them. This is the *opposite* of one-idea-per-turn. The engine kept pivoting to "a similar/simpler" example without closing the previous open question. |
| 6 | **Automaticity** | n/a | Not measured in one session. |
| 7 | **Layering** | 🟡 | Inverse-operation skill is the cross-cutting subskill; the engine kept returning to it, which is layering — but the open-question churn (Principle 5) undermined the layering payoff. |
| 8 | **Non-Interference** | n/a | Single-topic session. |
| 9 | **Spaced Repetition** | n/a | Out of scope. |
| 10 | **Interleaving** | n/a | Out of scope. |
| 11 | **Testing Effect / Retrieval Practice** | ✅ | Retrieval-first stance held: every wrong answer triggered an attempt-again, not a reveal. |
| 12 | **Targeted Remediation** | ❌ | The misconception ("inverse of +8 is +") was named once correctly on T2 ("slip is in the arithmetic when you applied the inverse operation"), but never *isolated* as a sub-skill. The engine never moved to `name_misconception` despite three consecutive wrong answers (T1, T3, T4) which is one of the move-selection triggers documented in `move_prompts.py`. |
| 13 | **Gamification** | n/a | UI element, not message content. |

### What's working
- **Grader robustness on bare-answer math.** All bare-number wrong answers were correctly tagged `wrong`. Phase-3 bare-answer handling preserves correctness signal.
- **T2 was excellent** — surfaced the slip ("inverse-operation arithmetic"), asked the diagnostic sub-question ("what is 23 − 8?"), AND posed a parallel item (4x = 32) to keep the student moving. Best single turn of any of the three reports.
- **Effort praise, not ability praise.** "Good effort showing your reasoning", "Good effort for asking" — adheres to growth-mindset framing in `SHARED_PREAMBLE`.
- **Locale-aware items** (ocean-conservation leaflets, Seychelles fisherman) appeared in tool-posed bank questions.

### What's not working

1. **`worked_example` move is unreachable when the student asks for one.** T9 message verbatim: "im totally lost can you show me a worked example?" → the move selector chose `scaffold_hint` and conformance rejected the response, firing a safe template. This is a **direct contradiction of Direct Instruction (Ch.11)** and **Cognitive Load (Ch.14)**: when a learner explicitly signals overload, the prescribed remedy is a worked example with labelled subgoals, not another retrieval prompt. The pivot trigger for `worked_example` in `move_selection.py` needs to fire on explicit help requests, not just on attempt-count thresholds.

2. **Open-question churn.** The tutor introduced 4 distinct equations in 5 turns without resolving any. This burns working memory the opposite way Ch.14 prescribes — every new problem context is a fresh load. The scaffold_hint prompt allows "pose a SIMPLER bank slot" but doesn't say "wait until the current item is resolved before pivoting" — and the engine has no global counter on "open items abandoned without resolution".

3. **High fallback rate on `unverified` and meta-question turns.** Of 9 turns, 4 fell back to safe templates (T4, T6, T8, T9). Each fallback is a wasted turn pedagogically — the student gets one of two stock responses, neither carries instructional content. Two of these (T6, T9) were on explicit student help-requests, the worst possible place to lose teaching bandwidth.

4. **`name_misconception` never fired** despite ≥3 consecutive wrong attempts on the inverse-arithmetic subskill (T1, T3, T4). Its docstring says "Three wrong attempts on the same item — OR three consecutive unverified turns" but the trigger appears to be wired to a single-item counter rather than a subskill counter.

### Areas for improvement
- **A1.** Wire explicit student help-requests ("can you explain", "show me", "I don't understand", "worked example", "what is the rule") to `explain` or `worked_example` move selection unconditionally — regardless of grader verdict. This is the single largest pedagogical gap in this session.
- **A2.** Track an "open items count" in `runtime_state` and forbid `scaffold_hint` from posing a new item if there are already ≥2 unresolved open items on the same lesson. Force `pivot` or `explain` instead.
- **A3.** Group misconception counters by subskill tag, not by item id, so 3 wrong attempts on inverse-operation arithmetic across different equations triggers `name_misconception`.
- **A4.** When `unverified` is returned on a *meta-question* (student asks for help / pivots) rather than an attempt, route to `explain` not `scaffold_hint`.

---

## 4. Move-Prompt Review (`apps/tutoring/v2/services/move_prompts.py`)

Reviewing the prompts against this session's failure modes and against the
prompting-fundamentals + claude-prompting / gemini-prompting principles (the v2
models are Sonnet 4.6 for tutor, Gemini 3 Flash for grader, Haiku 4.5 for classifier).

### Strengths

- **Principle attribution is explicit.** Every `MovePrompt` carries a `principles`
  tuple keyed to `science-principles.md` rows, which keeps the audit traceable.
- **Positive-instruction discipline.** Most prompts use "do X" framing, which is correct
  for Gemini 3 and not harmful for Sonnet 4.6.
- **Tight length** (≤400 tokens per move) avoids the 460-line legacy bloat called out
  in the file docstring.
- **Verdict-aware lead-ins** in `POSE_QUESTION` and `SCAFFOLD_HINT` are well-designed —
  uncertain verdicts must surface uncertainty, wrong verdicts must not affirm.

### Gaps surfaced by this session

#### G1. `SCAFFOLD_HINT` allows pivot-to-new-item without an "open item" gate

Lines 285–286: *"End with a tool call to a SIMPLER bank slot that re-tests one sub-skill
of the open question."*

This invites exactly the churn seen in T3, T5, T7 — the model pivots to a "simpler"
bank item and silently abandons the previous open item. Recommend adding:

> **Open-item discipline.** If the open question has been live for fewer than 3 student
> attempts AND the student has not asked to move on, finish helping on the OPEN
> question instead of posing a new bank slot. Reissue or rephrase the open question's
> stem in the lead_in; do not author a parallel problem.

#### G2. No prompt-level instruction for "student requests explanation / worked example"

Across all 9 prompts, none addresses the *explicit help-request* pattern. The engine's
move-selection layer should route these to `explain` / `worked_example`, but the
prompts themselves should also tolerate the case where they are invoked on a meta-
question. Recommend adding a single sentence to `EXPLAIN` and `WORKED_EXAMPLE`:

> **Help-request trigger.** If the student's last message is an explicit ask for an
> explanation ("can you explain", "what does X mean", "show me", "I don't understand"),
> answer the *content* of the ask directly — do not redirect to retrieval.

#### G3. `WORKED_EXAMPLE` over-restricts numerical authoring

Lines 343–344: *"Pick numbers from the visible problem or bank — do not author new
numerical examples."*

In a single-session math lesson where the bank may only have 4–5 stems, this risks
the worked example *repeating* the very item the student just got wrong, defeating
the cognitive-load purpose. Allow:

> Pick numbers from the visible problem, the bank, OR a structurally equivalent toy
> example (e.g. "if 2x = 10, then x = 10 ÷ 2 = 5") that doesn't overlap with bank
> stems the student will face next.

#### G4. `NAME_MISCONCEPTION` triggers are item-scoped not subskill-scoped

Lines 305–308: *"Three wrong attempts on the same item — OR three consecutive
unverified turns…"*

This session had three wrong attempts across THREE different equations on the same
subskill (inverse-of-addition) and the move never fired. Recommend revising the
trigger to a subskill-tagged counter — or at minimum loosening the prompt's intro
text to read "three wrong attempts on the same **subskill or item**", then plumb the
subskill tag from the bank.

#### G5. `SCAFFOLD_HINT` unverified path leaks "I want to check that with you…"

The verdict-keyed safe-template fallback for `unverified` produces the literal string
*"I want to check that with you before I'm sure either way. Let's try a question on
this together."* (T6, T9 of this session.) This is currently a **terminal-template
artefact**, not a move-prompt artefact — but the move prompt's unverified guidance
encourages exactly this register, which is what then gets cannibalised when
conformance rejects the live response.

Recommendation: make the safe template content-aware on whether the unverified turn
followed an attempt (then say "let's pin it down") vs. a meta-question (then say
"here's a worked example" + invoke worked_example template). Two safe templates, not
one.

---

## 5. Summary

- **P1**: PASS (0 unacceptable errors).
- **Strongest turn**: T2 (precise misconception surface + diagnostic sub-question + parallel item).
- **Weakest moment**: T9 (explicit worked-example request → safe template).
- **Top fix**: route explicit help-requests to `explain`/`worked_example` moves, not `scaffold_hint`.
- **Second fix**: track open-item count to stop the engine from posing parallel equations while earlier items are still live.
- **Third fix**: subskill-scoped misconception counter so `name_misconception` fires when the same slip recurs across items.
