# Large Textbook Parsing — Hardening Plan (2026-05-14)

## Problem

Two ~270-page geography textbooks (Kelly *Complete Geography*, *The New Wider World 3rd ed.*) fail to parse via the teaching-material upload pipeline. Both Rich and Fast modes return `Could not extract meaningful text from document` with 0 chunks. Pilot teachers can't use the materials they have on disk.

Root cause is **not** simple "size" — it's that the OCR fallback path is unsuited for large scanned PDFs. Specifically:

1. `_extract_pdf_with_vision` (`apps/curriculum/curriculum_parser.py:349`) renders **every page** at 200 DPI as PNG into RAM **before** the first LLM call. A 272-page textbook = ~135–500 MB of base64-encoded images held in process memory.
2. Then 27 sequential vision LLM calls (10 pages each, ~30–60 s/call) — total wall clock 15–45 min.
3. Any single batch failure throws an exception that's caught at `apps/curriculum/curriculum_parser.py:144` and **silently swallowed**, returning the original near-empty embedded text. The user sees the generic "could not extract meaningful text" error, not the real cause (rate limit / timeout / OOM / oversized image).
4. The whole job runs in `threading.Thread(daemon=True)` (`apps/dashboard/background_tasks.py:54`) inside a gunicorn worker. If the worker is recycled (120 s timeout, OOM, scale event, **a code deploy**) the daemon thread is killed mid-run; the row stays `processing` or moves to `failed`.
5. **Fast mode is not actually fast for figure-rich PDFs.** `index_teaching_material` (`apps/curriculum/knowledge_base.py:670`) calls `extract_figures_from_pdf` which itself runs vision LLM on every page detected as containing a figure. A geography textbook is mostly figure-bearing pages, so Fast hits the same vision-call avalanche.

## Decisions confirmed (user, 2026-05-14)

- **Extract every page — no sampling.** Cost is acceptable; coverage is non-negotiable for textbook KB.
- **Survive container restarts**, including code deploys mid-job.
- **Hybrid routing by size, not by material type.** Materials with **<50 pages** use the existing in-process background-thread pipeline (fast UX, no cold-start, no confirm screen). Materials with **≥50 pages** dispatch to an Azure Container Apps Job. The threshold is a single source of truth — `MATERIAL_JOB_PAGE_THRESHOLD = 50`. Same rule applies to textbooks, references, worksheets, notes, question banks.
- **Execution model for large materials: Azure Container Apps Job.** Same Docker image as main app, on-demand only (zero idle cost), runs to completion in its own isolated container, survives main-app restarts/deploys natively.
- **Pre-confirmation cost estimate** — but only for Job-routed materials (≥50 pages). Small materials skip the screen entirely; large ones see "272 pages → est. ~$4.50 LLM spend, ~25 min. [Confirm and start]".

## Current state (from audit)

**Entry points**
- Upload form → `apps/dashboard/views.py:2683` (`material_upload_to_course`)
- Background dispatch → `run_async()` in `apps/dashboard/background_tasks.py:54` (daemon thread, no persistence)
- Two pipelines: `process_teaching_material` (Rich, line 16) / `process_teaching_material_fast` (Fast, line 132) in `apps/dashboard/material_tasks.py`

**Where vision is invoked**
- Text-OCR fallback for scanned PDFs: `apps/curriculum/curriculum_parser.py:349` `_extract_pdf_with_vision` — full doc, 10-page batches, 200 DPI
- Figure description (Fast + Rich both): `apps/curriculum/curriculum_parser.py:177` `extract_figures_from_pdf` — every figure-bearing page, 100 DPI
- Rich-mode structured extraction: `apps/dashboard/material_tasks.py:213` `extract_material_with_vision` — every page, 5-page batches, 150 DPI

