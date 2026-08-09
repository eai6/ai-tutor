# AWS fixes + media migration + dashboard date picker — Plan (2026-08-08)

## Context

The Azure → AWS data migration is done: `https://migration.edwardamoah.com`
serves real production data behind HTTPS and a blocking WAF. Three follow-ups
came out of using it:

1. A logo upload at `/dashboard/settings/` returned 403.
2. Media/figures were never copied, so every image 404s.
3. The dashboard sessions chart is stuck on a hardcoded 14-day window.

Two audit findings reframe the work, and one corrects something I said earlier.

**I was wrong that I had fixed `CSRF_TRUSTED_ORIGINS` in code.** The commit
message for `fcef83a` claims it was set to the domain; the diff never touched it.
`infra/aws/__main__.py:144` still reads `"CSRF_TRUSTED_ORIGINS": f"http://{a[4]}"`
where `a[4]` is the raw ALB DNS name. So `pulumi up` alone would not fix it — the
code has to change first.

**The 403 you saw is probably NOT that bug.** Evidence: an unauthenticated
multipart POST to `/dashboard/settings/` returns Django's CSRF page — left
aligned, `<h1>Forbidden (403)</h1>`, body text "CSRF verification failed". Your
screenshot is a *centered, bare* "403 Forbidden" with no body text, which is the
AWS WAF default block page. Separately, `SECURE_PROXY_SSL_HEADER` is set whenever
`DEBUG=False` (`config/settings.py:457-460`) — it is **not** gated on
`HTTPS_EDGE`, which I had assumed. So `request.is_secure()` is true, Django's
`_origin_verified()` compares the Origin against `https://<request host>` and
matches, and CSRF passes *despite* the wrong env var. That makes the env var a
latent bug that bites the moment a request arrives without an Origin header.

So: fix the env var because it is wrong, but do not expect it to fix the upload.
Diagnose the upload separately.

## Part 0 — Diagnose the 403 definitively (do first, ~15 min)

Do not fix anything until this is settled. Two candidates, cheaply separable.

1. **Reproduce at real size.** My probe used a 2 KB payload and passed the WAF.
   `POST` a realistic logo (200 KB – 2 MB PNG/JPG) to `/dashboard/settings/`.
   If the small one yields Django's CSRF page and the large one yields the
   centered bare page, it is the WAF and the trigger is size/content.
2. **Read the WAF's own record.** `aws wafv2 get-sampled-requests` must be called
   **per rule** (`--rule-metric-name ALL` returns nothing, confirmed), and only
   covers a 3-hour window. Enumerate rules from
   `aws wafv2 get-web-acl --name aitutor-dev-waf --scope REGIONAL` and query each.
   If the sample window has expired, enable WAF logging to CloudWatch first, then
   reproduce.
3. **Cross-check Django.** If the request reached the app there will be a line in
   `/ecs/aitutor-dev` `web/*`; silence means it was blocked upstream.

**If it is the WAF** (most likely): the fix is a scoped exclusion, not disabling
the firewall. `AWSManagedRulesCommonRuleSet` contains `SizeRestrictions_BODY`
(8 KB body inspection limit) and the `CrossSiteScripting_BODY` /
`GenericRFI_BODY` rules, all of which routinely block legitimate multipart image
uploads because binary bytes look like attack payloads. Add a scope-down
statement excluding the specific upload paths, or set those individual rules to
Count, in `infra/aws/components/edge.py` (the managed rule groups are at
`edge.py:105-143`). Keep the rest of the ACL blocking — do not revert to Count
wholesale.

**If it is CSRF**, Part 1 fixes it.

## Part 1 — `CSRF_TRUSTED_ORIGINS`, and the deploy gap behind it

**The code fix**, `infra/aws/__main__.py:144`:

```python
"CSRF_TRUSTED_ORIGINS": f"https://{domain_name}" if domain_name else f"http://{a[4]}",
```

Mirror the shape already used one line above at `:142` for `HTTPS_EDGE`, which is
correctly domain-aware. Keep the ALB fallback so a stack with no domain still works.

**Then the part that is easy to get wrong.** Two mechanisms conspire to keep a
corrected value from reaching the running service:

- The ECS service is created with
  `ignore_changes=["taskDefinition", "desiredCount"]` (`compute.py:291`), so
  `pulumi up` registers a new task-definition revision but never points the
  service at it.
- The deploy workflow reads the **live** task definition and overwrites only
  `.image` (`deploy-aws.yml:158-161`), so env comes from whatever is deployed.

Sequence that actually lands it:

1. `cd infra/aws && pulumi preview` — read the diff before applying.
2. `pulumi up` — registers new `aitutor-dev-web` / `-migrate` / `-material`
   revisions carrying the corrected env.
3. Point the service at it:
   `aws ecs update-service --cluster aitutor-dev-cluster --service aitutor-dev-service --task-definition aitutor-dev-web --force-new-deployment`
   (`describe-task-definition` by family resolves to the newest revision, so a
   subsequent CI deploy also picks it up correctly.)
