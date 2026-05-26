# AI Tutor Eval Harness — Plan (2026-05-26)

## Problem

We need a **curated, repo-checked-in evaluation suite** that exercises the AI Tutor end-to-end across the 5 personas, produces one comparable number per run, and can be rerun any time — especially after significant changes to the engine, prompts, judges, or model configs.

This is **distinct from** the two existing systems and explicitly does NOT extend them:

| System | What it is | Why it doesn't fit |
|---|---|---|
| `apps/benchmark/` (eval_benchmark_v2_simplified) | Samples real production turns into `BenchmarkItem` snapshots; Edward labels `expected_labels` per item; sampler stratifies by `wrong_answer` / `regenerated` / `judge_flagged` | Requires real-pilot traffic to feed it, requires human labeling per item before each item is usable, and the dataset shifts as new turns are sampled — not reproducible between runs |
| `apps/tutoring/student_sim/` (llm_student_simulator) | Drives synthetic sessions via personas, dumps SessionTurns into the same benchmark sampler | A traffic generator, not an evaluator — produces sessions but doesn't tell you "did the system pass" |

What we want is a third thing: a **fixed test-suite** in the style of `lm-evaluation-harness` / `inspect_ai`, where:
- Inputs are **version-controlled** in the repo. Same git SHA → same dataset forever.
- Each test case carries its **own expected behavior** — no human-in-the-loop per run.
- Coverage is **the persona × pedagogical-situation matrix**, deliberately authored to exercise known failure modes.
- A single command runs the suite end-to-end and emits a comparable pass-rate per persona + per failure category + cost.

We reuse the personas (`apps/tutoring/student_sim/personas.py`), the unified judge (`apps/tutoring/judges/unified.py`), the label vocabulary (`apps/benchmark/labels.py:98`), and the validator (`apps/tutoring/validator.py:314`) as **building blocks**. We do not reuse the sampling pipeline or the `BenchmarkItem` snapshot/annotation flow — that's a different shape of problem.

## Goals

1. Author a fixed dataset of ~80-100 scenarios covering the persona × situation matrix.
2. Single command `python manage.py run_eval` runs all scenarios and emits a single comparable score.
3. CI-grade subset (`--quick`) runs single-turn-only scenarios in under 5 minutes and under $1.
4. Each run writes a dated, sha-tagged JSON result file; `report.py --diff <prev>` shows which scenarios newly passed or regressed.
5. Scenarios are authored once and **rarely revised** — drift is in code/prompts, not in the dataset.

## Non-goals

- Replacing the production-sampling benchmark. That tool answers "what's happening on real traffic?" — orthogonal to "did my change regress known behaviors?"
- Human annotation per run. The dataset bakes expected behavior in at authoring time.
- Continuous nightly runs in CI/Azure. v1 is operator-triggered locally; nightly is a follow-up if signal is valuable.
- Auto-grading entirely by an LLM (option ruled out by user — scoring is hybrid).
- Multi-turn-everywhere (option ruled out by user — single-turn-heavy with multi-turn only where session dynamics matter).

## Conceptual model

```
   dataset/*.yaml         fixtures/lessons.json
        │                          │
        └──────────┬───────────────┘
                   ▼
            evals/runner.py
                   │
                   ▼
         ConversationalTutor.respond()     ← apps/tutoring/conversational_tutor.py:545
                   │
                   ▼
            tutor response + judge_outputs
                   │
                   ▼
     ┌─────────────┼─────────────┐
     ▼             ▼             ▼
  deterministic  unified-judge  LLM-rubric
    scorer         scorer         scorer
     │             │             │
     └──────┬──────┴─────────────┘
            ▼
     scenario.passes (bool)
            │
            ▼
   runs/<ts>-<sha>.json   ← per-run result blob
            │
            ▼
   report.py --diff <prev>   ← regression / improvement view
```

The runner is **a thin loop over scenario files**. Each scenario produces exactly one pass/fail verdict plus per-axis sub-scores. Aggregation is mechanical.

## Directory layout

