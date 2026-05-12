# Agentic Platform Architecture — Plan (2026-05-12)

## Problem

The AI tutor today is a **monolithic-prompt-plus-post-hoc-judges** system. One large LLM call (Opus 4.7, ~37–39 KB system prompt) produces a tutor response, then 8 concurrent judges + a 5-layer validator post-evaluate and may trigger regen. The pattern works — correctness gains are real — but it has four growing problems:

1. **Limited observability.** We just shipped per-judge persistence (`SessionTurn.judge_outputs`, 2026-05-11), but there's no per-LLM-call trace, no orchestration event log, no easy way to ask "what fired, in what order, why."
2. **Untyped state.** `engine_state` JSON has no schema. `metadata` and `judge_outputs` overlap. Backward-compat heuristics accumulate.
3. **No measurement loop.** Prompt changes ship blind — the benchmark (v2 plan, 2026-05-11) is locked but the infrastructure to run it isn't built.
4. **Single-prompt ceiling.** Most failures (false accept + leak, lesson drift, incoherent setup) come from one prompt being asked to do too much. The judges catch some but not all, and we have no mechanism to swap or compare prompts.

**Goal:** evolve into a **modular agentic platform** with traceable executions, integrated benchmarking, and data-quality tracking — without a big-bang rewrite. The conservative bias holds: instrument before splitting, measure before refactoring, follow the Rule of Three before extracting.

## Current state (from audit)

Synthesized from `codebase-architecture-expert` audit. **Citations are file:line throughout.**

### Load-bearing patterns to preserve (mirror these)

1. **`BaseLLMClient` factory dispatch** (`apps/llm/client.py:38`, factory at `:714`) — clean ABC + concrete-per-provider + `get_llm_client(config)`. Best abstraction in the codebase.
2. **`judges/` decomposition** (`apps/tutoring/judges/`) — one file per judge, common result-dataclass shape with `skipped` + `skip_reason`, fail-soft concurrent orchestration via `run_all_judges()`. This is the existing template for any future agent-style decomposition.
3. **Purpose-based `ModelConfig`** (`apps/llm/models.py:106`, `get_for(purpose)` at `:225`) — runtime config-driven dispatch. No hardcoded model IDs in tutor/judge code.
4. **Curriculum hierarchy with priority ordering** (`apps/curriculum/models.py:15`) — `LessonStep.priority` (REQUIRED/CORE/ENRICHMENT) lets short sessions drop content without re-planning.
5. **Lesson `content_status` state machine** (`apps/curriculum/models.py:142`) — the most reliable state machine in the codebase. Use as template for future state machines.
6. **`SessionTurn.metadata` + just-shipped `judge_outputs`** (`apps/tutoring/models.py:228, :234`, populated at `conversational_tutor.py:8693`) — per-turn evaluation results persisted as structured JSON.

### Architectural debt (don't propagate; fix when adjacent)

1. **Untyped `engine_state` JSON** (`apps/tutoring/models.py:67`). Keys discovered via grep. Fragile to renames. Has backward-compat hacks for old `phase` field. **Risk:** silent breakage on key drift.
2. **`metadata` and `judge_outputs` overlap** (same row). Judge results live in both — flattened in `metadata` for legacy dashboard, structured in `judge_outputs` for benchmark. **Risk:** two sources of truth.
3. **Multi-tenancy query duplicated 10+ times** (`Q(institution=inst) | Q(institution__isnull=True)` in `apps/tutoring/views.py:73,87,293,331`, `apps/dashboard/views.py`, etc.). **Past Rule-of-Three threshold; needs extraction.**
4. **Vision judge silent-skip** (`apps/tutoring/judges/__init__.py::run_all_judges`). Optional `vision_client`, `image_reader`, `attached_media` callbacks — silently skips if any missing. **Risk:** feature appears to work but never fires.
5. **Backward-compat heuristics** (`Course.is_math` falls back to `MATH_KEYWORDS`, `LessonStep.priority` defaults to REQUIRED for legacy rows). Exist because old data wasn't backfilled. **Risk:** behavior depends on data vintage.
6. **`CombinedJudgeResult` 26 flat fields** (`apps/tutoring/combined_judge.py:55`). Bloated; many fields only meaningful for certain judges. New `to_judge_outputs()` already shows the better nested shape — `to_metadata()` is the legacy one to retire.

### Existing agent-orchestration patterns (already there, just informal)