4. Confirm the running task's env, not the definition's:
   `aws ecs describe-tasks ... ` → check `CSRF_TRUSTED_ORIGINS` is the https domain.

**Two hazards to check during `pulumi preview`:**

- `Pulumi.dev.yaml` has an **uncommitted** change to the `django-secret-key`
  secure value. Applying it rotates `SECRET_KEY` in Secrets Manager, which
  invalidates every active session and every Fernet-encrypted value. Confirm this
  is the intended Azure-matched value before `up`, or stash it.
- `db-password` is now `config.require_secret` rather than a generated
  `RandomPassword` (`data.py:70`, with the incident write-up at `:50-69`). Preview
  must show **no change** to the RDS instance. Anything touching `password` there
  means the config value has drifted from the live master password.

**Also worth doing while here:** nothing reconciles Pulumi's `task_environment`
into the running service, so any of those vars can drift silently. Diff the live
task env against `__main__.py:125-145` and note anything else stale.

## Part 2 — Media: Azure → S3

**Good news from the audit: no database rewriting is needed.** Both
`S3MediaStorage` (`apps/media_library/s3_media.py:54-69`) and `AzureMediaStorage`
(`blob_media.py:58-73`) store the raw `upload_to` path as the object key with no
prefix, and every DB value is a relative path — `MediaAsset.file`
(`media_library/models.py:37`), `PlatformConfig.logo` (`accounts/models.py:399`),
exit-ticket `.image` (`tutoring/models.py:812`), feedback `.screenshot`
(`dashboard/models.py:339`), and the `/media/...` strings baked into
`LessonStep.media` JSON. None embed a host or an Azure marker. Copy bytes under
matching keys and existing rows resolve.

**Where the bytes actually are — this is the wrinkle.** Azure prod runs
`AzureMediaStorage` (blob is enabled: `Pulumi.pixel.yaml:37` `enable-blob: "true"`),
**but the bulk File Share → Blob copy was stopped mid-flight on 2026-06-09** and
never resumed (archived at `memory/archives/august_2026/blob_media_hosting_plan.md`).
Only `help/intro.mp4` and uploads since that date are in Blob. Everything older —
`curriculum_uploads, feedback_screenshots, institution_logos, material_uploads,
media, platform_logos, help` — exists only on the SMB File Share, reached through
`blob_media.py`'s `_filesystem_fallback` (`:89-97`).

So **both sources must be copied**, File Share first then Blob overlaid (Blob
wins on conflict, being newer). A Blob-only copy would silently miss most images —
and would look like success.

Approach:

1. **Inventory before moving.** Count files and bytes per top-level directory on
   both the File Share and the Blob container. This is the number to verify
   against afterwards, and it decides whether this is a 10-minute or multi-hour job.
2. **Copy.** `azcopy` handles both Azure sources; it does not write to S3, and
   `aws s3 sync` does not read Azure — so either stage through a local/EC2 disk
   (`azcopy` down, `aws s3 sync` up), or use `rclone` which speaks both and can go
   direct. Recommend **rclone** for a one-shot copy of this size; keep the same
   relative paths, no re-prefixing.
3. **Do not put media in the ops bucket.** Target is the existing media bucket
   (`storage.py`), which `AWS_MEDIA_BUCKET` already points at
   (`__main__.py:127`) — the app is wired and the bucket is simply empty.
4. **Verify by key, not by count alone** — spot-check that
   `platform_logos/<file>` and a `media/<institution-slug>/<file>` resolve through
   `/media/<path>` on the AWS site.

**Security note, unchanged by this work:** `/media/<path>` has **no auth gate** on
any backend (`config/urls.py:68-84`). Copying student-uploaded content into S3
preserves that exposure. Out of scope to fix here, but it should be a named
follow-up rather than a surprise.

## Part 3 — Custom date range on the sessions chart

Current state: `dashboard_home` (`apps/dashboard/views.py:242-392`), chart block
at `:364-375`. It builds **15** points (`range(14, -1, -1)` is inclusive) with
**one COUNT query per day**, filtering `TutorSession.started_at__date`. The
template `templates/dashboard/home.html:42-64` + `:114-203` draws the bars by
hand in plain JS — no Chart.js — from `activity_data` injected as JSON at `:120`.

Design:

- **Scope the picker to the chart only.** The other five tiles use three
  different hardcoded windows (7-day active, 30-day sessions, all-time mastery —
  `views.py:253-339`) and none share the chart's window. Making the picker
  page-wide is a much larger change; keep it to the chart and say so in the UI.
- **Query params**: `?start=YYYY-MM-DD&end=YYYY-MM-DD`, plus preset links.
  **Reuse the existing parser** at `apps/benchmark/views.py:486-506` (`_parse_dt`)
  — it already does `datetime.fromisoformat` in a try/except returning `None` on
  bad input, and snaps date-only strings to start/end of day. Do **not** copy the
  pattern at `views.py:2093` (`int(request.GET.get('days', 30))`), which 500s on
  `?days=abc`.