```
evals/                                        # NEW top-level (sibling of apps/)
├── README.md                                 # how to author, how to run, how to read results
├── dataset/
│   ├── math/
│   │   ├── correct_bare_struggler_001.yaml
│   │   ├── correct_bare_average_001.yaml
│   │   ├── wrong_arithmetic_struggler_001.yaml
│   │   ├── probe_resistant_refusal_chain.yaml
│   │   ├── capable_catches_tutor_error.yaml
│   │   └── …
│   ├── science/
│   │   └── …
│   └── crosscutting/                         # not tied to a subject — exit ticket, safety, format
│       └── …
├── fixtures/
│   ├── lessons.json                          # frozen lesson rows extracted from prod_content_dump.sql
│   ├── institution.json                      # eval-only Institution + simulator-bot User
│   └── README.md                             # how to re-extract / why these lessons
├── runner.py                                 # loads scenarios → drives respond() → scores
├── scorers/
│   ├── __init__.py
│   ├── deterministic.py                      # label match, banned phrases, structural
│   ├── judges.py                             # wraps apps/tutoring/judges/unified.py output
│   └── llm_rubric.py                         # LLM-as-judge for rubric items
├── personas.py                               # re-exports Persona objects from apps/tutoring/student_sim/personas.py
├── report.py                                 # aggregate, diff, pretty-print
├── runs/                                     # gitignored
│   └── 2026-05-26T14-30_abc1234.json
└── tests/
    ├── test_runner.py                        # mock-LLM unit tests on the runner
    └── test_scorers.py
```

The Django entry is one management command:

```
apps/tutoring/management/commands/run_eval.py   # CLI wrapper that calls evals/runner.py
```

## Scenario file schema (the atomic unit)

YAML, one file per scenario. Filename is the scenario `id`. Fields:

```yaml
# evals/dataset/math/correct_bare_struggler_001.yaml

id: correct_bare_struggler_001                # must match filename stem
description: |                                # human-readable; appears in reports
  STRUGGLER gives a correct numeric answer with no working,
  but had shown working two turns earlier. Tutor must ADVANCE,
  not ask for working again (over-eager working request).
persona: STRUGGLER                            # one of the 5 personas
subject: math                                 # math | science | crosscutting
lesson_id: 638                                # references fixtures/lessons.json
tags: [over_eager_working_request, ASK_WORKING_regression]   # free-form, used for grouping in reports

# Two modes — pick one per scenario
mode: single_turn                             # OR: multi_turn

# ────────────── single_turn only ──────────────
seed_history:                                 # canned conversation prefix
  - {role: tutor,   text: "What's 180 - 42?"}
  - {role: student, text: "138 — I did 180-40=140 then took 2 more"}
  - {role: tutor,   text: "Right! Now find 180 - 35."}
student_turn: "145"                           # the message we evaluate the tutor's response TO

# ────────────── multi_turn only ──────────────
# max_turns: 15
# opener: ""                                  # let tutor open; or override
# (no seed_history / student_turn — persona drives the whole session)

# ────────────── assertions (deterministic + judge-derived) ──────────────
assertions:
  must_label:     [ADVANCE]                   # at least one of these must appear in actual_labels
  must_not_label: [ASK_WORKING, BANNED_OPENER]
  must_not_contain_phrase:                    # case-insensitive substring match
    - "show me your working"
    - "walk me through"
  must_end_with_question: false               # math correct-answer case → no probing needed
  max_paragraphs: 1                           # MULTI_PARAGRAPH guard

# ────────────── rubric (LLM-as-judge) ──────────────
rubric:                                       # each item scored 0.0-1.0 by judge LLM
  - "Confirms the student's answer briefly without effusive praise"
  - "Moves to the next item or briefly explains the underlying concept"
  - "Does not leak any answer to a future step"
rubric_judge:                                 # pinned to make scoring reproducible
  provider: anthropic
  model: claude-haiku-4-5
  temperature: 0
pass_threshold: 0.8                           # weighted mean of rubric items

# ────────────── overall pass criterion ──────────────
# scenario.passes = (all assertions pass) AND (rubric_score >= pass_threshold)
```

Multi-turn scenarios use assertions that cover trajectories, not single responses:

```yaml
# evals/dataset/math/probe_resistant_refusal_chain.yaml
mode: multi_turn
persona: PROBE_RESISTANT
max_turns: 15
lesson_id: 638

assertions:
  no_repeated_tutor_phrase_within_window:     # window over the last N tutor turns
    window: 5
    threshold: 3                              # same phrase 3× in 5 turns → fail
  no_label_anywhere: [TOOL_LEAK, THINKING_LEAK, SAFETY_HARMFUL]
  max_total_cost_usd: 0.30                    # session-level budget enforced by runner

rubric:
  - "After two refusals to show working, the tutor changed strategy (e.g., offered scaffolding) instead of repeating the same request"
  - "The tutor never leaked the answer outright"
  - "The session terminated cleanly (completed, exit_ticket, or max_turns) — no infinite loop"
```

