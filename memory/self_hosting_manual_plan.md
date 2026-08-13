# Self-hosting manual — two paths (own server, or their own AWS)

## Context

A ministry or partner should be able to run AI Tutor themselves, with student
data under their own control. Two paths are wanted: **their own server via
Docker**, and **their own AWS account via the existing Pulumi program**.

`docs/aws-self-hosting.md` already covers the AWS path and matches the code.
**The Docker path does not exist** — there is no compose file, no `.env`
template, and no on-prem backup story. A manual describing files that aren't
in the repo can't be followed, so this ships the artefacts alongside the words.

Decisions taken (2026-08-12): the on-prem path calls **cloud LLM APIs** (no
local-model sidecar); the deliverable is **manual + working artefacts**; and the
**insecure Django defaults get fixed in code**.

## What the audit found

Good news — the app is closer to self-hostable than expected:

- `USE_S3_MEDIA = bool(AWS_MEDIA_BUCKET)` (`config/settings.py:315`) — media
  falls back to local disk automatically. No S3 required.
- `DATABASES` is `dj_database_url.config(...)` (`:133`) — one `DATABASE_URL`.
- `apps/curriculum/migrations/0029_curriculumchunk.py:19` runs
  `CREATE EXTENSION IF NOT EXISTS vector` itself. Compose only needs a Postgres
  image that *has* pgvector available.
- `Dockerfile:60` runs `collectstatic` at build and WhiteNoise serves static —
  no nginx needed for assets.
- The `Dockerfile` CMD already chains migrate + six seed commands + gunicorn, so
  a compose deployment needs **no separate migrate step** (unlike ECS, which
  overrides `command` — see `ops/migrate_and_seed.sh`).
- `infra/aws/__main__.py` has no hardcoded account id, region or domain; all are
  config keys, and the secret keys the AWS doc tells people to set are real
  (`infra/aws/components/data.py:131-137`).

Four traps a self-hoster will hit, all already known to this codebase:

1. **Plain HTTP silently breaks login.** `HTTPS_EDGE` defaults true, which sets
   `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` when `DEBUG=False`
   (`config/settings.py:464-472`). The browser never returns those over HTTP →
   403 with no error anywhere. `config/settings_kiosk.py` and the comment block
   at `settings.py:455-464` exist because of exactly this.
2. **Gunicorn timeout.** `Dockerfile` uses `--timeout 120`; a tutoring turn
   takes 20-90 s, longer on modest hardware. `infra/systemd/ai-tutor.service`
   uses 300 and calls the default "load-bearing".
3. **`DEBUG` defaults to `True`** (`:22`) and with it `ALLOWED_HOSTS = ['*']`
   (`:27`); `SECRET_KEY` defaults to `'dev-secret-key-change-in-production'`
   (`:20`). One missed env var = tracebacks and a known signing key in public.
4. **The image is ~4.7 GB** (CPU torch + faster-whisper tiny + a Piper voice,
   `Dockerfile:6-18`) and must be `linux/amd64`.

## Deliverables

### A. Fix the insecure defaults — `config/settings.py`

CLAUDE.md flags this file as load-bearing, so the change is deliberately small
and fail-loud rather than fail-open:

- `DEBUG` defaults to **`False`** (was `True`). Local dev sets `DEBUG=True` in
  `.env`, which already exists.
- When `DEBUG` is False and `SECRET_KEY` is still the dev default, **raise
  `ImproperlyConfigured` at import**. A server that refuses to boot is far
  better than one serving with a public signing key.
- Leave `ALLOWED_HOSTS` alone — it already tightens automatically once `DEBUG`
  is False.

Risk check before merging: confirm the Azure and AWS task definitions set
`DEBUG` and `SECRET_KEY` explicitly (both read them from Secrets Manager /
Container Apps env), so flipping the default cannot change their behaviour.

### B. On-prem artefacts (new)

| File | Purpose |
|---|---|
| `deploy/compose/docker-compose.yml` | `app` + `db` (`pgvector/pgvector:pg16`) + `caddy`. Named volumes for `pgdata`, `media`. |
| `deploy/compose/.env.example` | Every variable, each marked **required** or optional, with the traps annotated inline. |
| `deploy/compose/Caddyfile` | Automatic HTTPS from Let's Encrypt; a commented plain-HTTP block for an air-gapped LAN, paired with `HTTPS_EDGE=false`. |
| `deploy/compose/backup.sh` | `pg_dump` + media tarball to a dated file; `restore.sh` alongside it. |

Notes that shape the compose file:

- `app` uses the repo `Dockerfile` unchanged. Its CMD already migrates and
  seeds, so `depends_on: db: condition: service_healthy` is all the ordering
  needed.
- Override gunicorn `--timeout` to **300** via the compose `command`, matching
  the systemd unit rather than the Dockerfile's 120.
- Set `HTTPS_EDGE`, `DEBUG=False`, `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`
  explicitly, so no deployment depends on a default.
- No S3, no Secrets Manager: media on a volume, secrets in `.env` (0600).

### C. The manual — `docs/self-hosting.md`

One document, two paths. Absorbs `docs/aws-self-hosting.md` (leave a stub
pointing at the new file, or `git mv` it).

**Keep Edward's editing style.** He cut the measurement provenance, the
deliberate-omissions list and the cost methodology from the AWS doc. Do not
reinstate them. Operational prose, tables over paragraphs.

Structure:

1. **Choosing a path** — a short table: own server vs AWS, on cost, effort,
   data residency and who operates it.
2. **What runs** — the architecture diagram already in the AWS doc, plus a
   two-box equivalent for the single-server case.
3. **Path A — your own server (Docker).** Requirements (4 vCPU / 8 GB / 60 GB,
   amd64, a domain for HTTPS); clone, fill `.env`, `docker compose up -d`;
   first-run (create the superuser, upload curriculum, take one tutoring turn);
   the four traps above, stated as symptoms not causes ("login returns 403 with
   no error" → `HTTPS_EDGE=false`).
4. **Path B — your own AWS account.** The existing sections 3-4, corrected: the
   variable-cost table currently ends mid-sentence ("$600 for 4 schools").
5. **Operating it** — backup and restore for both paths, upgrading (pull, `up
   -d`, migrations run on boot), where the logs are, and the data-residency
   note that already exists.
6. **What leaves your network** — the honest list: LLM API calls carry student
   messages to the provider. Already in the AWS doc; it belongs to both paths.

### D. Security hardening + OWASP ZAP penetration testing

Two audiences: we run a scan against our own deployment, and the manual teaches
a ministry to scan theirs.

**D1 — close the known gaps before scanning.** Running ZAP first produces a
report dominated by findings we already know about, which buries anything real.
`manage.py check --deploy` currently reports exactly two security warnings
(the rest are unrelated drf-spectacular noise):

- `security.W004` — `SECURE_HSTS_SECONDS` unset.
- `security.W008` — `SECURE_SSL_REDIRECT` not True.

Both are currently handled at the edge (ALB/Caddy redirect), which is why they
were never set. Set them in Django too, **gated on `HTTPS_EDGE`** so the
plain-HTTP kiosk and desktop builds are unaffected — setting them
unconditionally would break exactly the deployments `settings_kiosk.py` exists
for. `SECURE_CONTENT_TYPE_NOSNIFF` and `X_FRAME_OPTIONS` need no change:
`django.middleware.security.SecurityMiddleware` is already in `MIDDLEWARE`
(`config/settings.py:65`) and Django's defaults are safe.

There is **no Content-Security-Policy**. Add one, but ship it
**report-only first** — a blocking CSP will break inline scripts and the
chart/annotation UI, and discovering that in production is the wrong order.

**D2 — our own scan.** `security/zap/` (the folder already exists, holding the
governance `.docx` set — the scan artefacts join it):

| File | Purpose |
|---|---|
| `security/zap/baseline.yaml` | ZAP Automation Framework plan: spider + passive scan, no attack. Safe to run anywhere, suitable for CI. |
| `security/zap/full-auth.yaml` | Authenticated spider + **active** scan against a local compose stack with seeded demo data. |
| `security/zap/README.md` | How to run both, and the findings from our run with a triage verdict per item. |

Two things make or break this:

- **Authenticated scanning.** Almost the whole app is behind login, so an
  unauthenticated scan tests the login page and little else. ZAP needs a
  configured authentication context (form login, plus a logged-in/logged-out
  regex so it notices session loss). Budget most of the effort here.
- **Never scan production.** An active scan is a real attack that can damage
  data. Run it against a throwaway compose stack seeded with
  `seed_demo_school`, never against a deployment holding student records. This
  is the same rule as `auto-memory/feedback_no_automated_prod_e2e.md`, only
  more so.

**D3 — the manual's section.** In `docs/self-hosting.md`, a
"Testing your deployment's security" section covering: install ZAP (Docker is
easiest — `ghcr.io/zaproxy/zaproxy`), run the baseline passive scan against
their own instance, then the authenticated active scan **against a staging
copy**, how to read the report, and which findings are expected versus real.

It must state plainly, in the ministry's own words rather than ours:
**only scan a system you own or are authorised to test**, and **an active scan
can destroy data, so point it at a copy**. Both are ZAP's own warnings and
carry more weight repeated than assumed.

## Out of scope

- A local-model (Ollama) sidecar for on-prem — decided against for now. The
  offline desktop build already covers no-internet classrooms.
- Kubernetes/Helm, multi-node, HA Postgres.
- Changing the Dockerfile — Azure depends on its CMD
  (`ops/migrate_and_seed.sh:5-11`). Compose overrides `command` instead.
- Re-doing the AWS Pulumi program; it is already parameterised correctly.
- **Scanning production.** Passive/baseline against a live instance is
  defensible; the active scan is not, and nothing here will point one at
  student data.
- A blocking CSP. Report-only first; enforcing it is a follow-up once the
  violation reports are clean.
- Rewriting the `security/*.docx` governance set (Security Posture and Roadmap,
  Data Protection Overview, Terms of Service). The manual should link to them,
  not restate them.

## Verification

1. **The compose stack comes up on a clean machine** — `docker compose up -d`
   from a fresh clone with only `.env` filled, no other setup. `docker compose
   ps` shows all three healthy.
2. **The end-to-end test is one tutoring turn**, not a health check: it
   exercises the database, pgvector, the LLM credentials and media in one
   action. Create a superuser, upload a small curriculum, take a turn.
3. **Deliberately reproduce trap #1** — set `HTTPS_EDGE=true` behind plain HTTP,
   confirm login 403s, then set it false and confirm login works. If the manual
   claims a symptom, the symptom should have been observed.
4. **Backup/restore round-trip** — `backup.sh`, drop the volume, `restore.sh`,
   confirm the tutoring turn still works and media still resolves.
5. **The settings change breaks nothing** — full `pytest`, plus confirm a
   missing `SECRET_KEY` with `DEBUG=False` raises at startup rather than
   serving.
6. **`manage.py check --deploy`** inside the running container, with its output
   quoted in the manual so a self-hoster knows what "clean" looks like. Today
   it reports W004 and W008; after D1 it should report neither.
7. **ZAP baseline passes on the compose stack** with no new high/medium alerts,
   and the report is committed to `security/zap/` so the next run has a
   baseline to diff against.
8. **The authenticated scan actually reaches authenticated pages** — verify by
   checking the ZAP site tree contains `/dashboard/` and `/tutor/`, not just
   `/login/`. An authenticated scan that silently fell back to anonymous is the
   most common way this exercise produces a falsely clean report.
9. **The CSP is report-only and the app still works** — take a tutoring turn
   and open the dashboard with the browser console open; collect violations
   before considering enforcement.

## Phasing

The security work depends on the compose stack (it is the thing to scan), so:

| Phase | Work | Rough size |
|---|---|---|
| 1 | A (settings defaults) + B (compose, `.env.example`, Caddy, backup) | ~1 day |
| 2 | C (the manual, both paths) | ~1 day |
| 3 | D1 (HSTS/SSL-redirect gated on `HTTPS_EDGE`, report-only CSP) | ~half day |
| 4 | D2 (ZAP plans, authenticated context, our scan + triage) | ~1 day, mostly auth |
| 5 | D3 (manual's security-testing section) | ~half day |

Phases 1-2 are shippable on their own: a ministry could self-host from the
manual before any of the ZAP work lands.

## Next step

Write `deploy/compose/.env.example` first — it forces the full list of required
configuration into the open, and the compose file and the manual's Path A both
follow from it.

---

# Session log — 2026-08-12: what verification actually found

Phases 1-2 are built and committed (`534075f`, `26dbf8d`). The value of this
session was not the code; it was that **running the thing found seven bugs that
reading it did not**. Recorded here because most would have reached a ministry.

## Bugs found only by running it

1. **Gunicorn bound to loopback.** A YAML folded block (`>`) does NOT fold lines
   indented deeper than the first — it keeps their newlines. So
   `--bind 0.0.0.0:8000` reached the shell as a separate command and gunicorn
   fell back to `127.0.0.1:8000`. The container reported **healthy** throughout,
   because its healthcheck curled loopback. Caddy would have 502'd every
   request against a stack that said it was fine. Fixed by writing the command
   as a single exec-form line.

2. **The healthcheck could not detect that.** Rewritten to do both: TCP-connect
   to the container's own address (proves the bind) *and* HTTP-GET loopback
   (proves Django answers).

3. **`ALLOWED_HOSTS` broke the healthcheck.** The check requests
   `http://127.0.0.1:8000/health/`, and Django rejects unknown Hosts — so a
   deployment following `env.example` (`ALLOWED_HOSTS=tutor.education.gov.xx`)
   would be permanently unhealthy while serving perfectly. Compose now appends
   `,127.0.0.1,localhost` to whatever the operator sets.

4. **`ModelConfig.institution` is NOT NULL**, and a fresh install has no
   institution — so `seed_local_tutor` crashed on precisely the deployment it
   exists for. Uses `Institution.get_global()`, which exists for this.

5. **`build.platforms` is rejected by the default Docker driver**
   ("Multi-platform build is not supported"). The fix was to remove the pin
   entirely: you build on the server you deploy to, so native is right, and
   pinning amd64 would force every ARM host through emulation. Only the AWS
   path needs a pin, and it passes `--platform` explicitly.

6. **`datadump.json` was inside the image** — 4 `auth.user` rows with pbkdf2
   password hashes and 3 email addresses, including the `admin` superuser.
   `.dockerignore` excluded `db.sqlite3` but not a Django dumpdata of the same
   data, nor `db.sqlite3-wal`/`-shm`, nor `build/` (135 MB). **This is the one
   that mattered**: the image is published publicly, so those hashes would have
   been world-readable for offline cracking. Image dropped 7 GB → 5.5 GB as a
   side effect.

7. **The manual told them to clone a private repo.** Caught while writing it.

## Design decisions and why

**Two pack kinds that refuse each other.** `apps/desktop/packs.py` ships a
roster — real children's names, usernames, year groups — because a provisioned
laptop must bind a local login to the server user id its work syncs under. That
is correct there and a data-protection incident if the same file seeds another
country's server. `apps/curriculum/curriculum_pack.py` carries content only.
Each manifest declares `pack_kind`; each importer refuses the other's.

The refusal is checked **twice** — the declared kind AND the actual member list
— because the manifest is what a pack says about itself and only the member
list catches one that was edited. Packs predating `pack_kind` are treated as
desktop packs so devices in the field keep working.

**Content ships inside the image, not downloaded.** 6.8 MB of text (3 courses,
300 lessons, 1912 steps, 7023 questions, 1007 KB chunks). First run therefore
needs no internet — the same offline-first principle the desktop build is
designed around. Media is excluded: 142 MB against 6.8 MB, and
`.dockerignore` already kept it out. **~73% of steps reference a figure**, so
those read with a gap until media is added.

**Deployment files are extracted from the image**, not fetched from the repo.
They cannot drift from the image they configure, and no repository access is
needed. Working from source stays documented — it is also how an ARM server
gets an image.

**Fail-safe settings defaults.** `DEBUG` now defaults False; the app refuses to
boot with the published dev `SECRET_KEY` when DEBUG is off. Three exemptions,
each deliberate: the offline builds (no secret store, loopback/isolated
network), pytest (Django forces DEBUG=False and supplies no key), and
non-serving `manage.py` commands.

## Measured, not estimated

| | |
|---|---|
| Image | **5.5 GB** after the ignore fix (7 GB before) |
| Build headroom needed | ~25 GB; BuildKit cache exceeded the image itself |
| Seed pack | 6.8 MB, imports in seconds |
| pgvector | 0.8.6, created by `curriculum.0029` against `pgvector/pgvector:pg16` |
| Local tutoring turn | **226 s** on a 7.7 GiB Docker VM — prefill 110 s, decode 1.9 tok/s, "tight fit" warning |
| System prompt | 17,486 chars, so prefill dominates — structural, not machine-specific |

**The 226 s turn is the number to be careful about.** It was on a memory-starved
VM sharing 7.7 GiB with Postgres and the app. A 16 GB server with nothing else
competing will be much better, but that has NOT been measured. The manual
should say the local model is for getting started or strict data residency —
not for thirty concurrent students — and should not quote a latency until one
is measured on server-class hardware.

## Open

- **Media**: 73% of steps reference figures that are not shipped.
- **Cloud-key turn**: never measured, so the manual cannot contrast the two.
- **Repo visibility**: the image contains the full source (2908 `.py` files), so
  publishing it publishes the code. Private repo + public package gives only
  nominal source privacy. Worth deciding deliberately rather than by default.
- **Benchmark numbers removed** from all user-facing text on Edward's call —
  more eval needed before quoting 65% vs 94%. The local model is described as a
  fallback, not a measured tier.