1. **Concurrent fail-soft judges** = the closest thing to a multi-agent pattern. Each judge is a focused specialist. No coordination overhead because they're parallel. This is the right model for THIS codebase.
2. **Draft→audit→revise via validator regen** (`apps/tutoring/validator.py:194`, regen ensemble at `apps/tutoring/regen/`) = Self-Refine-style loop, capped at one regen cycle. Already exists; just needs more instrumentation.
3. **Deterministic-anchored LLM eval** (`apps/tutoring/conversational_tutor.py:6691`) — deterministic numeric/MCQ check fed into the LLM judge as `deterministic_verdict`. Cost-aware routing in microcosm.

### Where it's still monolithic

The `respond()` loop at `apps/tutoring/conversational_tutor.py:1591` does everything in one ~350-line system prompt: pedagogy + math rules + format rules + DOK guidance + media signaling + step phase context. **This is the eventual decomposition target — but only after the benchmark proves which sub-responsibility is the bottleneck.**

## Target design — north star

A **modular agentic tutoring platform** for THIS project specifically. Not a wholesale rewrite — an evolution that mirrors patterns already present.

### Components (in priority order)

1. **Trace logging layer** — every LLM call, every tool call, every judge fire, every regen decision emitted as a structured event. Per-turn trace stored in DB; per-session aggregate queryable. OpenTelemetry GenAI conventions adopted for span shape but not necessarily the full OTel SDK (lightweight JSON spans are fine for v1).
2. **Typed state schemas** — Pydantic models for `engine_state`, `judge_outputs`, `orchestration_trace`. Read/write through them. Backfill validates on read, logs unknown keys.
3. **Benchmark infrastructure** — implements `memory/eval_benchmark_v2_simplified.md`. `BenchmarkItem` + `BenchmarkAnnotation` models. Sampling management command. Annotation UI in super-admin. Auto-population from `judge_outputs`.
4. **Lesson graph extension** — `LessonStep` gains `allowed_next_step_ids`, `forbidden_tutor_moves`, `advancement_rule`, `remediation_step_id`. Engine consumes graph to drive advancement. Backfill is per-lesson via LLM-assisted authoring.
5. **Risk-aware routing** — pre-generation classifier marks turn `low_risk` (e.g., "yes", "help", clarification) vs `high_risk` (math claim, factual statement). Low-risk skips combined_judge to cut cost. Mirrors `eval_layer` deterministic short-circuit pattern.
6. **Modular agent decomposition (DEFERRED)** — split monolithic tutor prompt into named agent roles (interpreter, generator, auditor, pedagogy reviewer, orchestrator). **Only built after benchmark measures the monolithic prompt as the bottleneck.** Mirrors `BaseLLMClient` + `judges/` patterns.

### Cross-cutting

- **Data-integrity tracking** — per-prompt-version label-frequency snapshots, drift detection on judge accuracy, label-distribution dashboards. Built on top of the trace + benchmark, not separately.
- **Testing** — integration tests that assert on the **orchestration trace** (events fired in expected order, judges triggered when expected), not just final response text. Existing tests in `apps/tutoring/tests/` are mostly response-text assertions.

### Architectural principles applied (sources: `architecture-patterns-expert`, `agent-orchestration-expert`)

- **Conservative abstraction**: extract only at Rule of Three (multi-tenancy query is past it; agent decomposition is NOT yet).
- **Modular monolith, not microservices**: every component lives in this Django app. No service extraction.
- **Simplest first**: instrument before splitting. Measure before refactoring.
- **Mirror existing patterns**: `BaseLLMClient` factory for any new pluggable component; `judges/` shape for new evaluators.
- **Structured outputs from critics**: any new agent emits JSON, never prose.
- **Cap retries at every level**: per-call timeouts, per-loop cycle caps, per-session LLM-call budget.

## Data model changes (summary across phases)

### New tables

```python
# apps/tutoring/models.py
class TurnSpan(models.Model):
    """One span per LLM call / tool call / judge fire within a turn."""
    turn = models.ForeignKey(SessionTurn, related_name='spans', on_delete=models.CASCADE)
    kind = models.CharField(choices=[('llm_call','llm_call'), ('tool_call','tool_call'),
                                     ('judge','judge'), ('regen','regen'), ('audit','audit')])
    name = models.CharField(max_length=100)         # 'generate_response', 'arithmetic_judge', etc.
    model = models.CharField(max_length=80, blank=True)   # for llm_call
    purpose = models.CharField(max_length=40, blank=True) # ModelConfig.purpose
    started_at = models.DateTimeField()
    duration_ms = models.PositiveIntegerField()
    tokens_in = models.PositiveIntegerField(null=True)
    tokens_out = models.PositiveIntegerField(null=True)
    cost_estimate_usd = models.DecimalField(max_digits=8, decimal_places=5, null=True)
    payload = models.JSONField(default=dict)        # structured inputs/outputs
    error = models.TextField(blank=True)
```

