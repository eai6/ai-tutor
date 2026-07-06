# Multi-turn evaluation of the top-15 (Improved Eval 3) — design

**Date:** 2026-07-06
**Status:** approved design → implementation
**Owner:** offline-eval workstream (continuation of single-turn Improved Eval 2)
**Scope:** Qwen + Gemini families only (per standing project constraint).

---

## 1. Motivation

Improved Eval 2 scored 29 models on **single-turn** scenarios (60 YAML cases, one graded
tutor turn each). Its top-15 (11 Qwen + 4 Gemini) is the shortlist for a non-Anthropic
tutor. But single-turn only ever exercised the engine's **GRADE** path (an in-flight
question was pre-seeded, the student's one answer was graded). A real pilot session is a
**full 5E trajectory**: opening warm-up → pose → teach → grade → advance → exit ticket.
The POSE/TEACH/pacing/exit-ticket behaviour of each family's prompt was never measured.

This eval closes that gap: run the same top-15 through **multi-turn** sessions and see which
models actually *conduct a session*, not just grade one turn.

## 2. Load-bearing findings (verified against the code)

1. **The multi-turn harness already exists.** 20 scenarios in `evals/dataset/multi_turn/`,
   driven by `apps/tutoring/student_sim/` (a persona-LLM plays the student; the
   tutor-under-test plays the tutor). Scored by `evals/scorers/trajectory.py` (deterministic
   trajectory verbs) + `evals/scorers/llm_rubric.py::score_trajectory` (whole-session rubric).
   Entry point: `manage.py run_eval --multi-turn` → `runner._run_multi_turn` → `simulate_session`.

2. **Per-family prompts already carry into multi-turn — no rewrite into new files needed.**
   `simulate_session` honors `SIMPLE_TUTOR_ENGINE` (`driver.py:184`), so multi-turn drives the
   same `apps/tutoring/simple_tutor/engine.py` as single-turn and production. `respond()`
   resolves the family from `TUTOR_MODEL_OVERRIDE` and calls
   `build_system_prompt(family=…)` → `family_prompts.build_family_block_0()`. Both
   `start_for_view` and `respond_for_view` route through `respond()`. So Qwen already gets the
   Markdown Block-0 and Gemini already gets XML+targeted-rules across the whole session.
   `apps/tutoring/simple_tutor/family_prompts.py` is the single shared source for both turn modes.

3. **Judge and student-sim are fixed across the sweep**, so they don't bias the comparison:
   - Judge (rubric): Anthropic **Haiku 4.5** @ temp 0 (`DEFAULT_RUBRIC_JUDGE`).
   - Student-sim: **Gemini 2.5 Flash** (`Purpose.STUDENT_SIM`, seeded in migration 0021).
   - Only the **tutor** model varies, via `TUTOR_MODEL_OVERRIDE`.