- **Defaults and guards**: no params → today-14d..today (unchanged behaviour).
  Invalid → fall back to the default silently. Clamp the span (suggest 366 days)
  so `?start=1970-01-01` cannot generate 20,000 buckets.
- **Fix the N+1 while here.** Replace the per-day loop with a single grouped
  query — `.annotate(day=TruncDate('started_at')).values('day').annotate(n=Count('id'))`
  — then fill missing days with zero in Python. At 14 days it is 15 queries; over
  a year it would be 366.
- **Bucketing**: keep daily for ranges up to ~90 days; beyond that the hand-rolled
  bars get unreadably thin, so switch to weekly buckets and label accordingly.
- **UI**: match the existing filter styling at
  `templates/dashboard/reports/overview.html:8-16` (GET-link buttons, active state
  via `btn-primary`), adding two `<input type="date">` fields and an Apply button.
  The date-input markup already exists at `templates/benchmark/list.html:157-163`
  to copy from. The x-axis label logic at `home.html:189-198` parses
  `d.date.split(' ')[1]` and will need adjusting for longer ranges.

## Out of scope

- Making AWS authoritative — DNS cutover, retiring Azure. Still a parallel
  evaluation environment.
- Adding an auth gate to `/media/<path>` (pre-existing on both clouds).
- Backfilling the stalled Azure File Share → Blob migration on the Azure side.
  We copy from both sources to S3; Azure's own inconsistency is left as-is.
- Page-wide date filtering for the other dashboard tiles.

## Verification

- **403**: reproduce the upload with a real logo through the browser at
  `https://migration.edwardamoah.com/dashboard/settings/` and confirm a success
  message plus the logo rendering. Screenshot it — per `CLAUDE.md` a UI change is
  not verified by DOM inspection alone.
- **CSRF**: `aws ecs describe-tasks` shows `CSRF_TRUSTED_ORIGINS=https://migration.edwardamoah.com`
  on the *running* task; and a POST from the domain succeeds.
- **Media**: per-directory file counts and total bytes match the pre-copy
  inventory; `/media/platform_logos/<file>` returns 200 (currently 404); a lesson
  with figures renders its images.
- **Date picker**: `?start=2026-07-01&end=2026-07-31` shows July with correct
  daily counts cross-checked against a direct DB query; no params reproduces
  today's 14-day chart exactly; `?start=garbage` falls back instead of 500ing.
  Add the first `apps/dashboard/tests/` view test for this — the package exists
  (`test_job_dispatch.py`) but nothing covers `dashboard_home`. Model it on
  `apps/benchmark/tests/test_sampling_filter.py`.
- **Azure untouched** throughout — no Azure deploy, no Azure config change.

## Suggested order

Part 0 → Part 1 (both unblock you and are small) → Part 3 (self-contained,
testable locally) → Part 2 (largest, and the only one whose duration is unknown
until the inventory is done).

---

## Part 2 — Media copy: DONE 2026-08-08

**10,521 objects / 9.97 GB** in `aitutor-dev-media-968025288404`.
`rclone check --one-way --size-only` reports **0 differences**, per-prefix counts
match Azure exactly, and sampled keys return 200 through
`https://migration.edwardamoah.com/media/<key>`.

### The plan's premise was stale

It said the File Share was authoritative because the 2026-06-09 Blob migration
was stopped mid-flight. Reality: **Blob holds everything** — 10,520 files across
`media/` (10,408), `material_uploads/` (79), `feedback_screenshots/` (19),
`curriculum_uploads/` (10), `institution_logos/` (2), `platform_logos/`, `help/`.
The File Share had only two directories left, and only ONE file that Blob did
not have: `platform_logos/Coat_of_arms_of_Seychelles.svg` — which was already in
S3 because it had been re-uploaded through the fixed settings page.

Lesson: inventory both ends before trusting an archived plan's account of what
lives where.

### Tooling

**AzCopy cannot do this** — it copies S3 → Azure, not Azure → S3. Used `rclone`
(installed via brew) streaming Blob → S3 directly, no local staging.

Two failures worth remembering:

1. **`InvalidStorageClass`.** rclone passes the Azure blob's access tier through
   as an S3 storage class and S3 rejects it. Fixed with
   `--s3-storage-class STANDARD`.
2. **`ExpiredToken`, ~1,300 of them.** `aws login` issues short-lived
   credentials; rclone captures them once at startup. A full pass takes longer
   than the token lives, so it died at ~36% and a refresh-between-passes loop
   never got to refresh because pass 1 never ended. Fixed with
   **`--max-duration 8m`** so each pass ends inside the credential lifetime and
   the loop re-exports fresh ones. `rclone copy` skips what is already there, so
   passes are cheap and it converges. Script: `/tmp/media_copy_loop.sh`.

### Still true

`/media/<path>` has **no auth gate** on any backend (`config/urls.py`). Student
feedback screenshots and curriculum uploads are now readable by anyone with the
URL on AWS, exactly as on Azure. Unchanged by this work, but it is now real
student content in a second place.