```python
# apps/benchmark/models.py (new app)
class BenchmarkItem(models.Model):
    item_id = models.CharField(max_length=80, unique=True)
    source_turn = models.ForeignKey(SessionTurn, on_delete=models.SET_NULL, null=True)
    subject = models.CharField(max_length=20)
    lesson_id = models.IntegerField()
    snapshot = models.JSONField()                   # frozen item per benchmark v2 schema
    created_at = models.DateTimeField(auto_now_add=True)

class BenchmarkAnnotation(models.Model):
    item = models.ForeignKey(BenchmarkItem, on_delete=models.CASCADE, related_name='annotations')
    annotator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True)
    system_variant = models.CharField(max_length=40)  # 'production_v1', 'stripped', etc.
    actual_labels = models.JSONField()
    expected_labels = models.JSONField()
    student_claim_correct = models.BooleanField()
    rationale = models.TextField()
    failure_category = models.CharField(max_length=60, blank=True)
    safety_concern = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
```

### New JSONField on existing tables

```python
# TutorSession — cumulative session-level trace summary
session_trace = models.JSONField(default=dict, blank=True)
# {total_llm_calls, total_tokens, total_cost, regen_count, ...}
```

### Extended `LessonStep` (Phase 4)

```python
# apps/curriculum/models.py
allowed_next_step_ids = models.JSONField(default=list, blank=True)
forbidden_tutor_moves = models.JSONField(default=list, blank=True)  # subset of v2 benchmark action labels
advancement_rule = models.JSONField(default=dict, blank=True)
# {"requires": "answer_correct", "min_exchanges": 2, "fast_path_on_deterministic": true}
remediation_step_id = models.IntegerField(null=True, blank=True)  # which step to route to on failure
```

### Pydantic schemas (Phase 3)

`apps/tutoring/schemas.py` (new file):

```python
class EngineStateSchema(BaseModel):
    session_state: Literal['tutoring', 'exit_ticket', 'completed']
    current_topic_index: int
    target_minutes_override: Optional[int] = None
    question_pool_ids: List[int] = Field(default_factory=list)
    rendered_bank_ids: List[int] = Field(default_factory=list)
    # ... etc. — codified from grep of current usage

class JudgeOutputsSchema(BaseModel):
    step_eval: StepEvalSchema
    arithmetic: ArithmeticSchema
    # ... — mirrors CombinedJudgeResult.to_judge_outputs() shape
```

## Backend changes — by phase (detailed in phased delivery below)

## Frontend/mobile changes

**Minimal across phases 1–5.** The annotation UI in Phase 2 is the only new frontend surface:

- `templates/dashboard/benchmark/list.html` — list of items, filter by subject / status
- `templates/dashboard/benchmark/annotate.html` — split-pane: conversation + production response on left, label-picker on right
- `apps/dashboard/views.py` — `benchmark_list`, `benchmark_annotate`, `benchmark_save_annotation` view functions
- Standard form-based Django (no React/Vue). Mirrors existing dashboard pages.

## Out of scope (explicitly deferred)

These are NOT part of this plan. Calling them out so they don't sneak in:

1. **Multi-agent decomposition of the main tutor prompt.** Until the benchmark measures the monolithic prompt as the dominant failure source AND a specific sub-responsibility we'd split, this is premature. Phase 6 in the table below is conditional, not committed.
2. **Service extraction.** Trace logging stays in-process. Benchmark scoring stays in-process. No microservices, no separate Python workers. Modular monolith stance.
3. **Full OpenTelemetry SDK adoption.** Following OTel GenAI semantic conventions for span *shape* is in scope. Running the OTel collector/exporter is not — JSON in Postgres is sufficient for v1.
4. **Rewriting `CombinedJudgeResult`'s 26-field shape.** Keep `to_metadata()` for legacy dashboard; new code reads `to_judge_outputs()`. Retirement happens organically.
5. **Replacing `engine_state` JSONField with normalized columns.** Pydantic typing is the v1 fix. Column normalization would be a v2 refactor with downtime risk.
6. **Annotation UI for non-super-admin users.** Phase 2 scopes to super-admin only.
7. **Cross-language / cross-school benchmark federation.** v1 is single-language, Seychelles pilot data only.
8. **Replacing Opus 4.7 as the tutoring model.** Current model is load-bearing per `memory/archives/judge_disagreements_audit.md`. Don't touch without measured benchmark evidence.

## Phased delivery

