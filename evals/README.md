# AI Tutor Eval Harness

A curated, repo-checked-in test suite that exercises the tutor end-to-end across the 5 student personas. Distinct from `apps/benchmark/` (production sampling) and `apps/tutoring/student_sim/` (synthetic traffic generation) — see `memory/eval_harness_plan.md` for the full design and rationale.

## Quick start

```bash
# One-time: load the frozen lesson fixtures into your dev DB.
python evals/fixtures/extract.py                            # re-extract from prod_content_dump.sql (only if you have the dump)
python manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json

# Run the Phase-1 smoke scenario.
python manage.py run_eval --smoke

# Run a specific scenario by id.
python manage.py run_eval --scenario smoke_001

# Run the full suite (excludes smoke/).
python manage.py run_eval

# Inspect the latest run.
python -m evals.report

# Compare the latest two runs.
python -m evals.report --diff

# Explicit diff between two specific runs.
python -m evals.report path/to/run_A.json --diff path/to/run_B.json
```

Each run writes `evals/runs/<timestamp>_<git-sha>.json` with per-scenario results.

## Layout

```
evals/
├── dataset/                  # scenario YAMLs (the dataset)
│   └── smoke/                # plumbing scenarios — excluded from full runs
├── fixtures/                 # frozen lesson content + eval institution
│   ├── extract.py            # parse prod_content_dump.sql → fixtures
│   ├── institution.json      # eval Institution + simulator-bot User
│   └── lessons.json          # Course → Unit → Lesson → LessonStep + ExitTickets
├── scorers/                  # (Phase 2+) deterministic / judge / LLM-rubric
├── runner.py                 # discover scenarios, drive respond(), score, persist
├── personas.py               # re-export of apps/tutoring/student_sim/personas
└── runs/                     # gitignored — per-run result blobs
```

## Scenario file shape

See `evals/dataset/smoke/smoke_001.yaml`. Full schema in `memory/eval_harness_plan.md`.

## Phased delivery

- ✅ **Phase 1** — skeleton + fixtures + smoke scenario.
- ✅ **Phase 2** — deterministic + judge-derived assertion vocabulary; 10 single-turn scenarios.
- ✅ **Phase 3** — LLM-as-judge rubric scorer (default: Claude Haiku 4.5 @ temp 0).
- ✅ **Phase 4** — multi-turn driver wiring + trajectory scorer; 3 multi-turn scenarios.
- ✅ **Phase 5** — `evals/report.py --diff <prev>` for run-over-run comparison.
- ⏳ **Phase 6** — grow the dataset toward ~60–80 scenarios across the full persona × situation matrix.

## Known constraints

- **Personas**: the simulator (`apps/tutoring/student_sim/`) currently implements only `struggler` and `capable`. The `average`, `probe_resistant`, `non_responder` personas are described in `memory/llm_student_simulator_plan.md` but not yet built. Multi-turn scenarios in this eval are limited to the two available personas until the simulator's persona library expands.
- **Cost estimator**: `apps/llm/cost_estimator.py` from the simulator plan hasn't shipped; the harness reports tokens but not USD. `max_total_cost_usd` assertion verb is in the plan but blocked on this.

See `memory/eval_harness_plan.md` for the phase plan.
