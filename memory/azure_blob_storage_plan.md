# Azure Blob Storage Migration Plan

**Goal:** Move generated images (and uploaded teaching materials, eventually) from Azure File Share to Azure Blob Storage.

**Why:** File Share is a fixed-quota SMB filesystem mounted at `/app/media`. It just hit `errno 28` (5 GiB exhausted by accumulated generated images, bumped to 100 GiB as a stop-gap). Blob is the right substrate for image hosting:

- Pay-as-you-go scaling (no quota fights)
- ~⅓ the cost per GB
- Public URL access (no Django `serve` view needed for media)
- Decouples deploys from a specific filesystem mount → easier to redeploy the platform to new tenants / regions / clouds (the user's stated motivation)
- Container Apps can scale horizontally without coordinating SMB locks

## What's on the File Share today

Two flavours of media live in `/app/media`:

| Path | Source | Migration priority |
|---|---|---|
| `/app/media/media/global/generated_*.png` | `image_service.py` — AI-generated images for steps | **Phase 1** (high traffic, bulk of growth) |
| `/app/media/media/<institution>/uploaded_*` | Teacher uploads (worksheets, PDFs) | Phase 2 |
| `/app/media/vectordb/...` | ChromaDB persistence | **Stays on File Share** — already copied to `/tmp/vectordb` on container start (per CLAUDE.md). Don't migrate. |

The bulk of growth is generated images. Migrating just those buys plenty of headroom and is the fastest path to "platform redeployable."

## Architecture

### What changes

```
                 ┌───────────────────────────────────┐
                 │  Django MediaAsset.file (FileField)│
                 └─────────────┬─────────────────────┘
                               ▼
                 ┌───────────────────────────────────┐
   BEFORE  →     │  Default storage = local FS       │
                 │  → /app/media/...                 │  (Azure File Share mount)
                 └───────────────────────────────────┘

                 ┌───────────────────────────────────┐
   AFTER   →     │  Per-field storage = Blob backend │
                 │  → https://<acct>.blob.core...    │  (Azure Blob)
                 └───────────────────────────────────┘
```

The calling code (`asset.file.save(filename, ContentFile(bytes))`) doesn't change. `asset.file.url` returns a Blob URL instead of `/media/...`.

### Provisioning (Pulumi)

Add a Blob container alongside the existing File Share:

```python
# infra/__main__.py
blob_container = storage.BlobContainer(
    "media-blob",
    container_name="media",
    account_name=sa.name,
    resource_group_name=rg.name,
    public_access=storage.PublicAccess.BLOB,  # public reads, auth writes
)
```

Public-read access is fine for AI-generated lesson images (no PII; same as today's `/media/...` URLs which are publicly served by the `serve` view). If we later need private images (e.g., student work), a separate private container handles that.

### Library

`django-storages[azure]` (~stable, well-maintained):

```
pip install django-storages[azure]
```

### Settings

```python
# config/settings.py — additive, not replacing
USE_AZURE_BLOB_FOR_GENERATED_IMAGES = os.getenv(
    "USE_AZURE_BLOB_FOR_GENERATED_IMAGES", "false"
).lower() == "true"

if USE_AZURE_BLOB_FOR_GENERATED_IMAGES:
    AZURE_ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "")
    AZURE_ACCOUNT_KEY = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "")
    AZURE_CONTAINER = os.getenv("AZURE_BLOB_CONTAINER", "media")
    AZURE_CUSTOM_DOMAIN = (
        f"{AZURE_ACCOUNT_NAME}.blob.core.windows.net"
        if AZURE_ACCOUNT_NAME else ""
    )
```

Env-flag-gated so we can roll out per-environment without forcing all deploys at once.

### Per-field storage (NOT global)

Don't replace `DEFAULT_FILE_STORAGE` — that would migrate ALL media (uploads, vectordb, etc.) at once. Instead, declare Blob storage on the specific field:

```python
# apps/media_library/models.py
from django.db import models
from django.conf import settings
from storages.backends.azure_storage import AzureStorage


def _generated_image_storage():
    """Return Azure Blob storage for generated images, or default
    (local FS / File Share) when the flag is off. Per-field rather
    than global so existing teacher uploads stay on the File Share
    until we explicitly migrate them in Phase 2."""
    if getattr(settings, "USE_AZURE_BLOB_FOR_GENERATED_IMAGES", False):
        return AzureStorage(
            account_name=settings.AZURE_ACCOUNT_NAME,
            account_key=settings.AZURE_ACCOUNT_KEY,
            azure_container=settings.AZURE_CONTAINER,
            custom_domain=settings.AZURE_CUSTOM_DOMAIN,
            location="generated",  # path prefix within the container
        )
    return None  # falls through to default storage


class MediaAsset(models.Model):
    file = models.FileField(
        upload_to=_compute_upload_path,
        storage=_generated_image_storage,  # callable evaluated at attribute-access time
    )
    ...
```

Django supports a callable for `storage=`, evaluated lazily. When the flag is off, `None` falls through to default. When on, writes go to Blob.

### URL handling

When Blob is on, `asset.file.url` returns `https://<acct>.blob.core.windows.net/media/generated/<path>` — already a working public URL. No `serve` view, no sign-and-serve hop. Frontend `src=` works directly.

Existing local-disk URLs (`/media/media/global/generated_*.png`) remain valid because the File Share is still mounted. Old assets are served as today; new assets are served from Blob. Mixed coexistence is fine.

## Phase 1 — Generated images only

Concrete steps:

1. **Provision Blob container** in Pulumi (`infra/__main__.py`). One `BlobContainer` resource. `pulumi up`.

2. **Add storage backend dependency** — `django-storages[azure]` to `requirements.txt`.

3. **Settings + model field** — additive changes, env-flag-gated.

4. **Set env vars in production**:
   ```
   USE_AZURE_BLOB_FOR_GENERATED_IMAGES=true
   AZURE_STORAGE_ACCOUNT_NAME=<existing storage account, same as File Share>
   AZURE_STORAGE_ACCOUNT_KEY=<from existing keys>
   AZURE_BLOB_CONTAINER=media
   ```
   Reuse the same Storage Account that already hosts the File Share — no new account, no new keys.

5. **Deploy**. New images go to Blob; old images keep serving from File Share. Verify with one image-gen call: `curl -I` the returned URL; expect `https://<acct>.blob.core.windows.net/...` and `200`.

6. **Optional one-shot migration** of existing File-Share images to Blob:
   ```python
   # apps/media_library/management/commands/migrate_images_to_blob.py
   for asset in MediaAsset.objects.filter(asset_type='image'):
       if not asset.file.url.startswith('http'):  # still on File Share
           with asset.file.open('rb') as f:
               data = f.read()
           # Re-save under Blob storage — Django handles upload
           new_name = asset.file.name.split('/')[-1]
           asset.file.save(new_name, ContentFile(data), save=True)
   ```
   Run once, optional, only if you want existing images on Blob too.

7. **Deprovision File Share growth path**: leave the File Share as-is for now (vectordb still uses it). Eventually drop the quota back down once Phase 2 finishes uploads migration too.

Estimated effort: **~3–4 hours** end-to-end (mostly Pulumi + smoke testing).

## Phase 2 — Teacher uploads (later)

Same pattern — add `storage=_uploaded_doc_storage` on `TeachingMaterialUpload.file` etc. Treat private/PII content separately (separate Blob container with no public access; serve via signed URL).

Out of scope for the immediate fix.

## What stays on File Share

- **VectorDB** (`/app/media/vectordb`) — already handled (Dockerfile copies to `/tmp/vectordb` on startup; SQLite on SMB was a known bug fixed by that copy).
- **Existing assets** — they keep serving from File Share until explicitly migrated. Mixed coexistence is supported.

## Cross-cloud portability bonus

Once images are on Blob, the platform's filesystem dependency shrinks to vectordb + DB. Redeploying to a new cloud / region:

- vectordb → just remount or recreate (it's regenerable from `KnowledgeBase.search`)
- DB → existing Pulumi handles Postgres provisioning per-stack
- Images → swap `AZURE_*` env vars for S3 / GCS equivalents (django-storages supports both with the same model-field shape)

Currently the File Share mount is the awkward bit when redeploying. Removing it from the hot path = much easier multi-tenant / multi-region rollout.

## Risks + mitigations

- **Risk:** Azure account key rotation breaks reads/writes. **Mitigation:** Use a Managed Identity in production instead of account key. Phase 1.5 work.
- **Risk:** CORS — direct browser fetches of Blob URLs may need a CORS rule on the storage account. **Mitigation:** add it via Pulumi when provisioning.
- **Risk:** Cost surprise from public reads at scale. **Mitigation:** Azure Blob egress is included up to a generous threshold; the pilot is well below it. Monitor monthly.

## What I'd ship next

1. The 100 GiB quota bump (immediate — already in the working tree)
2. Phase 1 of this plan as a 1-day commit (provisioning + flag + first deploy)
3. Phase 2 + migration command as separate work when teacher uploads start hitting the share

This document is the source of truth for the migration; reference it when scheduling the work.
