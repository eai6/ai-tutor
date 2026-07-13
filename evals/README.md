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

# Run the full suite (400 scenarios — expensive; the 200 multi-turn sessions dominate).
python manage.py run_eval

# Day-to-day: a seeded random sample. Same seed + same dataset = the same draw, so
# every model you compare sees the same scenarios. This replaces the old `core15`.
python manage.py run_eval --multi-turn --sample 60 --seed 0
python manage.py run_eval --single-turn --sample 50 --seed 0

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
├── matrix.py                — the 400-row allocation: persona × lesson × archetype, solved
│                              deterministically BEFORE any content is written
├── gen_multi_turn.py        — generates the 200 multi-turn YAMLs from 12 shape templates
├── authoring_brief.py       — per-lesson brief (real content + assigned rows) for scenario authors
├── lint_dataset.py          — structure + groundedness + balance checks
├── test_dataset_balance.py  — the same checks as a pytest gate
├── dataset_plan.json        — the solved plan (generated; do not hand-edit)
├── dataset/                 — 400 scenario YAMLs (the dataset)
│   ├── math/                — single-turn, math (86)
│   ├── geography/           — single-turn, geography (80)
│   ├── multi_turn/          — full-session trajectory scenarios (200)
│   ├── crosscutting/        — single-turn, legacy grouping (24)
│   ├── format/              — single-turn, legacy grouping (3)
│   ├── pedagogy/            — single-turn, legacy grouping (2)
│   ├── personas/            — single-turn, legacy grouping (5)
│   └── smoke/               — plumbing check (1; excluded from full runs)
│
│   NOTE: the directory a single-turn scenario lives in is now organisational
│   only. The archetype it tests is carried by the plan (dataset_plan.json), not
│   by its folder — the old math/geography/crosscutting/format split was a mixed
│   taxonomy (some by subject, some by failure mode) and no longer means anything
│   the runner or scorer reads. Discovery is a recursive glob.
└── runs/                    — gitignored — per-run JSON result blobs
```

## How a scenario is scored

Every scenario passes only when ALL applicable scoring layers agree.

**Layer 1 — Deterministic assertions** (pure-Python checks; free, instant). Verbs: `response_nonempty`, `must_contain_phrase`, `must_not_contain_phrase`, `must_label`, `must_not_label`, `must_end_with_question`, `meta_reasoning_leak`, `passive_ending`. The `max_paragraphs` verb was removed on 2026-05-27 — the prompt audit dropped length caps entirely (tutor is free to explain at whatever length serves the lesson). Its successors `meta_reasoning_leak` (catches "I shouldn't…", "the student has…", "let me prompt…") and `passive_ending` (catches "take your time", "ready for the next one?") police what actually matters regardless of length. Both default to `false` (the desired state) and are injected into every single-turn scenario by the runner — scenario authors don't need to repeat them.

**Layer 2 — Judge-derived labels** (reuses the production judge pipeline via `apps.benchmark.autopopulate.derive_suggested_labels`). Free; piggybacks on the judge calls the engine already makes.

**Layer 3a — Scenario rubric** (Claude Haiku 4.5 @ temperature 0). Each scenario's `rubric:` block is a list of natural-language pedagogical properties scored 0.0–1.0; the weighted mean must meet `pass_threshold`. Roughly $0.001 per scenario. Carries: (a) scenario-specific items (e.g., *"Treats '90' as incorrect"*) and (b) the 8 BEA-aligned standard items appended by `scripts/migrate_bea_rubric.py`.

**Layer 3b — Universal pedagogical dimensions** (added 2026-05-27). One additional judge call per single-turn scenario returns structured per-dimension verdicts on the same 8 BEA-aligned dimensions, in categorical form (yes/no, or yes/no/encouraging/neutral for `tutor_tone`). All "desirable" dimensions must be at their desired value for the scenario to pass on this layer. Implemented in `evals/scorers/llm_rubric.py::score_pedagogical_dimensions`. The per-dimension pass rates render in the summary (`python -m evals.report`) so individual dimensions can be tracked across runs.

**Prompt-rule coverage** (added 2026-05-27). The eval summary now ends with a `PROMPT-RULE COVERAGE` section showing which prompt rules have at least one eval check. The registry lives in `evals/rule_registry.py`; the contract — "a rule without a check is a process bug" — is locked in by `evals/test_rule_registry.py`. Adding a rule to `apps/tutoring/simple_tutor/prompts.py` without a matching registry entry + check will be visible in the next eval report.

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
- ✅ **Phase 8** (2026-07-13) — dataset to **400** (200 single-turn + 200 multi-turn) on **16
  lessons**, balanced by construction across persona × lesson × archetype; `core15` deleted in
  favour of `--sample N --seed S`. See `memory/eval_dataset_400_plan.md`.

## Known limitations

- **Cost estimator**: `apps/llm/cost_estimator.py` from the simulator plan hasn't shipped; the harness reports tokens but not USD.
- **Lesson coverage**: 16 frozen lessons (8 math + 8 geography).
- **Cost**: a full 400-scenario run is expensive, and the 200 multi-turn sessions dominate it
  (~40x the old core15 subset). Use `--sample N --seed S` for day-to-day runs — the draw is
  seeded, so every model sees the same scenarios — and reserve the full set for publication.
- **No prior sweep is comparable**: sweep 1/2 ran a different dataset on different lessons.
  Sweep 3 is a new baseline.
- **Known defective lesson content**: six of the 16 lessons ship items whose stored answer is
  wrong or self-contradictory. The dataset routes around them; the curriculum still needs
  fixing. See `memory/lesson_content_defects_2026-07-13.md`.

See `memory/eval_harness_plan.md` for the original design plan, and `AI_Tutor_Eval_Harness.docx` (regenerated by `scripts/build_eval_doc.py`) for the comprehensive standalone explainer.
