# GEO-S5 Tutor Evaluation — Run 6 (2026-05-26)

**Persona**: Struggling S5 / Cycle-5 Geography student. Mostly wrong answers, hesitant tone ("idk", "uhh", "im stuck").
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on`).
**Session ID**: 91.
**Lesson evaluated**: L1428 — *Six-Figure Grid References* (Belonie Geography S3). Not used in prior runs (run-1..5 used L1425, L1438, L1439, L1440, L1451).
**Turns**: 10 student turns → exit ticket fetched.

---

## Transcript summary (engine annotations: `move / verdict / fallback_used`)

| Turn | Role | Content (abridged) | Engine annotation |
|------|------|--------------------|-------------------|
| 0 | tutor | Direct-instruction opener: 4-figure ref = 1 km², 6-figure ref = 100 m × 100 m. Ends with engage Q: "why would knowing a 100 m square be more useful than 1 km?" | `explain / — / no` ✅ clean opener, real-world Seychelles anchor |
| 1 | student | "umm idk maps are confusing" | — (low-confidence signal) |
| 2 | tutor | Empathetic reframe ("totally fair … we'll break it down"). Restates the 1 km vs 100 m contrast (a "football pitch" anchor — nice dual coding). Asks the *same* engage Q in slightly different words. | `explain / — / no` ✅ growth-mindset framing, appropriate restart |
| 3 | student | "i guess the smaller one because there is less area to look" | — (correct intuition, not formal) |
| 4 | tutor | "Worth confirming before we move on. Holding on the same idea: Students will be able to calculate and use six-figure grid references for more precise map location. Pick the part you feel most confident about and start there." | `pose_question / — / **yes**` ❌ **fallback template + leaks the LO statement verbatim** ("Students will be able to …") |
| 5 | student | "can you just give me a real question please" | — (explicit pivot request) |
| 6 | tutor | Tool-posed bank stem: *"On a rescue map of Seychelles, a stranded boat is marked in grid square 78 45. It is positioned 4 tenths of the way across (east) and 7 tenths of the way up (north) … What is the six-figure grid reference?"* | `pose_question / — / no` ✅ clean pose, complete stem, Seychelles anchor |
| 7 | student | "is it 7845?" | — (wrong — dropped the tenths) |
| 8 | tutor | "Almost — that doesn't match the expected answer. Let's slow down on the same question … Tell me what you'd do first, even if you're not sure about the rest." | `scaffold_hint / wrong / yes` ✅ does NOT reveal answer, asks for working — appropriate diagnostic |
| 9 | student | "i dont know how to add the tenths to the grid square. what do i do first" | — (explicit help-request) |
| 10 | tutor | Restates the contrast (1 km vs 100 m), then **re-asks the same question** — *"Try one part of it and I'll guide you from there."* | `explain / — / yes` ❌ **Direct Instruction violation** — student asked "what do I do first?", got a re-statement of the principle but no *method* (no walked example). This is the same failure pattern as run-5's help-request rule violation, on a different lesson. |
| 11 | student | "uhh maybe 784574?" | — (wrong — guessed by concatenation but in the wrong order) |
| 12 | tutor | "Not quite — that doesn't match the expected answer. Let's slow down on the same question … Tell me what you'd do first, even if you're not sure about the rest." | `scaffold_hint / wrong / yes` ⚠ same template as T8, identical opener despite SHARED_PREAMBLE's "vary your phrasing" rule |
| 13 | student | "i think the easting is 78 and i add the 4 so 784. is that right" | — (**partially correct** — easting digit-append is right) |
| 14 | tutor | "Not there yet — that doesn't match the expected answer … Pick one piece of it that feels closest to something you can answer, and try just that part." | `name_misconception / wrong / yes` ❌❌ **P1 candidate — see §1.** Move=`name_misconception` selected but fallback fires — no misconception is ever named. The student's `784` reasoning is exactly the textbook method but is judged "wrong" without acknowledging the partial. |
| 15 | student | "im really stuck. can you show me how to do it step by step?" | — (explicit worked-example request) |
| 16 | tutor | **EXCELLENT TURN**: explicit subgoals 1-4 walkthrough: split into easting/northing → append tenths to easting (78→784) → append tenths to northing (45→457) → combine easting first (784 457). Ends with: "Now try it yourself … give the six-figure grid reference." But then **stacks a SECOND question**: *"Using a six-figure grid reference for the same person, which of the following best describes what happens?"* with **no MCQ options shown**. | `worked_example / — / no` ✅ on the labelled-subgoals delivery (R2-shaped success). ❌❌ on the stacked second question — **P1 incomplete question** (says "which of the following" with no following). |
| 17 | student | "okay so 784 457?" | — (**correct** answer to the boat problem) |
| 18 | tutor | "Not quite — You provided an example of a grid reference instead of describing how the search area changes. Here's the question again, in plain words: A coastguard needs to locate a person in distress near Port Louis … which of the following best describes what happens?" | `scaffold_hint / **wrong** / yes` ❌❌❌ **P1: correct answer (784 457) graded WRONG.** The engine silently switched the open question from the boat problem to the stacked Port Louis question between T16 and T17, then graded the student's correct boat answer against the new question. |
| 19 | student | "wait you didnt give me any options for that question. what are my choices?" | — (student notices the missing MCQ) |
| 20 | tutor | "Let me check that with you before we go further. Here's the question again, in plain words: A coastguard needs to locate a person in distress near Port Louis … which of the following best describes what happens? Pick one piece of it that feels closest to something you can answer, and try just that part." | `scaffold_hint / unverified / yes` ❌ **MCQ options STILL missing** — confirmed P1 incomplete question across at least 3 turns (T16, T18, T20). |
| (close) | system | Exit-ticket fetched via `chat_exit_ticket` endpoint — 10 items returned (fill-in-blank on eastings/northings, etc.). | — |

---

## §1 — P1 (unacceptable) errors

| Category | Count | Where | Notes |
|----------|------:|-------|-------|
| Tutor says correct answer is wrong | **2 hard, 1 soft** | T18 (hard); T20 (echoes T18 framing); T14 (soft: partial "easting = 784" is the textbook method, graded wrong without crediting it) | T18 is the clean hard P1: student answered "784 457" to the boat problem; engine had silently re-pointed `open_question` at the Port Louis MCQ between turns; verdict=`wrong`. T20 perpetuates the same misgrading. T14 grades a partial-correct sub-step as wholesale wrong instead of using the `confirm_and_extend` move — the `scaffold_hint` prompt's own line 416 ("Credit what they did get — when partial — use the `what_right` cue") was not honored. |
| Tutor says wrong answer is correct | 0 | — | Not observed. Engine was conservative on wrongs (the failure mode is the opposite). |
| Incomplete question | **3** | T16, T18, T20 | "Which of the following best describes what happens?" posed with no following MCQ options on three consecutive turns. The lesson author (or the bank slot for this LessonStep) packaged an MCQ stem but the rendering pipeline dropped the options. CLAUDE.md / `auto-memory/feedback_visual_check_required.md` style: an MCQ stem without options is unanswerable — the student even flagged it explicitly at T19 and got no recovery. |

**Bonus PII / leak** (not P1 but worth flagging): T4's fallback template leaks the raw learning-objective wording: *"Holding on the same idea: Students will be able to calculate and use six-figure grid references …"*. This is internal curriculum-metadata speech, not student-facing pedagogy. The same anti-pattern appeared in run-5 (see commit `f530e54`'s "scripted voice fix"). The varied-phrasing rule in `SHARED_PREAMBLE_TEMPLATE` is silently bypassed by the verdict-keyed safe template.

---

## §2 — Science of Learning evaluation

Scored against the 13 distilled principles (`design/science-principles.md`).

| # | Principle (Ch.) | Verdict | Evidence |
|---|------------------|---------|----------|
| 1 | **Active Learning** (10) | ✅ | Student is *doing* on every turn — 10/10. Practice-problem to worked-example ratio is healthy (only T16 was a worked example; everything else was an attempt or a help-request, which is correct shape). |
| 2 | **Direct Instruction** (11) | ⚠ partial | T0 / T2 introduce the contrast clearly (4-fig = 1 km², 6-fig = 100 m × 100 m) before any retrieval — correct order. ❌ T10 fails the rule: the student explicitly asked "what do I do first?" and got a restatement of the *principle*, not the *method*. The `WORKED_EXAMPLE` prompt's "When triggered by an explicit help-request: take the help-request as the brief: answer the *thing they asked*" (lines 572-580) was not honored. |
| 3 | **Deliberate Practice** (12) | ⚠ partial | ✅ The 78-45 / 4-tenths / 7-tenths question is genuinely edge-of-ability for a struggling student — calibration on item difficulty was correct. ❌ No diagnostic distinguished "doesn't know what tenths means" from "doesn't know the order easting/northing". The same wrong-shape stem repeats. |
| 4 | **Mastery Learning** (13) | ❌ | The student demonstrated mastery of the easting half at T13 ("78 + 4 = 784") and of the full method at T17 ("784 457"), and the engine acknowledged neither. The bar was held — but in the wrong direction (refusing to confirm). |
| 5 | **Minimising Cognitive Load** (14) | ⚠ partial | ✅ T16 is the textbook example: 4 labelled subgoals, anchored to the visible problem, no harder content introduced. This is exactly what `WORKED_EXAMPLE` prompt's lines 552-566 prescribe. ❌ Then T16 *stacks* a second un-rendered MCQ at the end, which violates the same prompt's "End with a short practice prompt that exercises ONE of the subgoals" (line 567) — ONE prompt, not two. |
| 6 | **Automaticity** (15) | n/a | Single session. |
| 7 | **Layering** (16) | n/a | One topic. |
| 8 | **Non-Interference** (17) | n/a | One topic. |
| 9 | **Spaced Repetition** (18) | n/a | OUT OF MVP. |
| 10 | **Interleaving** (19) | n/a | OUT OF MVP. |
| 11 | **Testing Effect / Retrieval Practice** (20) | ⚠ partial | ✅ Retrieval-first order is mostly held (T0 → T6 attempt → T8 hint). ❌ When the student got it right (T17 "784 457"), the retrieval that succeeded was not registered. The principle says "frequent low-stakes quizzes with immediate feedback" — the feedback half failed. |
| 12 | **Targeted Remediation** (21) | ⚠ partial | ✅ T8 asks for working without revealing the answer — correct diagnostic shape per the `scaffold_hint` prompt's bare-answer-wrong rule. ✅ T16 delivers a real labelled-subgoal worked example after the help-request. ❌ T18 / T20 re-pose an unrenderable MCQ — no remediation can land if the question itself is malformed. |
| 13 | **Gamification** (22) | n/a | OUT OF MVP. |

---

## §3 — Recommendations for `apps/tutoring/v2/services/move_prompts.py`

### R1. `WORKED_EXAMPLE` — forbid the stacked second question
Lines 567-571 say "End with a short practice prompt that exercises ONE of the subgoals". T16 ended with TWO: the "give the full six-figure reference" follow-up + the unrendered Port Louis MCQ. The structural conformance check approved this (`fallback_used=no`).

Add to "What NOT to do":

> **Do not append a second pose tool-call. The worked-example move ends with exactly ONE practice prompt, and that prompt is in prose (no tool call), addressed to the same open question.** If a separate bank slot needs to fire, that's the next turn's `pose_question` after the student responds — not this turn. (T16's stacked MCQ caused two cascading P1s in turns T18 and T20.)

### R2. `SCAFFOLD_HINT` — actually credit the partial
Lines 416-417 say *"Credit what they did get (when partial) — use the `what_right` cue, but phrase it naturally"*. T13 was a textbook partial — "the easting is 78 and I add the 4 so 784" is one half of the canonical method. T14 graded it wholesale wrong with the generic "doesn't match the expected answer" template. The `what_right` cue (if populated) was not surfaced, OR the partial branch never fired because the grader returned `wrong` not `partial`.

Add as a hardened rule (move it from prose advice into a numbered imperative):

> **1. When the verdict is `wrong` BUT the student's response names a sub-step the canonical decomposes into (e.g. one of the worked-example subgoals), the lead must affirm that sub-step explicitly before asking for the next.** Example: "You've got the easting right — that's exactly the method. Now do the same for the northing." Not "doesn't match the expected answer." The current default "generic-wrong" template applies the harshest grading to the closest-to-correct attempts, which is the wrong direction.

This is move-prompt-fixable (the template wording lives in the verdict-keyed safe-template table that this prompt drives). It also matches `confirm_and_extend`'s intent — but `confirm_and_extend` requires a verdict of `correct`, which the grader won't produce here. The shortest fix is to soften the `scaffold_hint`-on-wrong template to surface partials.

### R3. `EXPLAIN` — when triggered by a help-request, defer to WORKED_EXAMPLE
T10's failure is structural: the student asked "what do I do first?" and the engine picked `explain` (which restated the principle), when the canonical move for "what do I do first" is `worked_example`. The fix isn't in `EXPLAIN`'s prompt body alone — it's in move selection (`apps/tutoring/v2/services/move_selection.py:detect_help_request`) — but `EXPLAIN`'s prompt should defensively guard against being mis-routed:

Add to `EXPLAIN`'s "What NOT to do" (lines after 600):

> **If the student's prior turn was a help-request ("how do I", "what do I do first", "show me", "I'm stuck") and you've been routed here, your output MUST end with a 1-sentence redirect inviting the student to ask for a worked example explicitly, OR you must deliver the method (not the principle) in 2-3 numbered steps.** Restating the principle without the method is the failure mode that drove the run-5 fix; this guards against repeat.

### R4. `SHARED_PREAMBLE_TEMPLATE` — strip LO leaks from fallback templates
T4 leaked *"Students will be able to calculate and use six-figure grid references for more precise map location"* — the raw LO. The verdict-keyed safe template that fires under `fallback_used=true` substitutes the LO as a re-anchor. That should be off.

Add to the "Voice (every turn)" block:

> **Never quote the lesson's learning-objective string verbatim. The LO is internal curriculum metadata. If you need to re-anchor the student on what the lesson is about, paraphrase the LO in natural student-facing language using the visible context, not the LO field.**

This is one line in the preamble template; the safe-template layer will inherit it.

### R5. Cross-cutting: detect "the bank slot was posed but the MCQ options didn't render"
Three consecutive turns (T16, T18, T20) posed *"which of the following best describes what happens?"* with no options. This is upstream of the move prompts — the `pose_question` tool description / bank-slot renderer has a path where the stem is committed but the options aren't. Surface this in `POSE_QUESTION`'s prompt body as a sanity check the model itself can apply:

Add to `POSE_QUESTION`'s "What NOT to put in this turn" (lines 293-309):

> **Do not commit a pose where the rendered stem includes the phrase "which of the following" but the visible options list is empty.** If the bank slot's options field is missing, fall back to an open-response equivalent ("describe how the search area changes when you use a six-figure reference") or pick a different bank slot. An MCQ stem without options is an incomplete question and will be rejected by structural conformance — better to catch it here than after.

R5 catches the highest-frequency P1 in this session (3/10 tutor turns).

---

## §4 — Engine-level (out of scope for prompts)

1. **Silent `open_question` pivot between T16 → T17.** The worked-example turn ended with two prompts (boat re-attempt + Port Louis stacked MCQ); the engine should not have rotated `open_question.id` to the second one until the first was resolved. The two-phase-commit ledger described in CLAUDE.md (`pre_pose_token` → conformance approval → ledger commit) appears to have committed the second pose without de-committing the first. This is the run-6 GEO equivalent of the run-3 "topic switching" finding — both surface as `open_question` state desync.
2. **MCQ option rendering for L1428 step bank**. Three consecutive turns posed a malformed MCQ. Either the LessonStep `choices` field is empty for this slot, or the renderer drops it when re-used as a follow-up. Worth a one-off DB inspection on L1428's step 1 `choices`.
3. **`name_misconception` selected without a misconception cue.** T14 picked `name_misconception` (per the move's "three wrong attempts" trigger, lines 495-496) but the fallback fired with no actual misconception named. The `first_misconception_redacted` cue was either empty or not surfaced. Move selection is firing the right move; the rendering layer can't fulfill it.

---

## §5 — Comparison to prior GEO-S5 runs

| Run | Lesson | P1 errors | Move-prompt issue surfaced |
|-----|--------|-----------|-----------------------------|
| run-1 | L1425 (Map Scale) | subject hallucinated ("S3 maths") | scripted voice / preamble |
| run-2 | L1451 (Hydrological Cycle) | 4× incomplete questions + diagram references with no diagram | figure_ref enforcement |
| run-3 | L1438 (Define Weathering) | topic switch mid-turn | open_question stickiness |
| run-4 | L1440 (Rock Properties) | topic drift on correct answers | grader bare-answer rule |
| run-5 | L1439 + L1438 (combined) | close_topic firing too early on advanced persona | exit-ticket dispatch |
| **run-6** | **L1428 (Six-Figure Grid Refs, NEW)** | **2 hard "correct graded wrong" + 3× incomplete MCQ + 1 soft partial-not-credited** | **(R1) worked_example stacked-second-question; (R2) scaffold_hint should credit partials; (R3) explain-vs-worked_example help-request routing; (R4) preamble LO-leak strip; (R5) pose_question MCQ-without-options self-check** |

The recurring `open_question` silent-pivot pattern is now observed across runs 3, 4, and 6 — three lessons, three personas, same symptom. The two-phase commit guard described in CLAUDE.md is the right architecture, but its enforcement of "only one `open_question` live at a time" is being bypassed by the worked-example move's stacked-prompt habit (R1) and by the bank-slot follow-up renderer (engine §4.1).

The `WORKED_EXAMPLE` move's labelled-subgoals delivery (T16) is a clear strength worth preserving — it's the first time across six runs that a struggling student got a textbook walked example with numbered subgoals on the actual visible problem. R1's restriction (one practice prompt, no stacked MCQ) should preserve that win while removing the downstream P1s.
