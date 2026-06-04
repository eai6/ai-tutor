# Tutor country/locale context — configurable, not hardcoded — Plan (2026-06-04)

## Problem
The tutor's **language** is already locale-driven (good), but its **country identity, grade vocabulary, and local-fact grounding are hardcoded to Seychelles**. A Mozambique (pt-mz) student's tutor replies in Portuguese yet is told in its system prompt that it teaches *"Seychelles secondary-school students (grades S3-S5)"* — wrong country and wrong grade band (Mozambique = 8ª–12ª Classe), with no Mozambican local context. We want country/language/grade and local context **derived from the selected Course → Unit → Lesson**, mirroring the per-course-locale architecture already locked in `memory/multi_locale_architecture_research.md`.

This plan **extends** those locked decisions (per-course locale, per-turn language instruction) to the parts they didn't cover: country identity, grade-band phrasing, and the local-fact library. It does **not** revisit tenancy/locale-resolution (settled there).

## Current state (from audit)
- Active engine on staging is the **simple tutor** (`SIMPLE_TUTOR_ENGINE=on`).
- Locale resolves from the course: `apps/tutoring/simple_tutor/engine.py:111` `_course_locale(session)` → `session.lesson.unit.course.locale` (default `en-us`).
- Language IS dynamic and cache-safe: `apps/tutoring/simple_tutor/prompts.py:747` `_build_locale_rule(locale)` returns `''` for `en-us` (byte-for-byte unchanged → no cache churn) and a Portuguese-register block for `pt-mz`. **This is the pattern to mirror.**
- **Hardcode 1 — role template:** `apps/tutoring/simple_tutor/prompts.py:300-309` `_BLOCK_0_TEMPLATE` literally embeds `"Seychelles secondary-school students (grades S3-S5)"`, then `{LOCALE_RULE}` at :310. Block 0 is **cache-marked / static per conversation**.
- **Hardcode 2 — local facts:** `apps/curriculum/models.py:705` `SeychellesContext` (admin-editable facts: economic/geographic/trade/climate/population/…). Injected **only** by the legacy engine `apps/tutoring/conversational_tutor.py:6168` `_build_seychelles_context_block()`; the **simple engine injects no country facts at all** (grep of `apps/tutoring/simple_tutor/` for context/country = empty). No Mozambique equivalent.
- **Hardcode 3 — legacy engine:** `apps/tutoring/conversational_tutor.py:5722` sets `locale_context="Seychelles", language="English"` unconditionally.
- Config plumbing to reuse: `Course.locale` (`apps/curriculum/models.py:57`), `PlatformConfig.get_grade_choices()` (country grade codes), `settings.LANGUAGES` (`en-us`/`pt-mz`), `apps/curriculum/locale_prompts.py` `locale_instruction_block` (content-gen only).

## Target design

**Two kinds of "country context", handled differently (by their nature):**

### A. Country *identity* — static, code-config keyed by locale
Country name, demonym ("Seychellois"/"Mozambican"), grade-band phrasing, language name, currency/units. These are static, rarely change, and must be version-controlled + cache-deterministic. **Do NOT put in DB.** Mirror `_build_locale_rule`'s locale-branch pattern: a `LOCALE_PROFILES` dict (or frozen dataclass) keyed by locale in a new `apps/tutoring/simple_tutor/locale_profiles.py`:

```python
@dataclass(frozen=True)
class LocaleProfile:
    locale: str
    role_audience: str   # e.g. "Seychelles secondary-school students (grades S3-S5)"
    demonym: str         # "Seychellois" / "Mozambican"
    currency: str        # "Seychellois rupee (SCR)" / "Mozambican metical (MZN)"

LOCALE_PROFILES = {
  'en-us': LocaleProfile('en-us', 'Seychelles secondary-school students (grades S3-S5)', 'Seychellois', 'Seychellois rupee (SCR)'),
  'pt-mz': LocaleProfile('pt-mz', 'Mozambican secondary-school students (8ª–12ª Classe)', 'Mozambican', 'Mozambican metical (MZN)'),
}
```
`role_audience` for `en-us` is the **exact current string** → Block 0 stays byte-for-byte identical → zero cache churn / zero Seychelles regression.

### B. Local *facts* — dynamic, admin-editable, DB, locale-keyed
Generalize `SeychellesContext` rather than add a parallel `MozambiqueContext` (Rule of Three: this is the 2nd country, and the locked architecture commits to more — Tanzania/Rwanda). Add a `locale` field; keep the table, rename in a follow-up if desired. A `LocalContext.for_locale(locale, subject, grade)` selector mirrors `_build_seychelles_context_block` but filters by `locale`.

### C. One resolver, derived from the lesson
A single `resolve_locale_context(session)` in the simple engine returns `(profile, locale)` from `_course_locale(session)`. The role template gains a `{ROLE_AUDIENCE}` placeholder filled from `profile.role_audience`; the local-fact block is built from `LocalContext.for_locale(...)`.