**State model** (`apps/dashboard/models.py:93`)
- `status` (pending|processing|completed|failed)
- `error_message` (TextField — currently truncated to 500 chars in Rich, untruncated in Fast)
- `processing_log` (TextField — append-only via `add_log`)
- `extracted_text_length`, `chunks_created`, `figures_extracted` (IntegerField — only written at end)
- **No** `pages_total`, `pages_processed`, `phase` field for partial progress

**Azure infra** (`infra/__main__.py`, Pulumi stack `pixel`)
- Container App Environment (D4 workload profile)
- Container App `aitutor-pixel-app` — main web app
- ACR `aitutorpixelacr.azurecr.io`, PostgreSQL Flexible Server, File Share mount
- **No Container Apps Job resource yet** — needs to be added

**What's already good** (don't reinvent)
- Per-image size guards with DPI fallback (`MAX_IMAGE_BYTES = 4_500_000`) — keep
- `add_log` infrastructure for streaming visibility — keep
- Status state machine — extend, don't replace

## Target design

### A. Hybrid routing — small in-process, large on Job

Single page-count gate at upload time:

```python
# apps/dashboard/material_routing.py (new)
MATERIAL_JOB_PAGE_THRESHOLD = 50

def should_dispatch_to_job(file_path: str) -> tuple[bool, int]:
    """Returns (use_job, page_count). Cheap PyMuPDF call."""
    page_count = _count_pdf_pages(file_path)  # ~50ms for any size
    return page_count >= MATERIAL_JOB_PAGE_THRESHOLD, page_count
```

- `pages < 50` → existing path: `run_async()` daemon thread on the main app, no cost-confirm screen, instant dispatch. Same UX teachers have today for worksheets, short references, single-lesson notes.
- `pages ≥ 50` → cost-confirm screen → Container Apps Job dispatch.

The same routing function is the single source of truth — no per-type branching, no separate textbook code path. A 70-page worksheet routes to the Job; a 30-page textbook stays in-process.

### B. Azure Container Apps Job (large materials only)

A new Container Apps Job named `aitutor-pixel-material-job`. Same Docker image as the main app (single CI build, single ACR). Triggered manually from Django via Azure Management SDK (`azure-mgmt-app`). Each invocation runs `python manage.py process_material <upload_id>` and exits. **Job-replica timeout: 24 h** (86,400 s) — generous upper bound chosen so we never have to revisit it for a larger textbook or a temporary rate-limit slowdown. A typical 270-page run is ~25 min; this just keeps the door open.

**Cost shape**: on-demand only. The Job container does not exist between executions — zero idle cost. Per-execution compute ≈ $0.10 for a 25-min run; LLM API spend dominates (~$3-8 per textbook).

Local dev parity: when `DJANGO_SETTINGS_MODULE=local`, the same management command is invoked via `subprocess.Popen` instead of Azure SDK — devs don't need Azure to test the pipeline.

### C. Stream page rendering + concurrency

Refactor the two vision passes (`_extract_pdf_with_vision`, `extract_material_with_vision`) to:
- Iterate pages in batches of 10 — render each batch's pixmaps **just before** the LLM call, discard after.
- Use `ThreadPoolExecutor(max_workers=5)` to parallel-dispatch batches. Anthropic comfortably handles 5 concurrent vision requests; the existing `AnthropicClient` retry-with-backoff catches occasional 429s. Memory ceiling per batch (~10 pages × 4 MB) × 5 workers ≈ 200 MB peak — fits the D4 with headroom.

Trade-off: 5× concurrency drops a 270-page textbook from ~2h sequential to ~25 min. Cost is unchanged (per-token billing).

These improvements apply to both code paths — small materials running in-process also get streaming + concurrency, just lower-stakes (a 30-page worksheet in 5-way concurrent vision = ~3 min wall clock, fine for the in-process path).

### D. Per-batch checkpointing (Job path only)

After every batch completes in the Job, persist:
- `pages_processed` (count of pages whose extracted text + figures are committed)
- `phase` (e.g. `vision_ocr_p140_of_272`)
- All extracted chunks indexed into ChromaDB with deterministic IDs `(upload_id, page_idx, chunk_idx)` so a re-run skips already-indexed pages