4. **Grading bug — per-response items judged "across the session."** Each multi-turn scenario's
   BEA-aligned rubric block is phrased *per-response* ("the **response** identifies the
   mistake…"), but `_build_trajectory_prompt` asks the judge to score each item "ACROSS the
   session." Over a 15-turn transcript with a mix of correct and wrong turns, a conditional
   per-response item is ambiguous → the judge can misjudge. Must fix.

5. **Dataset shape (current 20):** personas spread reasonably (capable 5, struggler 5,
   non_responder 3, probe_resistant 3, average 2, error_prone 2) but **math-skewed (13 math /
   7 geo)** and **lesson-1137-heavy (10 of 20)**. All 20 carry rubrics + `pass_threshold`
   0.60–0.65.

6. **max_turns vs lesson length trap.** Lessons: **1137 = 10 steps, 1138 = 10 steps**,
   1463 = 5, 1464 = 5 (all `content_status='ready'`). Completion-expecting scenarios on the
   10-step lessons with `max_turns: 15` and `expected_reason: [completed, exit_ticket]`
   (e.g. `capable_math_session_001`, `average_math_session_001`, `capable_speedrun_001`) can
   hit `max_turns` mid-lesson and fail *spuriously* — penalizing thorough models. Must fix.

7. **Top-15 (from `single_turn_results/results3`)**, 11 Qwen + 4 Gemini:
   qwen3.5:4b, qwen3-next-80b-instruct, qwen3.6:35b-a3b, qwen3.6:27b, qwen3.5:9b, qwen3:14b,
   gemini-2.5-flash, gemini-3.5-flash, qwen3:4b, qwen2.5:72b, qwen2.5:32b,
   qwen3-next-80b-thinking, gemini-3.1-pro, gemini-2.5-pro, qwen3:30b-a3b.
   Split: **6 API/Vertex** (4 Gemini + 2 Qwen-MaaS-80b) run locally; **9 OSS Ollama** run on Colab.

## 3. Decisions (user-approved)

- **Prompts:** *baseline first, then tune.* Run the sweep with the current per-family prompts
  **unchanged**, find where each family actually breaks in a full session, then make targeted
  per-family fixes. Anthropic base template stays byte-identical throughout.
- **Dataset:** *review + fix + expand* to ~30 balanced scenarios.
- **Folders:** move all 4 results dirs **+** associated reports into `single_turn_results/`;
  update path refs; new runs land in `multi_turn_results/`.

## 4. Plan

### Part 0 — Folder reorganization (structural; do first)

`git mv` (preserve history) under `offline_eval/`:

- **→ `single_turn_results/`**: `results/`, `results2/`, `results2_md/`, `results3/`, plus reports
  `IMPROVED_EVAL_1_PREPRINT.{md,docx}`, `IMPROVED_EVAL_1_REPORT.docx`,
  `IMPROVED_EVAL_2_REPORT.{md,docx}`, `EVAL1_BOTTLENECK_ANALYSIS_5MODELS.{md,docx}`,
  `FINDINGS_offline_model_eval.md`, `Offline_Model_Evaluation_Report.docx`,
  `leaderboard_combined.csv`.
- **→ `multi_turn_results/`**: created empty; target for the new sweep.
- **Stays at `offline_eval/` root** (cross-cutting infra): `PROMPT_ENGINEERING_FRAMEWORK.{md,docx}`,
  `aggregate.py`, `run_matrix.sh`, `run_cloud.sh`, `colab_eval.ipynb`, `_make_colab_nb.py`,
  `*models*.txt`, probes.
- **Path refs:** repoint default `RESULTS`/`RESULTS_DIR` in `aggregate.py`, `run_matrix.sh`,
  `run_cloud.sh` to `single_turn_results/results`; the multi-turn runs pass
  `RESULTS_DIR=…/multi_turn_results` explicitly. Regenerate `colab_eval.ipynb` for the
  multi-turn paths.
- **Caveat:** `IMPROVED_EVAL_2_REPORT.docx` has a live LibreOffice lock (`.~lock…#`); the lock
  file is skipped, and the user closes the doc before the move.

### Part 1 — Dataset: review + fix + expand

**Fix (correctness) — all 20:**
- **max_turns trap:** for completion-expecting scenarios on 10-step lessons, raise `max_turns`
  to ~22–25 **or** add `max_turns` to `expected_reason`. Decide per-scenario from a smoke run,
  not by guessing.
- **rubric-vs-persona audit:** every scenario's rubric must match its persona (e.g.
  probe_resistant must not penalize the tutor for the *student* refusing to show work;
  non_responder must not require the tutor to "advance" on non-answers).
- **label liveness:** confirm `no_label_anywhere: [BANNED_OPENER, TOOL_LEAK, ASK_WORKING]`
  actually fires in the multi-turn path — i.e. per-turn `SessionTurn.judge_outputs`/`metadata`
  are populated so `derive_suggested_labels` returns them. If they are not populated during a
  simulated session, either wire the derivation or drop the dead assertions (documented, not
  silently).

**Expand + rebalance to ~30 scenarios:**
- Target ≈ even **math/geo (≈15/15)** and even spread across all 4 lessons.
- All 6 personas represented in **both** subjects.
- Add targeted edge cases: mid-session clarification, self-correction chain, completion under
  length pressure, banned-opener-loop stress on geo, help-intensive on geo.
- New scenarios reuse the corrected session-level rubric block (Part 2) and validated
  `max_turns`.

### Part 2 — Rubric / grading fix

