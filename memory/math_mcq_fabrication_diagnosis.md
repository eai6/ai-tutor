# Math underperformance + max_turns bottleneck — root cause (cycle 5, 2026-07-18)

Diagnosis of two multi-turn eval bottlenecks that turned out to share one root
cause. Investigated on cycle-5 data (`offline_eval/multi_turn_results/fixcheck/_cycle5`,
3 models × n=20 seed-5: gemini-2.5-flash, kimi-k2-thinking, qwen3-next-80b).

## Symptoms

- **Math pass rate 5/30 vs geography 23/30.** Pass gate is rubric mean ≥ 0.6
  (`max_turns` is an *allowed* end-reason, so timing out is not itself the fail —
  see `evals/runner.py:429` `passed = deterministic_passed and rubric_passed`).
  Math rubric mean 0.53–0.57 vs geography 0.71.
- **20/60 sessions hit `max_turns`**, 15 of them math. Math averages ~21 tutor
  turns/session vs geography ~12 — the ~1.8× turn bloat holds for every model
  even on sessions that finish.

## Root cause (ONE defect, both symptoms)

**In `mcq`-only mode the tutor is forced to fabricate multiple-choice options for
math's open-numeric questions, and re-permutes the letters every turn.**

The judge reasoning on the worst math items is unanimous — the tutor *rejects
correct answers*: "saying 'Not quite' to correct answers… and 'Got it' to the
identical answer in other turns"; "first saying C is wrong, then saying C is
right"; "invents different option sets across turns". Raw transcript
(`baseline_full_session_error_prone_1141_09`, qwen): the student computes
5/6 ≈ 0.833 correctly on every turn, but the tutor re-issues options as
`C) 0.833` → `A) 0.83` → `B) 5/6` → `C) 5/6`, so the student's value never
matches the letter being graded. It even melts down in-dialogue: "the system says
C is correct… but 5/6 is clearly option B… there's an error in the system's
reference." The die question it posed **is not authored anywhere in the lesson** —
the tutor invented the question *and* its options.

### Why only math

`_allowed_tutoring_types()` (`apps/tutoring/simple_tutor/tools.py:155`) defaults
to `('mcq',)`. That default does two damaging things:

1. **Narrows the `pose_question.question_type` enum to `["mcq"]`**
   (`prompts.py:_narrow_pose_question_types`) → the tutor *cannot* pose a numeric
   question; every question must be MCQ, options invented.
2. **Drops the authored `short_numeric` question from the pool**
   (`tools.py:226-231`, `build_question_pool` Source 1 skips it when its type is
   not in the allowlist) → the tutor never sees the real lesson question, so it
   fabricates its own.

Lesson data confirms the split: **79% of math practice steps are open-answer**
(`short_numeric`/`free_text`/`true_false`, `choices=None`); **0% of geography
practice steps are open-numeric** — all MCQ or true/false with stored `choices`.
So geography just relays fixed authored options (letter-grading works); math
hands the tutor a numeric question it is forbidden to pose as numeric.

### Why this also causes the timeouts

A practice step completes after N correct answers. When the tutor rejects the
student's correct answers (moving-target letters), that condition never fires, so
the session loops to the turn cap. Same defect → both low rubric AND timeout.

## Two secondary max_turns contributors (independent, minor)

- **kimi-k2-thinking is turn-inefficient**: 10/20 timeouts, incl. geography.
  On *finished* geography sessions its median is 14.5 turns vs qwen 8, gemini 10 —
  a thinking-model turn tax, not the MCQ bug.
- **Tiny speedrun caps (artifact)**: 8/20 timeouts hit at exactly 6 turns —
  `speedrun_*`/`short_session_*` scenarios intentionally left with tight budgets;
  several pass anyway (rubric ≥ 0.6). Benchmark calibration noise, not a defect.

## Why the mcq-only default exists (the tension)

