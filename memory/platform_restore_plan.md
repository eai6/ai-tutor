# Restore from backup — audit + implementation plan

## Context

The platform can take a backup (`ai_tutor/apps/dashboard/backup.py`, shipped in
`668c451`): an archive holding a `pg_dump`, every media object, and a manifest,
landing in a versioned S3 bucket with a 90-day expiry. It can be downloaded.
**It cannot be put back through the product.** The only restore path is
`ops/restore_from_dump.sh` — an operator running AWS CLI from a laptop, with a
dump they obtained some other way, and it restores the database only.

That is half a disaster-recovery story. A backup nobody has ever restored is a
file that looks like a backup; the day it is needed is the worst possible day to
discover that the archive is truncated, the schema is three migrations behind, or
that the media half was never included. This adds the other half: a superadmin
picks an archive (from the bucket, or uploaded from elsewhere), sees exactly what
it would put back versus what is live now, and commits to it.

---

# Part 1 — Audit of the existing backup feature

Ordered by consequence. Items marked **[blocks restore]** must be fixed for Part 2
to work at all; the rest are findings in their own right.

### A1. The manifest is readable only by reading the whole archive
`backup.py:310-322` adds the dump, then media, then `manifest.json` **last**. A
`.tar.gz` is sequential and gzip is not seekable, so reading the manifest out of
the 9.3 GB full archive means streaming all 9.3 GB. Every useful pre-restore
check — what is in here, which engine, which schema, how many rows — is in that
file.

**The fix is not to reorder the tar.** `backup.py:331` already copies the whole
manifest onto `job.summary`, so for any archive still listed on the settings page
the manifest is a database read costing zero S3 bytes. The gap is only archives
whose row is gone (an upload from elsewhere; the pre-restore safety copy, whose
row the restore itself destroys).
**Fix:** have `_store()` (`backup.py:254-269`) also `put_object` the manifest to
`{key}.manifest.json` beside the archive. One small GET covers the orphan case,
every archive already in the bucket keeps working, and the in-tar manifest stays
exactly where it is — an archive handed to a regulator years from now should
still be self-describing, which is what `_restore_instructions` is for.

### A2. No checksum anywhere **[blocks restore]**
Nothing in the manifest or the `BackupJob` row records a hash of the dump or of
the archive. A truncated S3 upload, a half-copied file on a laptop, or a bit-rotted
archive is indistinguishable from a good one until `pg_restore` fails partway —
after the live database has already been dropped.
**Fix:** `sha256` of the dump file in the manifest; `sha256` of the whole archive
on the `BackupJob` row.

### A3. No record of schema version **[blocks restore]**
`_app_version()` (`backup.py:423`) reads the repo-root `VERSION` file, which has
said `0.1.0` since 2026-05-29 and is never bumped — it is not a version, it is a
constant. So an archive carries no usable answer to "which code does this schema
match?", and restoring a dump whose schema is *ahead* of the deployed code is
unrecoverable in a way that restoring one *behind* it is not.
**Fix:** record `django_migrations` heads per app in the manifest. That is the
signal that makes the pre-restore compatibility check real.

### A4. The settings page does a full S3 bucket listing on every render
`views.py:4013` calls `backup_service.inventory()` unconditionally; for
`USE_S3_MEDIA` that paginates `list_objects_v2` over all 10,521 media objects —
about 11 LIST round-trips — plus `.count()` on every model in eight app labels
(`backup.py:98-112`), all to render two numbers. This runs on every superadmin
settings page load, not just when a backup is taken.
**Fix:** cache `inventory()` for ~15 minutes (`django.core.cache`), with the
timestamp shown so the number is visibly an estimate.

### A5. `BackupJob`'s docstring describes a bucket the feature does not use
The model docstring (`models.py:~480`) says archives go to "the ops bucket, which
has a 7-day expiry rule". They go to a dedicated backups bucket with a **90-day**
expiry and versioning (`storage.py:139-222`, `settings.py:501-507`) — which is
what the UI text and `e45ed99` say. Stale from an earlier draft; the wrong half is
the docstring. Separately, `docs/self-hosting.md`'s bucket table omits the backups
bucket altogether (its `ops` row is accurate).

### A6. Ephemeral-storage headroom is thinner than the comment claims
`_add_media` (`backup.py:201-204`) correctly streams media object-by-object
rather than syncing the bucket, but the **archive itself** is written whole into
the same scratch directory: ~9.3 GB of largely incompressible image data, plus the
dump. Fargate ephemeral storage is the 20 GiB default — `ephemeral_storage` is not
set on any task definition in `compute.py`. It fits today with ~10 GB to spare;
it stops fitting when the media store roughly doubles, and the failure is a
mid-backup `ENOSPC`, not a warning.

