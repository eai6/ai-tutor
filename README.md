# Eval reports — CI-generated tutor evaluations

Each commit on this branch is a tutor evaluation run captured by
`.github/workflows/deploy.yml::post_deploy_eval`. The run is
identified by the commit SHA of the main-branch deploy that
triggered it plus the matrix mode.

Layout:

    runs/<sha>-<mode>/
    ├── FINAL_REPORT.md      ← ranked prompt-edit recommendations
    ├── summary.md           ← per-cell + per-principle tables
    ├── cell_results.jsonl   ← raw programmatic metrics
    ├── cost_latency.md
    ├── per_cell/            ← full transcripts + judge evidence
    └── judge_scores/        ← structured judge output

Run manually: GitHub Actions -> Deploy -> Run workflow
(uncheck `skip_eval`, optionally set `eval_matrix_mode=full`).

Companion human-written reviews live on `main` under
`eval-reports/`.
