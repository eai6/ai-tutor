# Azure → AWS data copy, without exposing the database — Plan (2026-08-08)

## Context

Parts 1 and 2 of the previous plan are done: `https://migration.edwardamoah.com`
serves with a valid certificate, port 80 redirects, the WAF is blocking with a
rate rule, and CI deploys end to end. What remains is the data copy.

A 20 MB dump of Azure production has already been taken and verified against the
runbook's recipe — 784 TOC entries, 72 `TABLE DATA`, `EXTENSION - vector`
present. `SECRET_KEY` on AWS has been set to Azure's value, so the
Fernet-encrypted `ModelConfig.api_key_encrypted` column will decrypt after the
restore instead of silently falling back to env vars
(`apps/llm/models.py:270-274`).

Two things block finishing, and one is a live exposure I created.

**The Azure firewall hole.** Taking the dump required a temporary rule,
`tmp-migration-dump`, allowing this laptop's IP (`8.46.37.38`) to reach
production Postgres. Deleting it failed — the `prod-no-delete` CanNotDelete lock
on `aitutor-pixel-rg` blocks it. It is still open. This is the highest-priority
item in the plan and is unrelated to the rest of the work.

**The AWS database is unreachable from here, correctly.** RDS is
`publicly_accessible=False` in private subnets (`data.py:88`). The restore has
to run from inside the VPC. Making it public temporarily was rejected: it is the
same pattern that just left a hole open on Azure, and this data is student
records.

## Part 1 — Close the Azure hole (DONE 2026-08-08)

```
az lock delete --name prod-no-delete -g aitutor-pixel-rg
az postgres flexible-server firewall-rule delete -g aitutor-pixel-rg \
    -n aitutor-pixel-pg --rule-name tmp-migration-dump --yes
az lock create --name prod-no-delete --lock-type CanNotDelete -g aitutor-pixel-rg
```

Done and verified: `tmp-migration-dump` no longer listed, `prod-no-delete` is
back at `CanNotDelete`, and a `psql` from this laptop to the production DSN now
times out rather than connecting.

The lock is the thing protecting production from accidental deletion — it goes
back in the same breath as the delete, not "later".

While there: `allow-local` (`108.31.132.119`) and `diag-dmarie-232758`
(`67.22.23.225`) are pre-existing rules not present in IaC. Not mine to remove,
but worth asking whether they should still exist.

## Part 2 — `postgresql-client` in the image

`Dockerfile` has **no `apt-get` step in either stage**. The client must go in the
**runtime** stage (after line 22, before `COPY . .`) — packages installed in the
builder would not carry over, because only `/usr/local`, the HuggingFace cache
and `/models/piper` are copied forward, and Debian puts binaries in `/usr/bin`.

```dockerfile
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/*
```

Pin the major version to match RDS (Postgres 16, `data.py:26`) — a `pg_restore`
older than the server cannot read its dumps. ~20-30 MB on an image that already
carries CPU torch and two speech models, so proportionally nothing.

**Check the Azure side is unaffected.** `ops/migrate_and_seed.sh:2-11` records
that the Dockerfile is deliberately left alone because Azure depends on its CMD.
This change adds a layer and does not touch CMD, but the Azure deploy must still
be green afterwards.

## Part 3 — A separate bucket for the dump

**Do not use the media bucket.** The task role already has `s3:GetObject` on it
(`compute.py:126-134`), which makes it tempting — but `apps/media_library/s3_media.py:93`
serves objects out of that bucket at `/media/<path>`, and per CLAUDE.md that
route has no auth gate. Putting a production database dump there could make it
downloadable over the internet. That is the exact failure this whole approach
exists to avoid.

New private bucket, in `infra/aws/components/storage.py` alongside the existing
one:

- `{prefix}-ops-{account_id}`, public access block all four flags true
- SSE-S3, matching the media bucket (`storage.py:41-52`)
- **A lifecycle rule expiring objects after 7 days** — so a forgotten dump
  deletes itself rather than sitting there indefinitely
- Task role granted `s3:GetObject` on this bucket only

## Part 4 — The restore task

**Destination handling: drop and recreate the database.** The AWS database is
not empty — `ops/migrate_and_seed.sh` has run, creating every table and seeding
`ModelConfig` and terms rows that would collide with the dump.

Sequence:

1. Scale the ECS service to 0 (`aws ecs update-service --desired-count 0`) and
   wait — you cannot drop a database with live connections.
2. Upload the dump to the ops bucket.
3. Run a one-off task. **Reuse the `migrate` task definition with a command
   override** rather than adding a fourth definition: it already carries
   `DATABASE_URL` and the other five secrets (`compute.py:169-177` — all three
   containers get all six), runs in the private subnets, and uses the security
   group RDS accepts.
4. Restore, in this order — the pgvector research is specific about this:
   - connect to the `postgres` database (not `aitutor`, which is being dropped)
   - `DROP DATABASE aitutor; CREATE DATABASE aitutor;`
   - `CREATE EXTENSION IF NOT EXISTS vector;` **before** `pg_restore`
   - `pg_restore --no-owner --no-acl -j 4`
     `--no-owner --no-acl` are what avoid "must be owner of extension vector" —
     the RDS master user is `rds_superuser`, not a true superuser.