### A7. The download button already points a browser at `*.amazonaws.com`
`settings.py:449-460` and `s3_media.py:5-10` state as policy that media is served
from our own domain and *never* a presigned link, because schools allowlist
`www.seselai.sc` and block `*.amazonaws.com`. `backup_download` (`views.py:9998`)
redirects to a presigned S3 URL. Defensible — it is a superadmin on a laptop, not
a student on school wifi, and a multi-GB download cannot pass through a worker
with `--timeout 120` — but it is an undocumented exception to a written rule, and
it will fail confusingly if anyone tries it from inside a pilot school.
**Fix:** a sentence in the card saying the download link is an AWS address and may
be blocked on a school network.

### A8. Media file metadata is lost, and S3 restore is slower than it needs to be
`_add_media` downloads each object to a scratch file and `tar.add`s it
(`backup.py:213-217`), so every archived file carries the temp file's mtime and
mode rather than the original's. Harmless for a restore keyed by object name,
worth knowing. The serial download of 10,521 objects is also the dominant cost of
a full backup; a small thread pool would cut it substantially. Not required here.

### A9. Two-hour blind spot on a dead job
`STALE_AFTER = 6h` (`backup.py:358`) is deliberately generous, but nothing surfaces
"this has been running for four hours" to the admin — the card shows a stage and a
percentage that simply stop moving. Low priority; noting it because the restore
job needs the same reaper and should not inherit the same silence.

### A10. `{prefix}-material` never receives a new image from CI
Not a backup bug, found while checking whether a new task family would stay
current. `deploy-aws.yml` re-registers exactly two families — `web_family` and
`migrate_family` (`deploy-aws.yml:183-184`). The word "material" does not appear
in the workflow at all. So `{prefix}-material` is pinned to whatever `image-tag`
said at the last hand-run `pulumi up` (`__main__.py:203`, default `latest`), and
material processing on AWS runs code that may be arbitrarily old. Worth a
separate fix; it is recorded here because it is why Part 2 no longer proposes a
new task family.

### What the audit found to be right
Worth stating, because Part 2 depends on it: the single-active-job partial unique
constraint (`models.py:545`) is enforced by the database rather than by a check in
the view; `reap_stale()` exists so a dead thread cannot wedge that constraint
forever; the failure handler's own failure is swallowed deliberately
(`backup.py:345-352`); `superadmin_required` (`views.py:228`) raises `Http404`
rather than redirecting; every create and download writes a `SafetyAuditLog` row;
the local-disk download path checks for traversal (`views.py:10006`); and the
Dockerfile already ships `pg_restore` pinned to 16 (`Dockerfile:44-77`),
explicitly so a restore can run from inside the VPC. The tests
(`test_platform_backup.py`) open the archive and read what is inside rather than
trusting a filename.

---

# Part 2 — Implementation plan

## Shape, and why

The proven restore already exists as `ops/restore_from_dump.sh` +
`ops/restore_inner.py`. It is not a starting point to improve on — it is a
record of things that went wrong. It scales the service to zero because a
database cannot be dropped while the pool holds connections; it waits for the
cluster to be *idle* because on 2026-08-08 a deploy's migrate task survived the
scale-down, reconnected to the recreated database, and raced `pg_restore` into a
primary-key collision; it creates the `vector` extension *before* restoring
because otherwise the pgvector table fails quietly and the app looks healthy
until search returns nothing; it uses `--no-owner --no-acl` because the RDS
master is `rds_superuser`, not a superuser.

**This plan productizes that script rather than reimplementing it.** The
management command is a port of `restore_inner.py` with its comments intact,
plus the four things a laptop operator did by hand: taking a safety backup,
handling media, catching the schema up, and reporting progress somewhere that
survives the database being dropped.

Three decisions taken with you:
- **Upload is capped at database-only archives.** Full ~9.3 GB archives are
  restored by selecting them from the bucket; they have no upload route.
- **Scale to zero; the ALB returns 503.** No maintenance-mode middleware. The
  admin sees the site go down and come back — which is the truth.
- **A database-only safety backup is always taken first, blocking.**

**Scope: AWS ECS and local dev.** Azure prod (`seselai.sc`) has no ECS service to
scale and no backups bucket; there the card explains the archive must be restored
by hand and shows the manifest's own instructions. Saying so is better than a
button that half-works.

