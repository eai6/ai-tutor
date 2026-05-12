---
name: cicd-expert
description: Expert on CI/CD for this project — GitHub Actions pipelines, Docker builds, deployment strategies, EAS Build for mobile, secret management, and rollback procedures. Auto-loads when working on workflow files. Covers multi-arch builds, blue/green deploys, preview environments, and the pipelines already in place (Azure Container Apps deploy on push to main).
paths:
  - ".github/workflows/**"
  - ".github/actions/**"
  - "Dockerfile"
  - ".dockerignore"
  - "eas.json"
  - "mobile/eas.json"
---

# CI/CD Expert — AI Tutor

Expert on CI/CD for this project. Production deploys via GitHub Actions to Azure Container Apps; mobile will use EAS Build (when mobile app exists).

## Current pipeline

`.github/workflows/deploy.yml` — triggers on push to `main`:

1. **Checkout** repo
2. **Set up Docker Buildx** (for cross-platform builds — we need amd64 on a Linux runner)
3. **Login to Azure** using service principal (`AZURE_CREDENTIALS` secret)
4. **Login to ACR**: `az acr login -n aitutorpixelacr`
5. **Build + push** amd64 image tagged with the commit SHA + `latest`
6. **Update Container App** to new image tag: `az containerapp update --image ...`
7. **Wait for healthy revision** (optional but recommended)

Deploy time: ~9-10 minutes. No tests run in CI currently (opportunity for improvement).

## Pipeline patterns used in this project

### Multi-arch builds

Docker build runs on `ubuntu-latest` (amd64 by default). The `Dockerfile` is arch-agnostic; only the base image selection matters. If we ever need arm64 (e.g., for Azure Ampere instances), use Buildx's `--platform linux/amd64,linux/arm64`.

### Secret management

All secrets live in GitHub's **repository secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose |
|---|---|
| `AZURE_CREDENTIALS` | Service principal JSON for `az login` |
| `ACR_LOGIN_SERVER` | `aitutorpixelacr.azurecr.io` |
| `AZURE_RESOURCE_GROUP` | `aitutor-pixel-rg` |
| `CONTAINER_APP_NAME` | `aitutor-pixel-app` |

For env vars passed to the app at build OR runtime, use **Pulumi config / Container App secrets** — NOT GitHub secrets. GitHub secrets are for CI-time credentials only.

### Service principal creation

```bash
# Create SP with Contributor + AcrPush on the RG
az ad sp create-for-rbac \
  --name "aitutor-github-actions-pixel" \
  --role "Contributor" \
  --scopes "/subscriptions/<sub-id>/resourceGroups/aitutor-pixel-rg" \
  --sdk-auth

# Then grant AcrPush specifically on the ACR:
az role assignment create \
  --assignee <appId> \
  --role "AcrPush" \
  --scope "/subscriptions/<sub-id>/resourceGroups/aitutor-pixel-rg/providers/Microsoft.ContainerRegistry/registries/aitutorpixelacr"
```

The `--sdk-auth` output goes in `AZURE_CREDENTIALS` as a single JSON blob.

## Recommended improvements

### Add pre-deploy tests

Currently tests only run locally. Add a test job that blocks deploy on failure:

```yaml
# .github/workflows/deploy.yml
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:15
        env:
          POSTGRES_PASSWORD: postgres
        ports: ['5432:5432']
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: pytest
        env:
          DATABASE_URL: postgres://postgres:postgres@localhost:5432/postgres
          DJANGO_SECRET_KEY: test-key-not-for-prod

  deploy:
    needs: test
    runs-on: ubuntu-latest
    # ... existing build+deploy steps
```

### Add preview deploys

For feature branches / PRs, deploy to a separate Container App revision with a preview URL. Pattern:

```yaml
on:
  pull_request:
    branches: [main]

jobs:
  preview:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy preview revision
        run: |
          az containerapp revision copy \
            --name aitutor-pixel-app \
            --resource-group aitutor-pixel-rg \
            --image $ACR_LOGIN_SERVER/aitutor:pr-${{ github.event.number }} \
            --revision-suffix pr${{ github.event.number }}
      - name: Comment preview URL
        uses: peter-evans/create-or-update-comment@v4
        with:
          issue-number: ${{ github.event.number }}
          body: Preview → https://aitutor-pixel-app--pr${{ github.event.number }}.niceground-....azurecontainerapps.io
```

Container Apps supports multi-revision mode — revisions get their own fqdn like `<app>--<revision>.<domain>`.

### Add health check post-deploy

Verify the new revision is serving before exiting the pipeline:

```yaml
- name: Wait for healthy
  run: |
    for i in {1..30}; do
      STATUS=$(curl -s -o /dev/null -w "%{http_code}" https://aitutor-pixel-app.niceground-....azurecontainerapps.io/health/)
      if [ "$STATUS" = "200" ]; then
        echo "Healthy"
        exit 0
      fi
      sleep 10
    done
    echo "Deploy did not become healthy"
    exit 1
```

## Docker patterns for this project

Current `Dockerfile` is multi-stage (build deps separate from runtime). Key CMD (simplified):

