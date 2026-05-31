# Mozambique Pilot — Portuguese localization plan

**Status**: planning — no code written. Ready for the next Claude to pick up.
**Trigger**: Paschal's Mozambique visit, week of 2026-06-01. Government has shared Grade 8 materials (sitting locally under `mozambique/Grade 8 Materials/`, currently gitignored; 30 files).
**Branch reserved for the i18n work**: `feature/i18n-bootstrap` (created 2026-05-31, no commits yet; tracks `origin/dev`).
**Plan author**: Claude Opus 4.7 session 2026-05-29 → 2026-05-31 (Edward driving).

---

## Architectural decision (UPDATED 2026-05-31)

**Locale is a platform-level setting, NOT an institution-level field.**

Earlier draft of this plan proposed `Institution.default_locale`. **Discarded.** Edward clarified that the Mozambique pilot is its own Azure deployment, separate from the Seychelles (`aitutor-pixel-app`) and staging (`aitutor-staging-app`) Container Apps. The two pilots never share runtime state — they're separate Pulumi stacks, separate Postgres servers, separate ACRs.

Implications:

| Question | Before (institution-level) | After (platform-level) |
|---|---|---|
| Where does locale live? | `Institution.default_locale` field on each row | `LANGUAGE_CODE` env var per deployment |
| Middleware | Custom locale middleware reads `request.user.studentprofile.institution.default_locale` per request | Django's built-in `LocaleMiddleware` reads `settings.LANGUAGE_CODE` (one value per deploy) |
| Single deploy supports both languages? | Yes (in principle) | No — each deploy is single-locale |
| Migration risk | New NOT NULL column on `accounts_institution` — backfill needed | Zero schema change |
| Routing complexity | Multi-tenant (locale × institution) | Per-deploy (one locale, one set of curriculum) |
| Aligned with how staging/preview/pixel stacks already work | No | **Yes** — same single-tenant-per-stack pattern |

**The Mozambique deploy is a new Pulumi stack (`mozambique`) running `LANGUAGE_CODE=pt-MZ`, with its own Postgres seeded with Portuguese-only curriculum.** The Seychelles `pixel` stack is untouched.

This simplifies every workstream below — no per-row locale routing, no auth-middleware-order subtlety, no "what if a teacher in Seychelles wants to test Portuguese" edge case. One deploy = one locale.

---

## What needs to change, in three layers

| Layer | What it covers | Where the text lives | How translated |
|---|---|---|---|
| **A. App UI** | Buttons, headers, banners, error messages, form labels, JS-rendered strings | `templates/*.html`, Python `_(…)` calls, JS via `JavaScriptCatalog` | One-shot Claude batch via `manage.py translate_po`, human spot-review |
| **B. LLM tutor responses** | Real-time tutor text in chat (hint ladders, affirmations, exit-ticket grading feedback) | Generated per-turn by the LLM | System prompt reads `settings.LANGUAGE_CODE`, appends "Respond in pt-MZ Mozambique register" when non-English |
| **C. Curriculum content** | Lesson titles, teaching scripts, objectives, MCQ stems + options + explanations | DB rows under `Course → Unit → Lesson → LessonStep → ExitTicketQuestion` | (a) government supplies Portuguese curriculum directly OR (b) translate Seychelles English curriculum via Claude batch |

A + B + C are independent workstreams. They merge separately. C is the gating concern for pilot launch — without Portuguese curriculum, there's nothing to teach in Portuguese.

---

## Open questions for Edward (block specific milestones, not all)

| # | Question | Blocks | Default if not answered |
|---|---|---|---|
| 1 | Are the materials in `mozambique/Grade 8 Materials/` already in Portuguese, or do we translate from Seychelles English curriculum? | M5 (curriculum) | Inspect the files — if PT, import; if EN, translate via Claude batch |
| 2 | Audio for v1: text-only OK for pilot launch, or required? | M7 (audio) | Text-only — defer M7 to v2 |
| 3 | Native-speaker translation review: do you have a Mozambique educator to spot-review the Claude-generated translations? | M3 (UI), M5 (curriculum) | Ship without review; add a "report bad translation" UI link |
| 4 | What's the Mozambique institution slug + display name in the Pilot DB? | M5 (curriculum), M6 (infra) | `mozambique-pilot` / "Mozambique Pilot Schools" |
| 5 | Pilot scope: Grade 8 only? Multiple subjects? Just math? Just geography? | M5 (curriculum scope) | Match what's in `mozambique/Grade 8 Materials/` literally |
| 6 | Tutor model on Mozambique: Opus 4.7 (matches Seychelles) or something cheaper? | M4 (tutor) | Opus 4.7 — pilot budget can absorb it |