---

## Step 1 — Make archives inspectable (`backup.py`) — **DONE 2026-09-13**

Fixes A1/A2/A3. Without this a restore cannot be checked before it is committed
to, and this is the one part of the plan that touches shipped code.

**The tar layout does not change.** An earlier draft moved `manifest.json` to the
front so preflight could range-read it; that is unnecessary (`job.summary` already
holds it, `backup.py:331`) and it would have broken every archive already in the
90-day bucket — exactly the archives someone reaching for a restore would pick.

In `build()` / `_store()`:

- `_store()` writes a **`{key}.manifest.json` sidecar** next to the archive, for
  the case where the `BackupJob` row is gone.
- Manifest gains:
  - `dump_sha256` — computed while the dump is written.
  - `migration_heads` — `{app_label: last_applied_migration}`, read from
    `django_migrations` (what is *applied*, not what the code ships). Confirmed
    cheap: 16 apps, one query. This is the real compatibility signal;
    `app_version` is not (`VERSION` has read `0.1.0` since May, never bumped).
  - `archive_format_version: 2` so preflight can recognise a pre-change archive
    and say so instead of guessing.
- `BackupJob` gains `archive_sha256` (set after upload) and `manifest_ok`.

**Row counts are taken before the dump, not of it.** `inventory()` runs at
`backup.py:287`, `_dump_database` at `backup.py:294`; anything written in between
makes them disagree, and on a 9.3 GB archive that window is not small. Label them
"at backup start" on the confirmation page. The sha256 is the integrity check;
the counts are a sanity signal. Keep the two visibly distinct.

Archives predating this change (one exists) lack `dump_sha256` and
`migration_heads`. Preflight must degrade to an explicit "cannot verify" — never
crash, and never silently pass.

**Also fix while here** (small, same file, all named in Part 1):
- A4 — cache `inventory()` for 15 minutes; show the cache time in the card.
- A5 — correct the `BackupJob` docstring and `docs/self-hosting.md:113`
  (90-day dedicated backups bucket, not 7-day ops bucket).
- A7 — one line in the card: the download link is an AWS address and may be
  blocked on a school network.

## Step 2 — `RestoreJob` + the lock — **DONE 2026-09-13** (migration `0027`)

**Revised during implementation: `active_marker` is KEPT, and paired with an
out-of-database lock.** The review argued for dropping it because the index dies
with the table it indexes — true, but the conclusion was one step too far. The
index still guards the *dispatch* path, which is the only race a person can cause
from the settings page, and it holds right up to the drop. Dropping it would have
weakened that for nothing. What it cannot guard is the window from `DROP DATABASE`
until the row is re-inserted; `apps/dashboard/restore_lock.py` covers exactly that.
Both mechanisms, each documented with what it does and does not cover.

Fields: `status`, `created_by` (SET_NULL), timestamps, `source`
(`backup` | `upload`), `source_backup` (FK, null), `source_key`,
`include_media`, `safety_backup_key` (a **key string, not an FK** — see below),
`manifest`, `preflight`, `stage`, `progress`, `task_arn`, `error`.

**The row does not survive its own job.** Step 5 drops the database that holds
it. So: progress lives in S3 during the restore, and after `pg_restore` the task
*re-inserts* a finished `RestoreJob` row plus a `SafetyAuditLog` row into the
restored database. Without that, a successful restore leaves no trace that it
happened — the table comes back as it was before the restore began.

`safety_backup_key` is a plain string for the same reason: the `BackupJob` row
for the safety archive is dropped too. The S3 object survives; the key is how the
admin finds it, and the card surfaces it prominently after any restore.

## Step 3 — `apps/dashboard/restore.py` — **DONE 2026-09-13**

- `preflight(source) -> dict` — non-destructive, runs in the request. Streams
  the archive header only. Checks: gzip/tar readable; `manifest.json` parses;
  `scope == 'platform'`; `dump_format` matches the target engine (**a
  cross-engine restore is refused outright**); `migration_heads` vs the code's
  own — *ahead of the code* is refused (there is no migrating backwards),
  *behind* is a warning; a row-count diff, archive versus live.
- `dispatch(job)` — mirrors `job_dispatch.py:114-127`: ECS `run_task` when
  `ECS_CLUSTER`/`ECS_SUBNETS` are set, otherwise a detached subprocess, so dev
  exercises the same command.
