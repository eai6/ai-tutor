# Releasing AI Tutor

The release contract:

- Every released version has an annotated **git tag** (`vX.Y.Z`)
  pointing at the exact commit on `main`.
- Every released version has a **Docker image tagged `aitutor:vX.Y.Z`**
  in the Pixel ACR (`aitutorpixelacr.azurecr.io`). This image is what
  serves production at that version.
- Every released version has a **GitHub Release** with notes that
  point at the tag.
- The repo-root **`VERSION` file** matches the tag; the `/health/`
  endpoint surfaces it so an operator can confirm what's running.
- The **`CHANGELOG.md`** entry summarises what's in the version and
  why.

Together these give us "re-deploy this exact build in the future"
without needing to remember anything beyond the version string.

## Cutting a new release (full recipe)

### 1. Ship code to `dev` first

All work flows through `dev` first. The staging Container App
(`aitutor-staging-app`) auto-deploys from `dev`; we exercise the
release candidate there before promoting.

### 2. Pick the version number

SemVer:
- `MAJOR` (`X.0.0`) — breaking change in public contract (Django
  views, API, env vars, DB schema requiring manual migration steps).
- `MINOR` (`0.Y.0`) — backwards-compatible feature.
- `PATCH` (`0.0.Z`) — bug fix only, no new feature.

Pre-1.0 (`0.x.y`) means "production-tested but pre-stable" — the
public contract is allowed to change between minors. Cut `1.0.0`
when the contract is stable enough to commit to.

### 3. Update files **in `dev`** before merging

```bash
git checkout dev
# Bump VERSION
echo "0.2.0" > VERSION
# Add the release entry at the TOP of CHANGELOG.md (newer-first).
# Use `git log --oneline <prior-tag>..HEAD` to find what to write about.
$EDITOR CHANGELOG.md
# Commit
git add VERSION CHANGELOG.md
git commit -m "release: v0.2.0"
git push
```

The staging deploy that follows will surface the new version via
`/health/` — verify it lands cleanly.

### 4. Merge `dev` → `main`

```bash
git checkout main
git pull --ff-only
git merge --ff-only origin/dev    # MUST be fast-forward; no merge commit
git push origin main
```

The push triggers GHA `Deploy` which builds + pushes
`aitutorpixelacr.azurecr.io/aitutor:<git-sha>` + `:latest` and
updates the production Container App. The tag-aware step also pushes
`:v0.2.0` if the push is a tag push (see step 5).

### 5. Create + push the annotated tag

```bash
git tag -a v0.2.0 -m "AI Tutor v0.2.0 — <one-line summary>"
git push origin v0.2.0
```

The tag push re-triggers GHA, which detects the ref-type tag and
**adds `aitutor:v0.2.0` as a third tag on the same image manifest**.

### 6. Lock the ACR tag against accidental deletion

ACR Basic doesn't have full immutability policies, but we can mark a
specific tagged manifest non-deletable + non-writable:

```bash
az acr repository update \
  --name aitutorpixelacr \
  --image aitutor:v0.2.0 \
  --delete-enabled false \
  --write-enabled false
```

The image is now pinned forever (until the lock is explicitly
released by the same command with `--delete-enabled true`).

### 7. Create the GitHub Release

```bash
gh release create v0.2.0 \
  --title "v0.2.0 — <one-line summary>" \
  --notes-file CHANGELOG-v0.2.0-notes.md   # or extract the CHANGELOG section
```

The release surfaces the tag at the top of the GitHub Releases page
and lets you attach assets (release artifacts, screenshots) if
useful.

### 8. Verify

```bash
# Prod /health/ should report the new version
curl https://aitutor-pixel-app.<env-hash>.centralus.azurecontainerapps.io/health/ | jq .

# ACR should have the v0.2.0 tag
az acr repository show-tags --name aitutorpixelacr --repository aitutor | grep v0.2.0
```

## Rolling back to a previous version

If a release goes sideways:

```bash
# Step 1 — pick the prior version
PRIOR=v0.1.0

# Step 2 — point the prod Container App at that image
az containerapp update \
  --name aitutor-pixel-app \
  --resource-group aitutor-pixel-rg \
  --image aitutorpixelacr.azurecr.io/aitutor:${PRIOR}

# Step 3 — confirm
curl https://aitutor-pixel-app.<env-hash>.centralus.azurecontainerapps.io/health/ | jq .version
# → should print "0.1.0"
```

That's it for the application layer. Two caveats:

- **Database migrations**: a forward migration applied for v0.2.0
  may not be reversible. Check `apps/*/migrations/` for any
  data-destructive migrations between the tags. If present, roll
  forward (fix the bug at HEAD) instead of rolling back.
- **Container App env vars**: Pulumi config drives them. Rolling
  back the image doesn't roll back env-var changes. Check
  `infra/Pulumi.pixel.yaml` history if a v0.2.0 deploy introduced a
  new env-var the v0.1.0 image doesn't read — usually fine; new
  vars are no-ops on the older image.

## Replaying a release on a fresh environment

To deploy v0.1.0 to a clean Container App (e.g. a new region, a
post-incident replacement):

```bash
# 1. Provision infra (Pulumi will create RG, ACR, Container App, etc.)
cd infra
pulumi stack init <new-stack-name>
# Set secrets per Pulumi.<stack>.yaml comments
pulumi up --stack <new-stack-name>

# 2. Import the v0.1.0 image from the source ACR
az acr import \
  --name $(pulumi stack output acr_login_server --stack <new-stack-name> | cut -d. -f1) \
  --source aitutorpixelacr.azurecr.io/aitutor:v0.1.0 \
  --image aitutor:v0.1.0

# 3. Point the new Container App at it
az containerapp update --name <new-app-name> --resource-group <new-rg> \
  --image <new-acr>.azurecr.io/aitutor:v0.1.0
```

This is exactly the same pattern as a rollback; just sourcing the
image cross-ACR.

## Why this works for "I want to re-deploy this exact version later"

- The **git tag** is immutable on GitHub (force-deletion would
  require explicit admin action). The tag points at a frozen commit
  SHA.
- The **Docker image** at that tag is immutable in ACR (locked via
  step 6). Pulling `aitutor:v0.1.0` always returns the same image
  manifest.
- The **VERSION file** in the image matches the tag, so `/health/`
  proves it's running v0.1.0.
- **Pinned dependencies** (`requirements.txt` is 148/148 == pinned)
  + the Dockerfile mean a rebuild from source would also be
  bit-identical.

Two of three would be enough for confidence. All three is overkill —
which is what we want for a release.