- **`_build_trajectory_prompt` (`llm_rubric.py`):** instruct the judge to score each item
  **per-applicable-turn across the session** — "satisfied whenever it was relevant; return
  `n/a` only if it was never relevant" — reusing the existing n/a-exclusion machinery
  (`applicable=False` items excluded from the mean).
- **Scenario rubric wording:** reword the shared BEA block from per-response to **session-level**
  phrasing in every scenario file (mechanical; the files are edited in the Part 1 audit anyway).
- **Threshold calibration:** treat `pass_threshold` 0.60–0.65 as a calibration point — confirm
  against the baseline that clean sessions aren't sunk and broken ones aren't passed. Adjust
  only with evidence.

### Part 3 — Baseline sweep (prompts unchanged) → targeted tuning

1. **Smoke test:** one multi-turn scenario on `gemini-2.5-flash` locally to prove the pipeline
   end-to-end (session runs, trajectory + rubric score, JSON written to `multi_turn_results/`).
2. **API/Vertex (6, local)** via `run_cloud.sh MODE="--multi-turn"` with a top-15 cloud rows
   file: gemini-2.5-flash, gemini-3.5-flash, gemini-3.1-pro, gemini-2.5-pro,
   qwen3-next-80b-instruct, qwen3-next-80b-thinking.
3. **OSS (9, Colab)** via `run_matrix.sh MODE="--multi-turn"` + `CLEANUP_MODELS=1`
   (regenerated notebook): qwen3.5:4b, qwen3.6:35b-a3b, qwen3.6:27b, qwen3.5:9b, qwen3:14b,
   qwen3:4b, qwen2.5:72b, qwen2.5:32b, qwen3:30b-a3b.
4. **Analyze** per-family failure modes from the baseline transcripts.
5. **Targeted tuning:** fix only the Qwen Markdown template + Gemini XML targeted-rules in
   `family_prompts.py` for the measured failures. **Anthropic `_BLOCK_0_TEMPLATE` byte-identical.**
6. **Re-run** affected models; compare baseline vs tuned; produce the leaderboard.

## 5. Verification

- `venv/bin/python manage.py check`; existing `simple_tutor` + eval tests pass.
- Folder move: `RESULTS_DIR=…/single_turn_results/results3 venv/bin/python offline_eval/aggregate.py`
  reproduces the Eval-2 leaderboard (no path breakage).
- Dataset: a scenario-lint pass (every scenario loads, has trajectory verbs + rubric, lesson
  exists, `max_turns` validated by smoke run).
- Rubric fix: unit-level check that an n/a item is excluded from the trajectory mean and a
  per-turn-applicable item is judged over the session.
- Pipeline: the gemini-2.5-flash smoke scenario passes end-to-end before the full sweep.

## 6. Threats to validity / risks

- **Student-sim quality (Gemini 2.5 Flash) bounds realism.** A weak/rushed synthetic student
  can make a good tutor look bad (or vice versa). Held constant, so comparisons are fair, but
  absolute pass rates are only as good as the personas.
- **Single run, single judge.** Multi-turn sessions have more variance than single-turn.
  Spot-check top transcripts before treating any number as final; consider n>1 for the podium.
- **Cost/runtime.** Each scenario = up to ~25 turns × (2-call tutor + 1 student call) + 1
  session-judge. 6 API models × ~30 scenarios is real API spend; 9 OSS on Colab A100 are slow.
  Sequence so the user can checkpoint.
- **Non-tool-calling models** hit the engine's fallback (as in single-turn). All top-15 support
  tools, so this should not recur, but watch the logs.

## 7. Out of scope

- Anthropic self-evaluation (incumbent + judge; not self-scored).
- Gemma (invalidated in Eval 2; excluded).
- New personas or a new student-sim model (fixed for comparability).
- Engine/architecture changes beyond the rubric fix and prompt tuning.

## 7b. Findings during implementation (2026-07-06)

