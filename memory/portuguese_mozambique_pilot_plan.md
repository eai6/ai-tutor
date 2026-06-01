# Mozambique Pilot — Portuguese localization plan

**Status (2026-06-01 EOD)**: M1, M2, M3, M4, M5-prep, M6d SHIPPED on `feature/i18n-bootstrap` (12 commits, all pushed). Demo-ready locally — pt-mz student logged in via chrome-devtools-mcp gets Portuguese UI end-to-end + simple_tutor responds in pt-mz. **Remaining**: M5 (Edward uploads PT curriculum via dashboard), M6 deploy to staging, M7 audio (opt). M5 reshaped — see § "M5 — Edward uploads via dashboard" below.
**Trigger**: Paschal's Mozambique visit, week of 2026-06-01. Government has shared Grade 8 materials (sitting locally under `mozambique/Grade 8 Materials/`, currently gitignored).
**Branch in flight**: `feature/i18n-bootstrap` (12 commits, all on origin).
**Plan author**: Claude Opus 4.7 session 2026-05-29 → 2026-06-01 (Edward driving).
**Architecture reference**: `memory/multi_locale_architecture_research.md` — the locked decisions table at the top of that doc drives this plan.

---

## Shipped on `feature/i18n-bootstrap` (chronological)

| Commit | Milestone | Summary |
|---|---|---|
| `ed01689` | M1 | i18n bootstrap — `LANGUAGES`, `LOCALE_PATHS`, Django `LocaleMiddleware`, `/jsi18n/`, `/health/` surfaces locale |
| `523f616` | M4 | `Course.locale` + `StudentProfile.preferred_locale` + `Institution.default_locale` + `LocaleResolverMiddleware` + tutor engine reads `session.course.locale` + 35 tests |
| `14b7f54` | M2a | `base.html` + accounts auth templates wrapped in `{% trans %}` |
| `b7f7e68` | M2b | `chat_tutor` + `catalog` templates wrapped |
| `d7e55c3` | M2c | Python student-facing flash messages wrapped |
| `2eea90c` | M2d | audit script + coverage tests + 142 msgids in `pt_MZ` catalog |
| `4a40cbf` | M3 | `translate_po` command + 141/141 pt-mz translations populated via Claude Opus 4.7 |
| `a092662` | M5-prep | content gen locale-aware + **MCQ-ONLY** exit tickets + A/B/C/D rebalance (mcq_distribution.py) |
| `051cb4e` | M4 follow-up | chat_tutor view activates `course.locale` for in-session UI |
| `b8b4586` | M6d | drop `SIMPLE_TUTOR_ENGINE` env-var dispatch — simple_tutor is the only respond engine. Legacy `ConversationalTutor.respond()` removed from the dispatch path; 5 unported call sites (start/resume/review/difficulty/bank) still on legacy, tracked as Phase 2 follow-up |

**Verified live in chrome (en-us → pt-mz → en-us flip cycle on local dev)**:
- Landing page + login + register + settings + catalog: all render Portuguese with `tu` informal form.
- Chat tutor UI shell renders Portuguese.
- Fresh simple-tutor turn (Claude Opus 4.7) responds in Portuguese with proper Mozambique register ("vais aprender", "pensa nisto", "és um operador").
- No regression: revert to `en-us` → all pages back to English.

---

## Decisions locked 2026-06-01 (Edward, post-architecture-review)

| # | Decision |
|---|---|
| 1 | **Curriculum source: government-supplied Portuguese materials.** Files under `mozambique/Grade 8 Materials/` are the source of truth. No translation pipeline. |
| 2 | **Subject for v1: Grade 8 Biology** (one subject). Keeps M5 scope small enough to land before Paschal's visit. |
| 3 | **Audio: opportunistic.** If ElevenLabs has a usable Portuguese voice, ship M7. If not, skip — text-only is acceptable. |
| 4 | **No native-speaker reviewer for v1.** Edward will source one after the demo; corrections land in a follow-up commit. |
| 5 | **Deployment: single unified deployment, not a per-country stack.** Both English (Seychelles) and Portuguese (Mozambique) coexist on the same Container App. Per-course locale field drives language routing. **No `LANGUAGE_CODE` env var flipping.** |
| 6 | **Tutor model: Opus 4.7** (matches prod / staging). No change. |
| 7 | **Locale string format: hyphenated lowercase** (`'pt-mz'`, `'en-us'`). Matches Django's `LANGUAGE_CODE` convention. The `.po` directory naming (`pt_MZ`) is a separate gettext convention and not in conflict. |
| 8 | **Scope: full unified-architecture from day one.** No Phase 1.5 deferrals. All three locale fields (`Course.locale`, `StudentProfile.preferred_locale`, `Institution.default_locale`), the full `LocaleResolverMiddleware`, and full UI translation (catalog + dashboard + accounts + chat) ship in the demo. |

