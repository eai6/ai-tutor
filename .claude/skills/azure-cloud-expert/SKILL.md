---
name: azure-cloud-expert
description: Expert on Azure Container Apps, Pulumi IaC, ACR, PostgreSQL Flexible Server, and Azure-specific production deployment for this project. Auto-loads when working on infrastructure files. Covers scaling rules, workload profiles, networking, secrets management, storage mounts, cost optimization, and the specific gotchas that have bitten this deployment (SMB+ChromaDB, SSE buffering, arm64 vs amd64).
paths:
  - "infra/**"
  - "Dockerfile"
  - ".dockerignore"
  - ".github/workflows/**/*.yml"
  - "Pulumi.*.yaml"
---

# Azure Cloud Expert — AI Tutor Deployment

Expert on the Azure stack for this project. Production is **Azure Container Apps** on Pixel Design Labs LLC subscription, provisioned via Pulumi (Python), deployed via GitHub Actions.

## Current production topology

```
┌──────────────────────────────────────────────────────────┐
│ Resource Group: aitutor-pixel-rg                         │
│                                                          │
│  ┌────────────────────────────────────────────────────┐ │
│  │ Container App Environment (D4 workload profile)    │ │
│  │  • 4 vCPU / 8Gi RAM Dedicated                      │ │
│  │                                                    │ │
│  │  ┌────────────────────────────────────────────┐   │ │
│  │  │ Container App: aitutor-pixel-app            │   │ │
│  │  │  • 1 replica min, scales on HTTP            │   │ │
│  │  │  • 4 CPU / 8Gi per replica                  │   │ │
│  │  │  • Gunicorn: 4 workers × 4 threads, 120s   │   │ │
│  │  └────────────────────────────────────────────┘   │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─ ACR ──────────────┐  ┌─ PostgreSQL Flex ────┐       │
│  │ aitutorpixelacr    │  │ pgserver-...          │       │
│  └────────────────────┘  └───────────────────────┘       │
│                                                          │
│  ┌─ Storage Account ──────────┐                          │
│  │ Azure File Share (SMB)     │                          │
│  │ Mounted at /app/media      │                          │
│  └────────────────────────────┘                          │
└──────────────────────────────────────────────────────────┘
```

**Live URL**: `https://aitutor-pixel-app.niceground-67d5237f.centralus.azurecontainerapps.io`

**Pulumi stack**: `pixel` (old `dev` stack on Microsoft Azure Sponsorship is deprecated).

## Before any Azure work — subscription

You MUST be on the right subscription:

```bash
az account show    # should show "Pixel Design Labs LLC"
az account set --subscription "Pixel Design Labs LLC"
```

Subscription ID: `656f4091-746b-44ef-8add-6d94eb4a7612`. If you accidentally operate on the wrong subscription, resources in the other subscription can be affected.

## Pulumi stack management

```bash
cd infra/
pulumi stack select pixel
pulumi config                    # view config (encrypted secrets masked)
pulumi preview                   # dry-run
pulumi up                        # apply changes
pulumi destroy                   # DANGER — destroys all resources
```

Stack file: `infra/Pulumi.pixel.yaml`. Secrets are encrypted with the passphrase (stored in auto-memory — do NOT echo it into chat).

To set the passphrase:
```bash
export PULUMI_CONFIG_PASSPHRASE='<passphrase>'
```

### Pulumi program

`infra/__main__.py` provisions:
1. Resource group
2. Container App Environment (D4 workload profile)
3. Container App (with env vars, secrets, scaling rules)
4. Azure Container Registry + pull-credential
5. PostgreSQL Flexible Server + database
6. Storage account + file share + mount
7. Managed Identity for ACR pull

When modifying `__main__.py`, always `pulumi preview` first and read the diff. Pulumi can silently recreate resources on breaking changes — especially environment/workload profile changes.

## Container Apps gotchas (production-scarred)

### 1. No SSE / chunked streaming

Azure Container Apps buffers `StreamingHttpResponse` — SSE hangs. All production endpoints return buffered `JsonResponse`. `respond_stream()` exists in `conversational_tutor.py` but is unused.

If you need streaming in future: use WebSockets via Django Channels — Container Apps DOES support WebSocket (set `transport: websocket` on the ingress).

### 2. ChromaDB on SMB file share hangs