A code deploy mid-run kills the Job's container. The Container Apps Job auto-restart policy re-runs it; the new run reads `pages_processed` and resumes at the next batch boundary. Worst case: the in-flight batch is redone (10 pages of redundant LLM calls — acceptable).

In-process path keeps simple progress counters but doesn't need resume logic — small jobs that crash get a "Reprocess" button click.

### E. Surface the *real* error

Stop swallowing exceptions in the vision OCR fallback. Replace the broad `except Exception` at `apps/curriculum/curriculum_parser.py:144` with structured handling — log + record the underlying error on the upload's `error_message` (JSON-encoded for UI rendering), and re-raise typed `OCRFailure(reason='rate_limit'|'timeout'|'oversized'|'unknown', detail=str)`. Applies to both paths.

### F. Decouple Fast mode from vision

`process_teaching_material_fast` becomes **truly text-only**. Add `extract_figures: bool = False` to `index_teaching_material`; Rich mode passes True, Fast passes False. Fast then becomes the safety valve: "I just want the text indexed, no LLM calls." Applies to both paths.

### G. Pre-dispatch cost estimate + confirmation (Job path only)

For materials ≥50 pages, replace the current "upload → fire-and-forget" flow with a two-step:
1. **Upload** stores the file, runs the page-count routing check, and (if Job-bound) creates a `TeachingMaterialUpload` row with `status='pending_confirmation'`. Estimates: pages × per-call token avg × current model price → flashes summary.
2. **Confirm** flips status to `pending` and dispatches the Container Apps Job.

Materials <50 pages skip this entirely — the existing instant-dispatch UX is preserved.

## Data model changes

`TeachingMaterialUpload` (`apps/dashboard/models.py:93`) — additive only:

```python
pages_total = models.IntegerField(default=0)
pages_processed = models.IntegerField(default=0)
phase = models.CharField(max_length=64, blank=True)  # e.g. "vision_ocr_p140_of_272"
estimated_cost_usd = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
estimated_duration_seconds = models.IntegerField(null=True, blank=True)
job_execution_name = models.CharField(max_length=128, blank=True)  # Azure Job execution ID
```

Add `'pending_confirmation'` to the `status` choices.

Migration `0014_teaching_material_progress.py` — additive, no data backfill.

## Backend changes

| File | Change |
|------|--------|
| `apps/curriculum/curriculum_parser.py:349` | Refactor `_extract_pdf_with_vision` to stream pages, accept `progress_cb`, `start_page`, raise typed `OCRFailure` |
| `apps/curriculum/curriculum_parser.py:177` | Same streaming refactor for `extract_figures_from_pdf`; deterministic chunk IDs per (upload_id, page) |
| `apps/curriculum/curriculum_parser.py:123` | Replace silent `except Exception` with typed catch + log + propagate |
| `apps/dashboard/material_tasks.py:213` | Stream + progress_cb + checkpoint for `extract_material_with_vision`; add 5-way ThreadPoolExecutor |
| `apps/curriculum/knowledge_base.py:608` | Add `extract_figures: bool = False` and `start_page: int = 0` to `index_teaching_material` |
| `apps/dashboard/material_tasks.py:132` | Fast path: pass `extract_figures=False` |
| `apps/dashboard/material_tasks.py:16` | Rich path: pass `extract_figures=True` |
| `apps/dashboard/material_tasks.py:*` | Wire `progress_cb` to write `pages_processed` + `phase` on the upload row + `add_log` after each batch |
| `apps/dashboard/material_routing.py` | New: `should_dispatch_to_job(file_path) -> (bool, page_count)` — single source of truth for the 50-page threshold |
| `apps/dashboard/management/commands/process_material.py` | New: `python manage.py process_material <id> [--mode rich\|fast]` — entry point invoked by Container Apps Job AND by re-process button |
| `apps/dashboard/job_dispatch.py` | New: `dispatch_material_job(upload_id, mode)` — uses `azure-mgmt-app` SDK to start a Job execution; returns execution name. Local-dev fallback: `subprocess.Popen([sys.executable, 'manage.py', 'process_material', str(upload_id)])` |
| `apps/dashboard/cost_estimator.py` | New: `estimate_material_cost(file_path, mode) -> dict` — opens PDF, counts pages, applies per-page token estimate × `ModelConfig.get_for('generation')` price |
| `apps/dashboard/views.py:2683` | Branch on `should_dispatch_to_job`: if False → existing `run_async(...)` path (preserved); if True → render confirm screen with cost estimate → on confirm, call `dispatch_material_job` |
| `apps/dashboard/views.py:NEW` | `material_confirm_processing` view (POST) — flips status to pending, dispatches Job |
| `apps/dashboard/models.py:93` | Add 6 progress/dispatch fields |
| `templates/dashboard/material_confirm.html` | New: confirmation screen with cost estimate + Confirm/Cancel |
| `templates/dashboard/teaching_materials.html` (or wherever the row renders) | Show `pages_processed/pages_total` + phase when status='processing'; add 5 s auto-refresh |
| `requirements.txt` | Add `azure-identity`, `azure-mgmt-app` (already present?) — verify |

