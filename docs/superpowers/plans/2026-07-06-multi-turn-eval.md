# Multi-turn Eval (Improved Eval 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the single-turn top-15 (11 Qwen + 4 Gemini) through the existing multi-turn harness, after reorganizing results folders, fixing the trajectory-rubric grading bug, and reviewing/expanding the multi-turn dataset — baseline first (prompts unchanged), then targeted per-family tuning.

**Architecture:** Multi-turn drives `apps/tutoring/simple_tutor/engine.py` via `student_sim.simulate_session` (a Gemini-2.5-Flash persona plays the student; the tutor-under-test is swapped with `TUTOR_MODEL_OVERRIDE`). Scoring = deterministic trajectory verbs (`evals/scorers/trajectory.py`) + a whole-session LLM rubric (`llm_rubric.score_trajectory`, Haiku 4.5 @ temp 0). Per-family prompts already apply (same engine as single-turn).

**Tech Stack:** Django 5 / Python 3.11, pytest-django, Ollama (local + Colab A100), Anthropic + Gemini + Vertex MaaS APIs, YAML scenarios.

**Design spec:** `memory/multi_turn_eval_v1_plan.md` (read it first).

## Global Constraints

- **Anthropic `_BLOCK_0_TEMPLATE` in `apps/tutoring/simple_tutor/prompts.py` stays byte-identical.** Never edit it. Family tuning touches only `family_prompts.py` (Qwen Markdown template + Gemini XML targeted-rules).
- **Families in scope: Qwen + Gemini only.** No Gemma, no Anthropic self-eval.
- **Always use `venv/bin/python`** for Django/management commands (system Python has a stale Django).
- **Fixed roles:** judge = `claude-haiku-4-5-20251001` @ temp 0; student-sim = Gemini 2.5 Flash. Only the tutor varies.
- **New multi-turn results go to `offline_eval/multi_turn_results/`** (pass `RESULTS_DIR` explicitly).
- **Vertex MaaS** (qwen3-next-80b) uses the isolated auth: `CLOUDSDK_CONFIG="$HOME/.config/gcloud-pixeldesignlabs"`, `GOOGLE_CLOUD_PROJECT="ai-tutor-499714"`; see `memory/vertex-model-garden-eval-setup.md`.
- **Don't auto-commit during active iteration**; commit at the task boundaries below.

---

### Task 1: Folder reorganization + path refs

**Files:**
- Move (git mv): `offline_eval/{results,results2,results2_md,results3}/` → `offline_eval/single_turn_results/`
- Move (git mv): reports `IMPROVED_EVAL_1_PREPRINT.{md,docx}`, `IMPROVED_EVAL_1_REPORT.docx`, `IMPROVED_EVAL_2_REPORT.{md,docx}`, `EVAL1_BOTTLENECK_ANALYSIS_5MODELS.{md,docx}`, `FINDINGS_offline_model_eval.md`, `Offline_Model_Evaluation_Report.docx`, `leaderboard_combined.csv` → `offline_eval/single_turn_results/`
- Create: `offline_eval/multi_turn_results/.gitkeep`
- Modify: `offline_eval/aggregate.py:17`, `offline_eval/run_matrix.sh:21`, `offline_eval/run_cloud.sh:15`

**Interfaces:**
- Produces: `single_turn_results/results3/*.json` (unchanged content, new path); the new default `RESULTS` path for the three scripts.

- [ ] **Step 1: Confirm the docx lock is gone.** Ask the user to close `IMPROVED_EVAL_2_REPORT.docx` in LibreOffice. Verify: `ls offline_eval/.~lock*` returns nothing (or exclude the lock file from the move).

- [ ] **Step 2: Create the two target dirs.**
```bash
cd offline_eval && mkdir -p single_turn_results multi_turn_results && touch multi_turn_results/.gitkeep
```

- [ ] **Step 3: git mv the 4 results dirs.**
```bash
cd offline_eval
for d in results results2 results2_md results3; do git mv "$d" "single_turn_results/$d"; done
```

