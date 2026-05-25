# pgvector migration plan

*AI Tutor • drafted 2026-05-24 • PR 1 of 2 in the storage-persistence sweep*

Replace ChromaDB with PostgreSQL `pgvector` for the curriculum
knowledge base. Fixes a production bug where teaching-material vector
embeddings have been silently lost on every container restart since the
SMB-SQLite hang workaround landed (vectordb writes go to `/tmp/vectordb`
but are never synced back to the Azure Files mount).

**Companion PR (not in this scope)**: `memory/blob_storage_migration_plan.md`
will migrate Django media (PDFs, images) from Azure Files SMB → Azure
Blob Storage. Tracked separately because the media layer already
persists correctly — that PR is infrastructure improvement, not bug-fix.

## Why pgvector, not sync-back

| Option | Reason rejected |
|---|---|
| Write-through sync `/tmp/vectordb` → SMB | Buys correctness but every restart still copies the (growing) vectordb from SMB to ephemeral disk. Cold-start slow. Tight write/sync race window. |
| Shutdown hook (SIGTERM rsync) | Azure Container Apps gives ~30s graceful shutdown; SIGKILLs lose data. Doesn't survive crashes. |
| Periodic cron sync | Up to N-min data loss window. Threading in gunicorn workers is awkward. |
| Azure NetApp Files (NFS instead of SMB) | $400+/mo minimum; overkill for current scale. |
| ChromaDB server mode (separate Container App) | Another service to operate. Network hop per query. Over-engineered. |
| **pgvector** | ✅ Selected. Vectors live in the existing Azure Postgres. Transactional. No `/tmp` workaround. Already on Azure's allowed-extensions list. |

## Facts established