## Infrastructure changes

`infra/__main__.py` (Pulumi):

```python
material_job = containerapp.Job(
    "aitutor-pixel-material-job",
    resource_group_name=resource_group.name,
    environment_id=managed_env.id,
    location=resource_group.location,
    configuration=containerapp.JobConfigurationArgs(
        replica_timeout=86400,            # 24 h hard ceiling — never revisit
        replica_retry_limit=2,            # auto-retry on container restart
        trigger_type="Manual",            # invoked from Django
        manual_trigger_config=containerapp.JobConfigurationManualTriggerConfigArgs(
            replica_completion_count=1,
            parallelism=1,
        ),
    ),
    template=containerapp.JobTemplateArgs(
        containers=[containerapp.ContainerArgs(
            name="material-processor",
            image=container_app_image,    # SAME image as main app
            command=["python", "manage.py", "process_material"],
            # args injected per-execution by the SDK call
            resources=containerapp.ContainerResourcesArgs(
                cpu=2.0, memory="4Gi",    # smaller than main; vision is mostly I/O wait
            ),
            env=container_env_vars,       # same env vars as main app
        )],
    ),
)
```

Service principal: the existing `aitutor-github-actions-pixel` SP needs `Microsoft.App/jobs/start/action` permission. The Contributor role at the resource-group level already grants this.

Local dev: `DJANGO_SETTINGS_MODULE` flag — if `local`, `dispatch_material_job` falls through to subprocess.

## Frontend changes

1. **Upload flow**: branch on page count (single source of truth: `should_dispatch_to_job`)
   - **<50 pages**: existing behavior preserved — instant dispatch + flash success message
   - **≥50 pages**: confirm screen — "**272 pages** · estimated **~$4.50** · ETA **~25 min** · [Cancel] [Confirm and start]"

2. **Materials list**: add `pages_processed/pages_total` next to "Processing" badge for Job-routed rows; auto-refresh every 5 s when any row is `processing`. Small-material rows look the same as today.

3. **Failed row**: structured error rendering (`error_message` is JSON `{reason, detail, batch_failed_at}`); add "Reprocess" button. Reprocess re-applies the routing rule — small failed materials retry in-process, large failed ones re-dispatch a Job.

## Out of scope

