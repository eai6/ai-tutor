# Tutor evaluation reports

Two stores, one purpose:

| Where | Contents | Updated by |
|---|---|---|
| `main` · `eval-reports/*.md` (this directory) | **Human-written reviews** synthesising one or more CI runs. Reading-friendly, opinionated, comparative. | Pull requests |
| `eval-reports` branch · `runs/<sha>-<mode>/` | **CI-generated raw reports** — full transcripts, per-principle judge scores, prompt-edit recommendations. One subdir per deploy. | `.github/workflows/deploy.yml::post_deploy_eval` |

## Reading a CI report

```bash
git fetch origin eval-reports
git checkout origin/eval-reports -- runs/<sha>-<mode>/
ls runs/<sha>-<mode>/
#   FINAL_REPORT.md      ← ranked prompt-edit recommendations (start here)
#   summary.md           ← per-cell + per-principle tables
#   per_cell/            ← full transcripts + judge evidence
#   cell_results.jsonl   ← raw programmatic metrics
#   cost_latency.md
#   judge_scores/        ← structured judge output (per principle, per cell)
```

Or browse the [eval-reports branch on GitHub](../../../../tree/eval-reports).

## Triggering a run manually

The post-deploy-eval job fires on every push to `main` automatically. To
re-run the eval without a fresh deploy:

1. Re-run jobs on the most recent `Deploy` workflow run from the
   [GitHub Actions UI](../../../../actions/workflows/deploy.yml).
2. Or trigger `Deploy` via **Run workflow** on a chosen branch with:
   - `skip_eval` = unchecked (default)
   - `eval_matrix_mode` = `deploy` (1 model × 2 lessons × 2 personas, ~5 min, ~$5)
     or `full` (2 models × 2 lessons × 2 personas, ~15 min, ~$15)

## What is measured

| Axis | Default (`deploy` mode) | `full` mode |
|---|---|---|
| Tutor models | Claude Sonnet 4 | Sonnet 4 + Gemini 3 Flash |
| Lessons | L1137 *Angles around a point* · L1425 *Map Scale and Map Types* | same |
| Personas | `struggler` · `capable` (synthetic LLM students) | same |
| Judge | Claude Opus 4.7 @ T=0, 10-principle Science-of-Learning rubric | same |
| Cells per run | 4 | 8 |

The fixture used is `eval-fixtures/baseline.json` — a 94-row Django
fixture covering the two lessons, their unit + course parents, their
lesson steps, and their exit-ticket questions. Regenerate with
`python scripts/extract_eval_fixture.py > eval-fixtures/baseline.json`
after loading a fresh prod dump.

## Caveats

- The harness monkey-patches the `tutoring` ModelConfig at runtime to swap in the spec'd tutor model, so the eval measures *that* model's behavior, **not** the live production `TUTOR_MODEL_OVERRIDE`. Treat it as "what the engine does with the eval-mode tutor model".
- Synthetic students; not real-student learning gains.
- 4–8 cells is a small sample. Use it to surface structured failure modes (validator-guard hit rates, top judge recommendations), not for tight statistical comparisons.

## Cost & latency

- `deploy` mode: ~5 min wall, ~$5/run
- `full` mode: ~15 min wall, ~$15/run
- Triggered on every push to `main` by default. Set `skip_eval=true` on a manual dispatch if you don't want the spend.

## Repository pointers

- Workflow: `.github/workflows/deploy.yml::post_deploy_eval`
- Simulator: `apps/tutoring/student_sim/`
- Runtime engine: `apps/tutoring/conversational_tutor.py`
- Validator + judges: `apps/tutoring/validator.py`, `apps/tutoring/judges/`
- A/B harness: `scripts/run_ab_test.py`, `scripts/judge_transcripts.py`, `scripts/generate_reports.py`
- Rubric: `.claude/skills/evaluate-tutor/SKILL.md`
- Active redesign plan that will obsolete several validator guards over time: `memory/tutor_engine_redesign_plan.md`
