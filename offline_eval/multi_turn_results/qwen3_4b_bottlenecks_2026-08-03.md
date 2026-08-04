# qwen3:4b multi-turn bottleneck analysis — mt50 board

Source: `offline_eval/multi_turn_results/mt50/qwen3_4b.{json,log}`
(50 scenarios, run 2026-07-23/24 at `86b9f3a4`, engine `simple_tutor`,
tutor `local_ollama/qwen3:4b`). Analysis + fixes 2026-08-03, motivated by the
decision to power the offline (Jetson) simple-tutor with qwen3-4b.

## Headline

**44/50 (88%)** — all 6 failures are `max_turn_count`; zero deadlocks, zero
errors, every session reached the exit ticket or the turn budget. The
protocol layer (tool compliance, slot management) is no longer the
bottleneck for this tag — 53 Call-2 repairs across 580 turns (4.7%), 0
`record_answer`-on-empty-slot. What remains is **pedagogical quality and
turn economy**, which is what the fixes below target.

Rubric tail (applicable items < 0.7): 74 low items, clustering as:

| # | Cluster (rubric items hit) | Count | Root cause |
| --- | --- | --- | --- |
| 1 | Logical self-contradiction | 14 | RC-1, RC-2, RC-6 |
| 2 | Wrong answer affirmed / right answer denied | 10 | RC-1 (grader), RC-7 (one-call) |
| 3 | Final answer revealed | 9 | RC-4 |
| 4 | Robotic/templated phrasing | 6 | RC-3 |
| 5 | Incorrect/irrelevant guidance | 5 | RC-2, RC-6 |
| 6 | Error not located specifically | 5 | RC-1 (false corrections misattribute) |

## Root causes → fixes

### RC-1 — Grader false negatives on percent/decimal scaling (highest impact)

The model poses micro-steps like "What's 1.00 as a percentage?" with a
bare-number reference (`'100'`, `'30'`). Students answer in probability form
(`1`, `0.3`); with no `%` sign on either side, math-verify correctly treats
30 ≠ 0.3 and the grade comes back INCORRECT. 5 of the run's 100 incorrect
verdicts were exactly a ×100 scaling of the reference (sessions 4, 16, 24,
40 — all probability lessons 1141/1144/1145). Each one spiralled: false
"Not quite — the question asks for a percentage" corrections, hint ladders
on already-right answers, then the tutor accepting an equivalent answer
later — the judges scored these as self-contradiction, second-guessing, and
"error not located".

**Fix (engine, `grader.py`)**: pass 2b in `_grade_math` — accept
`student == ref/100` when the question/reference context mentions
percent/probability (EN+PT) and the pair sits in (0,1] × (0,100]. One
direction only (student-in-decimal vs ref-in-points); range + context +
direction gates keep 6-vs-600 and "1% of 100"→"100" rejected.
**Fix (prompt)**: equivalence bullet now names 0.3 = 30% explicitly and bans
demanding a format change as if wrong.

### RC-2 — Answers acknowledged never, or about the wrong question

