# `pip install ai-tutor` — Path C for self-hosting (2026-08-13)

## Problem

A ministry should be able to run AI Tutor on a server they control without
Docker. Today the only non-AWS route is `docs/self-hosting.md` Path A, which
assumes Docker and the Compose plugin. Some institutions cannot install Docker
(policy, an existing managed-Python estate, or a shared host), and for them the
platform is currently unreachable.

This adds **Path C — pip install onto your own server**, alongside Docker (Path
A) and AWS (Path B). Docker stays the recommended route; see "What pip does not
give you" for why.

## Current state (audited 2026-08-13)

The repo is an application, not a package. Nothing is installable:

- No `pyproject.toml`, `setup.py`, `setup.cfg` or `MANIFEST.in`.
- Importable top-level packages are `apps`, `config`, `ops`, `evals`
  (each has `__init__.py`). A wheel built as-is installs `apps/` and `config/`
  into site-packages under those names.
- Entry points today are scripts at the repo root: `manage.py`, `serve.py`,
  `chat.py`, `desktop.py`, `desktop_server.py`.
- Assets that must ship inside the wheel: `templates/` (1.4 MB, 91 files),
  `static/` (2.0 MB, 88), `locale/` (240 KB), `deploy/seed/curriculum-pack.tar.gz`
  (6.8 MB), plus every app's `migrations/`.
- `infra/systemd/ai-tutor.service` already exists and already uses
  `--timeout 300`, so the process-supervision half is mostly written.

