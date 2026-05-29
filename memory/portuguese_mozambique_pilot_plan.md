# Portuguese localization for Mozambique pilot — planning doc

**Status**: planning only, no code yet. Future PR.
**Trigger**: Paschal's Mozambique visit next week. Government has shared curriculum + materials (TBC — Edward to circulate).
**Owner**: TBD
**Deadline**: ~7 days from 2026-05-29.

## What "Portuguese" actually means here

Three layers need translation, and they have different mechanics and costs:

| Layer | What it covers | Mechanism | Effort |
|---|---|---|---|
| **A. App UI strings** | Buttons, headers, banners, error messages, placeholders ("Type your answer…") | Django i18n (`gettext`) + JS string table | ~1.5 days |
| **B. LLM-generated turn content** | Tutor's chat responses, hint ladders, affirmations, exit ticket grading feedback | Tell the LLM to respond in Portuguese via system prompt + locale field on session | ~0.5 day |
| **C. Curriculum content** | Lesson titles, teaching scripts, objectives, MCQ stems + options + explanations, exit ticket questions | Bulk-translate existing English curriculum OR receive Portuguese-authored content from Mozambique government | ~2-4 days depending on source |

Treat these as three independent workstreams that can be merged separately.

## Architectural decisions to make first

### 1. Locale at the institution level vs the user level

**Recommendation: institution-level, propagated to user.** Mozambique pilot = one institution (or one school within one institution). Set `Institution.default_locale = 'pt-MZ'`, every student under it inherits unless overridden. Same pattern Seychelles uses implicitly (everything is `en` today).

**Rationale**:
- Curriculum content is already keyed by institution (one institution per pilot).
- Avoids the "user switches school" edge case.
- Localized content lives next to the curriculum (same scoping).

**Alternative**: per-user locale via `StudentProfile.preferred_locale`. Adds flexibility but doubles the routing surface (curriculum scoping + locale scoping multiply). Skip for v1.

### 2. App UI strings — `gettext` vs DB-stored vs hybrid

**Recommendation: Django `gettext` + `.po` files** for templates and Python code. Standard, IDE-friendly, translators-friendly (Weblate / Crowdin compatible).

For JS-rendered strings (the chat client, the artifact panel), use Django's `JavaScriptCatalog` view → serves a `gettext`-compatible JS function. Same `.po` source.

**Rationale**:
- One source of truth.
- Existing tooling — no new infrastructure.
- Easy review (PR shows the diff of every translated string).

**Skip**: DB-stored translations (e.g. `accounts_translation` table). Overkill for two locales; adds query overhead per page.

### 3. LLM tutor responses — language switching mechanism

**Recommendation: lightweight prompt augmentation per session.**

In the simple_tutor engine (`apps/tutoring/simple_tutor/engine.py::respond`):
```
locale = session.lesson.unit.course.institution.default_locale
if locale.startswith('pt'):
    system_prompt += "\n\nRespond to the student in Portuguese (Mozambique register, pt-MZ)."
```

This is a single-block append at prompt-build time — no engine refactor. The 5E phase prompts, hint ladder, R07/R14/R15 rules all transfer through; Claude/Sonnet handle Portuguese fluently. The intent classifier (`apps/tutoring/simple_tutor/intent.py`) needs **Portuguese regex equivalents** for `i don't know` → `não sei`, `obrigado` (thanks) etc.; otherwise everything routes through `answer_or_other` and the prompt's escape hatch fires the fallback path. ~0.5 day for the intent regex patches.

**Don't** retrain or use a Portuguese-specific model — Anthropic/OpenAI/Gemini are all strong on European + Brazilian Portuguese already. Mozambique register has some distinctive phrasing but isn't a separate model concern; cover it via the prompt.

### 4. Curriculum content — where does Portuguese content come from?

Two paths, **only one of which we control timing on**:

**Path A — Government supplies Portuguese curriculum.** Best case. We add it to the DB scoped to the Mozambique institution. Same `curriculum_lesson` / `curriculum_lessonstep` / `tutoring_exitticketquestion` tables, just with `pt-MZ` rows. The existing `Q(institution=inst) | Q(institution__isnull=True)` scoping pattern keeps it cleanly separated.