ChromaDB's SQLite can't handle the Azure File Share SMB mount — 120s worker timeouts. Fix:

- `VECTORDB_ROOT=/tmp/vectordb` (env var + settings)
- `Dockerfile` CMD copies vectordb from `/app/media/vectordb` → `/tmp/vectordb` on startup
- `/tmp` is local fast disk; data regenerated on container restart (acceptable for read-only KB)

Don't try to put SQLite on SMB. If ChromaDB read-only is too limiting, use Azure-native vector DB (Azure AI Search with vector index).

### 3. amd64 vs arm64 mismatch

Mac builds default to arm64. Azure Container Apps runs amd64 only. Symptoms: container crashes immediately with "exec format error".

Fixes:
- **Preferred**: let GitHub Actions build (`linux/amd64` runners by default)
- **Local build**: `docker build --platform linux/amd64 -t myimage .` then `docker push`
- **Always verify**: `docker inspect <image> | grep Architecture`

### 4. CSRF_TRUSTED_ORIGINS

Uses `env.default_domain` in Pulumi (`infra/__main__.py`) so the environment's auto-generated hash is correct. Don't hardcode the domain — if the environment is recreated, the hash changes.

```python
# config/settings.py reads CSRF_TRUSTED_ORIGINS from env
# infra/__main__.py sets this env var using env.default_domain
```

### 5. Media not serving

Django's `static()` only works with `DEBUG=True`. Production uses an explicit `serve` view in `config/urls.py`:

```python
# config/urls.py
from django.views.static import serve
from django.conf import settings

urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]
```

This serves `/app/media/` (mapped to the File Share mount).

### 6. Health probes

Pulumi configures:
- **Liveness** every 60s, 5 failures → restart
- **Readiness** every 30s, 3 failures → remove from load balancer

Endpoint: `/health/` in `apps/dashboard/views_health.py`. Must respond fast (<1s); don't put slow checks here. Check DB? Yes — cheap. Check LLM? No — unreliable + slow.

## Scaling rules

Current: HTTP-based scaling.

```python
# infra/__main__.py (abridged)
scale=containerapp.ScaleArgs(
    min_replicas=1,
    max_replicas=10,
    rules=[
        containerapp.ScaleRuleArgs(
            name="http-scale",
            http=containerapp.HttpScaleRuleArgs(
                metadata={"concurrentRequests": "50"},  # scale when >50 concurrent
            ),
        ),
    ],
)
```

Per-replica: 4 CPU / 8Gi. Each replica has its own Gunicorn (4 workers × 4 threads = 16 concurrent requests per replica).

### When to change scaling

- Scale OUT (more replicas) for: concurrent request load, long-running LLM calls occupying workers
- Scale UP (bigger workload profile) for: per-request memory (sentence-transformers loads ~500MB per worker), not concurrency
- Min replicas > 1 if: cold start on first-request matters. Currently 1 — acceptable because embeddings load slowly but we scale up fast.

### Sentence-transformers memory

`EMBEDDING_BACKEND=local` uses all-MiniLM-L6-v2 — loads ~500MB into each Gunicorn worker. 4 workers = ~2GB. The D4 (8Gi) handles this fine; the old 4Gi Consumption profile OOM'd.

## Storage / file share

Azure File Share mounted at `/app/media`. Files: uploaded curriculum, generated images, TTS output, vectordb (in `/app/media/vectordb` on SMB — but we copy to `/tmp` on startup).

Operations:
```bash
# Upload files to share:
az storage file upload-batch \
  --account-name <storage> \
  --destination <share>/path \
  --source ./local-files

# Mount locally (for inspection):
# SMB 3.0 required
```

Don't put read/write-hot paths on SMB — latency > local disk by 10-100×.

## Secrets

Two layers:
1. **Pulumi encrypted config** — API keys, DB passwords. Set via `pulumi config set --secret key value`. Encrypted with passphrase.
2. **Container App secrets** — Pulumi program reads encrypted config and creates Container App secrets, then references them as env vars.

Django reads via `os.getenv()` in `config/settings.py`. The `llm.ModelConfig` model ALSO stores encrypted API keys in the DB (Fernet with Django SECRET_KEY) — preferred for per-institution keys.

### Rotating an API key

```bash
# 1. Update Pulumi config
pulumi config set --secret ANTHROPIC_API_KEY sk-ant-...

# 2. Deploy — Pulumi updates the Container App secret
pulumi up

# 3. New requests use new key (container env reloaded on restart)
```