Measured import churn for a namespace move (tracked files, counted in Python —
the shell's `grep` is aliased to `ugrep` and miscounts here):

| Reference | Count |
|---|---|
| `from apps.…` | 2301 |
| `import apps.…` | 5 |
| `config.settings` | 83 |
| `"apps.x"` strings (INSTALLED_APPS, signal senders, `sender='tutoring.TutorSession'`) | 266 |
| **files affected** | **431** of 2037 tracked (411 `.py`, 12 `.md`, 3 `.ipynb`, and one each `.spec` `.service` `.yml` `.sh`) |

Files outside `apps/` that hardcode `config.wsgi` / `config.settings` and must
move in lockstep — every one of these is load-bearing for a live deployment:

    Dockerfile                        deploy/compose/docker-compose.yml
    infra/aws/components/compute.py   infra/systemd/ai-tutor.service
    AI-Tutor.spec                     manage.py, chat.py, desktop_server.py
    config/settings_{kiosk,desktop}.py

## Target design

### The name

Everything importable moves under a single distribution package:

    ai_tutor/
      config/          (was config/)
      apps/            (was apps/)
        tutoring/ curriculum/ dashboard/ accounts/ llm/ desktop/ safety/ …

`ai_tutor.apps.tutoring` rather than flattening to `ai_tutor.tutoring`. Both are
the same mechanical rewrite, but keeping the `apps.` segment makes the diff a
pure prefix insertion, which is far easier to review and to verify with a
grep than a two-part rename.

**Django app labels do not change — verified, not assumed.** All 11 `AppConfig`
classes declare only `name = 'apps.<x>'` and leave `label` implicit, and Django
derives an implicit label from the *last* dotted component. So
`ai_tutor.apps.tutoring` still yields label `tutoring`, and `django_migrations`
rows, `ContentType` rows and every `sender='tutoring.TutorSession'` string keep
working untouched.

This is the constraint that would have been expensive to get wrong — a changed
label makes every existing deployment try to re-run its migrations — and it
turns out to hold for free. Phase 1 still adds an explicit `label` to each
`AppConfig` as belt-and-braces, so a future rename cannot silently break it.

Baseline for the check: `manage.py makemigrations --check --dry-run` reports
"No changes detected" today (2026-08-13). It must still say that after the move.

`ops/` and `evals/` are developer tooling and are **not** shipped in the wheel;
they stay at the repo root and are excluded from the distribution.

### Configuration

`config/settings.py` reads ~everything from `os.getenv`. That stays — the Docker,
Azure and AWS deployments all depend on it, and Path C must not fork settings.

Path C adds one layer *above* env vars, because a systemd service is a bad place
to keep thirty `Environment=` lines and a secret:

    /etc/ai-tutor/ai-tutor.env      # KEY=value, mode 0600, owned by the service user

`ai-tutor` reads that file and exports it before Django loads. Precedence:
real environment > env file > built-in default. Same variable names as
`deploy/compose/env.example`, so the manual documents one vocabulary.

### State outside site-packages

A wheel's install directory is not writable and is replaced on upgrade, so all
mutable state moves to a data directory:

    AI_TUTOR_DATA_DIR   default /var/lib/ai-tutor (root) or
                                ~/.local/share/ai-tutor (user install)
      media/            uploads
      vectordb/         ChromaDB
      static/           collectstatic output, served by WhiteNoise
      db.sqlite3        only if the operator chose SQLite

### The CLI

One console entry point, `ai-tutor`, wrapping the management commands an
operator actually needs. `manage.py` stays for development.

| Command | Does |
|---|---|
| `ai-tutor init` | Create the data dir + env file, generate a `SECRET_KEY`, print what still needs filling in |
| `ai-tutor migrate` | Apply migrations |
| `ai-tutor seed` | Import the bundled curriculum pack (`--if-empty`) |
| `ai-tutor createsuperuser` | The first administrator |
| `ai-tutor serve` | Gunicorn, `--timeout 300`, binding configurable |
| `ai-tutor check` | `manage.py check --deploy`, so an operator can see "clean" |
| `ai-tutor systemd` | Print a ready-to-install unit file with the right paths |

### What pip does NOT give you

Stated plainly in the manual, because the gap is the whole reason Docker is
still recommended. Compose supplies these and pip cannot:

- **Postgres.** Path C documents installing it separately. SQLite is allowed for
  a single small school and must be labelled as such — pgvector is unavailable
  on SQLite, so `apps/curriculum/kb_storage.py` falls back to the brute-force
  cosine backend, which is fine at pilot scale and not beyond it.
- **TLS.** No Caddy. The manual covers a reverse proxy (Caddy or nginx) in front
  of `ai-tutor serve`, and this is where `HTTPS_EDGE` and the health-check
  exemption matter — see below.
- **Process supervision.** Handled by the systemd unit, which already exists.

### Deployment invariants that must survive

- `SECURE_REDIRECT_EXEMPT = [r'^health/$']` (added 2026-08-13, commit 153ec07)
  exists because a load balancer health-checks over plain HTTP without
  `X-Forwarded-Proto`. Any reverse proxy Path C recommends has the same
  property, so the manual must tell operators to health-check `/health/`.
- `HTTPS_EDGE=false` when nothing terminates TLS in front, or login returns 403
  with no error anywhere.

## Phased delivery

Phase 1 is the risky one and ships alone, with no packaging in it at all, so
that a regression is unambiguously attributable.

| Phase | Work | Status |
|---|---|---|
| 1 | Namespace move to `ai_tutor/` | **Done** — `9b416c1`, merged `56ad0f1`, deployed |
| 2 | `pyproject.toml`, assets into the package, `BASE_DIR`/`PACKAGE_DIR` split | **Done** — `aa20bba` |
| 3 | The `ai-tutor` CLI + env-file loader | **Done** — `7ee8c11` |
| 4 | `docs/self-hosting.md` Path C | **Done** — `968f9a4` |
| 5 | CI builds wheel + sdist on tag, attaches to the release | **Done** — this commit |

### What building it actually found

Each of these was invisible to reading and fell out of running the thing:

1. **The wheel shipped 9 of 88 static files.** `.gitignore` has a bare
   `vendor/` rule matching `ai_tutor/static/vendor/`. Git tracks those files
   anyway — tracking beats ignoring — but hatchling reads the pattern and
   dropped all 78: KaTeX, DOMPurify, the fonts. It built, imported and served,
   and would only have broken in a browser, on maths.
2. **The same bug reappeared in the sdist** after being fixed for the wheel,
   because `artifacts` is per-target. An sdist without them builds a
   KaTeX-less wheel one level down, where the wheel tests cannot see it. Now
   verified by building a wheel FROM the sdist and diffing the file lists:
   862 files each, zero difference.
3. **A crash-loop for fresh Docker deployments.** The bundled seed pack's
   `schema_rev` predates migrations added this week, and in `--if-empty` mode
   the refusal raised inside the `&&` chain ending in gunicorn. Startup now
   warns and continues.
4. **Two runtime paths were `BASE_DIR`-relative** and would have resolved into
   the data directory once installed: the PWA service worker and the
   translation catalog.
5. **The generated systemd unit could not read its own configuration.** It
   named the config file in a comment but never passed it, and
   `ProtectHome=yes` hides `/home`.

### Open questions — resolved

1. **Postgres or SQLite for Path C?** Postgres documented as required;
   SQLite explicitly allowed for one small school, with the pgvector
   consequence stated.
2. **Seed pack in the wheel?** Yes — 6.8 MB of an 11 MB wheel, and a first run
   with no internet is the point.
3. **Python floor?** `>=3.12`, decided by evidence rather than CLAUDE.md's
   stale 3.11: Django 6.0 requires 3.12 and the Dockerfile is
   `python:3.12-slim`.

### Still outstanding

- **`pip install ai-tutor` does not work** — nothing is on a public index. The
  release carries a wheel to install by URL; claiming the PyPI name is a
  separate decision (see Out of scope).
- **The seed pack is stale.** A fresh install comes up with an empty
  curriculum. It needs rebuilding from whichever source produced the original
  (3 courses / 300 lessons); this checkout holds demo and eval content, so it
  cannot be regenerated here.
- **A full `pip install` with dependencies has never been executed.** Testing
  used `--no-deps` against an existing environment because the dev machine had
  ~4.6 GB free and the pinned set pulls 2-3 GB. The dependency list is guarded
  by a drift test against `requirements.txt`, and the image build installs the
  same list daily — but that is inference, not a run.

## Verification

The namespace move is only safe if all four existing consumers still work, so
each gets an explicit check before Phase 1 is called done:

1. **Full test suite** at parity with the pre-move baseline. Compare counts, not
   "it passed" — `apps/tutoring/tests` currently has 34 pre-existing failures and
   `test_i18n_coverage` fails; those numbers must be unchanged.
2. **No new migrations.** `manage.py makemigrations --check --dry-run` must
   report nothing, proving app labels are intact.
3. **Docker image builds and serves** — `docker compose up -d`, all healthy, one
   real tutoring turn.
4. **AWS deploy is green** on `aws_deployment`, including the migration task and
   the smoke test.
5. **Desktop build** — `AI-Tutor.spec` still produces a runnable app; the
   PyInstaller `collect_submodules('apps.x')` calls are the likeliest breakage.
6. **A wheel installs into a clean venv** on a machine with no repo checkout, and
   `ai-tutor serve` answers `/health/` with 200.
7. **`ai-tutor check`** reports the same single `security.W021` the Docker path
   reports, so "clean" means the same thing on both.

## Out of scope

- Publishing to PyPI. Build the wheel and attach it to a GitHub release first;
  claiming the name is a separate decision.
- Replacing Docker as the recommended path. Path C is an alternative for
  institutions that cannot run containers.
- Bundling Postgres, a reverse proxy, or certificates.
- Packaging the desktop build as a wheel — end users there have no Python.
  `AI-Tutor.spec` (PyInstaller) stays the desktop distribution.
- `ops/` and `evals/` as shipped modules.
- Splitting the project into several distributions (core / dashboard / desktop).
  Rule of Three: one distribution until there is a reason for more.

## Open questions

1. **Postgres or SQLite as the documented default for Path C?** Recommend
   Postgres, with SQLite explicitly allowed for one school under ~100 students,
   because pgvector is unavailable there and KB search falls back to brute force.
2. **Wheel size.** The seed curriculum is 6.8 MB of the ~12 MB wheel. Recommend
   keeping it in — a first run with no internet is the point — but it could
   become an extra (`pip install ai-tutor[curriculum]`) if size matters.
3. **Python floor.** Currently 3.12 locally, 3.11 stated in CLAUDE.md. Needs
   pinning in `requires-python` before Phase 2.

## Next step

Phase 1, and nothing else in the same change: the mechanical move to
`ai_tutor/`, verified against all four consumers above. It is the only phase
that can break a live deployment, and it is worth landing on its own.