---

## Architecture (revised 2026-06-01)

**Locale lives on the `Course` row** as the primary signal, with student + institution as fallbacks. No env vars, no per-deploy locale.

Why course-level: a Seychelles school may eventually offer French courses alongside English; a Mozambique school may offer multiple Portuguese subjects. Course is the smallest stable unit of localization in this domain — same conclusion Duolingo reached.

**What we ship for the Paschal demo**:

1. **`Course.locale`** (`CharField(max_length=10, default='en-us')`) — the language of THIS course's curriculum. Drives tutor system prompt + UI when a student is in this course's chat session.
2. **`StudentProfile.preferred_locale`** (`CharField(max_length=10, null=True, blank=True)`) — what UI language the student sees outside chat sessions. Optional override.
3. **`Institution.default_locale`** (`CharField(max_length=10, default='en-us')`) — institutional fallback when student preference is unset.
4. **`LocaleResolverMiddleware`** — applies the resolution chain to ALL views (catalog, dashboard, accounts, chat). Chain: `course.locale (in-session) > student.preferred_locale > institution.default_locale > settings.LANGUAGE_CODE`.
5. **Tutor engine reads `self.session.course.locale`** instead of `settings.LANGUAGE_CODE`. The system prompt gets a per-turn locale instruction appended when the course is non-English.
6. **Full UI translation**: catalog, dashboard, accounts, chat tutor — all student-facing strings wrapped in `{% trans %}` / `_()` and translated to pt-mz.
7. **Mozambique curriculum import sets `Course.locale='pt-mz'`** on the new Course rows. Existing Seychelles rows backfill to `'en-us'`.

**What this kills from the prior draft**:

- ❌ No `LANGUAGE_CODE=pt-mz` env var on staging. Staging stays at `en-us` default.
- ❌ No Pulumi config edit. No `aitutor:language-code` plumbing.
- ❌ No "staging is dual-purpose during demo week" coordination problem.
- ❌ No "real Mozambique Pulumi stack" follow-up unless latency / data residency demands it later.
- ❌ No "Phase 1.5" deferral list. Everything ships together.

**What this gains**:

- One codebase, one deploy, today AND for future pilots (Tanzania, Rwanda, ...). Each new country = one curriculum import command. No infra changes.
- Backward-compatible — existing Seychelles flow unaffected.
- Aligned with Duolingo / Notion / Shopify industry pattern (course / user / storefront as the unit of localization).
- The whole platform (not just chat) renders in the right language end-to-end.

---

## What needs to change, in three layers

All three layers in scope for v1.

| Layer | What it covers | Where the text lives | How translated |
|---|---|---|---|
| **A. App UI** (full) | Catalog, dashboard, accounts, chat tutor — all student-facing strings | `templates/**/*.html`, Python `_(…)` calls, JS via `JavaScriptCatalog` | One-shot Claude batch via `manage.py translate_po`, no human review for v1 |
| **B. LLM tutor responses** | Real-time tutor text in chat (hint ladders, affirmations, grading feedback) | Generated per-turn by the LLM | System prompt reads `course.locale`, appends "Respond in pt-mz Mozambique register" per turn when course is non-English |
| **C. Curriculum content** | Lesson titles, teaching scripts, objectives, MCQ stems + options + explanations | DB rows under `Course → Unit → Lesson → LessonStep → ExitTicketQuestion`, scoped by `Course.locale` | **Government-supplied Portuguese files** — import as-is, no translation step |

C is the gating concern for demo readiness — without imported Portuguese curriculum, there's nothing to teach in Portuguese. A and B are independent.

---

## Milestone structure

Seven milestones. Critical path for Paschal's visit: **M1 → M2 → M3 → M4 → M5 → M6**. M7 (audio) parallelizes with M2-M6 once we confirm ElevenLabs has a usable voice.

M4 is the architecture milestone (all three fields + middleware + engine refactor). M2 is the full-UI translation wrap.

### M1 — i18n bootstrap (foundation)

**Effort**: 0.3 day. **Blocks**: M2, M3, M4. **Branch**: `feature/i18n-bootstrap`.

**Deliverables:**