## Data model changes
- `SeychellesContext` (`apps/curriculum/models.py:705`): **add** `locale = CharField(max_length=10, default='en-us', db_index=True)`. Migration backfills all existing rows → `'en-us'` (they're all Seychelles today). No data loss; additive.
- Optional follow-up: rename model `SeychellesContext → LocalContext` (separate migration; touches admin + the 2 call sites). Defer to Phase 2 to keep the pilot diff small.
- No new model for identity (code config, section A).

## Backend changes
- **New:** `apps/tutoring/simple_tutor/locale_profiles.py` — `LocaleProfile` + `LOCALE_PROFILES` + `get_profile(locale)` (fallback to `en-us`).
- **`apps/tutoring/simple_tutor/prompts.py`:** replace the literal `"Seychelles secondary-school students (grades S3-S5)"` in `_BLOCK_0_TEMPLATE:301-302` with `{ROLE_AUDIENCE}`; fill it in the same `.replace()` chain that already injects `{LOCALE_RULE}` (~:652). en-us output unchanged.
- **`apps/tutoring/simple_tutor/engine.py`:** pass `profile.role_audience` (and later facts) into `build_system_prompt` alongside the existing `locale=` arg (~:177).
- **Local-fact block (Phase 2):** new `_build_local_context_block(locale, subject, grade)` in simple-tutor prompts, placed in a **per-step block (Block 1)** not Block 0; gated so it injects only when `LocalContext` rows exist for that locale. NOTE: turning this on for `en-us` is a **behavior change for Seychelles** (the simple engine gives no facts today) → see Open Question 3.
- **Legacy engine:** `conversational_tutor.py:5722` + `:6168` — make `locale_context`/`language`/the context block locale-driven from the same resolver, so the two engines don't drift. Lower priority (not active on staging).
- **Prompting-skills consult (mandatory):** per CLAUDE.md, run `claude-prompting-expert` + `prompting-fundamentals-expert` before editing `_BLOCK_0_TEMPLATE` / writing the fact block.

## Out of scope
- Translating curriculum/lesson **content** (already locale-scoped rows per the locked architecture — different concern).
- Currency/unit auto-conversion inside lessons (only the tutor's *framing* mention; no math rewriting).
- Renaming `SeychellesContext` → `LocalContext` (Phase 2 cosmetic; additive `locale` field is enough functionally).
- French-in-Seychelles or any 3rd locale (architecture supports it; not built now).
- Per-institution PromptPack locale-keying.

## Phased delivery
| Phase | Work | Est. (solo days) | Pilot-critical? |
|---|---|---|---|
| **1** | `locale_profiles.py` + parameterize role template (`{ROLE_AUDIENCE}`) + engine wiring. en-us byte-identical; pt-mz = "Mozambican … 8ª–12ª Classe". Prompting-skills consult. Local eval that en-us prompt is unchanged + pt-mz correct. | **0.5–1** | **Yes** — stops calling Moz students Seychelles |
| **2** | Add `locale` to `SeychellesContext` + migration + `for_locale()` selector + cache-safe `_build_local_context_block` wired into simple engine (gated). Seed Mozambique facts. | 1.5–2 | No |
| **3** | Locale-drive the legacy engine hardcodes; optional model rename; currency/demonym polish. | 1 | No |

## Open questions (need your call before implementation)
1. **Grade phrasing for Mozambique.** Use the **band** "8ª–12ª Classe" in the role line (matching the current "grades S3-S5" style), or the student's **specific** grade ("8ª Classe")? *Recommend: band in the role line (cache-stable across students), and separately consider passing the specific grade as it's already known.* 
2. **Seed a Mozambique fact library now, and who provides the facts?** Seychelles' came from `auto-memory/reference_seychelles_context.md`. *Recommend: ship Phase 1 without facts for the pilot; you (or a data file) provide Mozambique facts for Phase 2 — the tutor works fine grounded in lesson content alone meanwhile.*
3. **Turn on fact injection for Seychelles in the simple engine?** It currently gets none there; enabling it is an improvement but a **behavior change + cache churn** for the live Seychelles pilot, needing an eval pass. *Recommend: Phase 2 adds facts for Mozambique only (gated by locale-has-library); leave Seychelles' simple-engine behavior unchanged until separately evaluated.*
4. **Identity config location.** Code dict (`locale_profiles.py`) vs admin-editable DB. *Recommend: code for identity (static, cache-deterministic, mirrors `_build_locale_rule`); DB only for the fact library.*

## Next step
Phase 1 only, on your go: create `locale_profiles.py` and replace the hardcoded role audience with `{ROLE_AUDIENCE}` — after a `claude-prompting-expert` consult — verifying the en-us system prompt is byte-for-byte unchanged before anything ships.

## Cross-refs
- `memory/multi_locale_architecture_research.md` (locked locale/tenancy decisions — this plan extends them).
- `memory/portuguese_mozambique_pilot_plan.md` (pilot plan).
- `auto-memory/staging_mozambique_env.md` (env facts).