5. Delete the object from S3.
6. Scale the service back to 1 and wait for stable.

**Copy the invocation pattern from `deploy-aws.yml:176-221`**, which is already
proven: read `networkConfiguration` off the live service rather than hardcoding
subnets, `run-task`, poll `lastStatus` on a 30-minute deadline (the comment at
:196 explains `ecs wait tasks-stopped` gives up at ~10 min), then check
`containers[0].exitCode` and tail
`/ecs/{prefix}` stream `migrate/migrate/{task-id}`.

Write it as `ops/restore_from_dump.sh` so it is repeatable, not a one-off shell
session.

## Out of scope

- **Media files.** Uploads live on the Azure SMB share (`infra/__main__.py:419-426`);
  AWS uses S3. The database holds paths only, so a DB-only copy gives a working
  app with broken images. The runbook records media handover as untested. Separate task.
- Cutover, DNS moves, or anything that makes AWS authoritative. This is a
  parallel evaluation environment.

## Verification

- **Azure**: `az postgres flexible-server firewall-rule list` shows no
  `tmp-migration-dump`, and `az lock list -g aitutor-pixel-rg` shows
  `prod-no-delete` present.
- **Image**: `docker run --rm <image> pg_restore --version` reports 16.x.
- **Restore**: row counts against the drill figures —
  `tutoring_sessionturn` 36,109 · `tutoring_tutorsession` 1,106 ·
  `curriculum_lesson` 354 · `auth_user` 389. Plus
  `SELECT count(*) FROM curriculum_curriculumchunk` non-zero and
  `\dx` listing `vector`, which is where a silent pgvector failure shows up.
- **End to end**: load a lesson at `https://migration.edwardamoah.com` and take
  one tutoring turn. That exercises `ModelConfig.get_api_key()` — the silent
  Fernet-fallback path — and is the only way to confirm the `SECRET_KEY` copy
  worked before a student finds out.
- **Azure unharmed**: the Azure deploy workflow still green after the Dockerfile
  change.

---

## Outcome — 2026-08-08 (all parts DONE)

Azure production data is live in AWS RDS at `https://migration.edwardamoah.com`.

Final counts, all at or above the drill figures (the source kept growing):
`tutoring_sessionturn` 37,107 · `tutoring_tutorsession` 1,148 ·
`curriculum_lesson` 354 · `auth_user` 396 · `curriculum_curriculumchunk` 1,007 ·
`accounts_institution` 13. `vector` extension present, 0 tables without a
primary key.

### Two things the plan did not anticipate

**The base image had moved to trixie.** Part 2 pinned the PGDG suite to
`bookworm`, but `python:3.12-slim` is now Debian 13, so apt was offered packages
built for a different release and could not resolve `libpq5`. The suite is now
read from `/etc/os-release` instead of hardcoded. Installed:
`pg_restore 16.14 (Debian 16.14-1.pgdg13+1)`.

**Scaling the service to 0 does not make the database idle.** The first restore
failed creating `support_supportchunk_pkey` — `Key (id)=(10) is duplicated` —
even though the dump was clean (816 rows, 816 distinct ids, one `TABLE DATA`
entry). Cause: a deploy's one-off migrate task was still running
`build_help_index`. One-off tasks hold connections and are invisible to
`desiredCount`. `DROP DATABASE` cut it off, Django reconnected to the new empty
database, and its upserts landed in `support_supportchunk` while `pg_restore`
was loading the dump's own rows. Blast radius was that one table only —
`apps/support/kb.py:86` is the sole writer, and it is derived data.
`restore_from_dump.sh` now waits for the cluster to be genuinely idle and
refuses to proceed if it is not.

### The step that is easy to miss

The AWS web task definition overrides `command` to gunicorn alone, so migrations
run ONLY via the one-shot `aitutor-dev-migrate` task. A dump from Azure carries
Azure's schema, which is behind this branch — `/staff/login/` returned 500 on
`column accounts_platformterms.locale does not exist` until the migrate task ran
(13 migrations applied). **Restoring an Azure dump is always a two-step
operation: restore, then run the migrate task.**

### Fernet / SECRET_KEY — a non-issue

All 60 `ModelConfig` rows have an empty `api_key_encrypted`; production keeps
provider keys in env vars. Nothing depended on the `SECRET_KEY` copy for API
access. The tutoring config resolves to `anthropic/claude-opus-4-7` with a live
key from Secrets Manager. `SECRET_KEY` still matters for session and signing
cookies.

### Still open

- **Media files.** Out of scope as planned. `/media/platform_logos/...` 404s —
  the database holds paths, the Azure SMB share holds the bytes.
- No end-to-end tutoring turn was taken. It needs a login, and the accounts in
  this database are real students — worth a throwaway account rather than
  impersonating one.
- The `aws_deployment` push trigger still deploys on every push now that real
  student data is here. Worth narrowing to manual dispatch.