- `config/settings.py`:
  - Add `LANGUAGES = [('en-us', 'English'), ('pt-mz', 'Português (Moçambique)')]`
  - Add `LOCALE_PATHS = [BASE_DIR / 'locale']`
  - Add `'django.middleware.locale.LocaleMiddleware'` to `MIDDLEWARE` (after `SessionMiddleware`, before `CommonMiddleware`)
  - `LANGUAGE_CODE` stays `'en-us'` — the global fallback. Resolution chain in M4 overrides it per-request.
- `config/urls.py`: mount Django's `JavaScriptCatalog` view at `/jsi18n/`
- `locale/pt_MZ/LC_MESSAGES/django.po` + `djangojs.po`: empty skeleton files (`manage.py makemessages` will populate)
- Extend `/health/` (`apps/dashboard/views_health.py`) to surface the active `LANGUAGE_CODE` — debugging aid.

**Non-deliverables (intentional, NOT in M1)**:

- ❌ Pulumi env-var threading for `LANGUAGE_CODE`. Removed.
- ❌ `aitutor:language-code` Pulumi config key. Removed.

**Tests:**

1. `python manage.py check` exits 0.
2. New test `apps/accounts/tests/test_i18n_bootstrap.py`:
   - Verifies `LANGUAGES` contains both `'en-us'` and `'pt-mz'`.
   - Verifies `LOCALE_PATHS` resolves to a real directory.
   - Verifies `/jsi18n/` returns HTTP 200 and a JavaScript content type.
3. Existing test baseline still passes.

**Reviewer checklist:**

- [ ] `LocaleMiddleware` placed correctly in MIDDLEWARE order (after `SessionMiddleware`).
- [ ] No DB migration in this PR (the model fields land in M4).
- [ ] No Pulumi changes in this PR.

---

### M2 — Wrap user-facing strings (full UI)

**Effort**: 1.5 day. **Blocks**: M3. **Depends on**: M1.