`short_answer` (embedding/verifier tier) produced *partial* verdicts that didn't
trigger step-advance — the 2026-05-28 "session 424 stuck on step 0" incident.
mcq-only was the blunt fix. But it over-corrected: **`short_numeric` is
deterministic** (routes to `_grade_math`, correct/incorrect via numeric equality
— `grader.py:106`), exactly as clean as MCQ letter-match. mcq-only disabled the
safe numeric type along with the problematic free-text one.

## The fix (implemented this branch)

1. **Enable `short_numeric` for the benchmark** (keep `short_answer` OFF): the
   eval entrypoint `run_eval.handle()` sets
   `TUTORING_QUESTION_TYPES=mcq,short_numeric` via `os.environ.setdefault`. Scoped
   to eval runs only (production serves via gunicorn, not `run_eval`), covers both
   local and Colab launches, still overridable. This restores the authored numeric
   question to the pool AND lets the tutor pose it as free-response.
2. **Prompt guidance** (base `_BLOCK_0_TEMPLATE` + qwen `MARKDOWN_BLOCK_0_TEMPLATE`
   + `GEMINI_TARGETED_RULES_XML`): "match each question's format to its answer —
   numeric/computed answer → `short_numeric` (student types the value; no A/B/C/D);
   fixed labeled choices → `mcq`. Prefer the authored `<question_pool>` question in
   its authored type; converting a numeric question to invented MCQ makes the
   option letters unstable turn-to-turn." A prompt rule alone is INERT under
   mcq-only (the enum forbids `short_numeric`) — the config flip is what makes the
   prompt fix able to fire.

## Production caveat (not changed here — needs its own validation)

Production likely also runs mcq-only (`infra`: `TUTORING_QUESTION_TYPES` set only
if the Pulumi `tutoring-question-types` config is present). If so, **real math
lessons suffer this same fabrication bug in prod.** Enabling `mcq,short_numeric`
in prod is the same low-risk flip (short_numeric is deterministic), but do it as a
separate change with a prod-DB dry-run + a live math-session check first.

## Cycle 6 result (2026-07-18, fix applied, 3 models × n=20 seed-5, errored=0)

**Mechanism decisively fixed, but no net pass-rate win (within noise).**

- **Fabrication collapsed**: math option-bearing tutor turns 69% → **15%** (453/652
  → 86/543). Geography ~held (74%→60%, still authored MCQ — fix doesn't touch it).
- **Every fabrication-driven math rubric item improved**: reaches-exit-ticket
  0.28→**0.60** (+0.32), logically-consistent 0.35→0.43, mistake-handling
  0.44→0.50, advances-when-demonstrated 0.20→0.28, answer-not-revealed 0.64→0.67.
- **Math end-reasons**: timeouts 15→9, exit_ticket 14→19 (the mis-grade loop that
  blocked advancement is largely gone).
- **Math rubric mean up for all 3**: gemini 0.51→0.61, kimi 0.61→0.64, qwen
  0.52→0.54. **Math pass 5/30→7/30** (+2, muted — math sits right at the 0.6 gate).
- **Geography pass 23/30→19/30, overall 28→26** — most likely n=10 noise (±~30pp;
  fix doesn't change the geo path), concentrated in kimi (7→5) + qwen; can't fully
  rule out that the added base-template rule mildly hurt kimi (thinking model,
  dislikes prompt bloat — kimi overall 11→9).

**Why quality didn't convert to passes**: math rubric now clusters AT the 0.6
threshold, so the +0.03–0.10 lift crossed few sessions over the line. Remaining
blockers: (a) residual 15% fabrication, (b) near-threshold quality + turn cost.
Next levers: tighten the residual fabrication rule; consider putting the
match-format rule behind kimi's lean appendix instead of the shared base;
re-examine strict per-scenario max_turn_count assertions for error-prone math.

Related: [[eval-benchmark work]]. Pairs with the cycle-5 prompt-tuning results.
(Living doc — spans the fix commit plus cycle-5/6 results; no single Commit anchor.)