- Azure Postgres 16 Flexible Server (`aitutor-pixel-pg`) — `azure.extensions` allowed list **includes `vector` and `pg_diskann`**. Neither is currently enabled. Enabling = `az postgres flexible-server parameter set --name azure.extensions --value vector` + `CREATE EXTENSION vector;`. No infra change.
- Embedding model: `sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions. Loaded as `CurriculumKnowledgeBase._shared_embedding_fn` class-level cache. Migration must reuse the same model so existing vectors stay comparable.
- Existing data: 470 chunks in `vectordb/institution_1` (Anse Boileau Secondary). Other institution buckets are 0 docs in prod. The 470 survived only because they were indexed before the `/tmp` switch — every TeachingMaterialUpload since has been silently lost.
- KB surface: 14 public methods on `CurriculumKnowledgeBase`, 25+ call sites across the codebase. See `apps/curriculum/knowledge_base.py` audit report (in conversation history, 2026-05-24).
- Trickiest piece: `query_with_global_fallback` — two-tier query that hits per-institution KB + global KB, merges by distance with `institution_boost=0.7`, deduplicates by content hash, optionally narrows the global side by course subject_code + grade_level.

## Out of scope

- Migrating Django media (PDFs, images) — that's PR 2.
- Changing the embedding model itself.
- Changing the chunk-extraction heuristics (curriculum chunker, exam-paper chunker, figure extractor).
- The 14 public methods' signatures — must stay drop-in to avoid touching the 25+ call sites.

---

## Phases (mapped to TaskCreate IDs)

### Phase 1 — Plan + sign-off (this doc · Task #1)
Status: ✅ in progress

### Phase 2 — Enable pgvector on Azure Postgres (Task #2)
- `az postgres flexible-server parameter set --resource-group aitutor-pixel-rg --server-name aitutor-pixel-pg --name azure.extensions --value vector`
- Restart server (parameter requires restart per Azure)
- New Django migration: `CREATE EXTENSION IF NOT EXISTS vector;` via `RunSQL`
- Verify: connect, `SELECT '[1,2,3]'::vector;` works
- **Production touch**: this is the only step that mutates prod infra before the code is ready. Reversible (drop extension) but irreversible if data is written to it. Safe to do early because no app code depends on it until Phase 4.

### Phase 3 — `CurriculumChunk` model + Django migration (Task #3)
- New model `apps.curriculum.models.CurriculumChunk` with:
  - `content: TextField`
  - `embedding: VectorField(dimensions=384)` (via `pgvector.django.VectorField` or raw SQL column)
  - All metadata keys from the audit (subject, grade_level, section, chunk_type, source_file, upload_id, institution_id, source_type, material_type, material_title, question_number, question_type, has_answers, year, paper_number, figure_type, figure_page, figure_number, figure_image_url)
  - `content_hash: CharField(64)` for dedup (sha256 of content)
  - `created_at`, `updated_at`
- Indexes:
  - HNSW on `embedding` using cosine distance (`vector_cosine_ops`)
  - btree on `(institution_id, chunk_type)`, `(institution_id, subject, grade_level)`, `(upload_id)`, `(content_hash)`
- Migration includes `CREATE EXTENSION` (idempotent) so dev environments can self-bootstrap.
- Use `pgvector-python` package (`pip install pgvector`) which ships `pgvector.django.VectorField`.

### Phase 4 — Rewrite `CurriculumKnowledgeBase` internals (Task #4)
- Same 14-method public API. Same return shapes (`QueryResult` dataclass etc.).
- ChromaDB internals → ORM queries.
- `_get_collection` becomes a no-op (or returns `CurriculumChunk.objects.filter(institution_id=...)`).
- `_index_chunks` becomes a bulk upsert: compute embeddings via `_shared_embedding_fn`, build a list of `CurriculumChunk(...)`, use `INSERT ... ON CONFLICT (content_hash, institution_id) DO UPDATE` via raw SQL or `bulk_create(..., update_conflicts=True, unique_fields=['content_hash', 'institution_id'])`.
- Embedding function reused unchanged — still `sentence-transformers/all-MiniLM-L6-v2`. Existing vectors stay comparable after port.
- Single-collection queries: `CurriculumChunk.objects.filter(institution_id=N).order_by(L2Distance('embedding', query_vec))[:n_results]` (or equivalent).
- Where-filter translation: ChromaDB `{"$eq": ..., "$in": ..., "$and": [...]}` → Django Q-objects. New helper `_chromadb_where_to_q(where_filter)` does the translation in one place.

### Phase 5 — Port `query_with_global_fallback` (Task #5)
The trickiest method. Two-tier behavior:
1. Run institution-scoped query → top-N with distance
2. Run global-scoped query → top-N with distance (filtered to platform-wide uploads matching course subject_code + grade_level when course is provided; falls back to subject string match)
3. Merge: institution distances multiplied by `institution_boost` (default 0.7 → boost), dedup by content hash (institution wins on tie), sort by adjusted distance, take top-N.

ORM approach: two `.annotate(distance=CosineDistance('embedding', query_vec))` queries, union in Python, dedup, sort. Could optimize later with a single UNION query.

### Phase 6 — Re-index command (Task #6)
- New management command: `python manage.py port_chromadb_to_pgvector --vectordb-path /app/media/vectordb`
- Walks every `vectordb/institution_*/chroma.sqlite3`, reads each chunk (id, document, embedding, metadata), inserts via ORM.
- Idempotent: skip rows whose `content_hash + institution_id` already exists in `CurriculumChunk`.
- Run locally first against `media/vectordb` (370 docs in institution_12, etc.).
- Run in prod ONCE after deploy via `az containerapp exec` against the file-share-mounted vectordb.
- Source data preserved on the file share until cleanup is verified.

### Phase 7 — Strip ChromaDB workarounds (Task #7)
- Remove `VECTORDB_ROOT` env var from Container App.
- Remove `cp -r /app/media/vectordb /tmp/vectordb` from Dockerfile CMD.
- Remove `VECTORDB_ROOT` logic from `config/settings.py:155`.
- Drop `chromadb` from `requirements.txt`.
- Delete `media/vectordb/` from the Azure Files share (cleanup; after prod re-index verified).

### Phase 8 — Test locally end-to-end (Task #8)
- Load eval fixture into a fresh local DB.
- Re-index via the port command.
- Run one full A/B cell (Sonnet × L1137 × error_prone).
- Run a content-generation call against the same lessons.
- Run the test suite (`apps/tutoring/tests` + `apps/curriculum/tests`).
- Compare BEA + 10p numbers to last known good (`eval-reports/larger-eval-2026-05-23.md`). Within ±10pp lenient is OK.

### Phase 9 — Ship + monitor (Task #9)
- Open PR. Get approval (Roy review), admin-merge.
- After deploy: run the **CI-driven port** by triggering the deploy workflow with `run_pgvector_port=true`. The post-deploy job runs on a GitHub runner:
  1. Pip-installs chromadb on the runner only (image stays clean)
  2. Opens a temporary Postgres firewall rule for the runner IP
  3. azcopy downloads `/vectordb` from the share to the runner
  4. Sets `DATABASE_URL` pointing at the target Postgres
  5. Runs `python manage.py port_chromadb_to_pgvector --vectordb-path ./vectordb_snapshot/vectordb`
  6. Runs `python manage.py audit_kb_coverage`
  7. Closes the firewall rule
  - Requires repo secret `PROD_DB_PASSWORD` (staging equivalent: `STAGING_DB_PASSWORD`).
- Manual fallback (if the workflow flag isn't available yet): from a dev machine with chromadb installed, open Postgres firewall to your IP → azcopy the vectordb dir to local → run the same two commands with `DATABASE_URL` set to the target stack.
- The port command is idempotent — re-running on an already-ported DB reports 0 new + N updated. Safe to retry.
- Verify `CurriculumChunk.objects.count()` matches expected (~470+).
- Trigger one manual tutor session via dashboard, confirm RAG retrieval returns results.
- Watch `[KB] inheritance OK` log lines in containerapp logs from live sessions.
- Watch the post-deploy-eval workflow output for the next commit.

### Phase 10 — Recover uploads with lost vectors

The ChromaDB `/tmp/vectordb` workaround silently lost writes on container restart since the last good snapshot in `/app/media/vectordb`. After the port:

1. **Audit coverage** — read-only report:
   ```bash
   python manage.py audit_kb_coverage
   ```
   Compares each `TeachingMaterialUpload.chunks_created` (Postgres, durable) against `CurriculumChunk.objects.filter(upload_id=u.id).count()` (post-port). Flags any upload with `actual/expected < 0.5`.

2. **Reset row state** — flip flagged uploads back to `status='pending'`:
   ```bash
   python manage.py reset_lost_materials --dry-run
   python manage.py reset_lost_materials  # write
   ```
   Does NOT re-run the indexer. Just resets row state so the existing dashboard "Process materials" button picks them up. Source files (PDF/DOCX) live on the persistent Azure Files mount so they're still re-processable.

3. **Trigger from the dashboard** — open each affected course in the dashboard and click "Process materials". The well-tested job-dispatch / mode-routing / progress-tracking path handles the re-index. Local embeddings → zero API cost.

Rationale for the "reset state, don't auto-re-index" choice: the platform-button path is the well-tested orchestration. Avoiding a parallel CLI re-runner keeps observability and audit-log integrity in the existing flow. See `auto-memory/feedback_prefer_state_reset_over_cli_automation.md`.

## Acceptance criteria

- `chromadb` no longer in `requirements.txt`.
- `VECTORDB_ROOT` env var gone from Container App.
- `cp -r /app/media/vectordb /tmp/vectordb` gone from Dockerfile CMD.
- `CurriculumChunk` table populated in prod with ≥ 470 rows after re-index.
- Local + CI eval runs return non-zero in-scope BEA turns AND RAG-grounded responses on lessons with KB content.
- All 14 public KB methods return the same shapes as before (no caller breaks).
- New TeachingMaterialUpload reaches `status='completed'` AND its vectors are queryable AFTER the next container restart.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Embedding model load changes vector values | Migration uses the SAME `SentenceTransformerEmbeddingFunction` instance — no re-embedding, just copying existing 384-d vectors |
| HNSW build slow on existing rows | pgvector HNSW supports concurrent build; expect <1 min for 470 rows |
| ORM where-filter translation drops some semantics | Unit test for `_chromadb_where_to_q` covering each operator ($eq, $in, $and, $ne) before flipping |
| `query_with_global_fallback` performance regresses (two queries + Python merge) | Within current scale (470 vectors) Postgres is fast enough. Optimize with single UNION later if profiling shows it. |
| `bulk_create(update_conflicts=True)` requires Django 4.1+ | Confirm Django version (we're on 6.0.2 — fine) |
| Re-index command runs against prod ChromaDB whose schema is non-trivial | Local dry-run first; idempotent so prod run is safe to retry |
| Some other code reads ChromaDB directly (bypassing KB class) | Audit complete — only `CurriculumKnowledgeBase` and its 25+ documented call sites use ChromaDB |

## Rollback

If something breaks after deploy:
1. Revert the merge commit (creates a new commit with old ChromaDB code restored)
2. Deploy fires automatically
3. ChromaDB still exists in `/app/media/vectordb` on the file share (re-index command didn't delete it); will be picked up on restart via the `cp` Dockerfile step
4. `CurriculumChunk` rows are left in Postgres (harmless; ignored after revert)
5. Once revert is verified, decide whether to retry the migration or delay

No data destruction in the forward path until Phase 7's "delete `media/vectordb/` from file share" — and that's gated on Phase 9 verification.

## Open questions

1. **Use `pgvector.django` (community package) or raw SQL via `RunSQL`?** Preference: `pgvector.django` — cleaner ORM integration, well-maintained, used widely. Adds one dep to `requirements.txt`.
2. **Cosine distance vs L2?** ChromaDB defaults to cosine (we set `hnsw:space='cosine'` in the HelpKB; curriculum KB uses ChromaDB default which is cosine for sentence-transformers). Match in pgvector: HNSW index `WITH (vector_cosine_ops)`.
3. **Should the re-index command be one-off (run manually) or automatic at deploy time?** Preference: manual one-off — re-running it on every deploy adds startup time, and a botched re-index would be hard to roll back.
4. **Delete `media/vectordb/` from the file share when?** After Phase 9 verification confirms pgvector is serving queries cleanly for at least 24h.

## Cross-references

- KB surface audit (in conversation 2026-05-24): 14 public methods, 25+ call sites
- `memory/blob_storage_migration_plan.md` (planned, PR 2): media files (PDFs, images) Azure Files SMB → Azure Blob
- BEA Shared Task (relevant: KB-grounded judges score better when KB has content): https://sig-edu.org/sharedtask/2025
- Auto-memory: `ChromaDB SQLite hangs over the SMB mount → VECTORDB_ROOT=/tmp/vectordb` (the workaround this PR eliminates)
