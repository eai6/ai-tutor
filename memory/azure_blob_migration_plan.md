# Azure Blob Storage Migration — Plan (2026-05-15)

Replace the SMB-mounted Azure File Share for binary media with
Azure Blob Storage. Lift uploaded textbooks, generated images, and
new audio (R9b) to Blob from day one. Avoids SMB latency / SQLite-
on-SMB class of bugs we already hit with ChromaDB.

## Problem

Current production media stack:
  - All media at `/app/media` — Azure File Share (SMB) mounted via
    Container Apps volume (`infra/__main__.py:180-211`).
  - Uploaded textbooks (5-50 MB each), generated lesson images,
    figure_facts JSON.
  - ChromaDB SQLite specifically had to be moved off this mount to
    `/tmp/vectordb` because of SMB lock contention (per CLAUDE.md
    "ChromaDB SQLite hangs over the SMB mount").
  - R9b (TTS + STT audio persistence) was planned to land here too.

Why this is wrong:
  - **SMB is wrong abstraction for binary blobs.** File Share is
    designed for file-system-style access (read/write/seek). Blob
    Storage is designed for object access (PUT once, GET many).
    The latter is faster, cheaper, and more durable.
  - **No CDN front-end** — every image GET hits Azure File Share
    via the app. Blob can sit behind Azure CDN.
  - **No lifecycle tiering** — generated images that haven't been
    accessed in 90 days could move to Cool / Archive at 1/4 the
    cost. File Share doesn't support tiering.
  - **No public-read containers for safe-to-share assets** — every
    image read goes through the Django process.
  - **Local dev mismatch** — `media/` on local disk vs SMB in prod
    means subtle bugs (path handling, permissions). Blob via
    `django-storages` uses the same API everywhere.

User direction (2026-05-15): "It might be time to properly implement
the azure blob database" — Blob is the right tier for media; File
Share stays for the few things that genuinely need POSIX access
(none today, possibly nothing).

## Current state (from audit)

- `infra/__main__.py:171` — Storage Account `aitutorpixelacr`
  exists.
- `infra/__main__.py:180-211` — `FileShare` named "media", mounted
  to Container App at `/app/media` via `containerapp.VolumeArgs`.
- `config/settings.py:150` — `MEDIA_ROOT = BASE_DIR / 'media'`
  (resolves to `/app/media` in container).
- `config/urls.py` — explicit `serve` view re-routes `/media/...`
  through Django (production-only; dev uses `static()`).
- No `django-storages` package installed.
- No `DEFAULT_FILE_STORAGE` set — uses Django's default
  `FileSystemStorage`.
- All `MediaAsset.file = FileField(upload_to=...)` fields write to
  the file share via FileSystemStorage today.

## Target design

### Container layout (one Storage Account, multiple containers)

| Container | Public read? | Lifecycle | Purpose |
|---|---|---|---|
| `media-images` | Yes (CDN-fronted) | Hot 90d → Cool → Archive 1y → delete 3y | Generated lesson images, uploaded thumbnails |
| `media-uploads` | No (signed-URL access) | Hot indefinitely | Uploaded textbooks (PDF/DOCX), curriculum docs |
| `media-audio` | No (signed-URL access) | Hot 30d → Cool → Archive 1y | TTS output + STT mic recordings (R9b) |
| `media-figures` | Yes | Hot 180d → Cool | Figure crops / SVG renders |
| `db-backups` | No (immutable) | Hot 30d → Archive 1y → delete 1y | pg_dump output (per database_backup_hardening_plan.md) |

Public-read containers (`media-images`, `media-figures`) get a
CDN endpoint so reads bypass Django entirely. Signed-URL access
for everything sensitive (uploads, audio, backups).

### Django integration

Install `django-storages[azure]`. Single backend class per container
via the `STORAGES` setting (Django 4.2+ shape):

```python
# config/settings.py
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.azure_storage.AzureStorage",
        "OPTIONS": {
            "account_name": os.getenv("AZURE_STORAGE_ACCOUNT"),
            "azure_container": "media-uploads",  # default fallback
            "token_credential": _managed_identity_credential(),  # see below
            "expiration_secs": 3600,  # signed URL TTL
        },
    },
    "staticfiles": { "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" },
    # Per-content-type buckets — set on FileField via storage=
    "images": { ... azure_container="media-images" ... },
    "audio":  { ... azure_container="media-audio" ... },
    "figures": { ... azure_container="media-figures" ... },
}
```