### Assertion verbs supported

A small, fixed vocabulary. Adding a new verb is intentional — keeps scenarios from drifting into ad-hoc Python:

| Verb | Meaning | Source |
|---|---|---|
| `must_label` | At least one label in the list must appear in `actual_labels` | `apps/benchmark/labels.py` |
| `must_not_label` | None of these labels may appear | same |
| `must_not_contain_phrase` | Case-insensitive substring absence in tutor text | deterministic |
| `must_contain_phrase` | Case-insensitive substring presence | deterministic |
| `must_end_with_question` | Last sentence ends with `?` | `validator._ends_with_question` (`apps/tutoring/validator.py:296`) |
| `max_paragraphs` | At most N paragraphs in response | deterministic |
| `max_total_cost_usd` | Session-level cost cap (multi-turn only) | runner |
| `no_repeated_tutor_phrase_within_window` | Repetition guard across turns (multi-turn only) | runner |
| `no_label_anywhere` | No tutor turn in the session carries these labels (multi-turn only) | runner |

Anything not expressible in this vocabulary belongs in the rubric.

## Scoring (hybrid, three layers, composed)

User chose **hybrid**. Composition rules:

### Layer 1 — Deterministic (free, instant)

Runs first. Hard pass/fail. Implemented in `evals/scorers/deterministic.py`:
- `must_label` / `must_not_label`: read `actual_labels` set populated by Layer 2.
- `must_contain_phrase` / `must_not_contain_phrase`: substring check on tutor text.
- `must_end_with_question`, `max_paragraphs`: structural.

If any assertion in Layer 1 fails, scenario fails — but Layers 2+3 still run so the report shows *why*.

### Layer 2 — Existing judges (free-ish; cost is the same as a real prod call)

The runner calls `ConversationalTutor.respond()`, which fires the unified judge (`apps/tutoring/judges/unified.py`) and writes `SessionTurn.judge_outputs`. The scorer reads `judge_outputs` and maps it to the 30-label vocabulary using the auto-population logic already implemented in `apps/benchmark/autopopulate.py`. This gives us `actual_labels` for Layer 1's `must_label` / `must_not_label` assertions.

We pin `UNIFIED_JUDGE=on` for eval runs (the production default). Kill-switch only kicked when explicitly testing the legacy judge path.

### Layer 3 — LLM-as-judge rubric (paid, but small)

Only for behaviors Layers 1+2 don't catch — coherence of explanation, appropriateness to persona level, "did the tutor adapt its strategy", etc.

```python
# evals/scorers/llm_rubric.py (sketch)

RUBRIC_JUDGE_SYSTEM = """You are an evaluator of AI tutor responses. \
For each rubric item, judge whether the tutor's response satisfies it. \
Return a JSON object: {"scores": [{"item": "...", "score": 0.0-1.0, \
"reasoning": "..."}, ...]}. Do not explain anything outside the JSON."""

def score_rubric(tutor_response, conversation, rubric_items, judge_config):
    # Single call with all rubric items in one prompt to keep cost low.
    # Pin temperature=0 (CLAUDE.md JUDGE invariant — apps/llm/models.py:225+).
    # Use claude-haiku-4-5 by default; configurable per scenario.
    ...
    return {"score": weighted_mean, "per_item": [...]}
```

The rubric judge is **always invoked even if Layer 1 already failed** — produces a richer report. The scenario only `passes` if BOTH Layers 1+2 pass AND `rubric_score >= pass_threshold`.

### Why not auto-grade with a much stronger model?

Cost. Running rubric on Opus 4.7 across ~100 scenarios would be ~$5-10 per eval. Haiku is more than capable for "does this short response satisfy this short rubric item." If signal degrades in practice, we can per-scenario override via `rubric_judge:` field.

### Flakiness mitigation

LLM-as-judge can give different scores on different calls. We reduce variance by:
1. Pinning model + `temperature=0` per scenario (already in schema).
2. Setting `pass_threshold` conservatively (~0.7-0.8) so single-call noise doesn't flip verdicts.
3. The runner tracks per-rubric-item score and surfaces high-variance scenarios in the report ("scenario X scored 0.85 last run, 0.65 this run — investigate"). Variance attribution is in the report, not the verdict — the verdict is one-shot.

## Coverage — the persona × situation matrix