**Path B — Translate existing English curriculum.** Use `generate_exit_tickets` and `content_generator` pipelines with a translation flag, OR run a one-shot `manage.py translate_curriculum --from=en --to=pt-MZ --institution=N` that batch-translates question stems / options / teaching scripts via Claude/OpenAI.

**Risk on path B**: translated MCQs may shift difficulty (idiom loss), and explanations may sound machine-translated. Need a teacher to spot-review 10-20% before pilot.

**Recommendation**: assume path A. If government delivery slips, fall back to path B with a flag day for spot-review.

### 5. TTS / STT — voice support

The chat tutor has an audio mode (Play/Replay buttons). Current backends are:
- **TTS**: ElevenLabs (voices: English only on current settings)
- **STT**: Azure Speech (model: English)

For Portuguese:
- **ElevenLabs** supports `pt` voices (Brazilian and European registers; Mozambique register isn't a separate model). Need to (a) pick a voice and (b) update `apps/tutoring/tts/elevenlabs.py` to read voice from session locale.
- **Azure STT** supports `pt-BR` and `pt-PT` recognition. `pt-MZ` isn't a separate model — use `pt-PT`. Update `apps/tutoring/stt/...` to read locale.

~0.5 day for the audio plumbing.

### 6. Date / number formatting

Django's locale-aware formatters work if `USE_L10N = True` (Django ≥ 4 defaults this on). Verify settings.py. Currency-style numbers in math questions ("R 1,800") may not need locale changes since they're rendered inline in MCQ text — they get translated as part of the question text, not as `{{ value|floatformat }}`.

## Concrete PR plan

### PR 1 — `i18n: bootstrap Django gettext + JS catalog + pt-MZ skeleton`

- Add `LANGUAGES = [('en', 'English'), ('pt-MZ', 'Português (Moçambique)')]` to `config/settings.py`.
- Add `LocaleMiddleware`, mount `JavaScriptCatalog` view at `/jsi18n/`.
- Wrap all template strings in `{% trans "…" %}` / `{% blocktrans %}`. ~120 strings across `templates/tutoring/`, `templates/accounts/`, `templates/dashboard/`. Mechanical but tedious.
- Wrap user-facing Python strings (form labels, validation errors, flash messages) in `_(…)`.
- Generate `locale/pt_MZ/LC_MESSAGES/django.po` and `djangojs.po`. Initially empty (auto-generated msgids, no msgstrs). PR is "infra ready, no translations yet".
- Add `accounts.Institution.default_locale` field + migration. Default = `'en'`. Wire `LocaleMiddleware` to read it.

**Test plan**: existing tests pass. New test: render a template with `?language=pt-MZ` set → confirms catalog wiring works (uses identity translation since `.po` is empty).

**Out of scope**: actual translations, LLM prompt changes, curriculum content.

### PR 2 — `i18n: pt-MZ UI translations` (separate so translators can work in parallel)

- Fill `locale/pt_MZ/LC_MESSAGES/django.po` and `djangojs.po`. Either:
  - Send `.po` files to a translator (Weblate, Crowdin, or just shared docs).
  - First-pass machine-translate with `manage.py translate_po --from=en --to=pt-MZ` (one-shot Claude call), then have a native speaker review.
- Compile with `manage.py compilemessages`.
- Visual regression: render every translated page at `?language=pt-MZ`; screenshot before/after.

### PR 3 — `tutor: pt-MZ responses + intent classifier`

- In `apps/tutoring/simple_tutor/engine.py::respond`, read `session.lesson.unit.course.institution.default_locale` and append a one-line directive to the system prompt when `pt-*`.
- In `apps/tutoring/simple_tutor/intent.py`, add Portuguese pattern lists alongside the English ones (or lift the existing patterns into a `LANG_PATTERNS[locale]` map). At minimum: `não sei`, `obrigado`, `pode explicar`, `eu acho que você quer dizer`, plus the equivalent off-topic and non-engagement markers.
- Test: extend `apps/tutoring/simple_tutor/tests/test_intent.py` with Portuguese examples.

**Risk**: 0/60 of the existing eval scenarios are Portuguese. We'd need a small Mozambique-flavored eval set (~10-15 scenarios) to validate before pilot. Either: handwrite them, or translate the strongest 10 English scenarios.

### PR 4 — `curriculum: load Mozambique pilot content`

This depends entirely on what the government sends. Two shapes:
- **If structured (CSV/JSON)**: write a `manage.py import_mozambique_curriculum` command that maps to `Course` / `Unit` / `Lesson` / `LessonStep` / `ExitTicketQuestion` scoped to a new `Institution`.
- **If documents (PDF/DOCX/web)**: feed through the existing curriculum-generation pipeline with a `--language=pt-MZ` flag forwarded to `content_generator`.

Either way: create the `Institution` for Mozambique first with `default_locale='pt-MZ'`, scope every record to it. Pilot-isolation by construction.

### PR 5 — `audio: pt voice selection + STT locale`

Lower priority — pilot can launch in text-only mode if audio isn't ready. Wire ElevenLabs voice + Azure STT region per session locale.

## Effort + sequencing

| PR | Effort | Can parallelize with |
|---|---|---|
| 1 (i18n bootstrap) | 1.5 day | none — blocker for 2 |
| 2 (UI translations) | 1 day (with translator) | 3, 4 |
| 3 (tutor + intent) | 0.5 day | 2, 4 |
| 4 (curriculum) | 2–4 days, depends on source | 2, 3 |
| 5 (audio) | 0.5 day | 2, 3, 4 |

Minimum viable for Paschal's visit: PRs 1 + 2 + 3 + 4 (text-only, pilot launches without audio). Total ~5 days of focused work, parallelizable to ~3 wall-clock days with two people.

## Open questions for Edward to decide

1. **Curriculum delivery**: are materials being supplied in Portuguese already, or do we translate from Seychelles? (Drives PR 4 size.)
2. **Audio for pilot v1**: text-only OK for week-one, or required day-one?
3. **Translator**: machine + spot-review, or hire a Mozambique-Portuguese translator? Latter is higher quality but adds wall-clock time.
4. **Institution slug**: what do we name the institution? "Mozambique Pilot"? Specific school name?
5. **Pilot scope**: one subject + one grade, or full curriculum from day one?

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Mozambique register sounds wrong (LLM defaults to pt-BR or pt-PT) | Medium | Prompt explicitly states "pt-MZ register"; spot-review tutor responses with a native speaker before pilot |
| Translated MCQs lose difficulty / idiom | Medium | 10-20% spot-review by a teacher; the MCQ B-bias fix (commit c56c804) applies equally to Portuguese generation |
| Wall-clock too short — pilot launches before content is ready | High | Ship PRs 1-3 first (always useful); PR 4 can have a phased rollout (one subject at a time) |
| Existing simple_tutor eval (78/80) doesn't cover Portuguese — we ship blind | Medium | Author 10-15 Portuguese scenarios in PR 3 before merging |
| ElevenLabs Portuguese voice quality vs English | Low | Pilot launches text-only if audio needs more work |

## Files that will change (rough surface)

- `config/settings.py` — `LANGUAGES`, `LOCALE_PATHS`, `USE_I18N=True`
- `apps/accounts/models.py` — `Institution.default_locale` field
- `apps/accounts/migrations/00XX_institution_default_locale.py`
- `templates/**/*.html` — wrap strings in `{% trans %}`
- Various Python files — wrap user-facing strings in `_(…)`
- `locale/pt_MZ/LC_MESSAGES/django.po` — new
- `locale/pt_MZ/LC_MESSAGES/djangojs.po` — new
- `apps/tutoring/simple_tutor/engine.py` — locale-aware system prompt
- `apps/tutoring/simple_tutor/intent.py` — `LANG_PATTERNS` map
- `apps/tutoring/simple_tutor/tests/test_intent.py` — pt cases
- `apps/curriculum/management/commands/import_mozambique_curriculum.py` (new) — depending on data shape

## What's intentionally out of scope

- Mobile app translations (RN paused; revisit when mobile resumes)
- Admin Django UI translations (English-only is fine for staff/admins)
- Email template translations beyond the verify-email flow (low traffic for pilot)
- Locale-aware time zones (handled by `USE_TZ=True` already)
- RTL support — Portuguese is LTR, no layout impact

---

**Next step when ready**: Edward answers the five open questions above; we kick off PR 1 (i18n bootstrap) — that's the only true blocker. Other PRs can fan out from there.