- `status_url(job)` / `write_status(job, ...)` — the S3 status object.
- `reap_stale()` — **not** a wall-clock copy of `backup.reap_stale()`. A 6-hour
  timer calibrated for a full-media backup would mark a live restore FAILED, and
  the reaper runs in the web process, which is scaled to zero for most of the
  restore — so it cannot see the restore when it matters and misjudges it when it
  can. The liveness signal is `ecs:DescribeTasks` on the ARN stored on the row,
  with a wall-clock backstop.

The **safety archive needs its own prefix**. `OPS_PREFIX` is a module constant
(`backup.py:42`) used at `backup.py:262`; parameterise `_store()` so the
pre-restore copy lands under `backups/pre-restore/<restore-uuid>/`. Otherwise it
is indistinguishable in the console from an ordinary backup, at exactly the moment
nobody wants to be reading timestamps.

`SafetyAuditLog` has no event type for this: the closest are `DATA_EXPORT` (what
backup uses) and `DATA_DELETE`, and a restore is neither. Add `PLATFORM_RESTORE`
— `event_type` is `max_length=30` so it fits. Note `user_id` is a plain
`IntegerField`, not an FK (`safety/models.py:32`), which is what lets the
re-inserted audit row survive an author whose `auth_user` row is not in the
archive. Leave a comment saying so, or someone will "fix" it into an FK.

**Path safety.** An uploaded archive's member names are untrusted. Never
`extractall`; accept only `manifest.json`, `manifest-final.json`, the declared
dump name, and `media/<relative path with no .. and no leading />`. Anything else
aborts the restore. (`pg_restore` on an untrusted dump can run arbitrary SQL —
not an escalation, since a superadmin already has that power, but it is why this
is superadmin-only and audited.)

## Step 4 — `manage.py restore_backup --job <id>`

A port of `ops/restore_inner.py`, keeping its comments. Order matters:

0. **Take the lock, outside the database.** `ecs:ListTasks --family` for the
   restore family: abort unless this task is the only one. This is the same call
   step 3 needs anyway, and unlike a `RestoreJob` row it survives `DROP DATABASE`.
1. Write the S3 status object; record `original_desired_count` **first**, so a
   dead task can be recovered with one `aws ecs update-service`. Log that literal
   recovery command to CloudWatch at WARN — it is what someone will actually read
   at 2am — and include it in the SES alert.
2. **Safety backup, and check that it worked.** `build()` *never raises*
   (`backup.py:338-352` catches everything and returns `None`), so a blocking call
   returning is not evidence the archive exists. Do what `create_backup.py --wait`
   already does: `reap_stale()`, create the row catching `IntegrityError`,
   `build()`, `refresh_from_db()`, and **abort if `status != DONE`**.
   Before that, and blocking: `rds:CreateDBSnapshot`. The instance already has
   14-day automated backups and `deletion_protection` (`data.py:90-103`); an
   explicit snapshot is atomic, takes minutes rather than the ~30–60 a DB-only
   `pg_dump` takes, cannot half-succeed, and does not depend on the application
   being healthy. The snapshot is the hard safety net; the `build()` archive is
   the portable one.
3. **Suspend autoscaling**, then scale to 0. The service has a registered
   scalable target with `min_capacity=1` and two target-tracking policies
   (`compute.py:320-370`); setting `desiredCount=0` by hand does not deregister
   it, and `min_capacity` is a floor the next scaling activity restores.
   Zero targets is itself the state that produces one, with a 60s scale-out
   cooldown. If that fires mid-restore a web task boots, `DROP DATABASE ... WITH
   (FORCE)` cuts it off, it reconnects to the new empty database, and that is the
   2026-08-08 incident again from a different direction.
   `RegisterScalableTarget` with all three `SuspendedState` flags; **capture the
   prior suspended state and restore that**, not a hardcoded `false`.
4. Wait for the service stable *and* the cluster idle — **excluding this task's
   own ARN**, read from `ECS_CONTAINER_METADATA_URI_V4`. `restore_from_dump.sh:77-91`
   can count running tasks to zero because it runs from a laptop *before*
   `run-task`; the same loop inside the task can never reach zero, because the
   restore task is one of the tasks it is counting. Without this the feature
   stalls 15 minutes and aborts on every single run.
   Re-check immediately before the DROP: CI deploys on push to `aws_deployment`
   and runs the full `migrate_and_seed.sh` chain as a one-off task invisible to
   `desiredCount` (`deploy-aws.yml:202`). The idle wait catches a deploy already
   running; it cannot stop one that starts thirty seconds later. Detect and abort
   cleanly. Also gate the deploy workflow on the restore lock object, so a push
   during a restore fails fast with a clear message instead of corrupting it.
