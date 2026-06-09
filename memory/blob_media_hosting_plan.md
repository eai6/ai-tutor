# Media hosting: self-host help video + Blob migration — Plan (2026-06-09)

## Problem
1. **Urgent:** the help/support page embeds a **YouTube** video (`Fh_nOqNESZs`).
   School networks **block YouTube**, so the video is unreachable for pilot users.
2. **Bigger:** media still lives on the SMB **Azure File Share** mount (`/app/media`,
   served via the Django `serve` view `config/urls.py:54`). We want to "host media
   properly" on **Azure Blob Storage** (the two archived plans:
   `memory/archives/may_1st_2026/azure_blob_storage_plan.md`,
   `memory/archives/may_18th_2026/azure_blob_migration_plan.md`).

## ⚠️ Constraint that rewrites the old plans
Both archived plans serve media **directly from `https://<acct>.blob.core.windows.net/...`**.
That's a **different domain** which locked-down school firewalls block — the **same reason
YouTube fails**. Also, we just moved prod **fully private behind the App Gateway/WAF**;
a public blob URL would bypass the WAF and add a new public surface.

**Therefore: all media must be served via `www.seselai.sc`** (the one domain schools
allow), not from the blob domain. This is the core design change vs the archived plans.

## Current state (from audit)
- `MEDIA_URL='media/'`, `MEDIA_ROOT=BASE_DIR/'media'` (`settings.py:149`), the Azure Files
  SMB mount in prod.
- `/media/<path>` served through Django `serve` (`config/urls.py:54`) → already on
  `www.seselai.sc` (good — allowed domain).
- `STORAGES.default = FileSystemStorage` (`settings.py:230`). No `django-storages`,
  no blob backend yet.
- App Gateway WAF in front; backend = internal Container App; static IP.

## Phase 0 — Fix the help video NOW (no blob needed, unblocks schools today)
Self-host the video and serve it from `www.seselai.sc`, which schools already allow.
- Put the file at `media/help/intro.mp4` (current Azure Files mount) — served via the
  existing `/media/` Django route, so reachable on school networks with **no new
  allow-listed domain**.
- Replace the YouTube `<iframe>` in `templates/help/index.html` with an HTML5
  `<video controls preload="metadata" poster=...>` pointing at `/media/help/intro.mp4`.
  Keep the responsive 16:9 frame.
- **NEEDED FROM USER: the actual video file (mp4).** The page currently only has the
  YouTube ID; I can't extract the file. Drop it in `media/test_uploads/` or hand me a
  path and I'll place + wire it. (A small poster image is a nice-to-have.)
- This is the urgent deliverable; it does NOT depend on the Blob migration.

Caveat: the Django `serve` view streams through gunicorn — fine for one occasionally-watched
help video; not how we'd serve high-traffic media long-term (that's the Blob phase).

## Phase 1+ — Blob Storage migration (proper media hosting)
Reuse the archived plans' storage design (django-storages, per-field/per-container,
env-flag-gated, vectordb stays on File Share) BUT change the **serving path** to go
through `www.seselai.sc`:

### Serving model (the key decision)
- **Recommended — App Gateway path routing `/media/*` → Blob (private).** Add a private
  endpoint from the VNet to the Storage Account, a blob backend pool + a path-based rule
  on the App Gateway so `https://www.seselai.sc/media/*` is served straight off Blob
  (fast, no gunicorn, behind the WAF, on the allowed domain, max-security/private). Set
  `django-storages` `AzureStorage(custom_domain="www.seselai.sc", ...)` so `file.url`
  emits `https://www.seselai.sc/media/...`.
- Alt A — allow-list `*.blob.core.windows.net` at schools + serve from blob domain.
  Rejected: asks schools to open a broad new domain; bypasses WAF.
- Alt B — Django proxies blob through the `serve`/a view. Rejected: ties up gunicorn for
  binary media.

### Migration steps (per archived plans, adjusted)
1. Pulumi: Blob container(s) on the existing Storage Account + **private endpoint** into
   the VNet; App Gateway `/media/*` path rule → blob backend.
2. Add `django-storages[azure]` to requirements.
3. Settings + per-field `AzureStorage` (callable storage, env-flag-gated;
   `custom_domain=www.seselai.sc`, container path aligned to `/media/`).
4. Migrate existing files (generated images first, then uploads) from File Share → Blob
   (one-off `az storage` copy / management command). Old `/media/...` URLs keep working
   during coexistence.
5. Flip the env flag; verify images/uploads/audio serve from `www.seselai.sc/media/*`.
6. vectordb stays on File Share (per CLAUDE.md).

## Decisions to confirm
1. **Serving model** for blob media: App Gateway `/media/*` → private Blob (recommended)
   vs allow-list blob domain. (Determines a chunk of the infra work.)
2. **Scope/sequencing:** do Phase 0 (video) immediately, then Phase 1 blob separately?
   (Recommended.) Or bundle.
3. **Blob access:** private endpoint (max security, matches the WAF posture) vs public
   container. Recommended: private.

## Next step
Phase 0: get the **mp4 from the user**, place at `media/help/intro.mp4`, swap the help
page `<iframe>` → `<video>`, deploy. Then plan/execute the Blob migration (Phase 1).