---

## Milestone structure

Seven milestones. **M1–M3** are i18n plumbing (English app + Portuguese-ready). **M4** is the tutor's locale awareness. **M5** is content. **M6** is the deployment. **M7** is audio (optional for v1).

Critical path for Paschal's visit: **M1 → M2 → M3 → M5 → M6** (M4 can ship before or after M5).

### M1 — i18n bootstrap (foundation)

**Effort**: 0.5 day. **Blocks**: M2, M3, M4. **Branch**: `feature/i18n-bootstrap` (already created).

**Deliverables:**

- `config/settings.py`:
  - Add `LANGUAGES = [('en', 'English'), ('pt-MZ', 'Português (Moçambique)')]`
  - Add `LOCALE_PATHS = [BASE_DIR / 'locale']`
  - Add `'django.middleware.locale.LocaleMiddleware'` to `MIDDLEWARE` (after `SessionMiddleware`, before `CommonMiddleware`)
  - `LANGUAGE_CODE` already exists as `'en-us'` — keep as the default; the Mozambique deploy overrides to `'pt-MZ'` via env var
- `config/urls.py`: mount Django's `JavaScriptCatalog` view at `/jsi18n/`
- `locale/pt_MZ/LC_MESSAGES/django.po` + `djangojs.po`: empty skeleton files (just the header, no msgids yet — `manage.py makemessages` will populate)
- `infra/__main__.py`: thread a new `LANGUAGE_CODE` env var from Pulumi config (`aitutor:language-code`, defaults to `'en-us'`)
- `RELEASING.md`: document that the Mozambique stack sets `aitutor:language-code = pt-MZ`

**Tests:**

1. `python manage.py check` exits 0.
2. New test `apps/accounts/tests/test_i18n_bootstrap.py`:
   - Verifies `LANGUAGES` contains both `'en'` and `'pt-MZ'`.
   - Verifies `LOCALE_PATHS` resolves to a real directory.
   - Verifies `/jsi18n/` returns HTTP 200 and a JavaScript content type.
3. Existing `apps.tutoring.simple_tutor` test suite still passes (380/380 baseline).
4. Smoke: `curl http://localhost:8000/jsi18n/ | head -3` returns JS.

**Non-goals (intentional, stay scoped to "plumbing only"):**

- DON'T wrap any template strings in `{% trans %}` (that's M2).
- DON'T translate anything (that's M3).
- DON'T change the tutor system prompt (that's M4).

**Reviewer checklist:**

- [ ] `LocaleMiddleware` placed correctly in MIDDLEWARE order (after `SessionMiddleware`).
- [ ] `LANGUAGE_CODE` not hardcoded anywhere except `settings.py`.
- [ ] No DB migration in this PR (locale is platform-level → no schema change).

---

### M2 — Wrap user-facing strings

**Effort**: 1.5 days. **Blocks**: M3. **Depends on**: M1.

**Deliverables:**

- Wrap every user-facing string in templates with `{% trans "..." %}` or `{% blocktrans %}` for multi-line.
  - Target: ~120 strings across `templates/accounts/`, `templates/tutoring/chat_tutor.html`, `templates/tutoring/catalog.html`, `templates/dashboard/`, and the landing pages.
  - DO NOT translate strings that are visible only to staff/teachers (admin UI, dashboard internals) for v1 — scope creep.
- Wrap user-facing Python strings in `_(…)` from `gettext_lazy`:
  - Form labels (`apps/accounts/forms.py`)
  - `messages.success/error` calls in views
  - `ValidationError` messages
- Generate `django.po` and `djangojs.po`:
  ```
  python manage.py makemessages -l pt_MZ
  python manage.py makemessages -l pt_MZ -d djangojs
  ```
- New file: `scripts/list_user_facing_strings.py` — a one-shot audit script that prints any user-facing string NOT wrapped, so M2 reviewers can verify coverage.

**Tests:**

