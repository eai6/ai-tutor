# Multi-locale + multi-tenant platform architecture — research

**Author**: Claude Opus 4.7 (1M context) — session 2026-05-31 → 2026-06-01.
**Status**: research synthesis, **decisions locked 2026-06-01 by Edward**. Now drives the Mozambique pilot plan.
**Trigger**: Edward asked, mid-Mozambique-planning, "in the long term it would not be good to have to handle two separate code bases and stuff. so we need a simple yet efficient solution. do some research on this and see how other platforms support multiple language and tenants or something".
**Related**: `memory/portuguese_mozambique_pilot_plan.md` — the pilot plan was rewritten 2026-06-01 to fold this architecture in from day one rather than defer it to "Phase 2".

---

## Decisions locked 2026-06-01 (Edward, post-review)

After reviewing the four axes below, Edward locked the following decisions. These supersede the original "Phase 1 vs Phase 2" framing — we now ship the unified architecture as part of the Mozambique pilot, not after it.

| Axis | Decision | Edward's rationale |
|---|---|---|
| Tenancy isolation | **Shared schema + `tenant_id`** (current `Institution` scoping). No move to schema-per-tenant or database-per-tenant. | "this is better given the low cost and it seems to align with institution or tenant" |
| Locale resolution | **Per-course** (primary) + **per-institution** (fallback). Both English and Portuguese run on the same deployment simultaneously. | "we can have both English and Portuguese at the same time on the same platform and deployment"; and "each school can teach multiple languages like French and English in the same school. Thus scoping at the course level is the best. This gives the opportunity to offer even French in Seychelles one day." |
| Content storage | **Locale-scoped rows** (each `Course` has a `locale` field). No translation tables. | Confirmed; curricula differ per country, not parallel translations |
| LLM prompt routing | **Per-turn language instruction** in the system prompt, sourced from `course.locale`. | "Let's add the language prompt to each turn then" — the two-call loop re-sends system every turn, so this is free |
| Three new fields (`Course.locale`, `StudentProfile.preferred_locale`, `Institution.default_locale`) | **Approved.** Ship all three. | "I like these small fields that efficiently solve the problem" |
| Locale string format | **Hyphenated lowercase** (`'pt-mz'`, `'en-us'`) — matches Django's `LANGUAGE_CODE` convention. Note: `.po` directories use Posix convention (`pt_MZ`); that's a gettext requirement, not a contradiction. | "do the recommended" |
| Per-region deployment | **Single deployment is the goal.** Multi-stack only if latency / data residency / compliance demands it later — not for language. | "We should be aiming for a single deployment" |
| Phase 2 timing | **Fold into Phase 1.** Don't ship the "flip staging `LANGUAGE_CODE`" workaround; go straight to the unified architecture for the Mozambique demo. | "update plan with research and my comments" |

The rest of this document is the research that led to the decisions above. The Mozambique pilot plan (`portuguese_mozambique_pilot_plan.md`) has been rewritten to reflect these decisions.

---

## The question this research answers

Today's Mozambique pilot plan flips `LANGUAGE_CODE=pt-MZ` on the staging Container App for the demo. That's fine for one week. **The long-term question**: what's the right architecture so that next time we add a country (Tanzania, Rwanda, etc.) we don't have to maintain a separate codebase, separate Pulumi stack, or separate deployment?

The answer industry-wide is roughly "Django's built-in i18n + locale-scoped content rows + per-user locale resolution". Below is the synthesis and what it means for AI Tutor specifically.

---

## Four axes of variability — pick one option per axis

Multi-tenant multi-language systems vary along four independent dimensions. The literature treats them separately.

### Axis 1 — Tenancy isolation (where each tenant's data lives)