Scope: all student-facing UI — catalog, dashboard, accounts, chat tutor. Skip admin-only and teacher-only screens for v1 (they're internal; English is fine).

**Deliverables:**

- Wrap strings in `{% trans %}` / `{% blocktrans %}` across:
  - `templates/tutoring/chat_tutor.html` (message placeholder, send button, "next lesson" CTA, exit-ticket controls, completion modal)
  - `templates/tutoring/catalog.html` (course cards, headers, filters)
  - `templates/dashboard/` student-facing pages (progress views, lesson navigation)
  - `templates/accounts/` (login, register, password reset, profile)
  - `templates/base.html` and the navigation shell
- Wrap Python user-facing strings in `_(…)` from `gettext_lazy`:
  - Form labels and help text (`apps/accounts/forms.py`, `apps/tutoring/forms.py` if exists)
  - `messages.success/error` calls in student-facing views
  - `ValidationError` messages
- Generate `.po` skeletons:
  ```
  python manage.py makemessages -l pt_MZ
  python manage.py makemessages -l pt_MZ -d djangojs
  ```
- New script `scripts/list_unwrapped_strings.py` — audit script that prints any student-facing string NOT wrapped.

**Out of scope for v1**: admin UI, teacher-only dashboard internals, content-generation tooling. These stay English; revisit when Mozambique teachers come online.

**Tests:**

1. `python manage.py makemessages -l pt_MZ --dry-run` reports no unwrapped strings in target templates.
2. New test `apps/accounts/tests/test_i18n_coverage.py`:
   - Renders every student-facing template at `LANGUAGE_CODE='en-us'` and `'pt-mz'` (with empty `.po` → identity translation).
   - Asserts no Django template syntax errors on either pass.
3. Visual regression: chrome-devtools screenshots of all student-facing pages at both locales (with empty .po, identical to today's English).
4. `pytest apps/ tests/` baseline still passes.

**Risks:**

- Mis-wrapped `{% trans %}` block with variables → runtime error. Use `{% blocktrans %}` for any string with `{{ var }}`.
- `gettext_lazy` is required in model / form-field labels (evaluating at import time with `gettext` causes the wrong locale to bake in).

**Reviewer checklist:**

- [ ] `{% load i18n %}` at the top of every wrapped template.
- [ ] `gettext_lazy` (not `gettext`) for model / form-field labels.
- [ ] No raw user-facing English strings in scoped templates (verified by the audit script).

---

### M3 — Translate UI strings into pt-mz

**Effort**: 1 day. **Blocks**: nothing — M4 and M5 parallelize. **Depends on**: M2.

**Deliverables:**

- New management command `apps/accounts/management/commands/translate_po.py`:
  - Reads `locale/pt_MZ/LC_MESSAGES/django.po`, batches msgids, sends to Claude Opus 4.7 with the Mozambique-register prompt below.
  - Writes msgstrs back. Idempotent — only translates rows where msgstr is empty.
  - Preserves variable placeholders (`{username}`, `%(var)s`) — assert in the command, fail loud if a placeholder is dropped.
  - Logs every translation pair for later spot-review.
- Run + compile:
  ```
  python manage.py translate_po --locale pt_MZ
  python manage.py compilemessages
  ```
- Commit `locale/pt_MZ/LC_MESSAGES/django.mo` and `djangojs.mo`.

**Before writing the translation prompt**: invoke `prompting-fundamentals-expert` then `claude-prompting-expert`. Non-negotiable per `feedback_consult_prompting_skills.md`. Lock in the prompt:

- Register: `tu` informal (students, age 13–14).
- Spelling: post-1990 Acordo Ortográfico (Mozambique signed on).
- Preserve `{varname}` and `%(var)s` placeholders verbatim.
- Mozambique-specific vocabulary: defer to Claude's pt-mz default; spot-check 20 samples.

**Tests:**

1. New test `apps/accounts/tests/test_i18n_translations_present.py`: counts msgids vs translated msgstrs — fails if coverage < 90%.
2. Spin up local with a pt-mz course (M4 + M5 prerequisites); verify catalog + dashboard + chat render in Portuguese for a Mozambique student.
3. Spot-check 20 randomly-sampled translations for variable-placeholder preservation.

**Reviewer checklist:**

- [ ] All variable placeholders preserved (automated in the command).
- [ ] Register consistent (`tu` throughout — sample 10).
- [ ] No raw "TODO" or "TRANSLATE_ME" markers left in `.po`.

---

### M4 — Locale fields + middleware + tutor engine

**Effort**: 1.5 day. **Blocks**: M5, M6. **Depends on**: M1.

The architecture milestone. Adds all three locale fields, the resolver middleware, and the engine refactor in one PR.

**Deliverables:**

**a) Model fields + migrations:**

- `apps/curriculum/models.py::Course`: add `locale = CharField(max_length=10, default='en-us')`.
- `apps/accounts/models.py::StudentProfile`: add `preferred_locale = CharField(max_length=10, null=True, blank=True)`.
- `apps/accounts/models.py::Institution`: add `default_locale = CharField(max_length=10, default='en-us')`.
- One migration per model OR a single combined migration — whichever the next Claude judges cleaner. `RunPython` data migration backfills:
  - All existing `Course` rows → `locale='en-us'`.
  - All existing `Institution` rows → `default_locale='en-us'`.
  - All existing `StudentProfile` rows → `preferred_locale=None` (the default).
- `apps/curriculum/admin.py` and `apps/accounts/admin.py`: expose the new fields in the relevant admin views.

**b) LocaleResolverMiddleware:**

- New file `apps/accounts/middleware/locale_resolver.py`:
  ```
  Resolution chain (first non-empty wins):
    1. course.locale  — if request resolves to a tutoring session view, read from session.course
    2. request.user.studentprofile.preferred_locale
    3. request.user.studentprofile.institution.default_locale
    4. settings.LANGUAGE_CODE
  ```
- Implemented as `django.utils.deprecation.MiddlewareMixin`-style class.
- Calls `django.utils.translation.activate(resolved_locale)` in `process_request`, `translation.deactivate()` in `process_response` (with `try/finally` semantics).
- Registered in `MIDDLEWARE` **after** `AuthenticationMiddleware` (needs `request.user`) and **before** Django's `LocaleMiddleware` (so we set the language before Django's middleware would).
- The "course.locale wins" branch needs the middleware to inspect the URL conf — for tutoring-session URLs (`/tutoring/session/<id>/...`), look up `TutorSession.course.locale`. Cache the lookup per-request.

**c) Tutor engine:**

- `apps/tutoring/simple_tutor/engine.py::respond`: read `locale = self.session.course.locale` (cached on the engine instance for the session).
- `apps/tutoring/simple_tutor/prompts.py`: when building the system prompt, append a per-turn locale block when `locale != 'en-us'`:
  ```
  <locale>
  Respond to the student in Mozambique Portuguese (pt-mz register).
  Use 'tu' informal addressing. Use post-1990 Acordo Ortográfico spelling.
  </locale>
  ```
  - Wrap in an XML tag — fits the existing system-prompt XML structure per `claude-prompting-expert` conventions.
  - Per-turn injection is "free" — the two-call loop already re-sends the full system block every turn.