1. `python manage.py makemessages -l pt_MZ --dry-run` reports no unwrapped strings in target templates.
2. New test `apps/accounts/tests/test_i18n_coverage.py`:
   - Renders every student-facing template at `LANGUAGE_CODE='en'` and `'pt-MZ'` (with empty `.po` → identity translation).
   - Asserts no Django template syntax errors on either pass.
3. Visual regression: take chrome-devtools screenshots of all student-facing pages at both locales (with empty .po, pages should look identical to today's English).
4. `pytest apps/ tests/` baseline still passes.

**Risks:**

- Mis-wrapped `{% trans %}` block (e.g. `{% trans "Hello {{ name }}" %}`) is a runtime error. Use `{% blocktrans %}` for any string with variables.
- Default `gettext` returns the msgid when no translation exists, so coverage gaps still display English — safe fallback.

**Reviewer checklist:**

- [ ] No raw `"Hello"`-style strings in templates that are student-visible.
- [ ] `{% load i18n %}` at the top of every wrapped template.
- [ ] `gettext_lazy` used in models / form-field labels (not `gettext`, which evaluates at import time).

---

### M3 — Translate UI strings into pt-MZ

**Effort**: 1 day. **Blocks**: nothing — M4 and M5 can parallelize. **Depends on**: M2.

**Deliverables:**

- New management command `scripts/translate_po.py` (or `apps/accounts/management/commands/translate_po.py`):
  - Reads `locale/pt_MZ/LC_MESSAGES/django.po`, batches the msgids, sends to Claude Opus 4.7 with a Mozambique-register prompt.
  - Writes msgstrs back into the file.
  - Idempotent — only translates rows where msgstr is empty.
  - Logs each translation pair for human review.
- Run the command:
  ```
  python manage.py translate_po --locale pt_MZ
  python manage.py compilemessages
  ```
- `locale/pt_MZ/LC_MESSAGES/django.mo` + `djangojs.mo` (committed; Django reads .mo at runtime, not .po).
- A Mozambique educator (if available — see Open Question #3) reviews the `.po` file. Any corrections land in a follow-up commit.

**Tests:**

1. Spin up a local server with `LANGUAGE_CODE='pt-MZ'` and `SIMPLE_TUTOR_ENGINE=on` (env var override).
2. Use chrome-devtools-mcp to navigate to the login page → verify "Sign In" reads "Iniciar sessão" or similar.
3. Verify the chat tutor placeholder "Type your answer…" reads "Escreve a tua resposta…" or similar.
4. New test `apps/accounts/tests/test_i18n_translations_present.py`:
   - Counts msgids vs translated msgstrs in `django.po` — fails if coverage < 90%.

**Risks:**

- Claude translates a string in a way that's grammatically correct but pedagogically off (e.g. addressing the student as `você` formal vs `tu` informal). Decide register UP-FRONT in the prompt; spot-review catches the rest.
- Some msgids contain template variables (`{username}`) — Claude must preserve them verbatim.

**Reviewer checklist:**

- [ ] All variable placeholders preserved in translations.
- [ ] Register is consistent (pick `tu` informal for students throughout).
- [ ] Mozambique-specific spellings honored (post-1990 orthographic agreement; Claude uses these by default).

---

### M4 — Tutor + intent classifier locale-aware

**Effort**: 0.5 day. **Blocks**: nothing — runs in parallel with M3/M5. **Depends on**: M1.

**Deliverables:**

- `apps/tutoring/simple_tutor/engine.py::respond`:
  - Read `settings.LANGUAGE_CODE` once at module load (cached).
  - If not `'en'` or `'en-us'`, append to the system prompt:
    ```
    Respond to the student in Mozambique Portuguese (pt-MZ register).
    Use 'tu' informal addressing. Use post-1990 Acordo Ortográfico spelling.
    ```
  - No engine logic change — same tools, same state machine, same flow.
- `apps/tutoring/simple_tutor/intent.py`:
  - Refactor pattern lists into a `LANG_PATTERNS: dict[str, list[re.Pattern]]` map.
  - Add `pt_MZ` patterns:
    - `non_engagement`: `não sei`, `não percebi`, `obrigado/obrigada`, `não consigo`, `é difícil`, `ok`, `sim`, `não`
    - `clarification`: `o que significa`, `não percebi`, `podes explicar`, `como`, `pode repetir`, `não compreendo`
    - `pushback`: `acho que queres dizer`, `acho que tu queres dizer`, `mas e se`, `não está certo`, `bom, na verdade`
  - Locale selection: read `settings.LANGUAGE_CODE` at module load; if `pt-MZ`, use pt_MZ patterns (otherwise English fallback).
- 10–15 Portuguese eval scenarios under `evals/dataset/portuguese/`:
  - Translate the highest-leverage 10 English scenarios (math + geography mix, all five personas).

**Tests:**

1. `apps/tutoring/simple_tutor/tests/test_intent.py` extended with Portuguese cases for each intent label.
2. New eval scenarios pass when run with `LANGUAGE_CODE='pt-MZ' SIMPLE_TUTOR_ENGINE=on python manage.py run_eval --scenarios portuguese`.
3. Existing English eval (78/80 baseline) still passes when `LANGUAGE_CODE='en-us'` — confirms no regression.
4. Manual chrome-devtools E2E on the locally-running Portuguese app: drive 3-turn tutoring session, verify tutor responds in Portuguese throughout.

**Risks:**

- Intent classifier is regex-based; new locales need their own regex tables. Risk: misclassification leads to wrong routing (e.g. "não sei" classified as `answer_or_other` instead of `non_engagement` → tutor tries to grade).
  - Mitigation: the 10-15 Portuguese eval scenarios MUST cover the high-traffic intent patterns.

**Reviewer checklist:**

- [ ] `LANG_PATTERNS` map gracefully falls back to English when an unknown locale is set.
- [ ] System-prompt addition is conditional — English deployments unaffected.
- [ ] No eval scenarios fail at `LANGUAGE_CODE='en-us'` (the existing 78/80 baseline still holds).

---

### M5 — Mozambique curriculum import

**Effort**: 2–4 days depending on Open Question #1. **Blocks**: M6. **Depends on**: M1 (for locale env-var threading).

**Two paths, picked by inspecting `mozambique/Grade 8 Materials/`:**

**Path A — Materials are already Portuguese:**
- New management command `apps/curriculum/management/commands/import_mozambique_curriculum.py`:
  - Reads the directory layout, maps to `Course → Unit → Lesson → LessonStep → ExitTicketQuestion`.
  - Scoped to a new `Institution(slug='mozambique-pilot', name='Mozambique Pilot Schools')`.
  - Locale field on each record is implicit — only loaded on Mozambique deploy, so no field needed.

**Path B — Materials need translation from Seychelles English curriculum:**
- New management command `apps/curriculum/management/commands/translate_curriculum.py`:
  - Pulls Seychelles curriculum for the relevant grade (Grade 8 ≈ Seychelles S2/S3 — need to confirm).
  - Translates lesson_text, MCQ stems + options + explanations via Claude Opus 4.7 batch (one prompt per lesson).
  - Writes into a new Mozambique-scoped Institution.
  - The MCQ B-bias fix (commit `c56c804`) applies — translated MCQs preserve A/B/C/D distribution.

**Tests:**

1. New test `apps/curriculum/tests/test_mozambique_import.py`:
   - Imports a tiny fixture (1 course, 1 unit, 2 lessons, 5 MCQs).
   - Verifies the right `Institution` is created.
   - Verifies relationships (Course → Unit → Lesson → LessonStep).
2. Quality check (manual): a Mozambique educator reviews 10 lessons + 50 MCQs randomly sampled.
3. Curriculum audit script: `python manage.py audit_kb_coverage --institution=mozambique-pilot` reports >= 1 question per enabling-objective.

**Risks:**

- Path B can produce translations that lose pedagogical precision (e.g. "find the missing angle" in English vs Portuguese math terminology).
- Curriculum size may exceed Claude's context window per lesson — chunk into smaller batch sizes.

**Reviewer checklist:**

- [ ] All curriculum rows scoped to the `mozambique-pilot` institution.
- [ ] No cross-pollination with Seychelles institutions (verify with `Lesson.objects.filter(institution__slug='mozambique-pilot').count()`).
- [ ] MCQ correct-answer distribution is roughly uniform (~25% each A/B/C/D — same audit as the B-bias fix).

---

### M6 — Mozambique Pulumi stack + deploy

**Effort**: 1 day. **Blocks**: pilot launch. **Depends on**: M1, M5.

**Deliverables:**

- `infra/Pulumi.mozambique.yaml`: new stack config:
  - `aitutor:workload-profile-type: Consumption` (same as staging)
  - `aitutor:container-cpu: "2.0"`, `aitutor:container-memory: "4Gi"`
  - `aitutor:min-replicas: "1"` (warm container for pilot UX)
  - `aitutor:postgres-sku: Standard_B1ms`
  - `aitutor:simple-tutor-engine: "on"`
  - `aitutor:tutoring-question-types: "mcq"`
  - `aitutor:language-code: "pt-MZ"` ← NEW key, plumbed in M1
  - Secrets via `pulumi config set --secret` after `pulumi stack init mozambique`
- `pulumi up --stack mozambique` provisions: RG, ACR, Postgres, Container App, Storage, FileShare, Log Workspace (mirrors staging/preview shape).
- Service principal grant (one-off): give the GHA principal Contributor + AcrPush on `aitutor-mozambique-rg`.
- `.github/workflows/deploy-mozambique.yml`: new workflow, triggers on push to `release/mozambique-*` tagged branches (or just on tag pushes matching `v*-mz`).
- Apply M5 curriculum import: `python manage.py import_mozambique_curriculum` against the Mozambique Postgres.
- Lock the v0.1.0-equivalent ACR image: `az acr repository update --image aitutor:v0.1.0-mz --delete-enabled false`.

**Tests:**

1. `curl https://aitutor-mozambique-app.<env-hash>.../health/ | jq` returns `{"version": "0.1.0-mz", "language": "pt-MZ"}` — extend the `/health/` endpoint to surface locale in M1.
2. Drive a 5-turn lesson via chrome-devtools-mcp against the Mozambique URL — confirm Portuguese throughout.
3. Inspect logs: `[simple_tutor] mode=GRADE intent=answer` markers appear; verify the system prompt sent to Claude contains "Respond in Mozambique Portuguese".
4. `pulumi destroy --stack mozambique` cleanly tears down (used at end of pilot if needed).

**Risks:**

- New Service-Principal RBAC grants on a new resource group — same pattern that bit us with the preview stack 2026-05-28; remember to grant BEFORE the first deploy.
- Database seeding can fail mid-stream over SSL on Azure managed Postgres — use `pg_dump --column-inserts` + retry-friendly loader (the pattern that worked for the preview seed).

**Reviewer checklist:**

- [ ] `LANGUAGE_CODE` env var on the Container App reads `pt-MZ` (verify via `az containerapp show`).
- [ ] No cross-pollination with the Seychelles `pixel` stack (resource group, DB, ACR all distinct).
- [ ] Mozambique users cannot accidentally access Seychelles curriculum (the deploys are physically separate — verify by checking each Container App's `DATABASE_URL`).

---

### M7 — Portuguese audio (optional for v1)

**Effort**: 0.5 day. **Blocks**: nothing — purely additive. **Depends on**: M1, M6.

**Deliverables:**

- `apps/tutoring/tts/elevenlabs.py`: when `settings.LANGUAGE_CODE == 'pt-MZ'`, use a Portuguese voice id (ElevenLabs has Multilingual v2 voices that handle European Portuguese well).
- `apps/tutoring/stt/...`: when `settings.LANGUAGE_CODE == 'pt-MZ'`, use Azure Speech model `pt-PT` (no `pt-MZ` model exists; Portugal Portuguese is the closest available).
- Voice + STT model picked by the locale, no UI change.

**Tests:**

1. Generate a sample tutor reply in Portuguese, play through the audio pipeline — sanity-check voice intelligibility.
2. Record a Portuguese answer via STT, confirm transcript accuracy on basic math vocabulary.
3. Existing English audio tests still pass at `LANGUAGE_CODE='en-us'`.

**Reviewer checklist:**

- [ ] No new third-party API costs beyond Multilingual v2.
- [ ] Audio quality good enough for pilot (>90% intelligibility on standard secondary-school vocab).

---

## Effort + sequencing summary

```
M1 — i18n bootstrap                  0.5 day      | START HERE
  ├── M2 — wrap user strings         1.5 day      | depends M1
  │     └── M3 — translate UI        1.0 day      | depends M2
  ├── M4 — tutor locale-aware        0.5 day      | depends M1; parallel with M2-M5
  └── M5 — curriculum import         2-4 day      | depends M1; parallel with M4 if path A
        └── M6 — Pulumi stack         1.0 day     | depends M5
              └── M7 — audio          0.5 day     | optional for v1
```

**Wall-clock minimum (text-only pilot, parallelized)**: ~3 days with one engineer, ~2 days with two.

---

## Handoff notes for the next Claude

1. **Read `RELEASING.md` first** if you don't have context on the deploy model. The v0.1.0 release in May established the tag → ACR-image → GitHub release pattern; M6 mirrors it for the Mozambique stack.

2. **The `mozambique/Grade 8 Materials/` directory is local-only** (not yet committed; gitignored). The next Claude needs to inspect those files to decide between M5 Path A vs Path B. If they're Portuguese — Path A. If English — Path B.

3. **Pulumi passphrase**: `ai-tutor` (set when the preview stack was created 2026-05-28; reuse for the Mozambique stack).

4. **Service principal for GHA**: `aitutor-github-actions-pixel` (appId: `d75a3030-52e8-4f5b-a9b7-ecf44783925d`). Grant Contributor + AcrPush on `aitutor-mozambique-rg` BEFORE the first GHA deploy (otherwise the workflow fails with "resource not found" — same trap the preview stack hit 2026-05-28).

5. **Branch reserved**: `feature/i18n-bootstrap` exists, tracks `origin/dev`, no commits yet. Use it for M1. Subsequent milestones get their own branches: `feature/i18n-wrap`, `feature/i18n-translate`, `feature/tutor-pt-mz`, `feature/curriculum-mozambique`, `infra/mozambique-stack`.

6. **Auto-memory rules to honor**:
   - `feedback_no_automated_prod_e2e.md` — automated chrome-devtools-mcp E2E is staging-only. The Mozambique deploy when live = treat as prod.
   - `feedback_django_template_comments.md` — `{# #}` is single-line only. Multi-line uses `{% comment %}…{% endcomment %}`. Shipped this bug 6× before; don't make it 7.
   - `feedback_consult_prompting_skills.md` — invoke `prompting-fundamentals-expert` + `claude-prompting-expert` before writing the M3 translation prompt or the M4 system-prompt addition. Non-negotiable per CLAUDE.md.

7. **Skills to consult per milestone**:
   - M1 — `django-expert`, `azure-cloud-expert` (Pulumi env var threading)
   - M2 — `django-expert`
   - M3 — `claude-prompting-expert` (translation prompt design), `prompting-fundamentals-expert`
   - M4 — `claude-prompting-expert`, `tutoring-engine-expert`
   - M5 — `codebase-architecture-expert` (curriculum hierarchy patterns), `claude-prompting-expert` (if Path B)
   - M6 — `azure-cloud-expert`, `cicd-expert`

8. **Cost ballpark for translation** (M3 + M5 Path B):
   - M3 UI: ~120 short strings, single Claude batch. ~$0.50 total.
   - M5 Path B curriculum: ~30 lessons × ~2 KB each + ~500 MCQs × ~200 tokens each. Estimate ~$30-50 in Opus tokens, depending on prompt overhead.
   - Pulumi + Azure resources for the Mozambique stack: ~$20-25/mo while live (matches staging).

9. **First commit on `feature/i18n-bootstrap` should be small and reviewable** — just M1's settings/middleware/JS-catalog changes + the `LANGUAGE_CODE` Pulumi plumbing. Don't bundle M2 into the same PR even if tempted; the templates-wrapping work has its own risk surface.

10. **If anything contradicts this plan** (e.g. the materials in `mozambique/Grade 8 Materials/` reveal pilot scope is Grade 6, not Grade 8), update this doc first, commit the update on `dev`, then revisit the milestones.

---

## What changed since the prior draft of this plan

- **Architectural decision flipped**: institution-level locale → platform-level locale (per Edward, 2026-05-31). All milestones simplified.
- **Removed**: `Institution.default_locale` field + migration. No schema change needed.
- **Removed**: custom locale middleware reading institution-from-user. Django's built-in `LocaleMiddleware` is sufficient.
- **Added**: M6 (Pulumi stack) as a first-class milestone — was implicit before.
- **Added**: Open Question #6 (tutor model for Mozambique).
- **Reorganized**: every milestone now has Deliverables, Tests, Risks, Reviewer Checklist sections — for handoff clarity.
- **Removed**: 5-PR table from the prior version (each milestone is its own PR now; tracked above).

Branch state at this plan's commit time: on `dev`, no uncommitted code changes beyond `mozambique/` untracked directory. `feature/i18n-bootstrap` branch exists, tracks `origin/dev`, no commits.