62 tutor turns responded to a graded answer with zero acknowledgement — the
reply opened directly onto the next question. Related: off-by-one feedback
("Exactly — oxidation…" as feedback for the *previous* question; "Co-interior
angles add up to 180°" after an alternate-exterior answer) in ≥5 sessions.
48 `orphan_in_flight` events show new poses replacing ungraded slots on
non-answer-intent turns.

**Fix (engine)**: `_align_reply_polarity` now *prepends* a rotated
verdict-consistent ack when the reply head carries neither polarity.
**Fix (prompt)**: GRADE-mode rule — feedback sentence must reuse the graded
question's numbers/key words; never describe a different question's rule.

### RC-3 — "Exactly —" template chains

Up to 10 of 13 tutor turns in one session opened "Exactly —". The prompt's
"vary your affirmations" is read past by a 4B.

**Fix (engine)**: `_rotate_repeated_ack` — when the opener lexeme matches
either of the last two tutor turns (correct verdicts only), the leading ack
phrase is swapped for a rotated opener. Deterministic; no-op on fresh
openers.

### RC-4 — Answer reveals on help/clarification turns

"Let's calculate: 1 − 0.8 = 0.2", "180 − 113 = 67°", "the given angle is
81°, so x must be 81° (option B)". Most landed on turns with **no verdict**
(clarification/help intent → no `record_answer`), which the old
incorrect-only `_filter_reveals` gate never inspected.

**Fix (engine)**: `_filter_reveals` now also runs on no-verdict turns with a
live slot (correct-verdict turns still skip — the slot then holds the newly
posed next question). **Fix (prompt)**: "a hint that computes the question's
own numbers has answered the question — work hint examples on DIFFERENT
numbers."

### RC-5 — Grading metadata leaked to the student

"The reference answer of 0.166667 (1/6) suggests this question might be
testing a different scenario" — spoken to a 13-year-old, twice.

**Fix (engine)**: `reference answer/value` added to the engine-vocab scrub
(sentence-level drop). **Fix (prompt)**: never say "reference answer"; a
mismatched reference means quietly switch questions.

### RC-6 — Verbatim re-asks of questions that never grade correct

The answered-correct anti-repeat guard only knows about *resolved* stems. A
mis-authored catalog question ("What is the probability value?" — the stem
contains its own value, reference `0.166667` doesn't match) was re-posed 3×
because every grade came back incorrect. 12 re-asked stems across the run.

**Fix (engine, `tools.py`)**: stage-1b per-stem pose cap — 2 poses max per
normalised stem per session, whatever the verdicts; the third is rejected
with corrective feedback and `_auto_pose_fallback` covers the turn with a
fresh pool question. **Fix (prompt)**: "skip broken pool questions" rule
(missing numbers / self-contradiction / stem-reference mismatch) — pose a
complete replacement, never invent numbers afterwards, never debate what a
broken question "meant" (the 1144_04 fisherman death-spiral).

### RC-7 — One-call mode writes prose before the verdict exists

Post-mt50, local families default to one-call mode (latency). The reply is
then written before grading; `_align_reply_polarity` fixes contradicting
*openers* but not hint content built on a wrong predicted verdict ("you used
0.65 instead of 0.60" about a right answer).

**Fix (engine)**: `_call1_contradicts_verdict` — when Call-1 prose asserts
the opposite of the grader's verdict, the turn escalates to Call 2 (rewrite
with verdict in hand). Contradiction turns are the minority, so one-call's
latency win survives.

### RC-8 — Turn-budget burn on help-intensive sessions

The 6 failures ran 11–27 turns against 10–20 budgets. Contributors: RC-1
spirals, RC-6 re-asks, and slots idling ≥6 tutor turns because
clarification/help turns never increment `attempt_count`, so neither the
attempt-based pivot (≥4) nor the prompt's pivot guidance ever fired.

**Fix (engine)**: slot-age pivot — `_force_pivot_stuck_slot` now also fires
when the same slot has been in flight for 6 tutor turns (age tracked in
`engine_state`; `InFlightQuestion.posed_at_turn` is always NULL because
nothing sets `_current_turn_id` — left as-is, age counter sidesteps it).

### RC-9 — MCQ letter-shuffle bookkeeping beyond a 4B

The prompt's "roll a fair 1-in-4 pick for the correct letter, track your
last 2 letters over an 8-question window" demands bookkeeping a 4B fumbles
into letter/text mismatches (the cycle-9 Beau Vallon failure class; 2
`ref letter overridden` repairs in this run for catalog stems, no net exists
for self-authored ones).

**Fix (prompt)**: replaced with "letters honest" — write options first, set
`reference_answer` to the letter that actually holds the correct text,
verify agreement, vary letters across questions. No rolling, no windowed
tracking.

## Content defects surfaced (not fixable in engine/prompt — flag for review)

- **Lesson 1144** (probability/expected value): "A problem states: 'The
  probability of catching a fish is 0.6.' What is the probability value?"
  — stem contains its own answer, reference is 0.166667 (mismatch). Also
  "A fisherman casts his net 100 times… How many nets do you expect to be
  damaged this season?" — no damage probability given.
- **Lesson 1139** (angles): MCQ with stem "One alternate interior angle is
  70°" whose options (116/81/99/90) don't include 70.
- **Lesson 1145**: balance-problem MCQ whose options (0.05/57/20/63) are
  nonsensical as "equations".
- **Lesson 1465** (grid references): tutor explanation error on 456734
  subdivisions was model-authored, but the six-figure item was re-asked from
  the pool after being answered.

## What was deliberately NOT changed

- `QWEN_BLOCK_0` stays defaulting to the full template; the compact variant
  remains a measurement arm (compliance-neutral, 43% faster — wire it only
  after `measure_call_compliance.py` on real turns).
- No multi-agent decomposition, no new LLM calls added anywhere — every fix
  is a deterministic net, a grader pass, or prompt text (CLAUDE.md
  conservative bias).
- The (rare) reverse-direction percent equivalence (ref 0.3, student "30"
  with no % sign) stays INCORRECT — unobserved in the run, and accepting it
  would credit real errors like "100" for "1% of 100".

Regression tests: `apps/tutoring/simple_tutor/tests/test_mt50_qwen4b_fixes.py`
(26 tests, Q1–Q8 map to RC-1…RC-8). Full `apps.tutoring.simple_tutor` suite:
608 tests OK. Also repaired 9 tests that were already failing on HEAD before
this work: the `config=` kwarg added to `_call_llm` by the model-choice
commit broke the `_fake_call` mocks in `test_stream_filter.py`, and the
always-rendered `<reply_length>` block (c16192f) changed the block counts
`test_prompts.EndToEndShapeTest` asserts.

Validation replay: `_scrub_engine_vocab` over all 580 persisted mt50 tutor
turns changes exactly the 2 turns with the reference-answer leak and nothing
else.

## Addendum — kiosk session 74 fixes (same day)

A live kiosk session (lesson 1463, `local_ollama/qwen3-4b-jetson`) surfaced
two failures the mt50 fixes hadn't covered:

- **K1 — bare letters waved off as non-answers.** The model called
  `record_answer('')` on a bare "a" (and again on "c"), and both server nets
  treated *any* record_answer call as "the model made a grading judgement" —
  the answers vanished and the same question was re-asked verbatim, twice,
  with zero feedback. Fix: only a call that actually RECORDED counts;
  `_auto_grade_fallback` now grades the raw message on strict-answer-intent
  turns with an empty call, and `autograde_bare_answer_if_clear`'s
  `already_recorded` gate keys on a real verdict (`_turn_verdict`).
- **K2 — incoherent wrong-answer turn.** "You picked C — that's not quite
  right. [reveal of option B's content]. Here's the next one: …" — graded
  incorrect, revealed the answer by paraphrase, and swapped the question in
  the same reply. Fixes: (a) `handle_record_answer` flags
  `_graded_incorrect_this_turn`; `handle_pose_question` rejects a same-turn
  pose over that slot until attempts ≥ 3 (`<pivot_guidance>` territory);
  engine-initiated pivots bypass via `engine_initiated=True`. (b)
  `_filter_reveals` gained a paraphrase net: an MCQ hint sentence covering
  ≥70% of the correct option's distinctive tokens (prefix-matched) is
  redacted — catches "has a large ratio (like 1:1,000,000 or bigger)…"
  without the letter ever being named. (c) one-call mode escalates to Call 2
  whenever a pose was rejected, so Call-1 prose never announces a question
  that failed to register.

## Addendum 2 — qwen_mt30 board results and the pre-grading fix (2026-08-03, same day)

The rerun (`qwen_mt30/qwen3-4b-jetson.{json,log}`, 30 v1 scenarios, two-call,
SHA 5bb9dce) scored **15/30 (50%)** — far below mt50's 88%. The regression is
real on identical scenarios (`average_geo_direction_001`: 0.95/5 turns →
0.51/18 turns) and its mechanism is now measured:

**Call 1 skipped the expected tool on 350 of ~450 turns (78%)** vs mt50's
4.7%, and the rate is FLAT by turn position (80/84/75/71% across session
quartiles) — so it is not context growth, not prompt size, not `num_predict`
truncation. The tag itself is a weak tool-caller: `qwen3-4b-jetson` builds
from **Qwen3-4B-Instruct-2507**, which local probes already measured at 10/20
on POSE tool calls (rung-1) and which the Jetson kiosk showed skipping tools
on 4 of 5 turns. mt50's `qwen3:4b` (July 23, unpinned Ollama) predates the
registry's re-point to the 2507 checkpoints — **the mt50 board's 88% was
measured on different weights than the Jetson ships.** The mt50-vs-mt30 delta
is a model-identity confound, not (only) a scenario or fix effect.

Downstream cascade, visible in every failed transcript: with grades landing
late (Call-2 repair) or never, `answered_correct`/pool-exclusion state
diverged from the conversation → verbatim re-asks (65 repeat-pose
rejections), auto-pose churn (128), interleaved questions, and — decisive
for the rubric — **no reply was ever written with the verdict in hand**
(Call-2 emits its repair tool call and text in one response, before the
grader runs), so the run bled points on affirmed-wrong/denied-right and
self-contradiction (21 and 19 low rubric items).

**Fix: server-side pre-grading.** The grader never needed the model — it
already grades raw messages in `_auto_grade_fallback`. On eval-family GRADE
turns with strict `answer` intent (bare letter / number shapes), the server
now grades BEFORE Call 1 (`engine._pre_grade_answer`), refreshes the
slot/mode/pool, and injects the verdict into the prompt as a `<last_grade>`
block with query-adjacent instructions. Consequences:

- The model's prose is verdict-aware from the first token; polarity nets
  become backstops instead of the primary author.
- A grade can no longer be lost to a skipped tool; anti-repeat and pool
  exclusion see every verdict.
- Wrong answers arm the same-turn hint guard before the model can pose over
  them; correct answers clear the slot so POSE forcing lines up.
- A model-issued `record_answer` on a pre-graded turn is refused
  ("already graded") so one answer can never double-bump `attempt_count`.
- The streaming gate sees the verdict at Call-1 time, so Call-1 prose can
  flush immediately (previously withheld pending the grade).

Production/Anthropic keep the model-driven flow untouched (same
`_FORCE_POSE_EXEMPT_FAMILIES` gate as every other net). `answer_or_other`
and conversational intents still route through the model's judgement.

Tests: `apps/tutoring/simple_tutor/tests/test_pre_grading.py`.

## Addendum 3 — iteration 2 (pre-grading engine) results and fixes (2026-08-04)

`qwen_mt30_it2/` (SHA 658732a): **16/30** — pre-grading fired on all 191
strict-answer turns and record_answer repairs fell 260 → 104, but the score
barely moved and logical-consistency complaints ROSE (19 → 27). The new
failure texture, from the transcripts + log:

1. **Model-authored wrong references, now ENFORCED.** The killer transcript
   (average_math_session_001): "Four angles around a point are 70°, 85°,
   90°, and x°" posed with `reference_answer='175'` — true answer 115. The
   grader marked the student's correct 115 wrong for five straight attempts,
   and the model argued with its own verdict in the visible reply ("So why
   is the grader saying it's wrong? Wait — no"). Before pre-grading, bad
   self-authored references were merely ignored; the authoritative verdict
   turned them into false corrections. Same family: numeric refs on
   compass-word stems (ref '135' vs student "SE").
   **Fix**: `tools.solve_authored_stem` + `verify_authored_reference` — a
   deterministic solver for the stem templates the model actually writes
   (angles around a point / straight line, bare arithmetic, vertically
   opposite, bearing↔compass, complement probability); a recognised stem's
   reference is recomputed at pose time and overridden on disagreement
   (compass answers also retype short_numeric → short_answer). Unrecognised
   stems are left alone. Catalog references untouched.
2. **Prose micro-steps graded against the stale slot.** The tutor asked
   "what is 360° − 175°?" in prose (poses are blocked mid-ladder by the
   same-turn hint guard), and the student's correct "185" was pre-graded
   against the slot's main-question reference — twice.
   **Fix**: stale-slot guard in `_pre_grade_answer` — pre-grade only when
   the tutor's LAST visible message re-anchors the slot's stem (same loose
   matching as `_ensure_posed_question_in_text`); otherwise fall back to the
   model-driven flow.
3. **MCQ value-for-letter answers rejected** ("360 degrees" vs option
   "360°"). **Fix**: `_option_number` strips spelled unit words, so the
   existing option-text value match resolves the letter.
4. **Two questions in one reply on pivots** — `_force_pivot_stuck_slot`
   appended its bridge after a reply that already ended in a prose question.
   **Fix**: strip the trailing prose question before bridging (same repair
   `_auto_pose_fallback` already used).
5. **Grader-speak leaked to the student** ("the grader") — added to the
   engine-vocab scrub.

Also: the Colab notebook now carries a second arm — the BARE `qwen3:4b`
registry tag under the mt50 configuration (no Modelfile, family-default
profile) as the model-identity control. Caveat printed in the notebook: the
registry re-points that tag over time, so it answers "what does the mt50
configuration score today", not necessarily "what did mt50 pull in July".

Tests: solver/correction/guard cases appended to `test_pre_grading.py`.

## Addendum 4 — iteration 3: the it2 fixes misfired (2026-08-04)

`qwen_mt30_it3/` (SHA 5dad170): **11/30** — worse than it2's 16. Both it2
mechanisms were the cause, convicted by their own log lines:

1. **The solver ran on MCQ poses** (no question-type gate) and on stems it
   should have refused. `authored ref corrected 'A' -> '0'` on the
   session-OPENING pose in four sessions: the greeting mentions "bearings…
   clockwise from north", the compass→bearing family fired on word
   co-occurrence, and a positional MCQ letter reference became the value
   '0' — every answer graded wrong from turn 1. Also matched numbers inside
   FEEDBACK prose the model packs into question_text ("Exactly — a bearing
   of 225° corresponds to Southwest…" → overrode ref '45'), and mis-solved
   algebraic angle variants ("the remaining angles are equal").
   **Fixes**: verify_authored_reference gates to short_numeric/short_answer
   and never touches letter references; the solver refuses greeting/
   feedback-contaminated stems (ack-marker prefix), refuses algebraic angle
   variants, and the compass→bearing family requires the explicit
   interrogative form.
2. **The stale-slot guard over-skipped**: 50 pre-grades were skipped because
   the last tutor turn didn't restate the stem — but most of those were
   plain hints followed by the student re-answering the main question, and
   they fell back into the chaotic model-flow (record repairs 104 → 192).
   **Fix**: the skip now requires an actual COMPETING question — the last
   tutor turn ends with a question-looking paragraph that matches neither
   direction against the slot stem. Plain hints pre-grade again.

**Model-identity control settled.** The bare `qwen3:4b` arm ran ~135 s/turn
(≈10× the jetson arm on the same GPU), completed 0/30 scenarios in 9.4 h,
was OOM-killed, and its visible replies stayed short while the tokens went
to a reasoning channel — today's registry tag is Thinking-2507. mt50's
overnight 88% on 50 scenarios cannot have been this model. The control arm
is retired from the notebook; the jetson tag is the only meaningful subject.

## Rerun setup (Colab)

- Original pre-expansion dataset (90 = 60 single + 30 multi) is now tagged
  `v1`; `run_eval --multi-turn --subset v1` selects exactly the original 30
  multi-turn scenarios.
- `offline_eval/colab_qwen_mt30.ipynb` (generator
  `_make_colab_nb_qwen_mt30.py`): local qwen Modelfile-pinned tags only
  (`qwen3-4b-jetson`, `qwen3.5-4b-jetson`, `qwen3.5-2b-jetson`),
  `TUTOR_CALL_MODE=two` (mt50-comparable), branch `offline-optimization`.
- `run_matrix.sh` now builds `infra/ollama/Modelfile.<tag>` tags via
  `ollama pull <base>` + `ollama create` instead of pulling (bare tags were
  deprecated in b5b7a68).