Each phase ships value standalone. No phase depends on the next; you can stop after any phase and ship.

| Phase | Goal | Days | Files | Mirrors pattern | Success metric | Risk |
|---|---|---:|---|---|---|---|
| **1. Trace logging foundation** | Every LLM call + judge fire emits a structured span. Per-turn trace queryable. | 4–6 | `apps/tutoring/models.py` (new `TurnSpan`), `apps/llm/client.py` (wrap `generate()`), `apps/tutoring/conversational_tutor.py` (instrument call sites + regen), `apps/tutoring/judges/__init__.py` | OTel GenAI conventions (span shape only) | Every prod tutor turn has ≥3 spans (generate + judge fan-out + validator); cost-per-turn queryable via SQL | Span volume in DB. **Mitigate:** keep TurnSpan rows; periodic archive job to cold storage for sessions >90 days. |
| **2. Benchmark infrastructure** | Implements `eval_benchmark_v2_simplified.md`. 50 items labeled. Baseline pass rate reported. | 8–12 | New `apps/benchmark/` (models, views, management commands, templates), `templates/dashboard/benchmark/` | `apps/safety/` shape (sub-app with its own models + admin) | 50 items annotated, baseline pass rate computed sliced by subject + `eval_layer` | LLM-as-judge model choice. **Mitigate:** lock the cross-check model (Gemini 2.5 Pro recommended) before scoring. |
| **3. Typed state + multi-tenancy dedup** | Pydantic schemas for `engine_state` and `judge_outputs`. Multi-tenancy query extracted to manager method. | 3–5 | `apps/tutoring/schemas.py` (new), `apps/tutoring/conversational_tutor.py` (read/write through schemas), `apps/curriculum/managers.py` (new), `apps/tutoring/models.py` (add managers) | `dataclass` + factory pattern already in `apps/llm/client.py::LLMResponse` | All `engine_state` mutations go through `EngineStateSchema`; `Course.objects.visible_to(inst)` replaces 10+ inline Q queries | Mass refactor risk. **Mitigate:** the schema initially is permissive (extra fields allowed); strict mode flipped after one prod cycle. |
| **4. Lesson graph state machine** | `LessonStep` gains graph fields. Backfill for 5–10 lessons. Engine consumes `advancement_rule` instead of generic step counter. | 8–10 | `apps/curriculum/models.py` (migration), `apps/curriculum/management/commands/backfill_lesson_graph.py` (new), `apps/tutoring/conversational_tutor.py::_should_advance_step` (read graph) | `LessonStep.priority` + `Lesson.content_status` state machine pattern | Benchmark `premature_advance` + `lesson_drift` category rates drop ≥30% on the 50-item set | Backfill quality. **Mitigate:** LLM-assisted draft + Edward review per lesson; only backfill lessons in the benchmark sample first. |
| **5. Risk-aware routing** | Pre-generation classifier routes low-risk turns through cheap fast-path (skip combined_judge). | 4–6 | `apps/tutoring/conversational_tutor.py` (new `_classify_turn_risk` + dispatch), `apps/llm/models.py` (`Purpose.RISK_CLASSIFIER`) | `eval_layer` deterministic-anchored short-circuit pattern | Avg cost/turn drops ≥20% on prod sample without `pass_rate` regression on benchmark | Mis-classification routes high-risk to cheap path. **Mitigate:** start conservative (only "yes", "help", single-word affirmations route cheap); expand only after benchmark evidence. |
| **6. Modular agent decomposition (CONDITIONAL)** | Split monolithic tutor prompt into named agent roles. Requires Phase 2 + 4 metrics showing single-prompt is the bottleneck. | 15–25 | New `apps/agents/` package, `BaseAgent` ABC, `OrchestrationGraph` model | `BaseLLMClient` + `judges/` patterns | Specific failure categories shrink (decided by Phase 2 data) | Big architectural change. **Do NOT start without measured bottleneck.** |

**Total phases 1–5: ~30 focused days. Solo-dev calendar time: ~10–12 weeks with existing pilot maintenance work running in parallel.**

## Testing infrastructure (cross-cutting, by phase)

| Phase | New tests |
|---|---|
| 1 | `TurnSpan` integration test: replay one turn through `respond()`, assert ≥3 spans persisted with expected `kind`s. |
| 2 | Annotation flow E2E: create item, save annotation, verify `verdict.passes` computed correctly. Auto-population test: from a fixture `judge_outputs`, assert expected labels pre-fill. |
| 3 | Schema validation tests: rejected payload for unknown enum values; permissive mode logs warning; strict mode raises. |
| 4 | Graph-driven advancement: 4 scenarios (correct → advance, wrong → retry, ambiguous → clarify, repeated wrong → remediate). Each replays a canned conversation and asserts `current_topic_index` matches expectation. |
| 5 | Risk classifier tests on 20 canned inputs (10 low, 10 high). Asserts classifier decision matches ground truth ≥90% on this set. |