Then on each model field:

```python
class MediaAsset(models.Model):
    file = models.FileField(storage=storages["images"], upload_to=...)
```

For new audio:

```python
class SessionTurn(models.Model):
    tts_audio = models.FileField(storage=storages["audio"], blank=True)
    stt_audio = models.FileField(storage=storages["audio"], blank=True)
```

### Auth — managed identity, not connection string

Container App already has a managed identity (per CLAUDE.md
"Managed Identity for ACR pull"). Grant it the
`Storage Blob Data Contributor` role on the storage account.
`django-storages` accepts a `token_credential` via
`azure.identity.ManagedIdentityCredential`. No connection string,
no key rotation, no secrets in `engine_state`.

### Migration of existing data

One-shot script (Django management command):

```bash
python manage.py migrate_media_to_blob \
    --container media-images --asset-type image \
    --batch 50
```

Reads all `MediaAsset` with `asset_type=image`, copies bytes from
File Share → Blob, updates `MediaAsset.file.name` to the new
location, verifies content-length + checksum, deletes from File
Share only after successful upload + verify (idempotent — safe to
re-run).

Run during off-peak (e.g. weekend). Container App keeps serving
during the migration; both old File Share path and new Blob path
work via the storage layer abstraction.

### CDN front-end (optional but recommended)

Public containers (`media-images`, `media-figures`) get an Azure
Front Door / CDN profile. URL pattern shifts from
`https://aitutor-pixel-app.../media/lessons/abc.png` to
`https://cdn.aitutor.io/images/abc.png`. Cuts media bandwidth from
Container App + speeds up loads for students on slower connections
(Seychelles pilot has variable internet).

## Data model changes

```python
# apps/media_library/models.py
class MediaAsset(models.Model):
    # No new fields — `file = FileField(storage=storages["images"])`
    # changes WHERE bytes live but not the schema.

# apps/tutoring/models.py — for R9b audio persistence
class SessionTurn(models.Model):
    tts_audio = models.FileField(
        storage=storages["audio"],
        upload_to=lambda inst, fn: f"tts/{inst.session_id}/{inst.id}.mp3",
        blank=True, null=True,
    )
    stt_audio = models.FileField(
        storage=storages["audio"],
        upload_to=lambda inst, fn: f"stt/{inst.session_id}/{inst.id}.webm",
        blank=True, null=True,
    )
```

Migration is purely additive (two new FileField columns).

## Backend changes

| File | Change |
|---|---|
| `requirements.txt` | Add `django-storages[azure]`, `azure-identity` |
| `config/settings.py` | Replace `STORAGES` block with multi-backend config; remove `MEDIA_ROOT` reliance for new code |
| `config/urls.py:55` | Remove the manual `serve` view route — direct-Blob URLs replace it |
| `apps/media_library/models.py` | `MediaAsset.file` gets `storage=storages["images"]` |
| `apps/tutoring/models.py` | New `tts_audio` + `stt_audio` FileFields (R9b) |
| `apps/tutoring/views.py::speak_text` | After synthesis, save to `turn.tts_audio` if `turn_id` provided. Lazy: re-serve cached file on subsequent ▶ clicks. |
| `apps/tutoring/views.py::transcribe_audio` | Save raw mic blob to `turn.stt_audio` |
| `apps/curriculum/management/commands/migrate_media_to_blob.py` | NEW — one-shot data migration |

## Infrastructure changes

| File | Change |
|---|---|
| `infra/__main__.py` | Add 4 BlobContainer resources (images / uploads / audio / figures); enable account-level CORS for the public ones; assign `Storage Blob Data Contributor` to the Container App's managed identity |
| `infra/__main__.py` | (later) Add Azure Front Door / CDN profile pointing at the public containers |
| `infra/Pulumi.pixel.yaml` | New env var `AZURE_STORAGE_ACCOUNT=aitutorpixelacr` |

