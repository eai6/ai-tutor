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
   - `eval_matrix_mode` = `deploy` (Sonnet + Gemini × 2 lessons × `error_prone` = **4 cells**, ~25 min, ~$15)
     or `full` (2 models × 4 lessons × 3 personas = **24 cells**, ~100 min, ~$60 — comprehensive sweep)

## What is measured

| Axis | Default (`deploy` mode) | `full` mode |
|---|---|---|
| Tutor models | Sonnet 4 + Gemini 3 Flash | same |
| Lessons | L1137 *Angles around a point* (math) · L1425 *Map Scale and Map Types* (geo) | + L1138 *Angles on a straight line* + L540 *Understanding Maps* |
| Personas | `error_prone` only (designed for max BEA in-scope coverage) | + `struggler` + `capable` |
| Judge | Claude Opus 4.7 — combined 10-principle + BEA-2025 rubric (one call) | same |
| **Cells per run** | **4** | **24** |
| Wall time | ~25 min | ~100 min |
| Cost | ~$15 | ~$60 |

### The two judging rubrics (run together in one Opus call)

| Rubric | Unit of analysis | Scale | What it's for |
|---|---|---|---|
| **10-principle Science-of-Learning** | Per session (whole transcript) | 0-5 per principle | Produces structured `prompt_recommendations` / `flow_recommendations` / `experience_recommendations`. Drives prompt-engineering iteration (think "Forbid X / Mandate Y" style edits). Inherited from PR #7's A/B harness; rubric in `.claude/skills/evaluate-tutor/SKILL.md`. |
| **BEA-2025 Shared Task** | Per tutor turn (after a student mistake/confusion) | 3-class: `Yes` / `To some extent` / `No` on 4 dimensions | Paper-aligned + leaderboard-comparable pass-rate metric. The 4 dimensions are Mistake Identification, Mistake Location, Providing Guidance, Actionability (Maurya et al. 2025, NAACL). Strict pass = `Yes` on all 4; lenient pass = `Yes` or `To some extent` on all 4. |

Both rubrics are emitted by a single Opus 4.7 call per transcript (see `scripts/judge_transcripts.py`), so we get the prompt-edit recommendations AND the paper-aligned pass rate without doubling judge cost.

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
- Rubric (10-principle): `.claude/skills/evaluate-tutor/SKILL.md`
- Rubric (BEA-2025): [BEA Shared Task page](https://sig-edu.org/sharedtask/2025) — Maurya et al. 2025, "Unifying AI Tutor Evaluation: An Evaluation Taxonomy for Pedagogical Ability Assessment of LLM-Powered AI Tutors", NAACL 2025
- Active redesign plan that will obsolete several validator guards over time: `memory/tutor_engine_redesign_plan.md`