- [ ] **Step 4: git mv the associated reports.**
```bash
cd offline_eval
for f in IMPROVED_EVAL_1_PREPRINT.md IMPROVED_EVAL_1_PREPRINT.docx IMPROVED_EVAL_1_REPORT.docx \
         IMPROVED_EVAL_2_REPORT.md IMPROVED_EVAL_2_REPORT.docx \
         EVAL1_BOTTLENECK_ANALYSIS_5MODELS.md EVAL1_BOTTLENECK_ANALYSIS_5MODELS.docx \
         FINDINGS_offline_model_eval.md Offline_Model_Evaluation_Report.docx leaderboard_combined.csv; do
  git mv "$f" "single_turn_results/$f"
done
```

- [ ] **Step 5: Repoint the default results path in all three scripts.** Change `offline_eval/results` → `offline_eval/single_turn_results/results`:
  - `aggregate.py:17`: `os.path.join(ROOT, 'offline_eval', 'single_turn_results', 'results')`
  - `run_matrix.sh:21`: `RESULTS="${RESULTS_DIR:-$ROOT/offline_eval/single_turn_results/results}"`
  - `run_cloud.sh:15`: `RESULTS="${RESULTS_DIR:-$ROOT/offline_eval/single_turn_results/results}"`

- [ ] **Step 6: Verify no path breakage** — aggregate reproduces the Eval-2 leaderboard from the moved dir.
```bash
RESULTS_DIR="$PWD/offline_eval/single_turn_results/results3" venv/bin/python offline_eval/aggregate.py | head -20
```
Expected: the 29-model leaderboard, `qwen3.5_4b` at 100% on top (matches `single_turn_results/IMPROVED_EVAL_2_REPORT.md`).

- [ ] **Step 7: Grep for any other stale path references** and fix or note them.
```bash
grep -rn "offline_eval/results3\|offline_eval/results\b\|results3/" offline_eval/*.py offline_eval/*.sh --include=* | grep -v single_turn_results
```
Expected: no hits that break (the Colab notebook is regenerated in Task 5, not here).

- [ ] **Step 8: Commit.**
```bash
git add -A offline_eval docs
git commit -m "offline-eval: reorg results into single_turn_results/ + add multi_turn_results/

git mv the 4 single-turn results dirs + associated eval reports under
single_turn_results/; create multi_turn_results/ for the multi-turn sweep;
repoint default RESULTS path in aggregate.py/run_matrix.sh/run_cloud.sh.

Refs: memory/multi_turn_eval_v1_plan.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Fix the trajectory-rubric grading bug

**Files:**
- Modify: `evals/scorers/llm_rubric.py` — `_build_trajectory_prompt` (~line 394)
- Test: `evals/tests/test_llm_rubric_trajectory.py` (create; if `evals/tests/` doesn't exist, create it with `__init__.py`)

**Interfaces:**
- Consumes: `llm_rubric.score_trajectory`, `_build_trajectory_prompt`, `RubricResult`, `_call_and_parse` (existing n/a exclusion via `RubricItemScore.applicable`).
- Produces: a `_build_trajectory_prompt` that instructs per-applicable-turn/session-level judging with `n/a`.

- [ ] **Step 1: Write the failing test** for the prompt-instruction content and the n/a-exclusion over a trajectory. Create `evals/tests/test_llm_rubric_trajectory.py`:
```python
from evals.scorers import llm_rubric
from evals.scorers.llm_rubric import RubricResult, RubricItemScore


def test_trajectory_prompt_instructs_session_level_judging():
    prompt = llm_rubric._build_trajectory_prompt(
        transcript=[{"role": "tutor", "content": "hi"},
                    {"role": "student", "content": "90"}],
        rubric_items=["If the student made a mistake, the tutor located it."],
    )
    low = prompt.lower()
    # Must tell the judge to evaluate per applicable turn across the session,
    # and to use n/a when an item never applied — not to average a single turn.
    assert "n/a" in low
    assert "whenever" in low or "each turn" in low or "any turn" in low


def test_na_item_excluded_from_trajectory_mean():
    # Two items: one scored 1.0, one n/a. Mean must be 1.0 (n/a excluded),
    # not 0.5 — proving the applicable-only mean holds on the trajectory path.
    result = RubricResult(pass_threshold=0.65)
    result.items = [
        RubricItemScore(item="a", score=1.0, applicable=True),
        RubricItemScore(item="b", score=0.0, applicable=False),
    ]
    scored = [s for s in result.items if s.applicable]
    mean = sum(s.score for s in scored) / len(scored)
    assert mean == 1.0
