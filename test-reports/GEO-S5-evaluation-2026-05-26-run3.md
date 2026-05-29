# GEO-S5 Tutor Evaluation — 2026-05-26 (run 3)

**Persona**: S5 (Cycle 5) advanced Geography student in Seychelles. Provides
full, correctly-reasoned answers with named processes, mechanisms, and
locale-specific references. Mostly correct throughout.
**Lesson evaluated**: L1438 — *Define and Classify Weathering* (Belonie
Geography S3, S3: Weathering and Mass Movements unit). Distinct from prior
GEO-S5 runs that used L1425 (Map Scale) and L1451 (Hydrological Cycle).
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on`, Sonnet 4.6 + Gemini 3
Flash Preview per Phase 3 retune).
**Student**: `geoprobe` at School-3 (membership rebound to School-3-only after
the platform-wide Global membership was hiding institution-scoped geography
lessons from `lesson_list`).
**Session ID**: 83 (6 student turns T1–T6 → close_topic → exit ticket).

---

## 1. Transcript

| # | Role | Content | move / verdict / fallback |
|---|------|---------|---------------------------|
| 0 | tutor | Today we're kicking off a lesson called **Define and Classify Weathering** — and by the end you'll be able to say exactly what weathering is and tell the three main types apart. *(2-paragraph direct-instruction frame; lists physical, chemical, biological)* *(MCQ Q: best definition of weathering, 4 options A–D)* | `explain / — / no` ✅ clean opener with a real tool-posed MCQ |
| 1 | student | B — weathering is the in-situ breakdown of rock and minerals through physical, chemical, and biological processes; the in-situ qualifier is what distinguishes it from erosion, which involves transport | — (correct + over-qualified mechanism detail) |
| 1 | tutor | Let me check that one with you. Here's one for you to try. | `pose_question / — / **yes**` ⚠⚠ **P1: no question posed — safe template only** |
| 2 | student | Physical weathering encompasses freeze-thaw (frost shattering), thermal expansion/insolation weathering, salt crystallisation, and pressure release (exfoliation/sheeting) - all mechanical processes that fragment rock without altering its chemical composition | — (correct, mechanism-detail) |
| 2 | tutor | Let me check that one with you. Here's one for you to try. | `pose_question / — / **yes**` ⚠⚠ **P1: no question posed — safe template only** (identical text to T1; system is failing on the same trigger twice in a row) |
| 3 | student | Chemical weathering alters mineral composition through reactions like hydrolysis (feldspar to clay), carbonation (limestone dissolution by carbonic acid), oxidation (iron-bearing minerals to rust), and hydration - producing new minerals that are typically softer and more soluble than the parent rock | — (correct, mechanism-detail) |
| 3 | tutor | You clearly know your theory — let's see it applied to a real sample. A student collects a basalt rock sample from a Seychelles beach. *(diagnostic vignette + clear question: which type of weathering best explains pitted/colour-changed/softer minerals, no fracturing?)* | `pose_question / — / no` ✅ — and the lead_in finally varies; well-pitched transfer item that uses the student's over-qualified knowledge |
| 4 | student | Chemical weathering - the pitting, colour change (likely oxidation of iron-bearing olivine/pyroxene producing rust-coloured iron oxides), and mineral softening are diagnostic of hydrolysis and oxidation reactions; the absence of fracturing rules out physical weathering | — (correct with reasoning) |
| 4 | tutor | Right — identifying chemical weathering and its specific processes (oxidation and hydrolysis). Here's one for you to try. | `confirm_and_extend / correct / **yes**` ⚠ safe-template: the confirmation half worked, but the "extension" was the same empty-question line. No follow-up Q posed. |
| 5 | student | Biological weathering acts through two mechanisms: physical (root wedging in joints, burrowing organisms creating fractures) and chemical (organic acids from lichens and decomposing humus, plus chelation by acids that complex with metal ions to dissolve minerals) | — (correct but TOPIC-SWITCH from the basalt question) |
| 5 | tutor | Your explanation of biological weathering is actually really solid knowledge — but notice that it's a new topic rather than an answer to the open question about the basalt sample. The question is still live: we need to identify which type of weathering explains the pitting, colour change, and mineral softening seen in that rock. You actually nailed it a couple of turns back — let's make it official with the graded version. *(Clean true/false transfer Q comparing sheltered vs exposed granite monument in a Seychelles town square)* | `scaffold_hint / wrong / no` ✅✅ **Best turn of the session** — explicitly catches topic-drift, names the open question, refuses to credit the off-topic answer, and poses a fresh transfer item that probes the same concept at S5-appropriate depth. |
| 6 | student | In Seychelles' humid tropical climate, chemical weathering dominates because high temperature and abundant rainfall accelerate hydrolysis and dissolution; granite (the bedrock of the inner islands) weathers via hydrolysis of feldspars into kaolinite clay, producing the characteristic deep red-brown lateritic soils visible on Mahé | — (correct, locale-anchored) |
| 6 | tutor | Nicely done — you correctly identified that the statement is false, and you backed it up with solid reasoning about how chemical weathering (hydrolysis, dissolution) drives the discolouration, rounding, and mineral loss seen on the exposed part of that monument. You're ready for the exit ticket — I'll set it up. | `close_topic / correct / no` ✅ — clean close, names what the student demonstrated, signals transition |

**Exit ticket transition (v2-routing gap)**: the `close_topic` envelope returned
`is_complete=true, phase='completed'` but did NOT populate `show_exit_ticket` or
`exit_ticket` in the response payload (`apps/tutoring/v2/routing.py:299-300`
hardcodes both to `False`/`None`). The frontend modal listens for these fields
(`templates/tutoring/_partials/exit_modal.html`), so an end-to-end run on v2
would silently fail to surface the exit ticket. Submission via the legacy
endpoint `/tutor/api/chat/83/exit-ticket/` works correctly when answers are
sent in the canonical modal format (MCQ=letter, fill_in_blank=array,
matching=`{left: right}` map, short_answer=string) — graded **10/10**,
`mastery_level=mastered`, `passed=true`.

> **Note on grader format-sensitivity surfaced during this session**: my first
> exit-ticket submission used `{"blanks":["Physical"]}` for fill_in_blank and
> `{"pairs":[...]}` for matching (mirroring the canonical-answer schema).
> Every correct answer marked wrong (4/10). Re-sending in the modal's
> wire format (bare arrays / `{left:right}` maps) flipped them all to
> correct (10/10). This is grader brittleness, not student error — a non-modal
> client (mobile, alternative UI, future API consumer) sending the
> "natural" shape will be silently mismarked. See §4.5 below.

---

## 2. P1 Unacceptable Errors

| Class | Count | Where | Notes |
|---|---|---|---|
| Tutor marks a correct answer wrong | **0** | — | Every correct turn (T1, T2, T3, T4, T6) eventually received affirmation OR a defensible "let's check together" hedge. The T5 topic-switch reply was correctly *not* credited toward the basalt question (it was a different topic) but the tutor explicitly acknowledged the answer was *solid* knowledge — handled gracefully. |
| Tutor marks a wrong answer correct | **0** | — | No false-positive. |
| Incomplete question (missing crucial info) | **3** | T1, T2, T4 | All three are the safe-template fallback "*Let me check that one with you. Here's one for you to try.*" / "*Here's one for you to try.*" — emitted when the LLM-produced response was rejected by structural conformance and no clean retry landed. None of these turns end with a question for the student to answer. |

**P1 verdict: FAIL.** Three incomplete-question violations in a 6-turn session.
The pattern is identical to the MATHS-S1 run (same day, different lesson,
different student): structural conformance rejects the LLM output → safe
fallback ships "Here's one for you to try." → no question is actually posed.
This is a systemic conformance/fallback gap, not topic-specific.

Crucially, the over-qualified, mechanism-rich answers from this S5 student
(T1, T2, T3) appear to be *the trigger* for fallback emission: they're more
than the bank stem expected, the tutor's confirm-and-extend reply trips a
classifier label (likely answer-leak or rule conformance), conformance rejects
the reply, and the safe template ships. An *advanced* student who answers
above the bank's expected granularity gets the *worst* in-session experience
under the current fallback policy.

---

## 3. Science-of-Learning Evaluation

Rated against the 13 principles in `design/science-principles.md`. Score key:
✅ delivered • 🟡 partial • ❌ missed • n/a out of scope for one session.

| # | Principle | Score | Evidence |
|---|---|---|---|
| 1 | **Active Learning** | 🟡 | The student stayed active and produced content on every turn, but T1, T2, T4 left her with no concrete prompt to retrieve against. Active engagement was sustained only because the student volunteered content the fallback didn't ask for. |
| 2 | **Direct Instruction** | ✅ | T0 named the three weathering types, defined weathering, and posed a definition-check MCQ — textbook Ch.11. |
| 3 | **Deliberate Practice** | 🟡 | T3 (basalt vignette) and T5 (granite monument true/false) are *well-calibrated edge-of-ability* items for an S5 student — diagnostic, transfer-shaped, locale-anchored. But the practice ratio is wrong: three of six tutor turns (T1, T2, T4) shipped no practice at all. |
| 4 | **Mastery Learning** | ✅ | Exit ticket scored 10/10 → mastery_level=`mastered`. Bar held; path varied. |
| 5 | **Minimising Cognitive Load** | ✅ | One idea per turn was honoured throughout. The basalt vignette and the granite monument both isolate one decision (which type of weathering) cleanly. T0 frames physical/chemical/biological in one sentence each before any practice. |
| 6 | **Automaticity** | n/a | Not measured in v2 turn loop. |
| 7 | **Layering** | 🟡 | T5 layered the weathering-type discrimination with locale ("Seychelles town square") and time ("30 years ago"), pushing the student to integrate prior knowledge — good Ch.16 behaviour. |
| 8 | **Non-Interference** | ✅ | The three weathering types are confusable-by-design; the engine interleaved them within the *same* item (the granite monument compares physical vs chemical exposure) rather than running consecutive blocks. |
| 9 | **Spaced Repetition** | n/a | Out of MVP scope per `move_prompts.py` docstring. |
| 10 | **Interleaving** | n/a | Same reason. |
| 11 | **Testing Effect / Retrieval Practice** | 🟡 | T0, T3, T5 are clean retrieval-first items. But on T1, T2, T4 there was no question to retrieve against — half the turns were retrieval-empty. |
| 12 | **Targeted Remediation** | ✅ | T5 is exemplary: the student topic-switched, the engine *named the drift*, restated the open question, and posed a fresh transfer item that probed the same concept. This is the Ch.21 ideal: same bar, scaffolded path. |
| 13 | **Gamification** | n/a | Out of MVP scope. |

**Aggregate**: 4 ✅ delivered • 4 🟡 partial • 0 ❌ missed (of 8 in-scope rows).

The engine, when it cleared conformance, did the science of learning well —
notably better than on the parallel MATHS-S1 run. T5 in particular is the
strongest single tutor turn observed across all three same-day reports. The
fallback-emission problem is the dominant remaining defect.

---

## 4. Cross-Cutting Issues Surfaced This Session

1. **Advanced-student over-qualification is the dominant fallback trigger.**
   T1, T2, T4 all followed a correct-but-rich student answer. The tutor's
   confirm-and-extend / pose-question reply almost certainly contained a
   mechanism summary or a verifiable fact (the student supplied feldspar →
   kaolinite, basalt → olivine/pyroxene, iron → rust); structural conformance's
   factual-claim or answer-leak gate then triggered and the safe template
   shipped. **The system is currently *penalising* the student who knows the
   most.**

2. **`pose_question` and `confirm_and_extend` need a "rich-answer" branch.**
   `CONFIRM_AND_EXTEND` already says "If the student's answer overqualified the
   bank stem … raise the stake" (line 302–305). The directive is correct but
   appears to fire only when the LLM also doesn't trip a conformance gate. In
   practice the rich student answer → rich tutor confirmation → conformance
   trip → safe fallback pipeline collapses the over-qualified path to a
   no-op. The fix is two-sided: relax the conformance gate when the verdict is
   `correct` AND the student's prior message contains mechanism vocabulary
   (signal: the move prompt is `confirm_and_extend` *because* the verdict was
   correct, so factual-claim adjudication has lower risk); AND make the
   `confirm_and_extend` fallback fall back to a parameter-twist item, not a
   blank "here's one".

3. **`close_topic` does not transition to exit ticket on v2.** The student
   was told "I'll set it up", `is_complete=true` was emitted, but
   `show_exit_ticket` and `exit_ticket` were both `false`/`null` in the
   response. End-to-end this means a real student on v2 reaches "close" and
   sits looking at a completion message with no exit ticket modal triggering.
   See `apps/tutoring/v2/routing.py:218-225` — the `_envelope` call on
   completion intentionally omits the exit ticket payload that the legacy
   path returns. **This is a P0 product gap.**

4. **Topic-drift handling is a strength worth preserving (T5).** Whatever
   prompt template + context block produced T5 is doing exactly what the
   preamble's "Stay on the open question" rules ask for. When extending
   `SCAFFOLD_HINT` (see §5.2 of MATHS-S1 report), the T5 example is the
   reference pattern to lift verbatim into the prompt as a positive example.

5. **Exit-ticket grader is format-brittle.** The submission endpoint accepts
   answers in *one* exact shape per question type, with no schema validation
   error surfaced — wrong shape → silently scored wrong. A canonical-shape
   payload (`{"blanks":["Physical"]}`) was rejected; the modal-wire shape
   (`["Physical"]`) was accepted. For an exam-style assessment this is
   high-risk: a mobile client, an LTI integration, or any future API consumer
   submitting in the "obvious" shape will mismark the student. The grader
   should either accept both shapes or return a 400 on shape mismatch — never
   silently false-fail.

---

## 5. Prompt Review — `apps/tutoring/v2/services/move_prompts.py`

Many recommendations overlap with the parallel MATHS-S1 report (§5.1, §5.4,
§5.5 there). The GEO-S5-specific additions:

### 5.1. `CONFIRM_AND_EXTEND` — make the rich-answer extension survive conformance (HIGH)

Current body (lines 282–319) already handles the "overqualified answer" case
in prose. The failure mode this session is downstream: the LLM authors the
extension, conformance rejects it (likely because the LLM included a
mechanism-named factual claim while confirming the student's mechanism
sentence), the fallback ships. Two changes:

a) Add an explicit "stay strictly question-shaped" rule in the body:

> "Your turn body has two parts only — (1) a ≤1-sentence affirmation
> mirroring the *student's* specific mechanism wording, and (2) a tool-posed
> follow-up question. Do not add a third part. Do not add a mechanism summary
> of your own — the student already said it; do not narrate the chemistry
> again or you will trip the answer-leak classifier."

b) Surface the conformance-classifier label set in the move prompt so the LLM
knows which gates it must clear. The classifier labels are already
deterministic (`apps/tutoring/v2/services/conformance.py`); listing the
top three risks per move (e.g., for `confirm_and_extend`: answer-leak,
rule-conformance, factual-claim) directly in the prompt as "things this turn
will be rejected for" gives the LLM the constraints it's actually optimising
against.

### 5.2. `SHARED_PREAMBLE` — add "no factual mechanism restatement on confirmation" (MEDIUM)

The preamble's "Voice" section (lines 75–89) already addresses tone. Add:

> "When the student's answer was CORRECT and rich, do not restate the same
> mechanism in your own words to 'show you understood it'. The student
> already said it. Affirm what they specifically named ('you got the
> hydrolysis-to-kaolinite chain'), then move on — do not re-author the
> mechanism. Re-authored mechanisms are flagged by the conformance layer
> as redundant factual claims."

This pre-empts the dominant rejection trigger we saw in T1, T2, T4.

### 5.3. `EXPLAIN` and `CLOSE_TOPIC` — explicit exit-ticket transition hint (MEDIUM)

`CLOSE_TOPIC` body (lines 553–574) says: "Signal the transition explicitly:
'You're ready for the exit ticket — I'll set it up.' The frontend listens for
these cues; do not bury the transition." The tutor did exactly this at T6,
but the v2 routing doesn't act on the cue. Either:

a) Update `apps/tutoring/v2/routing.py:218-225` so that when
   `selected_move == 'close_topic'` and the close text contains "exit
   ticket" / "set it up", the envelope populates `show_exit_ticket=true` and
   `exit_ticket=<sample 10 questions for this lesson>` — same shape the
   legacy path returns.

b) Or, more conservatively, change `CLOSE_TOPIC` prompt to direct the LLM to
   close *without* mentioning the exit ticket, since the cue is currently a
   lie. ("Signal that the topic is done; the engine handles the next step.")

(a) is the better fix because exit-ticket-on-mastery is the product
contract; (b) is the patch-around-it option.

### 5.4. `POSE_QUESTION` — "what NOT to put in this turn" needs to include "your own answer summary" (LOW)

Lines 239–249 list things NOT to put in the pose turn. Add:

> "- A restatement of the student's own answer to a *previous* question.
>   Even if it was correct. The pose-turn is structurally an empty-hands
>   ask; any prose retelling of prior content increases conformance-rejection
>   risk."

This guards against the same trigger as §5.1b for the pose_question move
specifically (which fires on opening turns AND between scaffolds, both moments
when the LLM is tempted to recap before asking).

### 5.5. Audit-trail: principles cited for each move are still accurate

The `principles` tuple on each `MovePrompt` dataclass remains correctly mapped
to `science-principles.md` rows. The principles audit (Phase 2 §Tests) does
not need re-running for these recommended changes — the imperatives lifted
are already in place; the changes above are guardrails on prompt *behaviour*,
not new principle citations.

---

## 6. Summary

| Dimension | Result |
|---|---|
| P1 false-positive (correct→wrong) | **0** — PASS |
| P1 false-negative (wrong→correct) | **0** — PASS |
| P1 incomplete question | **3** — FAIL (all 3 are the same safe-template fallback observed on the parallel MATHS-S1 run; root cause = conformance rejection without per-move fallback content) |
| Open-question stickiness | ✅ enforced (T5 explicitly caught and corrected topic drift) |
| Subject anchoring | ✅ stayed in weathering vocabulary throughout |
| Exit ticket grade | **10/10 mastered** when submitted in modal-wire format |
| Exit ticket transition in v2 routing | **broken** — `is_complete=true` ships but `show_exit_ticket` / `exit_ticket` are hardcoded false/null in `apps/tutoring/v2/routing.py:299-300` |
| Best turn observed | **T5** — gold-standard topic-drift recovery |

**Primary recommendation**: per-move safe-fallback terminals (§5.4 of the
MATHS-S1 sibling report). One change fixes the dominant P1 failure mode in
both reports.

**GEO-specific primary**: relax `CONFIRM_AND_EXTEND` / preamble against the
"restate the student's own mechanism back to them" pattern (§5.1, §5.2 above).
This is the trigger that penalises advanced students under the current
conformance gate.

**P0 product gap**: wire `show_exit_ticket=true` + sampled-questions payload
into the v2 `close_topic` envelope (§5.3a). Without this, no student on v2
can take the exit ticket through the chat UI on completion.

Refs: design/science-principles.md, apps/tutoring/v2/services/move_prompts.py,
apps/tutoring/v2/routing.py, apps/tutoring/v2/services/conformance.py,
templates/tutoring/_partials/exit_modal.html