## CI/CD pipeline

`.github/workflows/deploy.yml` — triggers on push to `main`:

1. Build amd64 image with Buildx
2. Push to ACR (`aitutorpixelacr.azurecr.io`)
3. Update Container App to new image tag via `az containerapp update`

GitHub secrets required:
- `AZURE_CREDENTIALS` (service principal JSON)
- `ACR_LOGIN_SERVER` = `aitutorpixelacr.azurecr.io`
- `AZURE_RESOURCE_GROUP` = `aitutor-pixel-rg`
- `CONTAINER_APP_NAME` = `aitutor-pixel-app`

Service principal: `aitutor-github-actions-pixel` (appId `d75a3030-52e8-4f5b-a9b7-ecf44783925d`), Contributor + AcrPush roles.

## Logging / observability

Container App stdout/stderr → Log Analytics workspace. Query via:

```bash
az monitor log-analytics query \
  --workspace <workspace-id> \
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'aitutor-pixel-app' | order by TimeGenerated desc | take 100"
```

Django's `LOGGING` config (`config/settings.py`) outputs to stdout so everything's captured. Use structured logging for better queries:

```python
logger.info("Event", extra={"user_id": user.id, "lesson_id": lesson.id})
```

## Cost tuning

D4 workload profile: ~$X/day baseline (dedicated, always-on). Scales per replica.

Levers:
- **Min replicas**: 1 is cheapest; 0 is cheaper still (cold starts) if consumption-tier. D4 doesn't scale-to-zero.
- **Scale rule threshold**: higher threshold = fewer replicas = slower response under load
- **PostgreSQL tier**: flexible server has burstable + general-purpose + memory-optimized. Match to workload.
- **Embedding**: `EMBEDDING_BACKEND=local` is free (no API cost, uses CPU). `openai` is fast but per-call cost adds up at scale.

If moving to higher traffic: consider Azure OpenAI for embeddings (cheaper than OpenAI API at scale, same model) + AI Search for vector DB (managed, scales without sharding).

## When something breaks in production

### Checklist

1. **Check logs**: Log Analytics query for errors in last 15 min
2. **Check health**: `curl https://aitutor-pixel-app.../health/` — is it 200?
3. **Check replicas**: `az containerapp show -n aitutor-pixel-app -g aitutor-pixel-rg --query properties.runningStatus`
4. **Check DB**: can you connect from Azure Portal query editor? Slow queries?
5. **Check deploys**: `az containerapp revision list -n ... -g ...` — recent revision rollback possible?

### Rollback

```bash
# List revisions
az containerapp revision list -n aitutor-pixel-app -g aitutor-pixel-rg -o table

# Activate previous revision (traffic shifts immediately)
az containerapp revision activate --revision <previous-revision-name> \
  --app aitutor-pixel-app --resource-group aitutor-pixel-rg
```

Blue/green is built-in: each deploy creates a new revision; old revisions stay around (configurable retention). Rollback = reactivate previous revision.

## Don'ts

❌ Don't run `pulumi destroy` without explicit user confirmation
❌ Don't run Azure commands without `az account show` to confirm subscription first
❌ Don't hardcode the Container App domain (use `env.default_domain`)
❌ Don't put read/write-hot paths on Azure File Share SMB
❌ Don't build locally for production on an Apple Silicon Mac without `--platform linux/amd64`
❌ Don't leave `PULUMI_CONFIG_PASSPHRASE` in shell history
❌ Don't echo Pulumi secrets into chat or commits
❌ Don't modify the old `dev` stack (Microsoft Azure Sponsorship) — it's deprecated

## Old deployment (deprecated)

For reference — do NOT deploy here:
- Subscription: Microsoft Azure Sponsorship
- URL: `https://aitutor-dev-app.victoriousbay-295239d3.centralus.azurecontainerapps.io`
- Stack: `dev` (2CPU/4Gi Consumption, OpenAI embeddings)
- Resources can be destroyed when no longer needed for reference

## Further context

- `memory/` — active plans (mobile, group lessons, competency changes)
- Auto-memory — full deployment history + incident log
- `CLAUDE.md` — always-apply project rules including Azure constraints
- `infra/__main__.py` — Pulumi program (read before any infra change)