```

- [ ] **Step 2: Run the test, verify the first fails.**
```bash
venv/bin/python -m pytest evals/tests/test_llm_rubric_trajectory.py -v
```
Expected: `test_trajectory_prompt_instructs_session_level_judging` FAILS (current prompt says "ACROSS the session" but not the per-applicable-turn/n-a instruction); the second PASSES (machinery already exists).

- [ ] **Step 3: Rewrite `_build_trajectory_prompt`** to instruct session-level, per-applicable-turn judging with n/a. Replace the function body's return with:
```python
    return (
        f"FULL TUTORING SESSION TRANSCRIPT:\n{conv_str}\n\n"
        f"RUBRIC ITEMS — judge each across the WHOLE session:\n{rubric_str}\n\n"
        "For each item: consider every tutor turn where the item is relevant "
        "(e.g. an 'if the student made a mistake' item applies only on turns "
        "where the student actually erred). Score how well the tutor satisfied "
        "the item on those turns overall — 1.0 if it held on every relevant "
        "turn, lower if it lapsed on some. If the item never became relevant "
        "anywhere in the session, return \"n/a\" for it (n/a is excluded from "
        "scoring, neither helping nor hurting). Do not judge a single turn in "
        "isolation.\n\n"
        "Return JSON."
    )
```

- [ ] **Step 4: Run the test, verify all pass.**
```bash
venv/bin/python -m pytest evals/tests/test_llm_rubric_trajectory.py -v
```
Expected: both PASS.

- [ ] **Step 5: Django check + broader rubric tests** don't regress.
```bash
venv/bin/python manage.py check && venv/bin/python -m pytest evals/ -q -k "rubric or trajectory"
```
Expected: check OK; tests pass.

- [ ] **Step 6: Commit.**
```bash
git add evals/scorers/llm_rubric.py evals/tests/test_llm_rubric_trajectory.py
git commit -m "evals: judge multi-turn rubric per-applicable-turn across the session

The BEA rubric items are phrased per-response but score_trajectory judged
them 'ACROSS the session' with no applicability rule, so conditional items
misjudged on mixed transcripts. Instruct the judge to score each item over
the turns where it's relevant and return n/a when it never applies (already
excluded from the mean).

Refs: memory/multi_turn_eval_v1_plan.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Audit + fix the existing 20 multi-turn scenarios

**Files:**
- Modify: `evals/dataset/multi_turn/*.yaml` (the 20 existing)
- Create: `offline_eval/lint_multi_turn.py` (scenario-lint helper, reusable in Task 4)

**Interfaces:**
- Consumes: lesson step counts (1137=10, 1138=10, 1463=5, 1464=5); persona set from `apps/tutoring/student_sim/personas.py`.
- Produces: 20 corrected scenarios; a lint script that fails on the known traps.

- [ ] **Step 1: Investigate label liveness** — do per-turn `SessionTurn.judge_outputs`/`metadata` get populated during a simulated `simple_tutor` session (so `no_label_anywhere` is live)? Run one session and inspect:
```bash
SIMPLE_TUTOR_ENGINE=1 TUTOR_MODEL_OVERRIDE="anthropic/claude-haiku-4-5-20251001" venv/bin/python manage.py shell -c "
from ai_tutor.apps.tutoring.student_sim import simulate_session
from ai_tutor.apps.tutoring.models import SessionTurn
r = simulate_session(lesson_id=1137, persona='probe_resistant', max_turns=6)
ts = SessionTurn.objects.filter(session_id=r.session_id, role='tutor').order_by('id')
for t in ts: print(t.id, 'meta_keys=', sorted((t.metadata or {}).keys()), 'judge_keys=', sorted((t.judge_outputs or {}).keys()))
"
```
Decision: if `judge_outputs`/`metadata` are empty for the label-bearing keys, the `no_label_anywhere` assertions are dead. Either (a) keep them (harmless, always-pass) and rely on the rubric + `no_repeated_tutor_phrase` for those behaviours, or (b) remove them with a one-line note in each scenario. Record the finding in the design doc.