| Pattern | Cost / complexity | When to use |
|---|---|---|
| **Shared schema with `tenant_id` column** (often + Postgres RLS) | Lowest. Single DB, single schema, every row filtered by tenant. | Early-stage SaaS, 100–10k tenants, no compliance constraints beyond SOC 2. | <this is better given the low cist. and it seems to align with institution or tenant>
| **Schema-per-tenant** | Middle. Single DB instance, separate schemas. | Mid-market, where per-tenant schema customization matters. |
| **Database-per-tenant** | Highest. Each tenant gets their own DB instance. | HIPAA, FedRAMP, large enterprise contracts that mandate it. |

**Where AI Tutor is**: shared schema with `tenant_id` column (`accounts_institution.id`, propagated to every curriculum row + every user row). This is fine for the pilot phase — no compliance pressure to move to schema-per-tenant or database-per-tenant.

### Axis 2 — Locale resolution (where the language preference lives)

| Layer | Driver | Granularity | Used today? |
|---|---|---|---|
| **Platform** | env var `LANGUAGE_CODE` | One deploy = one locale | Yes (default `en-us`; Mozambique plan flips to `pt-MZ`) |
| **Per-tenant** | `Institution.default_locale` field | Many tenants per deploy, each fixed to one locale | No (proposed once, dropped) | <Lets do this, so that we can have both english and portugeses at the same time on the same platform and deployment>
| **Per-user** | `UserProfile.locale` or session-based | User-pickable | No |
| **Per-request** | URL prefix (`/pt-MZ/...`), Accept-Language header, cookie | Anonymous + authenticated | No |

Django's built-in `LocaleMiddleware` already implements the per-user + per-request layers — the algorithm is: URL prefix → cookie → Accept-Language → session → fallback to `LANGUAGE_CODE`. No need to invent middleware.

### Axis 3 — Content storage (how translated rows live in Postgres)

| Pattern | Adding a new language | Tooling | Right when |
|---|---|---|---|
| **Column-per-language** (`title_en`, `title_pt`, …) | Schema migration each time | `django-modeltranslation` | Fixed locale set, optimize for read perf |
| **Translation rows** (each model has a `_translation` sidecar table, one row per language) | Just insert data | `django-parler` | Dynamic locale set; **the same content** translated multiple ways |
| **Locale-scoped rows** (each `Course` carries a `locale` field; the full row is unique per locale) | Just import a new content set | None — plain FK + filter | When **content differs per country**, not just translated | <year this sounds better>

**Crucial for AI Tutor**: Seychelles biology and Mozambique science are **not parallel translations of the same content**. They're independent curricula written for different national syllabi. Therefore:

- ❌ Translation tables (django-parler) are the wrong fit. They're designed for "this product description, in 5 languages".
- ✅ Locale-scoped rows are the right fit. Each `Course` has its own `locale` field. We're already 80% there via `Institution` scoping. <it sounds like institution level scoping seems better for unified platform.>

<Consider this. each school can teach multiple languages like french and english in the same school. Thus scoping at the course level is the best. This gives the opportunity to offer like even french in Seychelles on day. >

### Axis 4 — LLM system-prompt locale routing

Industry consensus from the LLM-tutor literature (Markaicode 2024, Alignment Drift CEFR paper 2025):

- Locale flows from the **user's stored preference** (or course's `locale`) into the system prompt at request time.
- Don't rely on the model auto-detecting from the student's first message — empirically unreliable.
- Don't switch locales mid-conversation — degrades user trust.

The CEFR alignment drift paper (Spanish tutoring) found that system-prompt locale constraints work for short interactions but degrade over long sessions — a reason to also wire locale into per-turn context, not just the initial system block. The simple_tutor's two-call loop already passes the system prompt every turn, so this is essentially free.

<Good. Lets add the language prompt to each turn then>

---

## What other platforms actually do