- `apps/tutoring/simple_tutor/intent.py`: refactor pattern lists into `LANG_PATTERNS: dict[str, list[re.Pattern]]`. Add `pt-mz`:
  - `non_engagement`: `não sei`, `não percebi`, `obrigado/obrigada`, `não consigo`, `é difícil`, `ok`, `sim`, `não`
  - `clarification`: `o que significa`, `não percebi`, `podes explicar`, `como`, `pode repetir`, `não compreendo`
  - `pushback`: `acho que queres dizer`, `acho que tu queres dizer`, `mas e se`, `não está certo`
- Locale selection in `intent.py`: read from `session.course.locale` at classification time, fall back to English patterns if unknown.

**Before writing the locale-prompt block + the intent patterns**: invoke `prompting-fundamentals-expert` then `claude-prompting-expert`. Non-negotiable.

**Tests:**

1. New test `apps/curriculum/tests/test_course_locale_field.py`:
   - Migration creates `Course.locale` with default `'en-us'`.
   - Backfill leaves no Course row with empty locale.
   - New Course rows pick up the default.
2. New test `apps/accounts/tests/test_locale_fields.py`:
   - `StudentProfile.preferred_locale` nullable; new profiles default to None.
   - `Institution.default_locale` defaults to `'en-us'`; backfill set all existing institutions.
3. New test `apps/accounts/tests/test_locale_resolver_middleware.py`:
   - Anonymous request → falls through to `settings.LANGUAGE_CODE`.
   - Logged-in student with `preferred_locale='pt-mz'` outside a session → middleware activates pt-mz.
   - Logged-in student inside a `Course.locale='pt-mz'` session → middleware activates pt-mz REGARDLESS of preferred_locale (course wins).
   - Logged-in student in en-us session right after a pt-mz session → middleware activates en-us cleanly (no leak).
   - Student with `preferred_locale=None`, institution `default_locale='pt-mz'` → middleware activates pt-mz.
4. New test `apps/tutoring/tests/test_engine_course_locale.py`:
   - Engine against an `'en-us'` course: system prompt contains NO locale block.
   - Engine against a `'pt-mz'` course: system prompt contains the locale block.
5. `apps/tutoring/simple_tutor/tests/test_intent.py`: extended with Portuguese cases for each intent label.
6. 10–15 Portuguese eval scenarios under `evals/dataset/portuguese/` — translate the highest-leverage 10 English Biology scenarios.
7. Existing English eval (78/80 baseline) still passes — confirms no regression.
8. Manual chrome-devtools E2E on local: log in as Mozambique demo student, enter a pt-mz Biology lesson, verify catalog → lesson → chat all render Portuguese AND tutor responds Portuguese.

**Risks:**

- Middleware `translation.activate()` without `deactivate()` leaks the locale into subsequent requests on the same worker thread. **Mitigation**: `try/finally` in the middleware, plus test 3.4 above.
- The "course.locale wins" branch requires URL inspection or session lookup in middleware — fragile if URL conf changes. **Mitigation**: cache on `request._cached_session_locale` keyed off `session_id`; one query per request max.
- Intent classifier misclassification on Portuguese ("não sei" → answer_or_other) → tutor force-grades. **Mitigation**: the 10–15 eval scenarios MUST cover the high-traffic intent patterns.
- The `meta_reasoning_leak` regex in the LLM judge is English-only. Document if it under-fires on pt-mz scenarios; tighten in a follow-up if needed.

**Reviewer checklist:**

- [ ] All three model fields land in a single coherent migration story (one PR, ideally one migration file per app touched).
- [ ] All Course / Institution rows have locale set post-migration (verify in Django shell).
- [ ] Middleware respects resolution chain in all 5 cases above.
- [ ] No locale bleed between requests on the same worker thread.
- [ ] `LANG_PATTERNS` falls back gracefully on unknown locales.
- [ ] System-prompt locale block only appears when course is non-English.
- [ ] Existing English eval at 78/80 baseline still passes.

---

### M5 — Edward uploads Mozambique Grade 8 Biology via the dashboard

**Effort**: ~30 min of teacher time on the upload UI, plus the platform's normal content-generation cycle. **Blocks**: M6 verification. **Depends on**: M5-prep (shipped).