The dataset deliberately exercises specific failure modes per persona. Not every cell is meaningful; we author the cells that map to known failure modes (most from CLAUDE.md "Critical rules — always apply" + `memory/eval_benchmark_v2_simplified.md` failure categories).

### Math (primary subject — Seychelles pilot is live)

| Pedagogical situation | STRUGGLER | AVERAGE | CAPABLE | PROBE_RESISTANT | NON_RESPONDER |
|---|---|---|---|---|---|
| Correct bare answer, working shown earlier | ✓ over-eager check | ✓ | ✓ | — | — |
| Correct bare answer, no prior working | ✓ ask-working diagnostic OK | ✓ | — | — | — |
| Wrong bare answer | ✓ ask-working as diagnostic | ✓ | — | ✓ refusal handling | — |
| Wrong MCQ answer | ✓ false-accept risk | ✓ | ✓ challenges back | — | — |
| Multi-step problem, error in step 2 | ✓ SURFACE_ERROR specifically | ✓ | — | — | — |
| Tutor makes arithmetic error | — | — | ✓ owns the error | — | — |
| 3× refusal to show work | — | — | — | ✓ no banned-opener loop | — |
| Bare "idk" / "yes" / "ok" chain | — | — | — | — | ✓ non-answer skip |
| Exit ticket boundary | ✓ | ✓ | ✓ | ✓ | ✓ gating |
| Figure-required question | ✓ figure_ref attached | ✓ | — | — | — |

### Science (secondary — Tanzania pilot prep)

Smaller matrix initially; covers the same failure modes with content-grounded variants.

### Cross-cutting (not subject-specific)

| Situation | Personas to cover |
|---|---|
| Student tries to redirect off-topic | STRUGGLER, AVERAGE |
| Safety probe — student mentions self-harm | CAPABLE (most articulate; hardest case for the safety judge) |
| Student asks tutor to write the answer | STRUGGLER, PROBE_RESISTANT |
| Format violations probe (multi-paragraph, info dump) | AVERAGE |
| Lesson with no figures references "the diagram" | crosscutting |

### Coverage targets

- **v1 baseline**: 60-80 scenarios. Math heavy (~50), crosscutting (~15), science minimal (~5-10).
- **Single-turn**: ~80% of scenarios. Fast, deterministic, cheap.
- **Multi-turn**: ~15-20% — only for trajectory-dependent behaviors (refusal chains, repetition, exit-ticket gating).

Cost estimate for one full run:
- Single-turn: ~80 × ($0.005 tutor + $0.003 rubric) ≈ **$0.65**
- Multi-turn: ~15 × ~12 turns × $0.04/turn ≈ **$7.20**
- Total per full run: **~$8**
- `--quick` (single-turn only): **~$0.65**, ~3 minutes wall time.

## Lesson fixtures — extract from prod_content_dump.sql

The dataset references `lesson_id`, which must resolve to a concrete `Lesson` + `LessonStep`s + parent `Course` and `Unit` in the DB. To keep eval results comparable across runs, we **freeze** ~5-10 lessons from the prod dump as the eval's lesson universe.

### Why freeze instead of pointing at the live DB

The live DB regenerates lessons (`Lesson.content_status` flows pending → generating → ready), occasionally deleting and recreating `LessonStep` rows. If the eval pointed at the live DB, a scenario's `current_step_id` could vanish silently, or a step's `expected_answer` could shift — the eval would become flaky for reasons unrelated to engine changes.

### Extraction recipe

One-off script (lives in `evals/fixtures/extract.py`):

1. Parse `prod_content_dump.sql` and select rows where:
   - `Course.subject_type IN ('math', 'science')`
   - `Course.is_published = true`
   - `Lesson.content_status = 'ready'` and `Lesson.is_published = true`
   - `Lesson` has ≥ 4 `LessonStep`s including at least one assessment step
2. Pick **5-10 lessons** by inspection — diverse on subject, presence-of-figures, MCQ vs free-response answer types, grade level.
3. Emit `evals/fixtures/lessons.json` as a Django fixture (`manage.py loaddata`-compatible), containing the picked lessons + all their parent Courses, Units, LessonSteps, and any KnowledgeBase rows they reference.

### Why a Django fixture, not a custom JSON

`ConversationalTutor.respond()` queries via the Django ORM. Easier to seed the dev/test DB with `loaddata` than to mock ORM responses in the runner. Runner does `call_command('loaddata', 'evals/fixtures/lessons.json')` as part of setup; cleans up after.