| Platform | Approach |
|---|---|
| **Duolingo** | Course content pre-built offline, serialized to S3, fetched + cached. User personalization injected via API. One backend serves 100+ courses in 40+ languages. Course is the unit of locale. |
| **Khan Academy** | Services-oriented architecture (post-rewrite). Content services serve per-locale; per-user services overlay personalization. |
| **Notion / Slack / Linear** | Per-user `locale` field in user profile; UI strings via gettext-equivalent; content stays in the language the user authored it. |
| **Shopify / Stripe** | Per-tenant default locale + per-user override; admin UI fully translated; merchant-authored content stays in whatever language the merchant wrote it. |

Common pattern: **the *unit of localization* in the data model is the smallest stable thing**. For Duolingo it's the course; for Notion it's the user; for Shopify it's the storefront. **For AI Tutor it's the Course** — because curricula are nationally specific. <Yes the course level makes the best sense.>

---

## Recommended architecture for AI Tutor (Phase 2)

Three small fields, no new infra:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `Course.locale` | `CharField(max_length=10)` | `'en'` | The language of THIS course's curriculum. Drives tutor system prompt + UI when a student is in this course's chat session. |
| `StudentProfile.preferred_locale` | `CharField(max_length=10, null=True)` | `null` | What UI language the student sees in catalog / dashboard outside a course. Optional. |
| `Institution.default_locale` | `CharField(max_length=10)` | `'en'` | Fallback when student preference is unset. Mozambique pilot institution sets `'pt-MZ'`. |

<Perfect! I like these small fields that efficienctly solves the problem.>

**Resolution chain** (custom middleware, ~15 lines):

```
locale =
    course.locale                            # if student is in a chat session
    or student_profile.preferred_locale       # if explicitly set
    or student_profile.institution.default_locale
    or settings.LANGUAGE_CODE                 # global fallback
```

**Why this shape**:

- **Single codebase, single deploy.** Mozambique + Seychelles + future Tanzania + future Rwanda all run on one Container App. Per-region stacks remain optional (for latency / data residency / compliance) but not required for language.
- **Backward compatible.** Existing Seychelles `Course` rows backfill to `'en'` in one migration. Nothing breaks.
- **Course is the strongest signal.** When a student is in a Portuguese-curriculum lesson, the tutor system prompt appends "respond in pt-MZ" — *regardless of* student or institution preference. Kills the "Seychelles teacher logs into a Mozambique-flipped staging and gets Portuguese" failure mode.
- **Standard Django `LocaleMiddleware`** handles UI rendering. We add one custom resolver that reads `StudentProfile.preferred_locale`.
- **No translation tables, no `django-parler`.** Curriculum rows are locale-scoped; we don't need parallel translations of identical content.

**LLM system prompt change** (the only material code change beyond the migration):

`apps/tutoring/simple_tutor/engine.py::respond` — change one line:

```python
# Before (platform-level):
locale = settings.LANGUAGE_CODE

# After (course-level):
locale = self.session.course.locale
```

The system-prompt builder then appends the locale instruction conditionally, same as the M4 logic in the current plan.

---

## Comparison of three paths forward