- [ ] **Step 2: Write `offline_eval/lint_multi_turn.py`** — loads every `evals/dataset/multi_turn/*.yaml`, asserts: has `mode: multi_turn`, has ≥1 trajectory verb in `assertions`, has a `rubric` + `pass_threshold`, `lesson_id` in {1137,1138,1463,1464}, and flags the max_turns trap (completion-only `expected_reason` on a 10-step lesson with `max_turns < 20`).
```python
import glob, sys, yaml
STEPS = {1137: 10, 1138: 10, 1463: 5, 1464: 5}
TRAJ_VERBS = {"expected_reason", "max_turn_count",
              "no_repeated_tutor_phrase_within_window", "no_label_anywhere"}
problems = []
for f in sorted(glob.glob("evals/dataset/multi_turn/*.yaml")):
    d = yaml.safe_load(open(f))
    name = f.split("/")[-1]
    if d.get("mode") != "multi_turn": problems.append(f"{name}: mode != multi_turn")
    a = d.get("assertions", {}) or {}
    if not (set(a) & TRAJ_VERBS): problems.append(f"{name}: no trajectory verb")
    if not d.get("rubric"): problems.append(f"{name}: no rubric")
    lid = d.get("lesson_id")
    if lid not in STEPS: problems.append(f"{name}: lesson {lid} not in fixtures")
    reasons = set(a.get("expected_reason", []) or [])
    mt = int(d.get("max_turns", 0))
    if lid in (1137, 1138) and reasons and "max_turns" not in reasons and mt < 20:
        problems.append(f"{name}: max_turns trap (10-step lesson, mt={mt}, "
                        f"completion-only reasons={sorted(reasons)})")
for p in problems: print("FAIL", p)
print(f"\n{len(problems)} problems across {len(glob.glob('evals/dataset/multi_turn/*.yaml'))} scenarios")
sys.exit(1 if problems else 0)
```

- [ ] **Step 3: Run the lint, see the max_turns traps.**
```bash
venv/bin/python offline_eval/lint_multi_turn.py
```
Expected: flags `capable_math_session_001`, `average_math_session_001`, `capable_speedrun_001` (and any others) for the max_turns trap.

- [ ] **Step 4: Fix the max_turns traps.** For each flagged completion-expecting scenario on a 10-step lesson, set `max_turns: 24` (≈2 turns/step + exit ticket). Do NOT add `max_turns` to `expected_reason` for capable/average (they should genuinely complete). Leave struggler/non_responder/probe_resistant/error_prone scenarios that already accept `max_turns` unchanged.

- [ ] **Step 5: Rubric-vs-persona audit** — read each scenario's rubric and reword any item that penalizes the tutor for the *student's* persona behaviour. Specifically:
  - probe_resistant: no rubric item may require the tutor to obtain working from the student.
  - non_responder: no item may require the tutor to advance the lesson on non-answers.
  - Reword the shared BEA block from per-response to session-level, e.g. "Across the session, whenever the student erred, the tutor identified the specific mistake" (replaces "the response identifies…"). Apply to all 20.

- [ ] **Step 6: Re-run lint, expect clean.**
```bash
venv/bin/python offline_eval/lint_multi_turn.py
```
Expected: `0 problems`.

