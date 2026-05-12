# Handoff brief — picking up at Phase 2.2 (2026-05-12)

Hi future Claude. The user just restarted to pick up the `chrome-devtools-mcp` server. This brief is the pointer to where we left off; the durable plans are in `memory/` and `CLAUDE.md`.

## What you should read first (in order)

1. **`CLAUDE.md`** — auto-loaded, but skim it for the Architecture + Conventions sections (the temperature controls + MCP note are recent additions).
2. **`memory/agentic_platform_architecture_plan.md`** — the multi-phase plan we're executing. Phases 1 + 1.x + 2.0 + 2.1 are shipped to main; **Phase 2.2 is next**.
3. **`memory/eval_benchmark_v2_simplified.md`** — the locked benchmark spec (30 labels, 20 failure categories, 3-section item schema). Phase 2.2 builds the UI that conforms to this.
4. **Skills available**: `prompting-fundamentals-expert`, `claude-prompting-expert`, `openai-prompting-expert`, `gemini-prompting-expert`, `architecture-patterns-expert`, `codebase-architecture-expert`, `agent-orchestration-expert`. Use them when they trigger.

## What just shipped (last 5 commits on main)

| Commit | Phase | Description |
|---|---|---|
| `91d665b` | judges/persist | `SessionTurn.judge_outputs` JSONField, populated at combined_judge call site |
| `ae22e61` | 1 | Trace logging foundation: `TurnSpan` model, `ContextVar` span buffer, `BaseLLMClient` Template Method |
| `e1adf79` | 1.x | Per-judge span names via `@traced_judge`, validator + regen spans |
| `72c16f3` | (invariants) | Temperature controls (judge=0, tutoring=[0.1,0.3]), regen max_cycles 3→4, fixed silent TypeError bug |
| `86ab3c9` | 2.0 | `apps/benchmark/` foundation: `BenchmarkItem` + `BenchmarkAnnotation` models, 30-label vocab, admin |
| `9801051` | 2.1 | `sample_benchmark` management command, stratified sampling, auto-derive `suggested_labels` from pipeline trace |
| `3ddfbf5` | (doc) | CLAUDE.md note about chrome-devtools-mcp |

All deployed to Azure on push. Production is capturing per-turn traces + per-judge breakdowns starting from `ae22e61`'s deploy.

## Phase 2.2 — Annotation UI (what to build)

Build a Django dashboard view (NOT just admin) for super-admins to annotate sampled `BenchmarkItem` rows. Spec from `memory/eval_benchmark_v2_simplified.md`:

**Two views minimum:**

1. **List** at `/dashboard/benchmark/` — shows all `BenchmarkItem`s with columns: `item_id`, `subject`, `lesson_title`, `stratum`, annotation count, pass/fail status (from latest annotation), failure_category. Filters by subject/stratum/status. Sort by created_at.

2. **Annotate** at `/dashboard/benchmark/<item_id>/` — shows the frozen snapshot:
   - Left pane: conversation history (rendered turns), then student turn (highlighted), then production tutor response, then pipeline trace summary
   - Right pane: form with label pickers (multi-select checkboxes grouped by source per `labels.py`), `student_claim_correct` tri-state, `expected_labels`, `rationale` textarea, `failure_category` dropdown (predefined list), `safety_concern` bool
   - Pre-fill `actual_labels` from `snapshot['production']['suggested_labels']` — annotator confirms/overrides
   - Save creates or updates `BenchmarkAnnotation` (the model has a unique constraint on `(item, system_variant, annotator)`)
   - Show computed verdict (`passes`, `missing_labels`, `extra_labels`) after save

**Permission**: super-admin only for v1 (Edward solo per the v2 plan).

**Existing patterns to mirror**: `apps/dashboard/views.py` has lots of teacher-facing views with the same shape — find one and copy the pattern. The `templates/dashboard/competency/` directory shows the dashboard's CSS conventions.

## Use chrome-devtools-mcp — don't speculate about rendered output

After your session loads, you should see `mcp__chrome-devtools__*` tools (navigate, click, take_screenshot, evaluate_script, etc.). The pattern for this work:

```
1. Start dev server in background: python manage.py runserver
2. Navigate to http://localhost:8000/dashboard/benchmark/
3. Take a screenshot, inspect what renders
4. Click into an item, take another screenshot
5. Fill the form, save, verify the BenchmarkAnnotation row
```

CLAUDE.md tells you this. Don't write a view and assume it renders right — drive the browser. If `mcp__chrome-devtools__*` tools aren't visible, ask the user to confirm the MCP install propagated.

## Key files for Phase 2.2

| File | Purpose |
|---|---|
| `apps/benchmark/models.py` | `BenchmarkItem`, `BenchmarkAnnotation`. Read the `passes`/`missing_labels`/`extra_labels` computed properties. |
| `apps/benchmark/labels.py` | The 30 labels + 20 failure categories. Source of truth for the form's label picker. |
| `apps/benchmark/sampling.py` | Snapshot shape — your detail view consumes `BenchmarkItem.snapshot`. |
| `apps/benchmark/autopopulate.py` | `derive_suggested_labels()` — already runs at sampling time; just read `snapshot['production']['suggested_labels']` in the view. |
| `apps/dashboard/views.py` | Existing dashboard view patterns to mirror. |
| `templates/dashboard/` | CSS conventions. |

## Sampling for local testing

To get items to annotate while you build:

```bash
python manage.py sample_benchmark --limit 10 --seed 42
```

Local SQLite has 210 eligible turns. Items will have empty `suggested_labels` because local data predates the `judge_outputs` persistence — that's expected. For populated suggestions, you'd need production data (or build a fixture).

## Open questions to surface before coding

Don't start coding the view until these are settled. Ask the user.

1. **List view sorting/pagination defaults?** (Default: newest first, no pagination for v1 at 50 items.)
2. **Multi-annotator handling?** (For v1, single annotator. UI hides the `annotator_user`/`annotator_model` fields — they default to current super-admin user, role=HUMAN.)
3. **System variants in v1?** (Just `production_v1`. UI hardcodes this; future systems get their own annotation rows.)
4. **`failure_category` UI**: dropdown with predefined options (locked vocab from `labels.FAILURE_CATEGORIES`) vs free-text? (Locked is better — drives clustering. Allow `other` for surprises.)
5. **Save behavior**: save-and-next (auto-advance to next unannotated item) vs save-and-stay? (Save-and-next is faster for batch labeling.)

## Architecture invariants (don't violate)

From `CLAUDE.md`:

- Temperature: judges always 0, tutoring clamped to [0.1, 0.3]. Enforced by `ModelConfig.effective_temperature`.
- Regen: max 4 cycles, temperature decays 0.05 per cycle (0.20→0.15→0.10→0.05). Early-exit on judge-clean.
- Multi-tenancy: every user-facing query needs `Q(institution=inst) | Q(institution__isnull=True)`. Benchmark items aren't user-facing per-se — Edward's super-admin view, no institution scoping needed for v1.
- New JSONField content? Document the schema near the writer.
- New evaluator-style component? Mirror the `apps/tutoring/judges/` shape (one file, fail-soft, structured result with `skipped` + `skip_reason`).
- Don't introduce multi-agent decomposition without benchmark evidence — that's Phase 6 (conditional).

## After Phase 2.2

Phase 2.3: scoring + LLM-as-judge cross-check + pass-rate reporting sliced by subject + `eval_layer`. Plan says ~2-3 days. The LLM judge should use a **different model from Opus 4.7** (Gemini 2.5 Pro recommended per the v2 plan, to avoid circular validation).

## Quick sanity checks for your first turn

```bash
# Confirm you're on main with the latest
git rev-parse HEAD     # should be 3ddfbf5 or later
git status --short     # mostly clean; .DS_Store / db.sqlite3 drift fine

# Confirm Phase 2.1 sampling still runs
python manage.py sample_benchmark --limit 5 --dry-run

# Confirm benchmark models load
python manage.py shell -c "from apps.benchmark.models import BenchmarkItem, BenchmarkAnnotation; print('OK')"

# Confirm MCP tools are visible — try this and see if a chrome-devtools tool is callable
# If yes → ready for Phase 2.2
# If no → ask user to confirm restart picked up the server
```

Good luck. Read the plan + benchmark spec first, then start with the list view scaffold + screenshot iteration loop.