- Celery / Redis (rejected per user — Container Apps Jobs is the chosen primitive)
- Resumable mid-batch checkpointing (resume at batch boundaries only — granular enough)
- DOCX / image-format hardening (PDFs only — we'll catch DOCX issues if they surface)
- Smart sampling (rejected — extract everything)
- Per-institution cost caps / budget guards (defer; pilot scale doesn't need this)
- Tesseract local OCR fallback (defer; LLM-vision is good enough once we make it reliable)
- Live log streaming during job execution (the existing `add_log` + 5 s poll is sufficient)
- Migration of existing failed uploads (add a "Reprocess" button — covered above; no automatic backfill)
- Page-cap bypass / per-material processing limits (defer)
- Per-material-type routing rules (rejected — single page-count threshold applies uniformly across textbooks/references/worksheets/notes/question banks)
- Manual override to force in-process for a >50-page material, or force Job for a small one (defer — add only if pilot shows need)

## Phased delivery

| Phase | Work | Days |
|-------|------|------|
| **P1 — Surface real errors + decouple Fast** | (1) typed `OCRFailure` + propagate up to `error_message`; (2) `extract_figures` flag; (3) Fast mode skips vision entirely. Ship this first to the existing pipeline so we get real signal on what's failing for the Kelly textbook. | 0.5 |
| **P2 — Stream + concurrency + checkpoint** | (1) generator-based page rendering; (2) 5-way ThreadPoolExecutor with rate-limit backoff; (3) data model migration for progress fields; (4) per-batch checkpoint writes; (5) UI progress indicator + auto-refresh. | 2 |
| **P3 — Container Apps Job** | (1) Pulumi resource + `pulumi up`; (2) `process_material` management command; (3) `dispatch_material_job` Azure SDK + local-subprocess fallback; (4) wire from upload view; (5) verify resume-from-checkpoint after manual container restart. | 1.5 |
| **P4 — Cost estimate + confirm UX** | (1) `cost_estimator.py` (page count + per-page token avg × model price); (2) `material_confirm.html`; (3) status='pending_confirmation' flow; (4) auto-confirm small files. | 1 |
| **P5 — Re-run failing textbooks** | Click Reprocess on Kelly + Wider World; observe full extraction. Spot-check: (a) chunks are indexed; (b) figures appear in MediaAssets; (c) progress bar advances; (d) one mid-job manual `az containerapp revision restart` proves resume works. | 0.5 |

Total: ~5.5 solo-dev days.

## Risks

- **Anthropic rate limits at 5× concurrency.** Existing `AnthropicClient` has retry-with-backoff; 5 parallel vision calls is the documented comfortable limit. If we see sustained 429s, drop to 3.
- **Container Apps Job startup cold-start (~30 s).** Acceptable for jobs that run 25+ min. Shows as "starting" in the UI.
- **D4 capacity.** Main app + a Job container both run in the same environment. Vision calls are mostly I/O wait — CPU/RAM contention should be minor. Monitor.
- **Resume idempotency.** Mid-job restart must not double-index chunks. Deterministic ChromaDB IDs `(upload_id, page_idx, chunk_idx)` solve this — `_index_chunks` will overwrite on collision. Need to confirm current ID scheme; if it's UUID-random, change it.
- **Cost surprises.** Mitigated by P4's confirm screen, but a teacher could still confirm a $30 reference book without realising. Add a soft cap (warn at $10) in P4.
- **Pulumi Job resource is new for this stack.** First Pulumi run after adding it will create the Job resource; preview before applying.

## Confirmed parameters (user, 2026-05-14)

1. **Routing threshold: 50 pages.** `MATERIAL_JOB_PAGE_THRESHOLD = 50`. <50 stays in-process; ≥50 routes to Job.
2. **Default upload mode: Rich.** Fast remains the explicit text-only toggle.
3. **Cost estimate: 3000 tokens/page** (vision input + output) × current `ModelConfig.get_for('generation')` price. Calibrate after first 5-10 real runs.
4. **Cost guardrails: warn at $10, hard-block at $50** (super-admin can bypass the hard block).
5. **Job replica size: 2 CPU / 4 Gi.** Vision LLM is mostly I/O wait — no need for the main app's 4/8.

## Next step

P1 ships first — it's the smallest change with the biggest immediate diagnostic value. After P1 we can re-run the Kelly textbook and see what's actually failing, which informs whether P2's design assumptions are right. Confirm or adjust open questions above and I'll start P1.