5. **Download the archive once** with `download_file` — multipart, parallel,
   retrying per part — then open it seekable (`'r:gz'`). Verify `dump_sha256`
   against the file.
   Not two streaming passes. I had proposed streaming with an early break, and
   measured that it works (reading only the first member of a 42 MB archive pulls
   10 KB). But a gzip stream **cannot be resumed from an offset**: one
   `ReadTimeoutError` twenty minutes into a 9.3 GB pass, after the database is
   already dropped, means starting over from zero. `restore_inner.py:67` does the
   boring `download_file` thing for exactly this reason. One download also
   collapses both passes into one open and makes member access random.
   This needs disk: set `ephemeral_storage` to 100 GiB on the task definition
   (`_task_def`, `compute.py:254-269`, sets none today — see A6). Billed per
   GB-hour of task runtime; pennies for an hour.
   Pin the `VersionId` returned by preflight's `head_object` and pass it to every
   later `get_object`/`download_file`. The bucket is versioned; without pinning,
   preflight and execution can read different bytes and the sha256 check is
   decorative.
6. `DROP DATABASE ... WITH (FORCE)`; `CREATE DATABASE`;
   `CREATE EXTENSION vector`; `pg_restore --no-owner --no-acl -j 4`.

   On SQLite (dev, and the only engine the tests can exercise end to end) this
   branch is `sqlite3`'s own backup API run in reverse — archived file as
   source, live file as target — mirroring `_dump_database` (`backup.py:171-186`).
   **Verified**: this restores in place through SQLite's own locking even with a
   live connection open, so it does not need the dev server stopped and it does
   not "replace a file" — which matters because `NAME` can be a
   `file:…?mode=memory&cache=shared` URI (`backup.py:180`), where replacing a file
   is meaningless. Same 30s busy timeout, same reason. Refuse outright on any
   engine that is neither postgres nor sqlite rather than half-working.
7. **`sh ops/migrate_and_seed.sh`** — not bare `migrate`. The archive may predate
   the deployed code, and the seed chain rebuilds derived data (help index,
   gamification, progress) that the restored rows may not match. Reusing the
   script keeps it in step with CI, and running it here — single, serialised,
   service at zero — is the safe place for `build_help_index`, which is what
   collided on 2026-08-08.
8. **Pass 2** (only when the archive has media) — stream the whole archive and
   upload each `media/*` member into the media bucket. Orphans are left in
   place, not deleted: an extra file is harmless, a wrongly deleted one is not.

   Use `upload_fileobj` (it needs only `.read()`) with
   `ExtraArgs={'ServerSideEncryption': 'AES256'}`, matching `_store`
   (`backup.py:264`). Seekable-mode members are ordinary file objects, so the
   `seekable()` trap below does not apply — it is recorded only because it bites
   immediately if anyone reverts step 5 to streaming: `tar.extractfile()` in
   `r|gz` mode returns a stream whose `seekable()` **raises `AttributeError`**
   (`_Stream` has no such attribute) and `s3transfer` calls it. Verified.

   Member allowlist, on untrusted uploads: `member.isreg()` only — reject dirs,
   symlinks, hardlinks, devices — reject absolute paths, reject `..` *after*
   normalisation, and cap `member.size` against the manifest's `media.bytes` so
   an uploaded gzip bomb cannot fill the disk.
9. **Normalise rows the archive resurrected.** Every archive contains its own
   `BackupJob` row with `status='running'`, because `build()` saves RUNNING
   (`backup.py:274-277`) *before* dumping (`backup.py:294`). Restore an archive
   taken less than `STALE_AFTER` (6h) ago and that resurrected row holds
   `dashboard_one_active_backup` shut — the settings page says "a backup is
   already running" and a progress bar sits at 25% for six hours. Sweep
   `BackupJob`/`RestoreJob` rows in pending/running to failed with
   `error='interrupted by a platform restore'`.
10. Insert the finished `RestoreJob` + `SafetyAuditLog` rows, **and re-insert a
   `BackupJob(status=done, storage_key=<safety key>)`** so the safety copy
   reappears in the list and `backup_download` just works. Don't make the admin
   reconstruct a presigned URL by hand on the worst day.
