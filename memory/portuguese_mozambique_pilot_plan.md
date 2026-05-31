# Mozambique Pilot — Portuguese localization plan

**Status**: planning — no code written. Ready for the next Claude to pick up.
**Trigger**: Paschal's Mozambique visit, week of 2026-06-01. Government has shared Grade 8 materials (sitting locally under `mozambique/Grade 8 Materials/`, currently gitignored).
**Branch reserved for the i18n work**: `feature/i18n-bootstrap` (created 2026-05-31, no commits yet; tracks `origin/dev`).
**Plan author**: Claude Opus 4.7 session 2026-05-29 → 2026-05-31 (Edward driving).

---

## Decisions locked 2026-05-31 (Edward, post-review)

These were open questions in the prior draft. Now closed.

| # | Decision |
|---|---|
| 1 | **Curriculum source: government-supplied Portuguese materials** (Path A in M5). No translation pipeline needed for the demo. The files under `mozambique/Grade 8 Materials/` are the source of truth. |
| 2 | **Subject for v1: Grade 8 Science** (one subject, not the full grade). Keeps M5 scope small enough to land before Paschal's visit. |
| 3 | **Audio: opportunistic.** If ElevenLabs has a usable Portuguese voice, ship M7. If not, skip — text-only is acceptable. |
| 4 | **No native-speaker reviewer for v1.** Edward will source one after the demo; corrections land in a follow-up commit. |
| 5 | **No new Pulumi stack for the demo. Reuse the existing `staging` environment.** This is the major architectural change vs the prior draft — see "Deployment approach" below. |
| 6 | **Tutor model: Opus 4.7** (matches prod / staging). No change. |

---

## Architectural decision (revised 2026-05-31)

**Long-term intent**: locale is a platform-level setting (`LANGUAGE_CODE` env var per Pulumi stack). Each pilot country gets its own deployment. This stays the goal for a real Mozambique pilot rollout.

**For the Paschal demo, we short-cut**: reuse the existing `staging` environment with `LANGUAGE_CODE=pt-MZ`. Don't provision new Azure infra yet.

What this means in practice:

- The `staging` Container App's `LANGUAGE_CODE` env var flips to `pt-MZ` for the demo window.
- Staging's Postgres carries **both** the Seychelles seed data (from the May seed) AND the Mozambique Grade 8 Science curriculum, scoped by `Institution` so they don't pollute each other.
- Tutor + UI render in Portuguese because `LANGUAGE_CODE=pt-MZ`. The Seychelles English data in staging is still in the DB but won't surface in the UI for Mozambique demo students.
- After the demo, if Mozambique becomes a real pilot, **then** provision the dedicated stack (deferred work — sketched at the bottom under "Post-demo: real Mozambique stack").

Why `Institution`-scoping still matters even with one shared deploy: staging will physically contain both English Seychelles courses and Portuguese Mozambique courses. Without an `Institution` filter (or equivalent scope) on the curriculum queries, a Mozambique demo student could see Seychelles English content. The platform already has multi-tenancy (`Q(institution=inst) | Q(institution__isnull=True)`) — we lean on it.

**Trade-offs the user should know about**:

- Staging becomes dual-purpose during the demo window. If Roy or another teammate needs staging for Seychelles QA at the same time, they get Portuguese UI — coordinate timing.
- Switching `LANGUAGE_CODE` back to `en-us` post-demo restores the Seychelles staging experience; the Portuguese data stays in the DB but won't render. Clean it up later if needed.
- This sets a precedent: "platform-level locale" was supposed to mean one-deploy-one-locale. We're now planning to flip it mid-life. Treat as demo-only; for a real Mozambique pilot, provision the dedicated stack.

---

## What needs to change, in three layers

