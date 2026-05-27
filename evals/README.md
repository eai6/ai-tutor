# AI Tutor Eval Harness

A curated, repo-checked-in test suite that exercises the conversational tutor end-to-end across 6 student personas (struggler, average, capable, probe_resistant, non_responder, error_prone), two subjects (math + geography), and two execution modes (single-turn response evaluation + full multi-turn session simulation).

Distinct from `apps/benchmark/` (production sampling, requires human labels per item) and `apps/tutoring/student_sim/` (synthetic traffic generation, no scoring). See `memory/eval_harness_plan.md` for the full design and rationale.

## Quick start

```bash
# One-time: load the frozen lesson fixtures into your dev DB.
python evals/fixtures/extract.py                            # re-extract from prod_content_dump.sql (only if you have the dump)
python manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json
python manage.py migrate                                    # apply any pending schema changes

# Run the smoke scenario (~30s, plumbing check).
python manage.py run_eval --smoke

# Run a specific scenario by id.
python manage.py run_eval --scenario math_correct_advance_001

# Run the full suite (~80 scenarios, ~30–90 min wall-clock).
python manage.py run_eval

# Target the simple_tutor engine (going forward, all evaluations use simple_tutor).
SIMPLE_TUTOR_ENGINE=1 python manage.py run_eval

# Inspect the latest run.
python -m evals.report

# Compare the latest two runs.
python -m evals.report --diff

# Explicit diff between two specific runs.
python -m evals.report path/to/run_A.json --diff path/to/run_B.json
```

Each run writes `evals/runs/<timestamp>_<git-sha>.json` with per-scenario detail.

## Layout

```text
evals/
├── README.md                — this file
├── runner.py                — discovers scenarios, drives respond(), persists JSON results
├── report.py                — JSON-in, text-out summary + diff tool
├── personas.py              — re-exports the simulator personas
├── scorers/
│   ├── __init__.py          — AssertionResult dataclass
│   ├── deterministic.py     — Layer 1 + 2 (phrase / structural / judge-label assertions)
│   ├── trajectory.py        — multi-turn verbs (expected_reason, repetition, no_label_anywhere)
│   └── llm_rubric.py        — Layer 3 LLM-as-judge rubric scorer
├── fixtures/
│   ├── extract.py           — parse prod_content_dump.sql → Django fixtures
│   ├── institution.json     — eval Institution + sim-bot User + ModelConfigs
│   └── lessons.json         — frozen Course → Unit → Lesson → LessonStep + ExitTickets
├── dataset/                 — 80+ scenario YAMLs (the dataset)
│   ├── math/                — math-specific failure modes (16)
│   ├── geography/           — geography parallels (10)
│   ├── multi_turn/          — full-session trajectory scenarios (20)
│   ├── crosscutting/        — safety / figure_ref / tool_leak / coherence / etc. (24)
│   ├── format/              — format rule guards (3)
│   ├── pedagogy/            — over-eager-working / diagnostic guards (2)
│   ├── personas/            — one signature behavioural test per persona (5)
│   └── smoke/               — plumbing check (1; excluded from full runs)
└── runs/                    — gitignored — per-run JSON result blobs
```

## How a scenario is scored

Every scenario passes only when ALL applicable scoring layers agree.

**Layer 1 — Deterministic assertions** (pure-Python checks; free, instant). Verbs: `response_nonempty`, `must_contain_phrase`, `must_not_contain_phrase`, `must_label`, `must_not_label`, `max_paragraphs`. `must_end_with_question` was removed on 2026-05-27 — "does the response leave a clear action" turned out to be a semantic judgement no regex could make reliably, so it lives in the rubric layer now.

**Layer 2 — Judge-derived labels** (reuses the production judge pipeline via `apps.benchmark.autopopulate.derive_suggested_labels`). Free; piggybacks on the judge calls the engine already makes.

**Layer 3 — LLM-as-judge rubric** (Claude Haiku 4.5 @ temperature 0, max_tokens 3072). Each scenario's `rubric:` block is a list of natural-language pedagogical properties scored 0.0–1.0; the weighted mean must meet `pass_threshold`. Roughly $0.001 per scenario.

Every scenario's `rubric:` carries:

1. **Scenario-specific items** (3–5 per scenario) — the fine-grained failure-mode detector. e.g., *"Treats '90' as incorrect"*, *"Surfaces the SPECIFIC misconception (used 270 instead of 360)"*.
2. **The 8 BEA-aligned standard items** (universal across all scenarios) — uniform cross-scenario coverage on the dimensions defined in BEA-2025: mistake identification, mistake location, no-answer-reveal, providing guidance, actionability, coherence, tutor tone, human-likeness. Appended to every scenario by `scripts/migrate_bea_rubric.py`.

## Engine target

Going forward, evaluations are run against the **simple_tutor** engine (`apps/tutoring/simple_tutor/`) rather than the original `ConversationalTutor`. Toggle via the `SIMPLE_TUTOR_ENGINE` env var — `evals/runner.py` honours it for single-turn scenarios; `apps/tutoring/student_sim/driver.py` honours it for multi-turn scenarios.

## Phased delivery

- ✅ **Phase 1** — skeleton + fixtures + smoke scenario.
- ✅ **Phase 2** — deterministic + judge-derived assertion vocabulary; 10 single-turn scenarios.
- ✅ **Phase 3** — LLM-as-judge rubric scorer.
- ✅ **Phase 4** — multi-turn driver wiring + trajectory scorer; multi-turn scenarios.
- ✅ **Phase 5** — `evals/report.py --diff <prev>` for run-over-run comparison.
- ✅ **Phase 6** — dataset growth to 80 scenarios across the full persona × situation matrix.
- ✅ **Phase 7** — BEA-aligned standard rubric appended to every scenario; `must_end_with_action` moved from assertion to rubric layer.

## Known limitations

- **Cost estimator**: `apps/llm/cost_estimator.py` from the simulator plan hasn't shipped; the harness reports tokens but not USD.
- **Lesson coverage**: 4 frozen lessons (2 math + 2 geography). Failure categories that need richer fixtures (figure_mismatch, bank_authoring) remain partially covered.

See `memory/eval_harness_plan.md` for the original design plan, and `AI_Tutor_Eval_Harness.docx` (regenerated by `scripts/build_eval_doc.py`) for the comprehensive standalone explainer.