**Reshaped 2026-06-01**: this milestone was originally a custom importer command. Dropped in favour of using the existing teacher-dashboard curriculum upload flow. Rationale:

- The platform already has a battle-tested upload path (`TeachingMaterialUpload` model → `content_generator.py` parses + generates `LessonStep`s + MCQs via LLM). Same path that built every Seychelles course. Writing a custom Mozambique importer would have duplicated all that logic with new failure modes to debug.
- M5-prep (commit `a092662`) made `content_generator.py` + the exit-ticket generator locale-aware. They now read `course.locale` and inject a `<locale>` instruction into the LLM system prompt. Uploading a PT curriculum + selecting `locale='pt-mz'` on the course → generated lessons + MCQs come back in Portuguese.
- The course-creation form on the dashboard exposes `locale` as a dropdown (`choices=settings.LANGUAGES` from M4) — labels render as "🇸🇨 Seychelles — English" / "🇲🇿 Moçambique — Português".

**What Edward does** (teacher-driven, no engineering work):

1. Visit the teacher dashboard → "Create a course".
2. Pick **🇲🇿 Moçambique — Português** in the language dropdown.
3. Set subject = Biology, grade = 8, title = "Biologia - 8ª Classe", institution = the Mozambique pilot institution (create one with `default_locale='pt-mz'` if not exists).
4. Upload the Grade 8 Biology materials from `mozambique/Grade 8 Materials/` (PDF / DOCX whatever Edward has).
5. Wait for content generation to finish (background job; takes ~5-10 min depending on lesson count).
6. Optional: run `python manage.py generate_exit_tickets --course <id>` if exit tickets didn't auto-generate. The MCQ-only prompt with A/B/C/D rebalance (M5-prep) fires automatically.

**What the platform does end-to-end** (verified by M5-prep changes):

- Parses the upload via the existing `content_generator.py` pipeline.
- For each generated lesson/step/MCQ: sends a system prompt that includes the M5-prep `<locale>` block → LLM emits all content in pt-mz with `tu` informal addressing + post-1990 Acordo Ortográfico spelling.
- For exit tickets: the new MCQ-only prompt (35 questions, no fill_in_blank / matching / short_answer) generates a 35-MCQ bank.
- Post-generation: `apps/curriculum/mcq_distribution.py::audit_distribution` tallies the correct-letter spread. If any letter > 35% of the bank, `rebalance_distribution()` deterministically permutes which option holds the correct answer so the bank is uniform. Educationally identical; just labels shuffled.

**Reviewer checklist** (Edward does this from the dashboard):

- [ ] Course shows `locale='pt-mz'` (visible as "🇲🇿 Moçambique — Português" badge / dropdown).
- [ ] All generated lessons read in Portuguese. Spot-check 5 lessons end-to-end.
- [ ] Generated MCQs are in Portuguese (stems + options + explanations).
- [ ] Exit ticket bank size = 35 MCQs (no other types).
- [ ] Spot-check 3 MCQs: A/B/C/D distribution looks uniform — no obvious B-bias.
- [ ] Logging confirms M5-prep firing: grep staging logs for `[mcq distribution]` after running `generate_exit_tickets` against the new course.

**Risks**:

- The locale block prompt was tuned for pt-mz specifically. Other languages would need their own branch in `apps/curriculum/locale_prompts.py::locale_instruction_block`. Right now only en-us and pt-mz are supported; unknown locales fall back to a generic instruction with a WARNING log.
- If the source materials are in English (vs Portuguese), the parser will store English text in the DB and the locale block won't fix that — the LLM only handles new generation, not transcription. Sanity-check the first lesson's `teacher_script` is actually in Portuguese.

**Old custom-importer plan**: the deleted contents of this section are preserved in commit history (pre-`a092662`). If the dashboard upload path proves insufficient, look there.

---

### M6 — Deploy to staging + verify

**Effort**: 0.5 day. **Blocks**: Paschal's demo. **Depends on**: M1–M5.

Normal dev → staging deploy via the existing GHA workflow. No env var flips, no Pulumi edits.

**Deliverables:**

1. Merge M1–M5 PRs into `dev`. GHA `deploy-staging.yml` runs automatically.
2. Run M5's importer against the staging Postgres:
   ```bash
   DATABASE_URL=<staging_db_url> python manage.py import_mozambique_biology
   ```