11. **Scale back up only on success.** Not in a blanket `finally`. The health
   check is `/health/` with `matcher="200"` (`edge.py:72-82`) and
   `views_health.py:44` returns **503** when the database is unreachable — so
   scaling up after a failed `pg_restore` gives an infinite boot/kill loop.
   The genuinely dangerous case is partial: `migrate` succeeded, `pg_restore`
   did not, so there is a valid **empty** schema, gunicorn reports healthy,
   and students and staff start writing rows into an empty platform. Re-running
   the restore then destroys those too. A clean 503 is recoverable; a healthy
   empty platform is not.
   On failure: stay at zero, write `failed` plus the safety-backup key and the
   RDS snapshot id to the status object, alert, stop. Gate success on row-count
   floors sourced from the manifest (`restore_inner.py:97-113` has the shape,
   but source the floors from the archive rather than the 2026-07-30 drill
   constants).
   `finally` un-suspends autoscaling only.

**`finally` is not a guarantee.** It does not run on SIGKILL, OOM, `StopTask`, or
a Fargate host reclaim — and ECS sends SIGTERM then SIGKILL after `stopTimeout`
(default 30s), which `pg_restore -j 4` will not beat. This turns a script a human
was babysitting into an unattended one, and removes the human who *was* the
recovery mechanism. So: `stopTimeout: 120` on the container, the recovery command
in the log and the alert (step 1), and an EventBridge rule on
`ECS Task State Change` for the restore family with `exitCode != 0` → SES.

**`pg_restore`'s exit code is not a verdict.** It exits non-zero when it *ignored*
errors, not only when it failed, and `--no-owner --no-acl` on RDS produces
ignorable noise. `restore_inner.py:37` uses `check=True`, which would abort a
restore that actually worked — after the database was dropped. Capture stderr,
parse the ignored-error count, and decide on that plus the row-count floors.

## Step 5 — Views, URLs, template

`views.py`, after the backup block (~9890-10010), all `@superadmin_required`:

- `restore_preflight` (POST) → builds the `RestoreJob` in `pending`, runs
  preflight, renders a **dedicated confirmation page**.
- `restore_upload_sign` (POST) — issues a **presigned POST** scoped to
  `restores/<uuid>.tar.gz` with a `content-length-range` condition; the browser
  uploads straight to S3. See the reversed decision below.
- `restore_start` (POST) — requires the typed confirmation; dispatches; audits.
- `restore_status` (GET) — reads the S3 status object, not the database, so it
  still answers while the database is gone. Modelled on `backup_status`
  (`views.py:9959`).

Template: a new card in `settings.html` immediately after the backup card
(before the `{% if is_superadmin %}` block closes at :746), using the
destructive-button convention from `course_detail.html:213`.

**Say that the admin will be signed out — and may need an old password.**
`django_session` is restored with everything else, so the session that started the
restore ceases to exist the moment `pg_restore` finishes; so does every student's.
Worse, `auth_user` comes from the archive too: if the admin's password changed
after the archive was taken, the login that works is the *archive-era* one. Say
both on the confirmation page, or the first thing they meet after a successful
restore looks like a broken login.

**The status object survives the restore; the page that would show it does not.**
Putting progress in S3 is right, but the view serving it is gunicorn — at
`desiredCount=0` — behind a login that needs the database. During the destructive
window the status object is reachable only by AWS console or CLI. So: issue a
**6-hour presigned GET for the status object on the confirmation page, before
dispatch**, so the admin leaves with a URL that works while the site is down, and
email transitions via SES (the task role already has `ses:SendEmail`,
`compute.py:171-176`).

**The confirmation is a page with a typed platform name, not `confirm()`.** The
codebase uses `onsubmit="return confirm(...)"` everywhere, and for a whole-platform
restore that is not enough — the page has to *show* the row-count diff, the
warnings, the expected downtime, and where the safety backup will be, because
that is the information that stops the wrong archive being restored.

### Reversed decision: upload goes direct to S3 after all

You chose "cap upload at DB-only archives, through the request." **That does not
work as specified, and I would not have offered it had I checked three things
first.** Flagging rather than quietly switching, because it reopens the option
you deliberately declined:

- `UploadedFile.chunks()` is a **generator** — no `.read()` — so the
  "stream `chunks()` into `upload_fileobj`" mechanism raises `AttributeError`.
  Verified.
- There is no streaming regardless: `TemporaryFileUploadHandler` spills the whole
  body to the web task's `/tmp` before the view function runs. Two concurrent
  uploads is 1 GB of a 20 GiB ephemeral disk shared with everything else.
- gunicorn and the ALB both time out at 120s (`compute.py:279`, `edge.py:58`),
  and the budget has to cover the browser→server *and* server→S3 legs. 500 MB
  needs ~33 Mbps sustained on both.