| Layer | What it covers | Where the text lives | How translated |
|---|---|---|---|
| **A. App UI** | Buttons, headers, banners, error messages, form labels, JS-rendered strings | `templates/*.html`, Python `_(…)` calls, JS via `JavaScriptCatalog` | One-shot Claude batch via `manage.py translate_po`, no human review for v1 |
| **B. LLM tutor responses** | Real-time tutor text in chat (hint ladders, affirmations, exit-ticket grading feedback) | Generated per-turn by the LLM | System prompt reads `settings.LANGUAGE_CODE`, appends "Respond in pt-MZ Mozambique register" when non-English |
| **C. Curriculum content** | Lesson titles, teaching scripts, objectives, MCQ stems + options + explanations | DB rows under `Course → Unit → Lesson → LessonStep → ExitTicketQuestion` | **Government-supplied Portuguese files** (`mozambique/Grade 8 Materials/`) — import as-is, no translation step |

A + B + C are independent workstreams. They merge separately. C is the gating concern for demo readiness — without Portuguese curriculum imported into staging, there's nothing to teach in Portuguese.

---

## Milestone structure

Six milestones (was seven; old M6 "new Pulumi stack" became a deferred post-demo follow-up).

**M1–M3** are i18n plumbing (English app → Portuguese-ready). **M4** is the tutor's locale awareness. **M5** is curriculum import (Path A only). **M6** is "flip staging to Portuguese demo mode". **M7** is audio (opportunistic — only if ElevenLabs supports pt).

Critical path for Paschal's visit: **M1 → M2 → M3 → M5 → M6**. M4 parallelizes with M2/M3. M7 parallelizes with everything once we confirm ElevenLabs has a usable voice.

### M1 — i18n bootstrap (foundation)

**Effort**: 0.5 day. **Blocks**: M2, M3, M4. **Branch**: `feature/i18n-bootstrap` (already created).

**Deliverables:**

- `config/settings.py`:
  - Add `LANGUAGES = [('en', 'English'), ('pt-MZ', 'Português (Moçambique)')]`
  - Add `LOCALE_PATHS = [BASE_DIR / 'locale']`
  - Add `'django.middleware.locale.LocaleMiddleware'` to `MIDDLEWARE` (after `SessionMiddleware`, before `CommonMiddleware`)
  - `LANGUAGE_CODE` stays as `'en-us'` default; staging deploy overrides to `'pt-MZ'` via env var during the demo window
- `config/urls.py`: mount Django's `JavaScriptCatalog` view at `/jsi18n/`
- `locale/pt_MZ/LC_MESSAGES/django.po` + `djangojs.po`: empty skeleton files (just the header, no msgids yet — `manage.py makemessages` will populate)
- `infra/__main__.py`: thread a new `LANGUAGE_CODE` env var from Pulumi config (`aitutor:language-code`, defaults to `'en-us'`). Threaded for ALL stacks — flips staging in M6.
- Extend `/health/` endpoint (`apps/dashboard/views_health.py`) to surface `LANGUAGE_CODE` in the JSON response — makes M6 verifiable via `curl`.

**Tests:**

1. `python manage.py check` exits 0.
2. New test `apps/accounts/tests/test_i18n_bootstrap.py`:
   - Verifies `LANGUAGES` contains both `'en'` and `'pt-MZ'`.
   - Verifies `LOCALE_PATHS` resolves to a real directory.
   - Verifies `/jsi18n/` returns HTTP 200 and a JavaScript content type.
3. Existing `apps.tutoring.simple_tutor` test suite still passes (380/380 baseline).
4. Smoke: `curl http://localhost:8000/health/ | jq .language` returns `"en-us"`.
5. Smoke: `LANGUAGE_CODE=pt-MZ python manage.py runserver` followed by `curl http://localhost:8000/health/ | jq .language` returns `"pt-MZ"` — proves the env var threads through.

**Non-goals (intentional, stay scoped to "plumbing only"):**

- DON'T wrap any template strings in `{% trans %}` (that's M2).
- DON'T translate anything (that's M3).
- DON'T change the tutor system prompt (that's M4).
- DON'T flip staging yet (that's M6).

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