## Out of scope (deferred)

- **Multi-region replication** — single region is fine for pilot
  scale. Geo-replicate Blob when multi-region traffic justifies it.
- **Per-tenant SAS tokens** — fine-grained access. Pilot is
  single-tenant; defer until multi-tenant becomes a hard requirement.
- **CDN purge automation on regen** — when an image is regenerated,
  its CDN cached copy is stale. Manual purge for v1; automate when
  it becomes annoying.
- **Lifecycle policy auto-delete** — start with all-tier-Hot for
  the first month so we can see actual access patterns before
  configuring tiering.
- **File-share decommission** — keep the share around for the first
  60 days post-migration as a safety net. Tear down after we're sure
  nothing reads from it.

## Phased delivery

| Phase | Work | Days |
|---|---|---|
| **A1** Pulumi blob containers + IAM | 4 BlobContainer resources, managed-identity role assignment, env var wiring | 0.25 |
| **A2** django-storages integration | Install, config, set per-field `storage=` on MediaAsset.file. Test new uploads land in Blob locally (Azurite) + prod | 0.5 |
| **A3** R9b audio persistence | New SessionTurn fields + speak_text/transcribe save calls + lazy-replay cache hit | 0.5 |
| **A4** Migration command + dry-run | `migrate_media_to_blob` script, idempotent, dry-run mode + verification | 0.5 |
| **A5** Run migration on prod (off-peak weekend) | Backup → run command → verify → keep File Share alive 60d | 0.25 |
| **A6** CDN front-end (optional) | Front Door profile + DNS switch for public containers | 0.5 |
| **A7** Tear down File Share | After 60d safety window, remove the volume mount + Pulumi resource | 0.25 |

Total: ~2.75 days (≈3 with A6). Ship A1+A2+A3 first as the new-data
slice — old data keeps living on File Share until A4+A5.

## Risks

- **A2 broken paths** — every model that reads file URLs via
  `instance.file.url` should still work (django-storages returns
  signed URLs for private containers). Test thoroughly on local
  Azurite emulator before prod.
- **A5 migration is destructive on the source** — keep File Share
  data intact (don't delete during migration). Tear down only after
  60d safety window.
- **A6 CDN cache poisoning** — public containers should NEVER
  contain anything sensitive. Audit before flipping public-read.
- **Cost shift** — Blob is cheaper per GB than File Share but reads
  cost ~$0.05/10k. Image-heavy lessons may add up. Estimate after
  A2 lands.
- **Local dev compat** — django-storages needs an emulator (Azurite)
  for local testing or a fallback to FileSystemStorage. Document in
  README + CLAUDE.md.

## Open questions

1. **Container naming convention.** Recommend prefix `media-` for
   the application containers (`media-images`, `media-audio`...) +
   `db-` for backup containers (`db-backups`, sibling plan). Clear
   namespace separation.
2. **Public vs private for figures.** Recommend PUBLIC for
   generated images and figures (no PII; cached aggressively at
   CDN). PRIVATE for raw mic recordings (have student voice) +
   uploaded textbooks (copyright-sensitive in some pilots).
3. **Should we keep File Share for ChromaDB persistence?**
   Recommend NO — ChromaDB already lives at `/tmp/vectordb` per
   CLAUDE.md, copied from File Share at startup. After this
   migration, copy from Blob instead. Delete the File Share entirely
   in A7.
4. **Migration order — model-by-model or container-by-container?**
   Recommend container-by-container, smallest first (audio is empty
   today; figures next; images; uploads last). Lets us catch bugs on
   small surfaces before betting prod-critical bytes.
5. **Should A6 (CDN) ship with A1-A5 or wait?** Recommend WAIT.
   Lift-and-shift first (App + DB unchanged); add CDN after the
   data is in Blob and we've measured actual read patterns.

## Next step

Confirm open questions (especially #2 — public-read on `media-images`
is a one-way trap door once teachers start sharing URLs externally).
Then start A1 (Pulumi container creation) — non-destructive, can
co-exist with current File Share for as long as we want.

Refs: memory/database_backup_hardening_plan.md (sibling — shares
the storage account but uses container `db-backups` with immutable
policy)