3. Create a Mozambique demo student account on staging tagged to the `mozambique-pilot` institution. Set `StudentProfile.preferred_locale='pt-mz'` so dashboard renders Portuguese.
4. **No Seychelles disruption**. Staging keeps serving English for Seychelles QA accounts. Portuguese only appears for users in the Mozambique institution / Mozambique courses.

**Tests:**

1. `curl https://aitutor-staging-app.<env-hash>.../health/ | jq` returns the expected version. `LANGUAGE_CODE` stays `en-us` globally; locale resolution happens per-request.
2. Drive a 5-turn Biology lesson via chrome-devtools-mcp on staging:
   - Log in as Mozambique demo student.
   - Verify dashboard renders Portuguese.
   - Open the Mozambique Grade 8 Biology course.
   - Verify catalog cards in Portuguese.
   - Verify chat UI in Portuguese.
   - Verify tutor responds in Portuguese throughout.
   - Verify exit ticket questions in Portuguese.
3. Cross-check: log in as a Seychelles teacher account on the same staging deploy.
   - Verify English UI throughout.
   - Verify Seychelles courses unaffected.
   - **Critical test — proves locale isolation works.**
4. Inspect staging logs: verify the system prompt sent to Claude contains "Respond in Mozambique Portuguese" for the pt-mz session and DOES NOT for the en-us session.

**Risks:**

- Translation `.mo` files need to be in the Docker image. Check `Dockerfile` includes `RUN python manage.py compilemessages` (or commit the `.mo` files).
- `translation.activate()` leak across requests is the highest-risk bug — exercised by test 3.

**Reviewer checklist:**

- [ ] Mozambique import ran successfully against staging Postgres (row count matches local import).
- [ ] Demo student account exists on staging with access to the Mozambique course only.
- [ ] Seychelles QA flow on staging unaffected.

---

### M7 — Portuguese audio (opportunistic)

**Effort**: 0.5 day. **Blocks**: nothing. **Depends on**: M4.

**Phase 0 — feasibility check first:**

- Confirm ElevenLabs Multilingual v2 has a usable Portuguese voice (pt-PT closer to Mozambique register than pt-BR; no pt-MZ voice exists).
- Generate a 30-second sample of a tutor reply in Portuguese, play back.
- Decision: voice quality acceptable for 13–14-year-olds? Yes → proceed. No → skip M7, document the finding in the commit message.

**Deliverables (if feasibility passes):**

- `apps/tutoring/tts/elevenlabs.py`: when `course.locale == 'pt-mz'`, use the chosen Portuguese voice id.
- `apps/tutoring/stt/...`: when `course.locale == 'pt-mz'`, use Azure Speech model `pt-PT`.
- Voice + STT picked by `course.locale`, no UI change.

**Tests:**

1. Generate a sample Portuguese tutor reply, play through the audio pipeline.
2. Record a Portuguese answer via STT, confirm transcript accuracy on Grade 8 Biology vocab (fotossíntese, célula, ecossistema, mitose, ADN).
3. Existing English audio tests still pass at `course.locale == 'en-us'`.

**Reviewer checklist:**

- [ ] No new third-party API costs beyond Multilingual v2 + Azure Speech.
- [ ] Audio quality ≥ 90% intelligibility on Grade 8 Biology vocab.
- [ ] Feasibility check result documented in commit message.

---

## Effort + sequencing summary

```
M1 — i18n bootstrap                    0.3 day   | START HERE
  ├── M2 — wrap full UI strings        1.5 day   | depends M1
  │     └── M3 — translate to pt-mz    1.0 day   | depends M2
  └── M4 — Locale fields + middleware  1.5 day   | depends M1; parallel with M2/M3
        └── M5 — curriculum import     2.0 day   | depends M4
              └── M6 — deploy staging  0.5 day   | depends M1-M5
                    └── M7 — audio     0.5 day   | optional; depends M4
```

**Wall-clock minimum** (single engineer, no audio): **~6.3 days serial, ~4.0 days parallelized**.

vs the prior "ship minimum + defer Phase 1.5" draft (~2.8 days): +1.2 days to ship the full unified architecture in one PR series instead of two. Worth it because there's no second migration cycle, no second deploy, and Paschal sees the polished end-to-end experience.

If timeline is tight, the recoverable cut is M2/M3 scope (chat shell only for v1, full UI as a Phase 1.5 follow-up) — would shave ~1 day. Edward chose against this cut on 2026-06-01; capture it here in case of replan.

---

## Optional future work (post-demo)

Not required for the Paschal demo. Track but don't block:

| Item | Effort | Trigger |
|---|---|---|
| Native-speaker translation review | varies | Edward sources a Mozambique educator post-demo; corrections land in `.po` |
| Admin UI translation | 0.5 day | When Mozambique teachers come online and need to author content |
| Locale switcher in user settings | 0.3 day | If students want to override their preferred_locale via the UI |
| Per-region Pulumi stack | 1 day | Only if latency / data residency / compliance demands it. Not for language. |

---

## Handoff notes for the next Claude

1. **Read `memory/multi_locale_architecture_research.md` first** — the locked decisions table at the top is the architecture source of truth. This plan operationalizes it.

2. **Inspect `mozambique/Grade 8 Materials/` BEFORE writing M5.** `ls -la` and `file *` first; read 2–3 representative files end-to-end second; write the importer third. Verify Biology is in scope (vs Ciências bundled).

3. **Pulumi passphrase**: `EdwardAmoah(eai6)`. Likely not needed — no Pulumi changes anywhere in M1–M7.

4. **Auto-memory rules to honor**:
   - `feedback_no_automated_prod_e2e.md` — automated chrome-devtools-mcp E2E is staging-only.
   - `feedback_django_template_comments.md` — `{# #}` is single-line only. Multi-line uses `{% comment %}…{% endcomment %}`. Shipped this bug 6× before.
   - `feedback_consult_prompting_skills.md` — invoke `prompting-fundamentals-expert` + `claude-prompting-expert` before writing M3's translation prompt or M4's system-prompt locale block. Non-negotiable per CLAUDE.md.

5. **Skills to consult per milestone**:
   - M1 — `django-expert`
   - M2 — `django-expert`
   - M3 — `claude-prompting-expert`, `prompting-fundamentals-expert`
   - M4 — `claude-prompting-expert`, `tutoring-engine-expert`, `django-expert` (middleware + migration patterns)
   - M5 — `codebase-architecture-expert` (curriculum hierarchy); also `claude-prompting-expert` if MCQs need generating
   - M6 — `azure-cloud-expert` (only if a deploy issue surfaces)

6. **Cost ballpark**:
   - M3 translation: ~120 full-UI strings, single Claude batch. ~$0.50 total.
   - M5 MCQ generation (if source files lack MCQs): Grade 8 Biology, ~8–12 units × ~10–20 MCQs. ~$5–15 in Opus tokens.
   - M7 audio (optional): ElevenLabs Multilingual v2 ~$0.15 per 1k chars. ~$0.75/lesson.

7. **First commit on `feature/i18n-bootstrap`** should be M1 only — minimal, reviewable. Don't bundle M2 into the same PR.

8. **The "Seychelles QA on staging stays alive" test in M6 is the critical correctness signal** for the unified-architecture design. If that fails, locale is leaking and the deploy is not safe.

9. **If anything contradicts this plan** (e.g. the Grade 8 Materials directory turns out to be English, or has no Biology unit), update this doc first, commit the update on `dev`, then revisit affected milestones.

---

## What changed vs the 2026-05-31 draft of this plan

- **Architecture flipped from "platform-level via env var" → "course-level via `Course.locale` field"**. Driven by `memory/multi_locale_architecture_research.md` and Edward's review.
- **Subject swapped**: Grade 8 Science → Grade 8 Biology (Edward's update).
- **Phase 1.5 deferrals removed.** All three locale fields, full middleware, and full UI translation ship in the demo PR series. Edward's call ("we are doing everything now").
- **M2 scope expanded** from chat-shell-only → catalog + dashboard + accounts + chat.
- **M3 scope expanded** accordingly (~120 strings vs ~50).
- **M4 scope expanded** to include `StudentProfile.preferred_locale`, `Institution.default_locale`, and the full `LocaleResolverMiddleware` chain.
- **M5 swapped** Science → Biology, Ciências → Biologia.
- **M6 reshaped** from "flip staging `LANGUAGE_CODE`" → "normal dev → staging deploy".
- **Locale string format locked**: `'pt-mz'` / `'en-us'` (hyphenated lowercase).
- **"Post-demo: real Mozambique stack" section removed.** Single-deploy is the destination.
- **Wall-clock**: ~4.0 days parallelized (up from ~2.8 prior, ~2.5 in the original draft). +1.2 days buys the full unified architecture in one PR series — no second migration, no second deploy.

Branch state at this plan's commit time: on `dev`, no uncommitted code changes beyond `mozambique/` untracked directory and the two locked memory files. `feature/i18n-bootstrap` branch exists, tracks `origin/dev`, no commits.