- New management command `apps/accounts/management/commands/translate_po.py`:
  - Reads `locale/pt_MZ/LC_MESSAGES/django.po`, batches the msgids, sends to Claude Opus 4.7 with a Mozambique-register prompt.
  - Writes msgstrs back into the file.
  - Idempotent — only translates rows where msgstr is empty.
  - Logs each translation pair for later review (no human reviewer at v1 — Edward will source one post-demo).
- Run the command:
  ```
  python manage.py translate_po --locale pt_MZ
  python manage.py compilemessages
  ```
- `locale/pt_MZ/LC_MESSAGES/django.mo` + `djangojs.mo` (committed; Django reads .mo at runtime, not .po).

**Before writing the translation prompt**: invoke `prompting-fundamentals-expert` then `claude-prompting-expert`. Non-negotiable per `feedback_consult_prompting_skills.md`. Decisions to lock in the prompt up-front:

- Register: `tu` informal (students, age 13–14).
- Spelling: post-1990 Acordo Ortográfico.
- Mozambique-specific vocabulary where it differs from European Portuguese (e.g. "autocarro" → keep if generic; "machimbombo" if vernacular asked for — defer to Claude's pt-MZ default and check 5–10 sample strings).
- Preserve variable placeholders verbatim (`{username}`, `{count}`, `%(var)s`).

**Tests:**

1. Spin up a local server with `LANGUAGE_CODE='pt-MZ'` and `SIMPLE_TUTOR_ENGINE=on`.
2. Use chrome-devtools-mcp to navigate to the login page → verify "Sign In" reads "Iniciar sessão" (or near equivalent).
3. Verify the chat tutor placeholder "Type your answer…" reads "Escreve a tua resposta…" (or near equivalent).
4. New test `apps/accounts/tests/test_i18n_translations_present.py`: counts msgids vs translated msgstrs in `django.po` — fails if coverage < 90%.
5. Hand-check 20 randomly-sampled translations for variable-placeholder preservation.

**Risks:**

- No native-speaker review at v1 — translations may be grammatically correct but pedagogically off. Mitigation: register locked in the prompt, hand-check 20 samples, plan to re-translate post-Paschal-feedback.
- Some msgids contain template variables — Claude must preserve them verbatim. Add an assertion in `translate_po.py` that rejects any translation that loses a `{varname}` token.

**Reviewer checklist:**

- [ ] All variable placeholders preserved in translations (automated in the command).
- [ ] Register is consistent (`tu` informal throughout — pick 10 samples to verify).
- [ ] No raw "TODO" or "TRANSLATE_ME" markers left in `.po`.

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
  - Translate the highest-leverage 10 English Science scenarios (since v1 is Grade 8 Science only).

**Before writing the system-prompt addition**: invoke `prompting-fundamentals-expert` then `claude-prompting-expert`. Non-negotiable.

**Tests:**

1. `apps/tutoring/simple_tutor/tests/test_intent.py` extended with Portuguese cases for each intent label.
2. New eval scenarios pass when run with `LANGUAGE_CODE='pt-MZ' SIMPLE_TUTOR_ENGINE=on python manage.py run_eval --scenarios portuguese`.
3. Existing English eval (78/80 baseline) still passes when `LANGUAGE_CODE='en-us'` — confirms no regression.
4. Manual chrome-devtools E2E on the locally-running Portuguese app: drive 3-turn Science tutoring session, verify tutor responds in Portuguese throughout.

**Risks:**

- Intent classifier is regex-based; new locales need their own regex tables. Risk: misclassification leads to wrong routing (e.g. "não sei" classified as `answer_or_other` instead of `non_engagement` → tutor tries to grade).
  - Mitigation: the 10–15 Portuguese eval scenarios MUST cover the high-traffic intent patterns.
- Locale-injection in the system prompt may interact unpredictably with the existing 10-axis judge dimensions in evals (e.g. `meta_reasoning_leak` regex is English-only). Spot-check the judge runs on a sample first.

**Reviewer checklist:**

- [ ] `LANG_PATTERNS` map gracefully falls back to English when an unknown locale is set.
- [ ] System-prompt addition is conditional — English deployments unaffected.
- [ ] No eval scenarios fail at `LANGUAGE_CODE='en-us'` (the existing 78/80 baseline still holds).

---

### M5 — Mozambique Grade 8 Science curriculum import (Path A only)

**Effort**: 1.5–2 days (down from prior 2–4; no translation step). **Blocks**: M6. **Depends on**: M1.

Path B from the prior draft (translation pipeline) is **dropped** — government supplied Portuguese materials directly.

**Phase 1 — Inspect the materials before writing the importer:**

The next Claude MUST first explore `mozambique/Grade 8 Materials/` and answer:

- What's the file format? PDFs, DOCX, structured XML, plaintext?
- What's the curriculum hierarchy? Likely Subject (Science) → Module/Unit → Topic/Lesson → Activity, but the exact terms come from the source files.
- Are there pre-built assessments (MCQs, short-answer questions) or just teaching content?
- How many lessons total? — bounds the import effort.

Run `ls -la "mozambique/Grade 8 Materials/" && file mozambique/Grade\ 8\ Materials/* | head -20` first. Then read 2–3 representative files end-to-end.

**Phase 2 — Importer:**

- New management command `apps/curriculum/management/commands/import_mozambique_science.py`:
  - Creates `Institution(slug='mozambique-pilot', name='Mozambique Pilot Schools')` if not exists.
  - Creates `Course(institution=mozambique_inst, subject='Science', grade=8, title='Ciências - 8ª Classe')`.
  - Maps source-file structure to `Unit → Lesson → LessonStep → ExitTicketQuestion`.
  - Idempotent: safe to re-run; existing rows updated by external_id, not duplicated.
- If MCQs aren't pre-supplied in the source files, generate them with the existing `generate_exit_tickets` command (which has the B-bias fix from commit `c56c804` baked in) — but DO this in Portuguese, which requires M4's system-prompt change to be live first.

**Note on Open Question #4 (institution slug) — for Edward**:

The `Institution` model is the platform's multi-tenancy boundary. Every course, every student, every session is tagged with an institution so that "Seychelles teachers don't see Mozambique courses" (and vice versa). For the demo we'll create one institution row called `mozambique-pilot` and tag the imported curriculum to it. This keeps the Seychelles and Mozambique data physically separate in the shared staging DB, even though both ride on the same Container App. The slug `mozambique-pilot` is just an internal handle (URL-safe identifier); the display name "Mozambique Pilot Schools" is what shows in the UI when relevant. If you want a different name/slug, change it before M5 ships — easy.

**Tests:**

1. New test `apps/curriculum/tests/test_mozambique_science_import.py`:
   - Imports a tiny fixture (1 unit, 2 lessons, 5 MCQs) shaped like the real Grade 8 Science materials.
   - Verifies `Institution(slug='mozambique-pilot')` is created.
   - Verifies relationships (Course → Unit → Lesson → LessonStep → ExitTicketQuestion).
   - Verifies all rows are scoped to `mozambique-pilot`, none to other institutions.
2. Re-import test: run the importer twice, verify no duplicate rows (idempotent).
3. Curriculum audit: `python manage.py audit_kb_coverage --institution=mozambique-pilot` reports >= 1 question per lesson.
4. Cross-pollination check: `Lesson.objects.filter(institution__slug='mozambique-pilot').count()` returns expected number; `Course.objects.exclude(institution__slug='mozambique-pilot').filter(title__icontains='Ciências').count()` returns 0.

**Risks:**

- Source-file structure may not map cleanly to the platform's Course → Unit → Lesson → LessonStep hierarchy. May need a translator layer (file → intermediate JSON → DB) rather than direct mapping.
- Source files may include figures/images. The platform's `MediaAsset` model supports them but bulk-import isn't wired — likely needs a one-off script in Phase 2 to upload images to Azure storage and FK them in.
- If MCQs need to be generated (not in source), it adds Claude API cost and must wait for M4 (so MCQs are generated in Portuguese, not English).

**Reviewer checklist:**

- [ ] All curriculum rows scoped to the `mozambique-pilot` institution.
- [ ] No cross-pollination with Seychelles institutions (verify with the cross-pollination check query above).
- [ ] If MCQs were generated: A/B/C/D distribution roughly uniform (~25% each — re-uses the B-bias audit pattern).
- [ ] Source files documented in the commit message (which file → which Lesson) so future maintainers can trace back.

---

### M6 — Deploy to staging in Portuguese demo mode

**Effort**: 0.5 day (was 1 day for the full Pulumi-stack version). **Blocks**: Paschal's demo. **Depends on**: M1, M5 (curriculum loaded into staging Postgres).

This is the demo-mode flip on the existing `staging` Container App. No new Azure infra.

**Deliverables:**

1. **Update staging Pulumi config**: edit `infra/Pulumi.staging.yaml`, add `aitutor:language-code: "pt-MZ"`. Run `pulumi up --stack staging` to apply.
2. **Load curriculum into staging Postgres**: run M5's importer against the staging DB:
   ```bash
   DATABASE_URL=<staging_db_url> python manage.py import_mozambique_science
   ```
   (Get the staging DB URL from `pulumi stack output --stack staging postgresConnectionString` or `az postgres flexible-server show`.)
3. **Create a Mozambique demo teacher account** on staging so Paschal can log in and explore.
4. **Document the rollback**: how to flip staging back to `en-us` after the demo. Single Pulumi config change + `pulumi up`. Add this to `RELEASING.md`.

**Tests:**

1. `curl https://aitutor-staging-app.<env-hash>.../health/ | jq` returns `{"version": "...", "language": "pt-MZ"}`.
2. Drive a 5-turn Science lesson via chrome-devtools-mcp against the staging URL — confirm Portuguese throughout (UI + tutor responses).
3. Inspect staging logs: `[simple_tutor] mode=GRADE intent=answer` markers appear; verify the system prompt sent to Claude contains "Respond in Mozambique Portuguese".
4. Verify Seychelles curriculum doesn't show up in the Mozambique demo student's course catalog (institution scoping working).
5. Rollback rehearsal: flip `aitutor:language-code` back to `en-us`, `pulumi up`, verify staging returns to English UI. Then flip back to `pt-MZ` for the demo.

**Risks:**

- Staging is dual-purpose during the demo window. If anyone else uses staging for Seychelles QA, they hit Portuguese UI. **Mitigation**: post in Slack / wherever the team coordinates: "staging is in pt-MZ demo mode from <date> to <date>".
- Cached translation files (.mo) need to be in the Docker image. The Dockerfile may need a `RUN python manage.py compilemessages` step. Check before pushing.
- The Seychelles English data still exists in staging's DB. If a Mozambique demo student somehow navigates to an English course (via direct URL guess), they'll see English content. Institution-scoping on the catalog should prevent this — verify the catalog view filters correctly.

**Reviewer checklist:**

- [ ] `LANGUAGE_CODE` env var on the staging Container App reads `pt-MZ` (verify via `az containerapp show --name aitutor-staging-app`).
- [ ] M5's curriculum import ran successfully against staging Postgres (row count matches local import).
- [ ] Demo teacher account exists in staging with access to the Mozambique courses.
- [ ] Rollback procedure documented in `RELEASING.md`.

---

### M7 — Portuguese audio (opportunistic, run if feasible)

**Effort**: 0.5 day. **Blocks**: nothing — purely additive. **Depends on**: M1, M6 (or staging once it's in pt-MZ mode).

Edward's note: "not required, but if ElevenLabs can do it then we should do it." So this is *do it unless ElevenLabs blocks us*.

**Phase 0 — feasibility check (do this FIRST before committing to M7):**

- Open the ElevenLabs API docs, confirm Multilingual v2 supports `pt-PT` or `pt-MZ` voices.
- Generate a 30-second sample of a tutor reply in Portuguese, play it back.
- Decision: voice quality acceptable for 13–14-year-old students? If yes → proceed. If no → defer M7 and document the audio quality decision in the commit message.

**Deliverables (if feasibility check passes):**

- `apps/tutoring/tts/elevenlabs.py`: when `settings.LANGUAGE_CODE == 'pt-MZ'`, use a Portuguese voice id.
- `apps/tutoring/stt/...`: when `settings.LANGUAGE_CODE == 'pt-MZ'`, use Azure Speech model `pt-PT` (closest available; no `pt-MZ` model exists).
- Voice + STT model picked by the locale, no UI change.

**Tests:**

1. Generate a sample tutor reply in Portuguese, play through the audio pipeline — sanity-check voice intelligibility.
2. Record a Portuguese answer via STT, confirm transcript accuracy on basic Science vocabulary (Grade 8 level — fotossíntese, célula, ecossistema, etc.).
3. Existing English audio tests still pass at `LANGUAGE_CODE='en-us'`.

**Reviewer checklist:**

- [ ] No new third-party API costs beyond Multilingual v2 + Azure Speech.
- [ ] Audio quality good enough for pilot (>90% intelligibility on standard Grade 8 Science vocab).
- [ ] Feasibility check result documented in commit message (voice id chosen + sample played).

---

## Effort + sequencing summary

```
M1 — i18n bootstrap                  0.5 day   | START HERE
  ├── M2 — wrap user strings         1.5 day   | depends M1
  │     └── M3 — translate UI        1.0 day   | depends M2
  ├── M4 — tutor locale-aware        0.5 day   | depends M1; parallel with M2/M3/M5
  └── M5 — curriculum import         1.5–2 day | depends M1; parallel with M2/M3/M4
        └── M6 — flip staging        0.5 day   | depends M1, M5
              └── M7 — audio (opt.)  0.5 day   | optional; depends on feasibility check
```

**Wall-clock minimum** (single engineer, no audio): **~3.5 days**.
**Wall-clock with parallelism** (M2/M3 sequential, M4/M5 parallel with M2/M3, audio after M6): **~2.5 days**.

The big change vs the prior draft: no new Pulumi stack provisioning, no GHA workflow setup, no Azure RG / SP grants. That's where the ~1–1.5 day saving comes from.

---

## Handoff notes for the next Claude

1. **Read `RELEASING.md` first** if you don't have context on the deploy model. Staging is just another Pulumi stack — flipping `LANGUAGE_CODE` is a one-line config edit + `pulumi up`.

2. **Inspect `mozambique/Grade 8 Materials/` BEFORE writing M5.** That directory contains the source-of-truth Portuguese curriculum. The structure of those files dictates the importer shape. `ls -la` and `file *` first; read 2–3 representative files end-to-end second; write the importer third.

3. **Pulumi passphrase**: `ai-tutor` (preview stack) or `EdwardAmoah(eai6)` (pixel/staging). Use the staging passphrase for the M6 config edit.

4. **Auto-memory rules to honor**:
   - `feedback_no_automated_prod_e2e.md` — automated chrome-devtools-mcp E2E is staging-only. Staging in pt-MZ demo mode is still staging — fine to drive automation.
   - `feedback_django_template_comments.md` — `{# #}` is single-line only. Multi-line uses `{% comment %}…{% endcomment %}`. Shipped this bug 6× before; don't make it 7.
   - `feedback_consult_prompting_skills.md` — invoke `prompting-fundamentals-expert` + `claude-prompting-expert` before writing M3's translation prompt and M4's system-prompt addition. Non-negotiable per CLAUDE.md.

5. **Skills to consult per milestone**:
   - M1 — `django-expert`, `azure-cloud-expert` (Pulumi env var threading)
   - M2 — `django-expert`
   - M3 — `claude-prompting-expert` (translation prompt design), `prompting-fundamentals-expert`
   - M4 — `claude-prompting-expert`, `tutoring-engine-expert`
   - M5 — `codebase-architecture-expert` (curriculum hierarchy patterns); also `claude-prompting-expert` if MCQs need generating in Portuguese
   - M6 — `azure-cloud-expert` (staging Pulumi stack edits)

6. **Cost ballpark for translation** (M3 only — M5 doesn't translate, just imports):
   - M3 UI: ~120 short strings, single Claude batch. ~$0.50 total.
   - M5 MCQ generation (if source files don't include MCQs): ~Grade 8 Science across 8–12 units, ~10–20 MCQs each. Estimate ~$5–15 in Opus tokens.
   - Audio (M7, optional): ElevenLabs Multilingual v2 ~$0.15 per 1k characters. A 10-lesson session ≈ ~5k chars ≈ ~$0.75/lesson.

7. **First commit on `feature/i18n-bootstrap` should be small and reviewable** — just M1's settings/middleware/JS-catalog changes + the `LANGUAGE_CODE` Pulumi plumbing + `/health/` extension. Don't bundle M2 into the same PR even if tempted; the templates-wrapping work has its own risk surface.

8. **Coordinate staging usage with Roy / teammates** before M6 deploys. Staging in pt-MZ mode is dual-purpose — anyone else using staging for Seychelles QA at the same time gets Portuguese UI.

9. **If anything contradicts this plan** (e.g. the materials in `mozambique/Grade 8 Materials/` turn out not to include Science, or are not in Portuguese), update this doc first, commit the update on `dev`, then revisit the affected milestones.

---

## Post-demo: real Mozambique stack (deferred)

When the Paschal demo is over and Mozambique becomes a real pilot (not just a demo), provision the dedicated stack. This is the "M6 done properly" — the version skipped for speed.

Outline (kept here so the work isn't lost; not part of the v1 critical path):

- `infra/Pulumi.mozambique.yaml`: new stack config mirroring `staging` shape (Consumption profile, 2 CPU / 4Gi, B1ms Postgres, min-replicas 1).
- Service principal grant: Contributor + AcrPush on `aitutor-mozambique-rg`. **Grant BEFORE the first GHA deploy** — same trap that bit the preview stack 2026-05-28.
- `.github/workflows/deploy-mozambique.yml`: new workflow, push-to-tag trigger.
- Migrate the staging-resident Mozambique data to the new Postgres: same `pg_dump --column-inserts` + retry-friendly loader pattern from the preview seed.
- After cutover: flip staging's `LANGUAGE_CODE` back to `en-us` to restore Seychelles staging.
- Expected effort when needed: 1 day.

Cost when live: ~$20–25/mo (same as staging).

---

## What changed vs the prior draft of this plan (2026-05-31, post-Edward-review)

- **All six open questions closed.** Decisions table at the top.
- **M5 collapsed to Path A only** (government Portuguese materials). Translation pipeline dropped — saves 1–2 days.
- **M5 scope narrowed** to Grade 8 Science (was "all of Grade 8, multiple subjects"). Manageable for one demo cycle.
- **M6 reshaped from "new Pulumi stack" to "flip staging to demo mode".** Saves ~1 day, defers Azure infra work. New Pulumi stack moved to "Post-demo" section so the work isn't lost.
- **M7 upgraded from "deferred to v2" to "opportunistic v1".** Per Edward: do it if ElevenLabs supports it.
- **M3 no-human-review acknowledged.** Native-speaker review deferred until post-demo; Claude translations ship as-is for v1.
- **Wall-clock dropped from ~3 days to ~2.5 days** (with parallelism), ~3.5 days serial.
- **Open Question #4 explained inline in M5** (institution slug = multi-tenancy boundary).

Branch state at this plan's commit time: on `dev`, no uncommitted code changes beyond `mozambique/` untracked directory. `feature/i18n-bootstrap` branch exists, tracks `origin/dev`, no commits.