So: **presigned POST** scoped to one key, with a `content-length-range` condition
that S3 itself enforces — which also settles the cap question, since it is no
longer a check in middleware anyone has to trust. The browser uploads directly;
preflight then reads an object that is already in the bucket. This is the
"presigned multipart" option from the original question, in its simplest form,
and it is the only one of the three that actually functions. The trade you were
avoiding — pointing an admin browser at `*.amazonaws.com`, against the policy at
`settings.py:449-460` — is real, but `backup_download` already does it (A7).

Still worth setting `DATA_UPLOAD_MAX_MEMORY_SIZE`/`FILE_UPLOAD_MAX_MEMORY_SIZE`
explicitly in `settings.py`: both are unset today (Django's 2.5 MB defaults), and
`edge.py:223` justifies leaving WAF body rules on Count *on the assumption these
are configured*. They are not.

**Uploaded payloads must expire, and currently cannot be deleted.** The task role
has no `s3:DeleteObject` on the backups bucket, deliberately (`compute.py:164-170`),
so a 500 MB blob of student records would sit under the bucket's 90-day rule for
three months. `restore_from_dump.sh:62` does `aws s3 rm` in its trap; that line
does not port. Add a `restores/` prefix lifecycle rule at 7 days, and a
`DeleteObject` narrowed to `arn:…:backups-…/restores/*` only.

### Gate on `is_superuser`, not `is_staff`

`superadmin_required` (`views.py:236`) checks `is_staff` — but `views.py:4892`
creates users with `is_staff=True` and `views.py:4907` is a promote/demote
toggle in the UI. For backup the accepted risk is exfiltration; for restore it is
"any promoted account can revert the platform to an arbitrary state, or take it
down for an hour." Those should not share a decorator. Require `is_superuser` on
the restore endpoints.

The typed platform name defends against a misclick, not against CSRF on a
logged-in superadmin. Make the confirmation token **server-issued and
single-use**: stash the archive key plus a nonce in the session at preflight and
require it back on the POST, so the destructive request cannot be forged from the
confirmation page's visible contents.

## Step 6 — Infrastructure (`infra/aws/`) — confirm before changing

Per CLAUDE.md, `infra/__main__.py` and friends are load-bearing; this is the
proposed shape, to be agreed before editing.

**Reuse `{prefix}-migrate` with a command override** — as `restore_from_dump.sh:14-16`
already argues, and reversing what I proposed first. A new `{prefix}-restore`
family would never receive a new image: CI re-registers exactly `web_family` and
`migrate_family` (`deploy-aws.yml:183-184`) and nothing else, so a Pulumi-created
family stays pinned to the last hand-run `pulumi up` (A10 — this is already
happening to `{prefix}-material`). That matters enormously here, because the
restore runs `migrate` **from the restore image**: a stale image migrates the
database to a stale head, and the current web tasks then come up against a schema
missing columns they query. The failure mode would be "the restore succeeded and
then the site broke." `{prefix}-migrate` also already carries `DATABASE_URL`, the
right subnets, and the SG that RDS accepts, and its log stream path is the one
`restore_from_dump.sh:140` already knows.

The consequence is that `ecs:UpdateService` and `application-autoscaling:*` land
on the **shared** task role, so a web container gets them too. Accepted, narrowly:

- `ecs:UpdateService`/`DescribeServices`/`ListTasks`/`DescribeTasks` scoped to
  this one service ARN; `application-autoscaling:RegisterScalableTarget`/
  `DescribeScalableTargets` (which need `Resource: "*"`) conditioned on the
  scalable dimension where supported.
- A web RCE gaining "can scale the service to zero" is a denial of service, not a
  breach — and the same actor already has media-bucket write and `ecs:RunTask` on
  `{prefix}-material` (`compute.py:178-195`).
- Note for whenever a separate role *is* worth it: `ecs:RunTask` + `iam:PassRole`
  lets the holder supply `containerOverrides`, and there is no IAM condition key
  for those — so role separation alone buys nothing. The mitigation that works is
  that overrides **cannot** replace `entryPoint`: pin
  `entryPoint: ["python","manage.py","restore_backup"]` so only arguments can be
  supplied, then validate those strictly. Verify first whether an `environment`
  override can shadow a same-named `secrets` entry; if it can, assert
  `DATABASE_URL`'s host matches the expected RDS endpoint.
- Also add: `rds:CreateDBSnapshot`/`DescribeDBSnapshots`, `s3:DeleteObject` on
  `restores/*` only, and `ephemeral_storage` at 100 GiB on `{prefix}-migrate`.
- `AWS_BACKUP_BUCKET` and the cluster/subnet vars already reach the task
  (`__main__.py:152`). `_container` (`compute.py:225`) gains `entryPoint` and
  `stopTimeout` parameters.

**Enable versioning on the media bucket** (`storage.py:30-71`) before pass 2 ever
writes to it. Only the backups bucket is versioned today (`storage.py:182`), and
the comment there — "a backup that can be silently replaced by a later, broken
one is not a backup" — applies verbatim the moment a restore starts overwriting
media keys. Orphans are left in place, but same-key objects *are* overwritten and
the DB-only safety backup covers none of them. This is the cheap fix and it
protects the app's ordinary writes too.

---

## Verification

Nothing here ships without a real restore having been run and observed.

1. **Unit/integration** — `apps/dashboard/tests/test_platform_restore.py`,
   mirroring `test_platform_backup.py`'s stance of opening the archive rather
   than trusting a filename:
   - round trip on SQLite: seed → backup → mutate → restore → the original rows
     are back and the mutation is gone;
   - preflight refuses a cross-engine archive, an archive whose migration heads
     are ahead of the code, and a tampered `dump_sha256`;
   - a member named `../../etc/passwd` or `/abs/path` aborts the restore;
   - two concurrent restores are refused by the **out-of-database** lock, tested
     the way `test_platform_backup.py:247` tests the backup constraint;
   - a resurrected `BackupJob(status='running')` from inside the archive is swept
     to failed, so the next backup is not blocked for six hours;
   - a failed `pg_restore` leaves `desiredCount` at zero — never scaled back into
     a healthy empty platform;
   - `pg_restore` exiting non-zero on *ignored* errors is not treated as failure;
   - presigned POST refuses an oversized object and an archive containing media.
2. **Backup-side regression** — the existing suite must pass unchanged, plus:
   the sidecar manifest is written beside the archive; preflight works from
   `job.summary` alone with zero S3 reads; and an archive lacking `dump_sha256` /
   `migration_heads` degrades to "cannot verify" rather than crashing or passing.
3. **Local end-to-end** — `runserver` on SQLite, drive the settings page with
   `mcp__chrome-devtools__*`: take a backup, change something visible, upload the
   archive back through the form, walk the confirmation page, run the restore
   via the subprocess backend, confirm the change is gone. **Screenshots at each
   step** (card, confirmation page, in-progress, result) per
   `auto-memory/feedback_visual_check_required.md`.
4. **Staging on AWS** — the only place scale-to-zero, autoscaling suspension and
   the S3 status object are real. Restore the existing 24 MB database-only
   archive; watch the ALB 503 and the service return; confirm the safety-backup
   key is surfaced afterward. Then a full archive with media.
5. **Deliberate failure drill** — `StopTask` the restore mid-run. Confirm
   autoscaling is un-suspended, the recovery command is in the log and the alert,
   and the EventBridge → SES notification fires. This is the drill that matters:
   `finally` does not run on SIGKILL, so the recoverability being tested is the
   out-of-band kind, not the Python kind.

Only after 3 and 4 pass: commit, `Refs:` the memory files, push.

## Order of work

Step 1 first and **shipped on its own** — archives taken from now on carry a
checksum, migration heads and a sidecar manifest, which is the thing with a
deadline (`auto-memory/feedback_ship_structural_changes_before_data_gen.md`).
Then 2–5: restorable on SQLite, fully verifiable locally, no infra change. Then 6
with the staging drill. Audit fixes A4/A5/A7 ride along with Step 1; A6
(`ephemeral_storage`) and media-bucket versioning come with Step 6; A10
(`{prefix}-material` frozen by CI) is separate work, not part of this.

## Open questions

1. **Azure prod (`seselai.sc`)?** Everything above targets the AWS stack, where
   the backup bucket path and `job_dispatch`'s ECS backend already live. Azure has
   a Container Apps Job backend (`job_dispatch.py:109-116`) but no equivalent to
   scaling an ECS service to zero, so a correct Azure restore is separate work.
   Planned as: the card on Azure lists the archives and shows the manifest's own
   instructions, and offers no button.
2. ~~The presigned-POST reversal~~ — **decided 2026-09-12: presigned POST.**
   Confirmed by the user after the three blockers were shown.
3. **The bare 503 window.** 30–90 minutes of no explanation. A listener rule with
   a `fixed-response` action would give a real maintenance page, at the cost of
   `elasticloadbalancing:ModifyRule` on the role and one more thing that can fail
   to be undone. Announcing the window and accepting the 503 is defensible —
   but it should be a decision, not a discovery.
