# Media → Azure Blob Storage migration plan

Status: **planning** (2026-06-03). No code changes yet. Forward-referenced from
`infra/__main__.py:237`. Pairs with `memory/pgvector_migration_plan.md` (the
vectordb half of getting off the SMB share).

## Why

Media currently lives on an **Azure File Share (SMB)** mounted at `/app/media`.
This is the source of several recurring pains:

- **SMB latency** is 10–100× local disk; every media read/write goes over the mount.
- **ChromaDB SQLite can't run on SMB** — it hangs with 120s worker timeouts. The
  current workaround is the Dockerfile CMD `cp /app/media/vectordb → /tmp/vectordb`
  on every startup (see `VECTORDB_ROOT`, azure-cloud-expert skill, CLAUDE.md).
- **Media is coupled to the container filesystem** — can't be served independently,
  no CDN, no direct-to-blob uploads, and the File Share quota is a shared ceiling.
- Serving goes through Django (`config/urls.py:64`, `serve` view) — every image byte
  occupies a Gunicorn worker, competing with tutoring requests. On the (now-fixed)
  undersized staging replica this compounded memory pressure.

Goal: move user/generated media to **Azure Blob Storage** via `django-storages`,
serve blobs directly (optionally via CDN), and drop the SMB mount once the vectordb
is also relocated (pgvector or blob-download).

## Current state (grounded)

| Concern | Where | Detail |
|---|---|---|
| Default storage | `config/settings.py:270` `STORAGES.default` | `FileSystemStorage` |
| Media paths | `config/settings.py:189-190` | `MEDIA_URL='media/'`, `MEDIA_ROOT=BASE_DIR/'media'` |
| Serving | `config/urls.py:62-64` | Django `serve` view at `/media/<path>` from `MEDIA_ROOT` |
| Prod mount | `infra/__main__.py:552-555, 679-682` | File Share `media` mounted at `/app/media` (app + material-processor Job) |
| File Share | `infra/__main__.py:230-261` | `share_name="media"`, quota via `file-share-quota-gb` |
| Storage account | `infra/__main__.py:220-221` | `aitutor{stack}sa` |
| Uploaded files | `apps/media_library/models.py:37` | `FileField(upload_to=media_upload_path)` — **relative**, migrates cleanly |
| Generated figures | `apps/curriculum/models.py:838` | `figure_image_url = URLField` — **persisted URL snapshot** ⚠ |
| URL snapshot source | `apps/curriculum/knowledge_base.py:760` | `figure_image_url = asset.file.url` captured at index time |
| Deps present | `requirements.txt:185` | `azure-identity>=1.16.0` (good — enables managed-identity auth) |
| Vectordb today | rides the same `/app/media/vectordb` SMB path, copied to `/tmp` on boot | |

## Target architecture

- Add `django-storages[azure]` + `azure-storage-blob`. Keep `azure-identity` for
  **managed-identity auth** (`DefaultAzureCredential`) so there's **no connection-string
  secret to rotate** — the Container App's existing managed identity gets a role grant.
- `STORAGES.default` → `storages.backends.azure_storage.AzureStorage`, parameterized by
  env (account name, container, `token_credential`). Dev/local stays `FileSystemStorage`
  (or Azurite emulator) behind an env switch — mirror the existing flag-by-env pattern.
- New blob **container** (`media`) in `infra/__main__.py`, alongside or replacing the
  File Share. Grant the Container App managed identity **Storage Blob Data Contributor**.
- Media URLs resolve to blob (optionally fronted by Azure CDN / Front Door later).

## Open questions — DECIDE BEFORE BUILDING

1. **Container access model — privacy.** Media includes institution-scoped and possibly
   student-generated content. Multi-tenancy rule (CLAUDE.md) says never leak across
   schools. Options:
   - **Public-read container + direct URLs** — simplest/fastest/CDN-friendly, but any
     media URL is world-readable if guessed/shared. Probably **unacceptable** for
     student data; maybe fine for generated lesson figures.
   - **Private container + short-lived SAS URLs** — app generates signed URLs on render.
     Keeps privacy, loses naive CDN caching, adds per-render signing.
   - **Split**: public container for generated lesson figures (non-PII), private+SAS for
     uploads/student artifacts.
   → Recommend the **split**. Needs a quick audit of what actually lands in `/app/media`.