| | A. Current plan (platform-level) | B. Hybrid (recommended for Phase 2) | C. Full per-user Django default |
|---|---|---|---|
| Effort to ship Mozambique demo | ~2.5 days | ~3.5 days | ~4 days |
| Cost of adding a 3rd country (Tanzania) | New Pulumi stack + ~1 day | One curriculum import + ~0.5 day | Same as B |
| Cross-locale leakage risk | None (deploys physically separate) | Low (filter by `Course.locale`) | Medium (user preference can mismatch course locale) |
| Code complexity | Lowest | Moderate (+3 fields, +1 middleware) | Highest |
| Coordination burden during pilots | High (can't QA Seychelles while Mozambique runs on staging) | Low (one staging, both languages coexist) | Low |
| Alignment with industry standard (Duolingo/Notion/Shopify) | Low | High | High |
| Reusable for non-locale per-tenant customization (e.g. exit ticket question types) | No | Yes — same pattern | Yes |

---

## Migration plan — folded into the Mozambique pilot (M4)

**Previously**: this section described a deferred "Phase 2" to run after the Paschal demo.

**Now (2026-06-01)**: Edward chose to fold this work into the Mozambique pilot itself rather than ship the "flip staging LANGUAGE_CODE" workaround first and refactor later. The migration steps below are implemented as **M4 of `memory/portuguese_mozambique_pilot_plan.md`** — see that doc for the operational plan, tests, and reviewer checklist.

| Step | Where it ships |
|---|---|
| `Course.locale` field + migration + backfill | Pilot plan M4 |
| Engine reads `course.locale`; per-turn locale prompt injection | Pilot plan M4 |
| Intent classifier locale-aware (`LANG_PATTERNS` map) | Pilot plan M4 |
| Chat view `translation.activate(course.locale)` with try/finally | Pilot plan M4 |
| `StudentProfile.preferred_locale` field | **Deferred** to Phase 1.5 in the pilot plan |
| `Institution.default_locale` field | **Deferred** to Phase 1.5 in the pilot plan |
| Full `LocaleResolverMiddleware` (chain: course > student > institution > global) | **Deferred** to Phase 1.5 in the pilot plan |
| Translate catalog + dashboard + accounts UI strings | **Deferred** to Phase 1.5 (M2 scope narrowed to chat-shell only for the demo) |

Phase 1.5 is the post-demo follow-up captured at the bottom of the pilot plan — total effort ~2 days, ships when Mozambique is confirmed as a real pilot or when a third country (Tanzania, Rwanda) is queued.

---

## Open questions — RESOLVED 2026-06-01

All three closed by Edward during the architecture review. Captured in the "Decisions locked" table at the top of this doc.

1. ~~Commit to Phase 2 now or after demo feedback?~~ → **Now.** Folded into the pilot plan as M4.
2. ~~Locale string format?~~ → **Hyphenated lowercase** (`'pt-mz'`, `'en-us'`), matching Django `LANGUAGE_CODE`.
3. ~~Multi-stack per region for non-language reasons?~~ → **Single deployment is the goal.** Multi-stack only if latency / data residency / compliance demands it later.

---

## Sources

- [Multi-tenant SaaS architecture 2026 — Northflank](https://northflank.com/blog/multi-tenant-saas-platform-deployment)
- [Multi-tenant SaaS architecture in 2026 — GSoft Consulting](https://gsoftconsulting.com/en/blog/building-multi-tenant-saas-2026)
- [Designing Multi-tenant SaaS on AWS — Clickittech 2026](https://www.clickittech.com/software-development/multi-tenant-architecture/)
- [Django i18n docs](https://docs.djangoproject.com/en/6.0/topics/i18n/)
- [Django REST framework — Internationalization](https://www.django-rest-framework.org/topics/internationalization/)
- [django-parler — translation-row pattern](https://github.com/django-parler/django-parler)
- [Multilingual database design — Redgate](https://www.red-gate.com/blog/data-modeling-for-multiple-languages-how-to-design-a-localization-ready-system)
- [Multilingual content schema patterns — Translated](https://translated.com/resources/multilingual-database-design-architecture-optimization-guide)
- [Building multi-language LLM applications — Markaicode](https://markaicode.com/build-multi-language-support-llm-applications/)
- [Multilingual chatbot LLM patterns — Analytics Vidhya 2024](https://www.analyticsvidhya.com/blog/2024/06/multilingual-chatbot-using-llms/)
- [Alignment Drift in CEFR-prompted LLMs for Spanish tutoring — arXiv 2505.08351](https://arxiv.org/pdf/2505.08351)
- [Duolingo backend rewrite (Scala + S3-cached course data)](https://blog.duolingo.com/rewriting-duolingos-engine-in-scala/)
- [Khan Academy backend rewrite (services-oriented)](https://blog.quastor.org/p/khan-academy-rewrote-backend)