### Eval-only namespace

To keep eval data isolated from anything else in the dev DB:
- All eval fixtures use IDs in a high range (e.g., `id >= 900000`) so they don't collide with locally-created content.
- One fixture `institution.json` creates `EvalHarness` Institution + `simulator-bot` User to satisfy FKs (`TutorSession.student`, `Lesson.institution_id` via course).
- Optional `--isolate` flag wraps the whole run in a transaction that rolls back at the end (clean DB after eval).

## Runner — execution flow

```python
# evals/runner.py (sketch)

def run_eval(scenarios: list[Scenario], *, max_run_cost_usd: float = 20.0,
             quick: bool = False) -> RunResult:
    if quick:
        scenarios = [s for s in scenarios if s.mode == 'single_turn']

    _ensure_fixtures_loaded()       # loaddata if not already
    inst, student_user = _eval_institution_and_user()
    total_cost = 0.0
    results = []

    for scenario in scenarios:
        if total_cost >= max_run_cost_usd:
            results.append(SkipResult(scenario.id, reason='run_budget_exhausted'))
            continue

        if scenario.mode == 'single_turn':
            result = _run_single_turn(scenario, inst, student_user)
        else:
            result = _run_multi_turn(scenario, inst, student_user)

        total_cost += result.cost_usd
        results.append(result)

    return RunResult(results=results, total_cost=total_cost,
                     git_sha=_git_sha(), started_at=..., finished_at=...)


def _run_single_turn(scenario, inst, student_user) -> ScenarioResult:
    # 1. Create a fresh TutorSession on the scenario's lesson.
    session = TutorSession.objects.create(
        student=student_user, lesson_id=scenario.lesson_id,
        institution=inst, status=ACTIVE,
        is_synthetic=True, sim_persona=scenario.persona,  # tag so prod sampler ignores
        engine_state={'eval_scenario_id': scenario.id},   # provenance
    )

    # 2. Inject seed_history as SessionTurns (role=student/tutor).
    _inject_seed_history(session, scenario.seed_history)

    # 3. Drive ONE call to respond() with the scenario's student_turn.
    tutor = ConversationalTutor(session)
    msg = tutor.respond(scenario.student_turn)

    # 4. Read the SessionTurn just written; pull judge_outputs.
    last_tutor_turn = session.turns.filter(role='tutor').order_by('-id').first()
    actual_labels = autopopulate.derive_labels(last_tutor_turn)

    # 5. Score.
    deterministic_result = score_deterministic(scenario, msg.content, actual_labels)
    rubric_result = score_rubric(scenario, msg.content, session.turns.all())
    cost = (last_tutor_turn.tokens_in, last_tutor_turn.tokens_out) → cost_estimator
         + rubric judge tokens → cost_estimator

    passes = deterministic_result.passes and rubric_result.score >= scenario.pass_threshold

    # 6. Tear down the session (or roll back if --isolate).
    return ScenarioResult(..., passes, cost, ...)
```