```dockerfile
CMD ["sh", "-c", "\
  python manage.py migrate --noinput && \
  cp -r /app/media/vectordb /tmp/vectordb 2>/dev/null || true && \
  gunicorn config.wsgi:application \
    --workers 4 --threads 4 --timeout 120 \
    --bind 0.0.0.0:8000"]
```

Critical: `migrate` + `cp vectordb` run on every container startup. Fine for short migrations; problematic for long-running data migrations (startup probe will timeout).

For big migrations, use a **separate job** (Azure Container Apps Jobs) that runs migrations once, then deploy the app container.

### Don't COPY large files

`.dockerignore` excludes:
- `venv/`
- `media/` (mounted separately)
- `db.sqlite3`
- `datadump.json`
- `openstax_resources/`
- `archives/`

If any of these get COPY'd in, images bloat and deploys slow down.

### Layer caching

Structure the Dockerfile so dependency installs cache:

```dockerfile
# Cache-friendly order:
COPY requirements.txt .
RUN pip install -r requirements.txt

# Code changes frequently — put last
COPY . .
```

GitHub Actions caches Buildx layers automatically via `cache-from` / `cache-to`:

```yaml
- uses: docker/build-push-action@v5
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max
    platforms: linux/amd64
```

## Mobile CI/CD (EAS Build — planned)

When the RN app exists (`memory/mobile_rn_plan.md`):

### EAS Build profiles (`mobile/eas.json`)

```json
{
  "build": {
    "development": { "developmentClient": true, "distribution": "internal" },
    "preview": { "distribution": "internal", "ios": { "simulator": false } },
    "production": { "autoIncrement": true }
  },
  "submit": {
    "production": {
      "ios": { "appleId": "...", "ascAppId": "..." },
      "android": { "serviceAccountKeyPath": "./google-play-key.json" }
    }
  }
}
```

### GitHub Actions for mobile

```yaml
# .github/workflows/mobile-preview.yml
on:
  pull_request:
    paths: ['mobile/**']

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - uses: expo/expo-github-action@v8
        with:
          eas-version: latest
          token: ${{ secrets.EXPO_TOKEN }}
      - run: cd mobile && npm ci
      - run: cd mobile && eas build --profile preview --platform all --non-interactive --wait
```

### OTA updates (EAS Update)

For JS-only changes, skip a full build via EAS Update:

```yaml
- run: cd mobile && eas update --branch production --message "Fix tutor crash"
```

Shipped in seconds, no App Store review. Only for JS/asset changes; native module updates still need a full build.

## Rollback strategies

### Azure Container Apps

Revisions are immutable. Rollback = reactivate an older revision:

```bash
# See revisions
az containerapp revision list \
  --name aitutor-pixel-app \
  --resource-group aitutor-pixel-rg \
  --query '[].{name:name, active:properties.active, created:properties.createdTime, trafficWeight:properties.trafficWeight}' \
  -o table

# Reactivate previous
az containerapp revision activate \
  --revision <previous-revision-name> \
  --app aitutor-pixel-app \
  --resource-group aitutor-pixel-rg
```

Traffic shifts immediately. Revision retention is configurable (currently keeps last 10).

### Mobile (EAS)

- **EAS Update**: publish a previous JS bundle via `eas update --republish`
- **App Store**: cannot force-revert users to an older version. Ship a hotfix + pull the bad version from review.

### Rule of thumb

If the broken deploy is:
- **Backend**: rollback via revision activate (seconds)
- **Mobile JS-only**: EAS Update republish (minutes, users get it on next app open)
- **Mobile native**: new EAS Build + hotfix review (hours to days). Don't deploy native changes to prod without canary.

## Branch strategy

- `main` = production
- Feature branches → PR → review → merge to main → auto-deploy

Straightforward for a solo dev. When team grows: add `develop` + release branches, or trunk-based with feature flags.

## Safety rules

❌ **Don't** skip `--no-verify` on git push unless user explicitly asks. Hooks catch things pre-flight.
❌ **Don't** force-push to `main`. Production deploys here.
❌ **Don't** deploy untested changes straight to `main` on a Friday evening 🙃.
❌ **Don't** commit secrets to `.env` or anywhere in the repo.
❌ **Don't** rotate secrets without a plan to update both Pulumi config + GitHub secrets + any running replica.
✅ **Do** verify `az account show` matches expected subscription before any Azure command.
✅ **Do** `pulumi preview` before `pulumi up`.
✅ **Do** run `docker inspect <image> | grep Architecture` to verify amd64 before pushing to ACR.
✅ **Do** watch Log Analytics for ~5 min after deploy to catch immediate errors.

## When something's wrong

1. **Broken deploy**: rollback via `az containerapp revision activate` to last known good revision.
2. **CI passing but prod broken**: check for env-specific issues (ENV vars, secrets, DB migrations). Compare last-good vs current revision's env.
3. **Build failing**: check Buildx layer cache. Occasionally clear with `cache-to: type=gha,mode=max,force=true`.
4. **`az login` failing in CI**: service principal credential expired or RBAC role changed. Rotate SP credential.

## Further context

- `azure-cloud-expert` skill — Azure Container Apps specifics
- Auto-memory — past incidents and their fixes
- `CLAUDE.md` — the amd64 / SSE / CSRF gotchas repeated here so they don't bite you again