- **The `no_label_anywhere` assertion was fully dead in the multi-turn path.**
  `simple_tutor` persists only `metadata={'tool_calls':…}` + `judge_outputs={'grader':…}`
  per turn, and `derive_suggested_labels` reads neither of those keys — it reads
  `validator_issues` / `rule_violations` / `judge_outputs['rule'|'safety'|…]`, none of
  which simple_tutor writes during a session. So `derive_suggested_labels` returned `[]`
  for every tutor turn, and `no_label_anywhere: [...]` passed vacuously for **all** labels
  (not just TOOL_LEAK/BANNED_OPENER/ASK_WORKING) across all 20 scenarios.
  - **Fix:** added a live deterministic trajectory verb `no_tool_syntax_in_any_turn`
    (regex over every tutor turn; catches leaked `record_answer(...)` / `<tool_use>` /
    `<thinking>` with no judge in the loop), replaced the dead `no_label_anywhere` with it
    in all 20 scenarios, and kept `no_repeated_tutor_phrase_within_window` for opener-loops.
    Commit `daad909`. The soft concerns the other labels encoded (INFO_DUMP, UNFOUNDED_PRAISE,
    INCOHERENT, PREMATURE_ADVANCE, SAFETY_*) remain covered by the session LLM rubric.
- **`max_turns` trap fixed** on the 4 completion-expecting 10-step-lesson scenarios
  (average_math, average_session_completion, capable_math, capable_speedrun): bumped
  `max_turns` + `max_turn_count` 15 → 24. Calibration of `average` completion is deferred
  to the Task-5 smoke run (average may need more retries than capable).