Multi-turn flow reuses `apps/tutoring/student_sim/driver.py::SessionDriver` (already built — that part of the simulator is a reusable component, not what we're "following") to actually drive the persona-vs-tutor loop, then runs assertions across the full session.

### Critical: institution scoping (CLAUDE.md non-negotiable)

Every `TutorSession.objects.create` and `Lesson` lookup in the runner sets `institution=eval_inst` explicitly. The eval institution is a real `Institution` row (`evals/fixtures/institution.json`); we don't bypass scoping with `institution=None`. CLAUDE.md flags missing scoping as a "cross-school data leak" risk — eval code must not undermine the invariant even if it's only writing to a local DB.

## Report — comparing runs

`evals/report.py` reads a `runs/<ts>-<sha>.json` blob and emits:

```
$ python -m evals.report runs/2026-05-26T14-30_abc1234.json --diff runs/2026-05-25T10-12_def5678.json

Eval run: abc1234 (2026-05-26 14:30 UTC)
  vs prior: def5678 (2026-05-25 10:12 UTC) — 73 scenarios shared

OVERALL PASS RATE
  this run:  72 / 80  (90.0%)
  prior:     65 / 73  (89.0%)
  Δ:         +1.0 percentage points

BY PERSONA
  STRUGGLER         18/20  (90%)   prior 18/20  ──
  AVERAGE           17/18  (94%)   prior 16/18  ↑
  CAPABLE           14/15  (93%)   prior 13/15  ↑
  PROBE_RESISTANT   11/15  (73%)   prior 10/12  ↑
  NON_RESPONDER     12/12  (100%)  prior  8/ 8  ──

BY FAILURE CATEGORY (from tags)
  over_eager_working_request    2 fails (was 5)  ↓
  false_accept                  1 fail  (was 1)  ──
  banned_opener_loop            3 fails (was 1)  ↑ ⚠
  ...

NEWLY PASSING (3)
  correct_bare_average_004    was: rubric_score=0.71 < 0.80
  multi_step_struggler_007    was: must_not_label=[ASK_WORKING] violated
  ...

NEWLY FAILING (1) ⚠
  probe_resistant_004    rubric_score=0.62 < 0.80
                         "tutor repeated 'walk me through' phrasing after persona refused twice"

COST
  total:  $7.84  (prior $7.31)
  by provider:
    anthropic.claude-opus-4-7:     $5.20
    anthropic.claude-haiku-4-5:    $1.84  (rubric judge)
    google.gemini-2.5-pro:         $0.80  (figure judge)

DURATION
  wall: 27m 18s  (prior 24m 02s)
```

The newly-passing / newly-failing diff is the unit of attention. A regression on one scenario tells you what to investigate; an improvement tells you a fix worked.

## Composition with existing code

| Existing component | How we use it | What we DON'T touch |
|---|---|---|
| `apps/tutoring/student_sim/personas.py` | Import persona system prompts directly. | Don't add new personas in v1; the existing 5 are the matrix axis. |
| `apps/tutoring/student_sim/client.py::StudentClient` | Reuse for multi-turn mode (persona LLM). | — |
| `apps/tutoring/student_sim/driver.py::SessionDriver` | Reuse for multi-turn mode. | Don't change its budget/deadlock logic. |
| `apps/tutoring/conversational_tutor.py::ConversationalTutor.respond` (line 1971) | The entrypoint we drive. | Don't add eval-specific code paths inside the tutor. |
| `apps/tutoring/judges/unified.py` | Fires naturally as part of `respond()`. | Don't bypass `UNIFIED_JUDGE=on`. |
| `apps/benchmark/labels.py:98` | Label vocabulary the dataset references. | Don't extend in v1; freeze. |
| `apps/benchmark/autopopulate.py` | Maps `judge_outputs` → label set. | Reused as-is. |
| `apps/tutoring/validator.py:314` | Structural checks (`_ends_with_question`, paragraph count). | — |
| `apps/llm/cost_estimator.py` (per simulator plan) | Per-turn cost accounting. | If not yet built, blocker for `max_total_cost_usd`. Confirm before Phase 2. |

We do NOT use: `apps/benchmark/sampling.py`, `apps/benchmark/views.py`, `BenchmarkItem` model, `BenchmarkAnnotation` model. Those are for the prod-sampling benchmark — a different evaluation shape.

## Phased delivery

Each phase ships value standalone. Stop after any if the signal isn't worth the next.

| Phase | Goal | Days | Files | Success metric |
|---|---|---:|---|---|
| **1. Fixtures + harness skeleton** | Extract 5-10 lessons from prod dump; bootstrap eval institution; runner can spin up a session, call `respond()` once, write a result blob. No scenarios yet. | 2 | `evals/fixtures/extract.py`, `evals/fixtures/lessons.json`, `evals/fixtures/institution.json`, `evals/runner.py` (skeleton), `apps/tutoring/management/commands/run_eval.py` | `python manage.py run_eval --smoke` creates a session, calls respond, writes runs/*.json with one trivial-pass result. |
| **2. Single-turn scoring (Layers 1+2)** | Deterministic + judge-derived scoring works for single-turn scenarios. Author 10 starter scenarios. | 3 | `evals/scorers/deterministic.py`, `evals/scorers/judges.py`, `evals/dataset/math/*.yaml` (10 files), `evals/personas.py` | Run produces honest pass/fail on 10 scenarios; at least one fails (proves the assertions bite). |
| **3. LLM rubric scorer** | Layer 3 lands. Same 10 scenarios now have rubric scores. | 2 | `evals/scorers/llm_rubric.py`, rubric prompt + JSON parsing + retry | Per-scenario rubric scores stable across 3 reruns (variance check). |
| **4. Multi-turn mode** | Trajectory assertions + driver integration. ~5 multi-turn scenarios. | 3 | `evals/runner.py` (multi-turn path), `evals/scorers/trajectory.py`, 5 multi-turn YAMLs | One PROBE_RESISTANT refusal-chain scenario detects the banned-opener loop reliably. |
| **5. Report + diff** | `report.py --diff <prev>` works; cost breakdown + per-persona table render. | 2 | `evals/report.py` | Diff between two runs shows which scenarios newly passed/failed. |
| **6. Coverage to 60-80 scenarios** | Author the rest of the matrix; tag with failure categories. | 5 | `evals/dataset/**/*.yaml` | All cells in the matrix table populated; coverage report shows ≥80% cells covered. |
| **7. CI hook (conditional)** | `--quick` runs in pre-merge CI on every PR; full run nightly. **Gated on Phases 1-6 producing signal Edward values.** | 3 | `.github/workflows/eval-quick.yml`, cost budget config | PR comments include eval delta. |

**Total Phases 1-6: ~17 focused days.** Phase 7 is conditional.

## Costs

- **Per scenario, single-turn**: ~$0.005 tutor (Opus 4.7) + ~$0.003 rubric (Haiku) ≈ $0.008
- **Per scenario, multi-turn**: ~12 turns × ($0.03 tutor + $0.005 student + $0.005 rubric) ≈ $0.48
- **Full v1 run** (80 single + 15 multi): ~$0.65 + ~$7.20 ≈ **$8** per run
- **`--quick`** (single-turn only): **~$0.65** per run, ~3 min wall time
- **Authoring**: ~10-15 min/scenario × ~80 scenarios = **~15-20 hours** one-time investment

Engine cost dominates; the eval-specific cost (rubric judge) is small.

## Out of scope (deferred)

Explicitly NOT in v1 so they don't sneak in:

1. **Real-pilot replay scenarios.** Hand-curating scenarios from real anonymized session turns. Useful eventually but expensive to authorize; v1 is purely synthetic.
2. **Vision-aware scenarios.** Personas don't see attached images; figure-handling scenarios test text-side behavior only (`FIGURE_REF_UNATTACHED` etc.). Vision rubric is v2.
3. **Cross-language scenarios.** All v1 scenarios in English. Tanzania pilot may need Swahili variants.
4. **Reproducibility across LLM provider drift.** If a provider model is deprecated, scenarios pinned to it will silently fall back. Document in scenario file; surface in report.
5. **Auto-generated scenarios.** Tempting to LLM-generate the matrix; v1 is human-authored to ensure each scenario actually probes a known failure mode.
6. **Per-scenario provider matrix.** v1 runs one provider config per run (whatever's active in `ModelConfig`). Sweeping over providers is a separate eval.
7. **Dashboard UI.** Report is CLI-only. Dashboard view comes after the data signal is validated.

## Open questions

To resolve before Phase 1 starts:

1. **Cost estimator availability.** `apps/llm/cost_estimator.py` is in the simulator plan but I haven't verified it shipped on `pixeldesignlabs-dev`. If missing, Phase 1 must include implementing it. **Action: confirm with user / audit code before Phase 1.**

2. **DB isolation for eval runs.** Options: (a) dedicated `eval` schema/database; (b) transaction-rollback per run; (c) just live in dev DB with high-ID namespace. **Recommend (c)** for v1 simplicity — eval-only Institution + `id >= 900000` lessons. Switch to (a) if pollution becomes a problem.

3. **Where do scenarios live in the repo?** `evals/dataset/` (sibling of `apps/`) vs `apps/tutoring/evals/` (under tutoring app). **Recommend top-level `evals/`** — it's not Django app code, it's test data + a thin runner.

4. **YAML vs JSON for scenario files.** **Recommend YAML** — human-authored, multi-line text fields (rubric items, descriptions), comments allowed. Parsed once at load time so the perf cost is irrelevant.

5. **Lesson selection — who picks the 5-10?** I can propose a slate after parsing `prod_content_dump.sql`; user signs off. **Action: produce a candidate list in Phase 1, get user approval before freezing.**

6. **Rubric judge model default.** Haiku 4.5 is the recommendation. Alternative: Gemini 2.5 Flash (cheaper). **Recommend Haiku** — Claude follows JSON output instructions more reliably than Gemini Flash in practice; cost difference is ~$1 per full run.

7. **Should the run write to a SessionTurn for the eval response?** This means eval data ends up in `tutoring_sessionturn` table. With `is_synthetic=True` it doesn't pollute the prod-sampling sampler — but it does grow the table. **Recommend yes**, reuse the existing persistence; periodically truncate `WHERE session.engine_state['eval_scenario_id'] IS NOT NULL` if size becomes a concern.

8. **Versioning the dataset.** Scenarios are checked in, so git is the versioning layer. Do we need an explicit `dataset_version` field in the report? **Recommend no** — git SHA already pins both code and dataset state.

## Risks

1. **Authoring effort is the binding constraint.** 80 scenarios × 15 min ≈ 20 hours. If user can't sustain the authoring sprint, the dataset stays incomplete and the eval is unreliable. **Mitigate:** start with 10 scenarios that hit known-active failure modes (over-eager working, banned-opener loop, false-accept); ship Phase 5 as soon as those work; expand from there.

2. **Rubric scoring drift.** Haiku may shift its judgment subtly across calls. **Mitigate:** pin temperature=0; conservative pass_threshold (0.7-0.8); per-rubric-item variance tracking in report.

3. **Lesson regeneration in fixture extraction.** If the user later re-extracts from a newer prod dump, scenario `lesson_id` references may not line up. **Mitigate:** fixture extraction emits stable IDs (use a deterministic mapping, not auto-increment); document in `evals/fixtures/README.md`.

4. **Engine changes that break the `respond()` interface.** Phase 1 hard-codes the call shape. **Mitigate:** test_runner.py asserts the contract; eval is part of the surface that breaks if `respond` changes.

5. **Multi-turn deadlock.** A persona refusing to engage could trap the driver. **Mitigate:** existing driver already has `max_turns` + deadlock detection; reuse it.

6. **Synthetic ≠ real distribution.** Pass-rate on this eval may not predict pass-rate on real students. **Mitigate:** report this eval and the prod-sampling benchmark side-by-side, never pool them. Track correlation over time.

7. **Polluting dev DB with eval sessions.** Hundreds of `TutorSession` rows accumulate. **Mitigate:** `is_synthetic=True` tag; periodic cleanup script; optional `--isolate` flag for transaction-rollback runs.

## Composition with related plans

- **`memory/eval_benchmark_v2_simplified.md`** — different shape (prod sampling + human labels). Shares label vocabulary. Both can run; pool only at the metrics-narrative level, never at the dataset level.
- **`memory/llm_student_simulator_plan.md`** — provides the personas and multi-turn driver we reuse. We do NOT route through the simulator's sampler integration (Phase 4 of that plan).
- **`memory/agentic_platform_architecture_plan.md`** — Phase 1 trace logging would give eval runs richer observability. No coupling required; eval works without traces.
- **`memory/benchmark_jsonl_export_plan.md`** — BEA-compatible export is for the prod-sampling benchmark. Not relevant to this harness in v1; could be extended later if external evaluators want to consume our curated suite too.

## Next step

Two paths, pick one to start:

**Path A — bottom-up.** Phase 1: build the harness skeleton + extract lesson fixtures + smoke-test that a trivial scenario runs end-to-end. Defers scenario authoring until the runner works. **Recommended** — shortest path to "does this even run."

**Path B — top-down.** Author 10 scenario YAMLs first (zero code), to ground the schema in real examples. Build harness after, fitting it to the scenarios. Risk: schema iterations cause rework on already-authored scenarios.

Recommend Path A: a 30-minute skeleton run proves the architecture; then scenario authoring rides on top with the schema settled.

---

How to run the Evaluation:
# 1. Extract lesson fixtures from prod_content_dump.sql (only if you
#    don't have evals/fixtures/lessons.json yet, or the dump changed):
python evals/fixtures/extract.py

# 2. Load fixtures into the dev DB:
python manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json


# ─── running the eval ──────────────────────────────────────────────
# Smoke test (plumbing check — single trivial scenario):
python manage.py run_eval --smoke

# Single scenario by id:
python manage.py run_eval --scenario math_correct_advance_001

# Full suite (everything under evals/dataset/ except smoke/):
python manage.py run_eval


# ─── reading results ───────────────────────────────────────────────
# Most recent run summary:
python -m evals.report

# Diff most recent vs the one before it:
python -m evals.report --diff

# Explicit two-run diff:
python -m evals.report evals/runs/<newer>.json --diff evals/runs/<older>.json

Refs: memory/eval_benchmark_v2_simplified.md, memory/llm_student_simulator_plan.md, memory/agentic_platform_architecture_plan.md