2. **Persisted URL snapshots — the migration trap.** `figure_image_url`
   (`curriculum/models.py:838`) and the vector metadata (`knowledge_base.py:760, 894`,
   pgvector port `port_chromadb_to_pgvector.py:55`) store `asset.file.url` **as a string
   captured at index time** — switching the storage backend does **not** rewrite them.
   After cutover these point at the old `media/...` path. Options:
   - Serve blob at the **same `/media/<path>` URL** (keep the app/CDN route mapping the
     old path to blob) so snapshots stay valid — least churn.
   - Or run a **data-rewrite migration** over `CurriculumChunk.figure_image_url` + vector
     metadata to new blob URLs. Heavier; must re-index or bulk-update metadata.
   → Decision gates the whole cutover. Audit how many rows hold absolute vs relative URLs first.

3. **Vectordb — not really "media".** It only rides the media share for convenience.
   Cleanest exit is the **pgvector migration** (`memory/pgvector_migration_plan.md`,
   `port_chromadb_to_pgvector.py` already exists) — removes ChromaDB+files entirely and
   kills the `/tmp` copy dance. If pgvector isn't ready at blob-cutover, interim option:
   download vectordb from blob → `/tmp` on startup (swap the SMB `cp` for an `azcopy`).
   → **Sequence pgvector before dropping the SMB mount**, or keep a tiny File Share just
   for vectordb until pgvector lands.

4. **Auth: managed identity vs connection string.** Prefer managed identity
   (`azure-identity` already present) — no secret. Fallback connection-string path for
   local/dev/Azurite.

## Phased implementation (after decisions above)

- **Phase 0 — app, flagged off.** Add deps; add `AzureStorage` backend selectable by env;
  default stays FileSystem. Local test against **Azurite**. No infra change. No behavior change.
- **Phase 1 — infra.** Add blob container in `__main__.py`; grant managed identity the
  blob data role; wire env vars (account, container, credential). `pulumi preview` on
  **staging** first. File Share stays mounted (parallel).
- **Phase 2 — data migration.** One-time `az storage copy` / `azcopy` File Share `media`
  → blob container, preserving paths. Verify counts + checksums.
- **Phase 3 — snapshot reconciliation.** Per decision #2: either confirm `/media/<path>`
  route maps to blob, or run the URL-rewrite migration. Verify a sample lesson renders
  figures end-to-end on **staging** (chrome-devtools visual check).
- **Phase 4 — cutover.** Flip `STORAGES.default` to AzureStorage on staging → verify →
  prod. Keep the `serve` view as fallback initially.
- **Phase 5 — decommission.** Once vectordb is off the share (pgvector or blob-download)
  and media is verified on blob: remove the `/app/media` mount, drop the File Share,
  delete the `serve` view + `file-share-quota-gb` knob.

## Risks / rollback

- **Don't drop the File Share until Phase 5** — it's the rollback. Backend flip is env-only;
  revert = flip env back.
- **Snapshot URLs (#2) are the likeliest production break** — gate cutover on a staging
  render check, not just DOM/HTTP 200.
- **Material-processor Job** also mounts `/app/media` (`__main__.py:679-682`) — update or
  give it blob access too, or its OCR/figure outputs won't land where the app reads them.
- Staging-first for every phase. Pixel (prod) only after staging parity confirmed.

## Cross-links

- `memory/pgvector_migration_plan.md` — vectordb half; sequence before SMB removal.
- azure-cloud-expert skill — SMB+ChromaDB gotcha, storage ops, managed identity.
- CLAUDE.md — multi-tenancy scoping (gates the container-access decision), "confirm before
  editing `infra/__main__.py`".