- **BEA rubric block reworded per-response → session-level** in all 20 (conditional
  "whenever/when" openers preserved so the Task-2 n/a rule fires on turns where an item
  doesn't apply). Prompting skills (fundamentals + claude) consulted first per CLAUDE.md.
- **Rubric-vs-persona audit:** the scenario-specific items for probe_resistant /
  non_responder are already persona-correct (they reward the tutor for NOT advancing on
  non-answers / NOT looping probes) — no changes needed.
- **Lint tool** `offline_eval/lint_multi_turn.py` added; guards the max_turns trap, dead
  labels, missing rubric/verb/threshold, and lesson-in-fixtures.

## 7c. Gemini POSE-path fix (2026-07-06, from the smoke test)

The gemini-2.5-flash smoke run **deadlocked at turn 3**: under Gemini's default
AUTO function-calling, it wrote MCQs as prose instead of calling `pose_question`,
so no gradable in-flight slot was created → the student's answer had nothing to
grade → the tutor repeated a teaching turn → deadlock. Single-turn never caught
this (the slot was pre-seeded). Root cause: `_call_llm` invoked the tutor with
`tool_choice` unset → GeminiClient set `function_calling_config mode=AUTO`.

Two-part fix (Gemini-family only; **Anthropic base + Qwen template byte-identical**):
1. **Prompt** (`family_prompts.py`): added a positive-framed "pose every question
   through the pose_question tool" rule + a pose example to `GEMINI_TARGETED_RULES_XML`.
   Alone this lifted the smoke rubric 0.59→0.75 but the session still hit max_turns.
2. **Engine** (`engine.py`): threaded an optional `tool_choice` through `_call_llm`
   → `generate_with_tools`. In `respond()`, **eval-only** (family=gemini, mode=POSE,
   intent not in {clarification, pushback, off_topic}), Call-1 forces
   `tool_choice={"type":"tool","name":"pose_question"}` (→ Gemini mode=ANY). None in
   production (family is None without `TUTOR_MODEL_OVERRIDE`) so the Anthropic call is
   byte-identical; the Anthropic path only adds the kwarg when non-None.

**Verified:** smoke scenario went deadlock@3 (0.59, FAIL) → max_turns@15 (0.75, FAIL)
→ **exit_ticket@9 turns (rubric 0.95, PASS)**; 0 slot-less record_answer events.
Pre-existing unrelated test debt: `IsEnabledTest.{test_default_off,test_off_falsy}`
fail on a clean tree (stale — the engine default flipped to ON); not touched here.

## 7d. Judge A/B — Haiku vs Sonnet (2026-07-06)

`offline_eval/judge_ab.py` scored 8 fixed gemini-2.5-flash transcripts (all 6
personas × both subjects) with Haiku 4.5 and Sonnet 4.6 (transcript held fixed →
difference is the judge). Result:

- Haiku mean-of-means **0.772**, pass **7/8**; Sonnet **0.588**, pass **2/8**.
- mean |Δ| 0.18, max 0.30; Pearson r 0.77; **pass/fail concordance 3/8 (38%),
  Cohen's κ 0.09** (≈ chance). **5/8 sessions: Haiku PASS / Sonnet FAIL.**
- Divergence concentrated on the hard cross-turn items: coherence Δ0.37,
  mistake-recognition Δ0.30, guidance Δ0.26; surface items agree (tone/no-reveal Δ0.12).
- **Grounded spot-check** (non_responder, Haiku 0.86 vs Sonnet 0.56): the tutor
  told the student "Not quite" to "360" (the correct sum) then admitted "you're
  right that it's 360°" — a real self-contradiction. Sonnet caught it; Haiku did
  not. So Haiku is not merely stricter — it **misses real coherence/mistake
  errors** over long transcripts.

**Conclusion: Haiku is too lenient for the multi-turn gate.** **Wired (2026-07-06):**
`runner.MULTI_TURN_RUBRIC_JUDGE = claude-sonnet-4-6` is the multi-turn default
(`_run_multi_turn` uses `scenario.rubric_judge or MULTI_TURN_RUBRIC_JUDGE`); a
scenario can still override. Single-turn keeps Haiku (`llm_rubric.DEFAULT_RUBRIC_JUDGE`)
for continuity with the frozen Eval 2 board. Each run's per-scenario `rubric_result`
records the judge model, so the sweep is self-auditing. ~$18 judge cost for the full
15-model sweep. (Cross-vendor podium check remains an option, not yet wired.)

## 7e. Sweep prep done (no-cost), sweep NOT started (2026-07-06)

- **Colab fixture verified** (the flagged risk): `evals/fixtures/lessons.json` has
  all 4 lessons with LessonSteps (1137/1138=10, 1463/1464=5) + exit tickets + 416
  exit-ticket questions; `institution.json` seeds the active `student_sim` config
  (Haiku). Judge (Sonnet) + grader cascade resolve in-memory from the `.env` keys.
  → multi-turn traversal works on Colab.
- **`cloud_models_mt.txt`** (6 API rows: 4 Gemini + 2 Qwen-MaaS) + **regenerated
  `colab_eval.ipynb`** (9 OSS Qwen, A100, `--multi-turn`, Sonnet judge, writes to
  `multi_turn_results/`, resume-safe, small→large order).
- **Runtime caveat (important):** multi-turn is ~15-25× heavier than single-turn
  (30 scenarios × up to 24 turns × tutor+student+judge). Small/MoE OSS models are
  hours-scale; large dense (27b/32b/**72b**) can be many hours each. The sweep needs
  several A100 sessions; consider running small→large and possibly a scenario subset
  or skipping 72b. **Open decision before the sweep:** run all 30 scenarios per OSS
  model, or a balanced subset, to bound Colab wall-clock.

## 7f. Anthropic benchmark tier (2026-07-06)

Adding an Anthropic tier as a ceiling/reference for the multi-turn board.
**Key discovery:** the production incumbent tutor is **`claude-sonnet-4-6`** —
which is *also* the multi-turn judge — so benchmarking the incumbent would be
Sonnet self-grading. Decision (user): **exclude the Sonnet incumbent**; benchmark
**claude-opus-4-8** (ceiling) + **claude-haiku-4-5** (cheap tier, comparable to the
small Qwen models), judged by the same Sonnet 4.6 rubric judge. That is a
same-vendor (Anthropic judge / Anthropic tutor) pairing → treat these two scores
as an **approximate ceiling with a mild favourable-bias caveat**, not like-for-like
vs the Sonnet-judged Gemini/Qwen numbers. Anthropic tutors run the native
production path (base template, native tool-calling; the Gemini pose-fix /
tool_choice are non-Anthropic-only, so the incumbent behaviour is unchanged).
Config: `offline_eval/cloud_models_anthropic.txt`. Runs AFTER the Gemini/Qwen
sweep (shared SQLite dev DB), into the same `multi_turn_results/`.

## 8. Cross-references

- Single-turn engine + family-prompt work: `apps/tutoring/simple_tutor/{engine,family_prompts,prompts}.py`.
- Eval-2 report + data: `offline_eval/single_turn_results/` (after Part 0).
- Prompt-engineering rationale: `offline_eval/PROMPT_ENGINEERING_FRAMEWORK.md`.
- Related plan: `memory/eval_benchmark_v2_simplified.md`.