- [ ] **Step 7: Commit.**
```bash
git add evals/dataset/multi_turn offline_eval/lint_multi_turn.py memory/multi_turn_eval_v1_plan.md
git commit -m "evals: fix multi-turn scenarios (max_turns trap, session-level rubric)

Raise max_turns to 24 on completion-expecting 10-step-lesson scenarios so
thorough tutors aren't failed spuriously; reword the BEA rubric block to
session-level phrasing; audit rubric-vs-persona. Add lint_multi_turn.py.

Refs: memory/multi_turn_eval_v1_plan.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Expand the dataset to ~30 balanced scenarios

**Files:**
- Create: ~10 new `evals/dataset/multi_turn/*.yaml`

**Interfaces:**
- Consumes: the corrected session-level rubric block + validated `max_turns` from Task 3; personas; lessons {1137,1138,1463,1464}.
- Produces: ~30 total scenarios, ≈15 math / ≈15 geo, all 6 personas in both subjects, spread across lessons.

- [ ] **Step 1: Compute the coverage gap.** Current: math 13 / geo 7; personas per subject uneven (geo missing several personas). Target additions (~10), all geo unless noted, to reach ≈15/15 and fill persona×subject cells:
  - `average_geo_session_001` (average, 1464)
  - `capable_geo_session_001` (capable, 1464)
  - `error_prone_math_session_002` (error_prone, 1138)  ← math, boosts BEA cells
  - `non_responder_geo_engagement_001` (non_responder, 1463)
  - `probe_resistant_geo_refusal_001` (probe_resistant, 1463)
  - `struggler_geo_session_001` (struggler, 1464)
  - `capable_geo_clarification_001` (capable, 1463) — mid-session clarification edge case
  - `average_self_correction_geo_001` (average, 1464) — self-correction chain
  - `struggler_geo_help_intensive_001` (struggler, 1463)
  - `error_prone_geo_session_002` (error_prone, 1464)
  Adjust the exact mix after Step 1's count so the final split is ≈15/15 and every persona appears in both subjects.

- [ ] **Step 2: Author each new scenario** by copying the nearest existing same-persona scenario as a template, then: set the correct `lesson_id`/`subject`/`persona`/`max_turns` (5-step geo lessons → `max_turns: 15` is ample; 10-step math → 24 for completion-expecting), give it a distinct `id`/`description`/`tags`, and paste the corrected session-level rubric block. Keep `expected_reason` honest to the persona (capable/average → `[completed, exit_ticket]`; struggler/non_responder/probe_resistant/error_prone → include `max_turns`).

- [ ] **Step 3: Lint the full set.**
```bash
venv/bin/python offline_eval/lint_multi_turn.py
```
Expected: `0 problems` across ~30 scenarios.

- [ ] **Step 4: Re-scan the balance.**
```bash
venv/bin/python - <<'PY'
import glob, yaml
from collections import Counter
subj, per, les = Counter(), Counter(), Counter()
for f in glob.glob("evals/dataset/multi_turn/*.yaml"):
    d = yaml.safe_load(open(f)); subj[d["subject"]] += 1; per[d["persona"]] += 1; les[d["lesson_id"]] += 1
print("subjects", dict(subj)); print("personas", dict(per)); print("lessons", dict(les))
PY
```
Expected: math ≈ geo; every persona ≥1 in both subjects.

- [ ] **Step 5: Commit.**
```bash
git add evals/dataset/multi_turn
git commit -m "evals: expand multi-turn dataset to ~30 balanced scenarios

Add geo + error_prone/edge-case scenarios to reach ~even math/geo and cover
all 6 personas in both subjects; new files use the corrected session-level
rubric + validated max_turns.

Refs: memory/multi_turn_eval_v1_plan.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Smoke test + sweep configuration

**Files:**
- Create: `offline_eval/cloud_models_mt.txt` (top-15 cloud rows: 4 Gemini + 2 Qwen-MaaS)
- Modify: `offline_eval/_make_colab_nb.py` (9 OSS models, `--multi-turn`, `multi_turn_results` paths) → regenerate `offline_eval/colab_eval.ipynb`

**Interfaces:**
- Consumes: `run_cloud.sh`/`run_matrix.sh` (both accept `MODE`, `RESULTS_DIR`, `MODELS_FILE`/`CLOUD_MODELS_FILE`).
- Produces: JSONs under `offline_eval/multi_turn_results/`.

- [ ] **Step 1: Smoke-test one scenario end-to-end** on gemini-2.5-flash (proves the multi-turn pipeline before spending on the full sweep). Run a single scenario:
```bash
SIMPLE_TUTOR_ENGINE=1 TUTOR_MODEL_OVERRIDE="google/gemini-2.5-flash" \
  venv/bin/python manage.py run_eval --multi-turn --scenario capable_geo_session_001 2>&1 | tail -30
```
(If `run_eval` has no `--scenario` flag, run the smallest suite subset it supports, or a shell-driven `simulate_session` + `runner._run_multi_turn` on one scenario.) Expected: a session runs, trajectory + rubric score, no tracebacks.

- [ ] **Step 2: Create `offline_eval/cloud_models_mt.txt`** (format: `spec  safe  region` — mirror existing `cloud_models_*.txt`). Rows:
```
google/gemini-2.5-flash            gemini-2.5-flash
google/gemini-3.5-flash            gemini-3.5-flash
google/gemini-3.1-pro              gemini-3.1-pro
google/gemini-2.5-pro              gemini-2.5-pro
<vertex-maas-spec-instruct>        qwen3-next-80b-instruct   <region>
<vertex-maas-spec-thinking>        qwen3-next-80b-thinking   <region>
```
Copy the exact Qwen-MaaS spec/region from the single-turn `offline_eval/cloud_models_qwen80b.txt`.

- [ ] **Step 3: Regenerate the Colab notebook** for the 9 OSS models + multi-turn. In `_make_colab_nb.py`, set the active model list to `qwen3.5:4b, qwen3.6:35b-a3b, qwen3.6:27b, qwen3.5:9b, qwen3:14b, qwen3:4b, qwen2.5:72b, qwen2.5:32b, qwen3:30b-a3b`, set `MODE="--multi-turn"`, `RESULTS_DIR=.../multi_turn_results`, `CLEANUP_MODELS=1`, and disk-clear before/after. Then:
```bash
venv/bin/python offline_eval/_make_colab_nb.py && venv/bin/python -c "import json;print('cells', len(json.load(open('offline_eval/colab_eval.ipynb'))['cells']))"
```
Expected: notebook regenerates.

- [ ] **Step 4: Commit the sweep config.**
```bash
git add offline_eval/cloud_models_mt.txt offline_eval/_make_colab_nb.py offline_eval/colab_eval.ipynb
git commit -m "offline-eval: multi-turn sweep config (cloud_models_mt + Colab notebook)

Add the top-15 cloud rows (4 Gemini + 2 Qwen-MaaS) and regenerate the Colab
notebook for the 9 OSS Qwen models in --multi-turn mode writing to
multi_turn_results/.

Refs: memory/multi_turn_eval_v1_plan.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Run baseline sweep, analyze, tune, re-run (interactive)

**Files:**
- Produces: `offline_eval/multi_turn_results/*.json` (+ `.log`); tuning edits to `family_prompts.py`.

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run the 6 API/Vertex models locally.**
```bash
CLOUD_MODELS_FILE="$PWD/offline_eval/cloud_models_mt.txt" \
RESULTS_DIR="$PWD/offline_eval/multi_turn_results" \
MODE="--multi-turn" bash offline_eval/run_cloud.sh 2>&1 | tail -40
```
Expected: 6 JSONs in `multi_turn_results/`. Watch for `generate_with_tools failed` / leaked `record_answer(...)` in the logs.

- [ ] **Step 2: User runs the 9 OSS models on Colab** (A100, `CLEANUP_MODELS=1`) using the regenerated notebook; downloads the JSON/log into `offline_eval/multi_turn_results/`. Checkpoint with the user here.

- [ ] **Step 3: Baseline leaderboard.**
```bash
RESULTS_DIR="$PWD/offline_eval/multi_turn_results" venv/bin/python offline_eval/aggregate.py
```

- [ ] **Step 4: Per-family failure analysis** — read the failing transcripts (`multi_turn_results/*.json` → `results[].transcript`), group by family, identify the top failure modes (opener loops, over-probing correct answers, exit-ticket gating, pacing, tool leaks). Write the findings into `memory/multi_turn_eval_v1_plan.md`.

- [ ] **Step 5: Targeted per-family tuning** — edit only the Qwen Markdown template + Gemini XML targeted-rules in `family_prompts.py` for the measured failures. **Anthropic `_BLOCK_0_TEMPLATE` byte-identical.** Consult the prompting skills (fundamentals + `gemini-prompting-expert` / provider-appropriate) before editing, per CLAUDE.md.

- [ ] **Step 6: Re-run affected models** into a sibling dir (e.g. `multi_turn_results_tuned/` or per-model overwrite with `FORCE=1`), compare baseline vs tuned, and produce the final leaderboard + short report.

- [ ] **Step 7: Commit tuning + results.**
```bash
git add apps/tutoring/simple_tutor/family_prompts.py offline_eval/multi_turn_results memory/multi_turn_eval_v1_plan.md
git commit -m "offline-eval: multi-turn baseline + targeted per-family tuning (Eval 3)

Refs: memory/multi_turn_eval_v1_plan.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** Part 0 → Task 1; Part 2 (rubric) → Task 2; Part 1 fix → Task 3; Part 1 expand → Task 4; Part 3 smoke+config → Task 5; Part 3 sweep+tune → Task 6. All spec sections mapped.
- **Anthropic-untouched constraint** appears in Global Constraints and Tasks 5/6.
- **Open items resolved at execution time (not placeholders):** exact Qwen-MaaS spec/region (copied from `cloud_models_qwen80b.txt`), the `run_eval --scenario` flag existence (fallback given), and label-liveness decision (Task 3 Step 1, with both branches specified).
