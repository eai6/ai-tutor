
## Cycle 1
- Diagnosis: timeouts (deepseek/gemini/qwen-next) = turn-budget artifact (10-step lessons capped at 12-15 turns). kimi 12 + qwen-next 3 deadlocks = Vertex 429 rate-limits (NOT thinking-model/repetition), engine returns None -> _FALLBACK_REPLY -> deadlock.
- Fix A (user-approved opt 1): raised max_turns to step_count+8 on 62 non-efficiency multi-turn scenarios; left speedrun/short_session tight.
- Fix B: transient-error retry (429/503/529/conn) in _call_llm, both Anthropic + generate_with_tools paths. Toggle SIMPLE_TUTOR_TRANSIENT_RETRY. Also hardens prod vs Anthropic overloads.
- glm: no action (ceiling). Verified: 11 retry unit checks + budget lint clean + 424-test suite (2 stale pre-existing fails).
- Baseline (cycle 0, n=20 seed5): glm 18, deepseek 15, gemini 7, qwen-next 7, kimi 3 = 50/100.

## Cycle 2
- Cycle-1 result: deadlocks 15→0 (transient retry worked), pass FLAT 50→50 (deadlocks converted to timeouts/rubric-fails).
- Diagnosis: 26/39 remaining timeouts = struggling personas on long lessons hitting even the step+8 budget (need ~2.5-3 turns/step). 13 = intentional efficiency scenarios.
- Fix (user opt 1): persona-aware budgets — struggling personas step*3, average step*2, capable step+8. Raised 60 scenarios (non_responder 24, error_prone 15, struggler 8, avg 9, probe 4). Efficiency left tight. Lint clean.
- Deferred: gemini rubric error-localization (low-confidence prompt tuning, revisit if budget doesn't move it); kimi 3 residual 429 (sustained Vertex overload, infra-bound).
- glm: no action (ceiling).

## Cycle 2 RESULT
- Persona-aware budgets + sim/judge transient retry. All 5 valid after re-running gemini+kimi (overload-wiped in first attempt; 40 retries rode through on re-run, 0 err).
- Board vs cyc1: glm 15=, deepseek 15→16, gemini 5→4, qwen-next 8→11, kimi 7→10. TOTAL 50→56 (+6).
- Timeouts cut broadly (persona budgets working). Remaining dominant failure: max_turns, now model-specific — gemini 11 (desync: poses over unanswered), kimi 7 (thinking-model slowness), qwen-next 6. Plus rubric-quality on completed sessions.

## Cycle 3
- Target (user pick): gemini desync — it orphaned an unanswered in-flight question 161x in cycle 2 (55% of poses), swapping the question out from under the student -> 8 timeouts.
- Fix: anti-desync guard in handle_pose_question — block a new pose while a prior question is UNANSWERED (attempt_count==0); allow a pivot after a wrong attempt (>=1). Family-agnostic. Toggle SIMPLE_TUTOR_ANTIDESYNC. Verified: 3 guard tests + 76-test suite (2 stale).
- sim/judge transient retry now live -> expect no overload wipeouts.
- glm/deepseek: ceiling, no action. kimi thinking-slowness: deferred.
- Board to beat (cyc2): glm 15, deepseek 16, gemini 4, qwen-next 11, kimi 10 = 56.

## Cycle 3 (3-model focus: gemini, kimi, qwen-next; glm+deepseek dropped at ceiling)
- Improvement: anti-desync guard (blocks posing over an UNANSWERED in-flight question; allows pivot after a wrong attempt). Targets the shared orphan bottleneck: gemini 55% of poses orphaned, kimi 21%, qwen 15%. Verified: 3 guard tests + 76-suite (2 stale).
- sim/judge transient retry now live (no overload wipeouts expected).
- 3-target board to beat (cyc2): gemini 4, qwen-next 11, kimi 10 = 25/60.

## Cycle 3 (3-model) RESULT + Cycle 4
- Anti-desync guard: gemini 4->8 (orphans 161->23!), kimi 10->12, qwen 11->9. Total 25->29. BUT gemini gained 3 deadlocks — guard too strict: blocked the tutor from pivoting when student said "idk" (attempt_count stays 0), trapping it re-posing.
- Cycle 4 fix: intent-gate the anti-desync guard — block only when student ATTEMPTED an answer (intent answer/answer_or_other); allow pivot when they DECLINED (idk->non_engagement). Verified: 4 guard tests + engine suite. Expect gemini's 3 deadlocks -> progress.
- Deferred: qwen rubric quality (5 completed-but-failed, low-confidence prompt tuning); kimi timeouts (thinking-model over-explaining to non-responders, hard).

## Cycle 5 (prompt tuning, 3 targets)
- Gave kimi its OWN prompt variant (KIMI_TARGETED_RULES_XML, lean/thinking-model: affirm+advance, no over-probe/second-guess, name specific error + one hint, pose concrete Q to non-responders, coherence). Base control untouched.
- gemini (GEMINI_TARGETED_RULES_XML): +anti-re-teaching, +coherence, +anti-spiral (simpler rung), +sharper error-localization.
- qwen (MARKDOWN_BLOCK_0_TEMPLATE): +brevity/no-info-dump, +coherence, +never-reveal-answer, +catch-every-wrong.
- Targets systematic rubric bleeds (all 3, n=20): self-contradiction 0.40, mistake-recognition 0.46, templating; + each model's signature.
- Board to beat (cyc4): gemini 5, qwen-next 8, kimi 9 = 22.

## Cycle 5 RESULT (prompt tuning)
- gemini 5->9, kimi 9->11, qwen 8=. TOTAL 22->28 (+6). Arc: 25->29->22->28.
- Targeted rubric items moved: over-probe 0.23->0.63 (kimi variant), info-dump 0.53->0.70 (qwen brevity), re-teach 0.33->0.42 (gemini), self-contradiction 0.41->0.45. FLAT (the ceiling): error-localization 0.49, mistake-recognition 0.51 — models under-follow "name the specific error".

## Cycle 6
- Changes since cycle 5: anti-repetition guard (31e1985 — streak-based force-advance on
  re-asks of already-correct questions), MCQ-fabrication fix (41cdfb1 — short_numeric
  enabled so numeric math questions aren't forced into fabricated MCQs), trajectory judge
  told how the session ended (29a19bc).
- Board: gemini 10, kimi 9, qwen-next 7 = 26/60.
- Bottleneck analysis on this cycle (evals/reports/multi_turn_bottlenecks_2026-07-18.md):
  dominant failure = prose-question/slot-question desync (model's visible question ≠
  graded slot question) → ignored correct answers, 21-30-turn sessions, "logically
  consistent" low-scored 26x across the three models. Secondary: engine vocabulary
  leaking into student text, empty-reply placeholder promising a question it never
  shows, re-asks of already-correct questions, broken fixture content (float-noise
  stems, statement-MCQs with lost option texts).

## Cycle 7 (bottleneck fixes) RESULT
- Fixes: (B1) slot is the single source of truth for the visible question — divergent
  trailing prose questions stripped, slot rendered server-side; same-turn pose repair
  after a correct verdict (Call 2 forced to register the next question, non-Anthropic
  families, non-last step); NO-IN-FLIGHT note engages with the student's answer instead
  of dismissing it. (B2) first re-ask of an already-correct stem rejected with
  corrective feedback. (B3) private-note markers on tool results + deterministic
  engine-vocabulary scrub. (B4) slot-aware empty-reply placeholder. (B5) fixture
  repairs: float-noise rounded (root cause fixed in parametric_renderer._sample_one),
  12 statement-MCQs re-authored; lint rules added. (B6) run header records tutor_model.
- Board vs cyc6: gemini 10->17, kimi 9->13 (3 Anthropic-overload errors re-run: 2 pass
  1 fail, merged), qwen 7->11. TOTAL 26->41 (+15, best cycle to date).
- "Logically consistent" low-scores 26->9 (gemini 0); max_turn_count failures 15->10;
  avg session turns down for gemini (13.7->9.3) and kimi (15.0->11.8). kimi absorbed
  177 Vertex 429s via transient retry (slower wall-clock, zero rate-limit deadlocks).
- Regressions: kimi 2 deadlocks from vocab-scrub leaving orphan '[' when kimi narrates
  tool JSON in text — fixed same day (punctuation-only residue scrubs to empty →
  slot-aware placeholder); re-check: long_session_capable_001 flipped back to PASS.
  qwen 2 = model-level grading confusion (insisted a correct MCQ answer was wrong),
  pre-existing class, not fix-related.

## Cycle 8 (dispatch/grading fixes) + Cycle 8b (salvage) RESULT
- Cycle-8 fixes from the cycle-7 transcript review: (1) semantic dispatch order —
  record_answer grades the question the student SAW (existing slot first; a fresh
  same-turn pose never grades the current message; late registration still poses
  first); (2) MCQ value→letter grading (unique option-value match beats the
  positional "2→B" map — kills the 8-turn "which letter?" nag); (3) options render
  even when the stem is visible; option letter-prefixes stripped at pose + render
  ("A) A) 11"); (4) pose-time validation of ungradable slots; (5) placeholder
  phrasing rotation; (6) attempt>=3 pivot guidance in the in-flight block;
  (7) hint-discipline line in the verdict block.
- Cycle-8 first run: gemini 10->17->18 (best ever, 3 rejections only, logic-lows 0).
  BUT kimi 5/20, qwen 4/20 — the strict pose validations REJECTED their malformed
  poses (kimi 88, qwen 91 — mostly catalog MCQs posed without options) and neither
  model recovers from corrective errors: rejected pose -> no slot -> narrated
  tool-JSON text scrubbed to empty -> verbatim "Let's keep going." placeholder ->
  25 deadlocks. Lesson: validations need salvage, not rejection, for models that
  can't self-correct.
- Cycle-8b salvage: optionless mcq + letter ref -> adopt options from catalog by
  stem match; + numeric/text ref -> convert to short_numeric/short_answer;
  short_numeric + letter ref + options -> convert to mcq. Also: tool-JSON
  narration scrubbed regardless of vocab; neutral placeholder rotates.
- Cycle-8 final board: gemini 18 (8a) + kimi 14 + qwen 15 (8b re-legs) = 47/60
  (78%), best cycle to date; ZERO deadlocks, zero errored. Remaining failures:
  modest turn overruns reaching exit_ticket (9 of 13), residual rubric quality.

## Cycle 9 (letter coherence, kimi+qwen legs) RESULT
- Cycle-8b transcript review: dominant class = slot reference letter disagreeing
  with the displayed option order. Root cause: the prompt's MCQ letter-rotation
  discipline applied to CATALOG questions (fixed option order) re-lettered their
  references; cycle-8's salvage compounded it by adopting catalog options while
  keeping the model's letter. Canonical failure: Beau Vallon — correct answer B
  graded wrong six times while the tutor invented coverage-area counterarguments.
- Fixes: (1) catalog letter coherence — for a catalog-stem match the catalog's
  correct TEXT is the authority; the reference letter is derived from where that
  text sits in the options actually shown (rotated orders handled); salvage now
  adopts catalog letter with catalog options; (2) rotation rule scoped to
  self-authored questions in all three prompt variants; (3) auto-pose fallback —
  a correct verdict can no longer end a turn with no question in flight (next
  pool question posed server-side; catalog authority; eval families only).
- Board: kimi 14->18 (logic-lows 4->0; only 2 marginal turn overruns remain),
  qwen 15->14 (noise band; all 6 fails are turn-budget overruns, 0 deadlocks).
  2-model total 29->32/40. Full board w/ gemini 18 (c8): 50/60 (83%).
- Interventions in-sweep: 121 catalog options+letter adoptions, 29 auto-pose
  fallbacks, 0 deadlocks, 0 errored.
- Residual: turn-budget overruns on non-responder/struggler long lessons (the
  budget itself may deserve review now that sessions are pedagogically clean),
  qwen residual self-consistency (3 logic-lows).

## Cycle 10 (qwen prompt-variant iteration, qwen leg only) RESULT
- Cycle-9 qwen signature (all six fails were turn overruns, wasted by): hint
  micro-step loops (0.70+0.30 asked ~8x; correct "1"/"yes" rejected), precision
  pedantry (33.3% for 1/3 rejected), re-asking answered questions (reworded past
  the verbatim repeat guard), reveal-in-hint ("That's option C..."), templated
  openers (which the variant's own worked example seeded), one authored question
  with an impossible premise.
- Qwen Markdown Block-0 iteration (other families untouched): spent-micro-step
  rule + worked example; accept-reasonable-precision; answered-question-is-
  finished; hint-without-revealing worked example; authored-number sanity check;
  opener variety + de-seeded "No worries" from the non-answer example. Pinned by
  5 substring tests (suite 491 green).
- Board: qwen 14->16 (all-time best; avg turns 13.4->10.5; logic-lows 3->1).
  Full board: gemini 18 + kimi 18 + qwen 16 = 52/60 (87%).
- Residual: one deadlock traced to a POOL question with a broken template
  ("probability of success is 5", inconsistent reference) — the number-sanity
  rule makes qwen question the premise but the grader holds the broken ref.
  Fixture/template content bug (float-noise family), next content-lint target:
  flag stems where a probability parameter renders > 1. Remaining fails
  otherwise: 2-4-turn budget overruns on clean sessions (budget review).

## Cycle 11 (qwen: content fix + budget consistency) RESULT
- Attended to cycle-10's two named qwen bottlenecks:
  (1) fixture item 595/2 — template dropped its denominator ("probability of
  success is 5" for 5/9); stem+explanation fixed; two lint rules added (bare
  probability > 1 in a stem; p/p_denom parameters whose fraction is absent).
  (2) dataset consistency — 9 struggling-persona scenarios on 10-step lessons
  still carried pre-cycle-2 budgets of 12 (formula: steps*3=30; 21 peers
  already at 3.0) → aligned; 12 scenarios with max_turns == budget while
  expecting a clean ending (full-budget sessions censored at the sim cap) →
  caps raised to 30, budget assertions untouched.
- Board: qwen 16->17. Honest decomposition vs old budgets: score would be 16 —
  the +1 net is budget-attributed (baseline_1141_09 passes at 19t vs old 12);
  but the CONTENT fix converted the 1144_07 deadlock into a clean 23-turn
  exit-ticket session with ZERO low rubric items (fails only its 15 budget),
  and two previously-failing scenarios now pass at/below their OLD budgets
  (refusal_chain_1141_12 at 12t, struggler_001 at 11t) — genuine improvement.
- Full board: gemini 18 + kimi 18 + qwen 17 = 53/60 (88%).
- Residual: the ratio-1.5 budget band (budget 15 on 10-step struggling
  scenarios, e.g. 1144_07 at 23t clean, 1142_13) was deliberately left
  untouched pending an owner decision on the formula; straight_line_1145_16
  keeps 4 low rubric items (real quality residue, next prompt/judge look).