**Anti-pattern guard:** none of these are "assert tutor response contains string X" tests. They assert on orchestration trace + state transitions. Mirrors agent-orchestration-expert principle: "log the trace, not just the answer."

## Data-integrity tracking (built on top of Phase 1 + 2)

After Phase 2 ships, a small dashboard + cron renders:

- **Per-prompt-version label frequency** — Are `UNFOUNDED_PRAISE` flags increasing after a prompt change?
- **Judge agreement rates** — Where do `arithmetic` and `factual` judges disagree on the same turn?
- **Cost-per-turn distribution** — Is the long tail growing? Which lesson is the hottest?
- **Regen rate** — Is the validator triggering more often? Which validator rule is firing most?

Built as Django dashboard views over `TurnSpan` + `BenchmarkAnnotation`. No external analytics tool. Following the modular-monolith stance.

## Open questions

Resolve before Phase 1 starts:

1. **Pydantic version + dependency footprint** — Add `pydantic>=2.0` to `requirements.txt`? Pydantic 2 is fast and well-suited for this use. **Recommend: yes, Pydantic 2.x.** Cost: ~6 MB image bloat. Benefit: typed schemas across the codebase.

2. **OpenTelemetry adoption level** — Just the span schema, or full OTel SDK + collector? **Recommend: span schema only for v1.** JSON in Postgres is sufficient; OTel collector adds operational complexity. Revisit after Phase 2 metrics if cost-per-turn analytics demand it.

3. **Benchmark scoring runtime** — In-app (synchronous on annotation save) or separate worker? **Recommend: in-app for v1.** Scoring is fast (set comparison). Worker introduces async complexity. Revisit only if scoring expands to re-running production through modified prompts.

4. **Annotation UI scope** — Super-admin only, or open to other roles? **Recommend: super-admin only for v1.** Per `memory/eval_benchmark_v2_simplified.md` we agreed Edward + LLM-judge cross-check.

5. **Risk-classifier model** — Cheap LLM (Haiku 4.5)? Or deterministic regex/heuristic? **Recommend: deterministic regex first.** "Yes", "help", "ok", single-word inputs — these are caught by regex. LLM classifier introduces another LLM call which defeats the cost savings. Only escalate to LLM if regex coverage proves insufficient.

6. **Lesson graph backfill scope** — All ~100 lessons or just benchmark sample? **Recommend: benchmark sample first** (5–10 lessons). Validate the graph schema with concrete cases. Bulk backfill after schema stabilizes.

7. **Phase 6 trigger threshold** — At what benchmark pass rate do we commit to agent decomposition? **Recommend:** if pass rate plateaus below 70% strict after Phases 1–5, and ≥3 failure categories share root cause "single prompt asked to do too much," then trigger Phase 6.

## Risks

1. **Span row growth.** Per-turn 3–5 rows × 100 sessions/day × 30 days = ~30k rows/month. Manageable, but archive policy is needed. **Mitigation:** monthly archive job to compressed Parquet on Azure Blob.
2. **Prod migration of `engine_state` schema.** Existing sessions have varied keys. **Mitigation:** Pydantic in permissive mode for one prod cycle; strict mode after monitoring `unknown_engine_state_key` log volume.
3. **Annotation labeling is bottlenecked on Edward.** 50 items × 5 minutes = ~4 hours, but the per-item time may grow. **Mitigation:** auto-population from `judge_outputs` (already shipped) carries 12 of 30 labels — Edward only authors 4 pure-judgment ones + expected_labels.
4. **Phase 4 lesson graph contradicts dynamic teaching.** A rigid graph might fight pedagogically valid digressions. **Mitigation:** `forbidden_tutor_moves` is small and conservative initially. Most teaching moves remain allowed by default.
5. **Phase 5 risk-classifier mis-fires.** Routing a math claim to fast-path = no judge verification = bad. **Mitigation:** "When in doubt, route full-path." Classifier must be high-precision on `low_risk`, not high-recall.

## Next step

**Confirm the open questions above** (especially #1, #2, #3, #5, #7). Then start Phase 1: write the `TurnSpan` model, generate the migration, instrument the three call sites in `apps/llm/client.py` (`AnthropicClient.generate`, `OpenAIClient.generate`, `GeminiClient.generate`) to emit a span per LLM call.
