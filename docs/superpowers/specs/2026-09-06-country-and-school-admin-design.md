# Country accounts and the school admin role

**Status:** approved design, not yet planned
**Date:** 2026-09-06
**Branch at time of writing:** `ui-ux-improvements` (`1d62eeee`)

## Goal

Two new tiers of account, for the country-adoption phase:

* A **country account** — a ministry or programme team. Sees every school in
  its country, adds schools, appoints their administrators, manages the
  national curriculum, and reads a roll-up across all of them.
* A **school admin** — runs one school: its people and its settings.

## What exists today

Three tiers, and only two of them are in the data model:

| Tier | Expressed as | Scope | Content |
|---|---|---|---|
| Super Admin | `User.is_staff` | every school | full |
| Teacher | `Membership.role = 'staff'` | their school(s) | none |
| Student | `Membership.role = 'student'` | one school | — |

`apps/dashboard/views.py:118` already records the gap this spec fills:

> There is no school-admin role in the data model — `Membership.Role` is STAFF
> or STUDENT, and every administrative action is gated on `User.is_staff` — so
> a staff membership without that flag is a teacher.

There is no country anywhere: no model, no field. Of the four institutions in
the development database, two are synthetic (`global`, `eval-harness`) and are
not schools.

`get_staff_context(request)` in `views.py` is the seam everything hangs off.
It returns `institution`, `all_schools`, `role_label` and three `can_*` flags,
and every staff view reaches it through the `staff_required` decorator.

## Decisions

Recorded here because each one closes off alternatives that look reasonable
later.

**A school admin does not touch content.** People and school settings only.
Curriculum upload, lesson editing and regeneration stay with the super admin,
which is a deliberate existing decision (`views.py:107-112`): uploads are
destructive and rebuild a whole course.

**A country account is a super admin scoped to one country.** Aggregate
reporting, appointing school admins, national curriculum, adding schools, and
editing or deactivating them.

**`institution=None` means "every school in this course's country", not
"every school on the platform".** There is no cross-country content tier. This
changes the meaning of a rule CLAUDE.md calls critical, and is the riskiest
part of the work.

**Existing data is backfilled to Seychelles.** The synthetic institutions get
a hidden `Platform` country so they never appear in a ministry's school list.

**Auth uses the existing `Membership` plus a new `CountryMembership`**, rather
than one nullable-scope table or a polymorphic role table. `Membership`'s
`unique_together (user, institution)` and every existing
`Membership.objects.filter(institution=…)` keep working untouched, and a
user↔country link is a genuinely different relation from a user↔school one.

## Design

### 1. Data model

```
Country
    name              CharField
    slug              SlugField, unique
    default_locale    CharField, choices=settings.LANGUAGES
    is_hidden         BooleanField(default=False)

Institution
    + country         FK(Country, on_delete=PROTECT)     non-null after backfill

CountryMembership
    user              FK(User, related_name='country_memberships')
    country           FK(Country, related_name='memberships')
    is_active         BooleanField(default=True)
    unique_together   (user, country)

Membership.Role
    + SCHOOL_ADMIN = 'school_admin', 'School Admin'

Course
    + country         FK(Country, on_delete=PROTECT)     non-null after backfill
```

`is_hidden` is what keeps `global` and `eval-harness` out of a ministry's
school list. Without it every list would special-case two slugs, which is the
kind of rule that gets copied to some call sites and not others.

`CountryMembership` carries no role field on purpose. A country account is one
thing; a second country role would be the third case, and the point at which
generalising is warranted rather than speculative.

`on_delete=PROTECT` on both country FKs: deleting a country that still has
schools or courses should fail loudly, not cascade through student records.

`Course.country` is redundant for a course that has an institution — the
country is reachable through `course.institution.country`. It is stored anyway
because a shared course has no institution to reach it through, and a query
that has to branch on whether the FK is null is exactly the kind of rule that
gets applied at some call sites and not others. The cost is one denormalised
field that can disagree with its school; a model `clean()` rejects a course
whose country differs from its institution's, so the two cannot drift.

`StaffInvitation.role` already uses `Membership.Role.choices`, so adding
SCHOOL_ADMIN to the enum makes it invitable with no further change.

### 2. Permissions — `apps/accounts/scope.py`

`get_staff_context` moves out of `views.py` into its own module, as four small
functions returning one shape. Four role branches inside a 9,000-line views
module is where permission bugs hide, and this change adds the fourth.

| | `institution` | `all_schools` | `can_add_schools` | `can_manage_people` | `can_edit_content` |
|---|---|---|---|---|---|
| Super Admin | any, or None | every school | yes | yes | yes |
| Country | any in country, or None | that country's | yes | yes | yes, within their country |
| School Admin | their school | their school | no | yes | no |
| Teacher | their school(s) | theirs, if >1 | no | no | no |

Two flags join the three that exist: `can_add_schools`, `can_manage_people`.

**Views ask the flag, never the role.** That is what lets the school admin
receive people-management without a second permission system growing beside
the first, and it is how the existing `can_edit_content` flag already works.

`role_label` gains `_('School Admin')` and `_('Country')`. The existing
resolution comment stays true: the label is decided in one place because
`Role.STAFF` reads "Staff (Teacher/Admin)" and names two roles while
committing to neither.

### 3. Content scoping

The rule today, quoted in CLAUDE.md as critical:

```python
Q(institution=inst) | Q(institution__isnull=True)
```

becomes "my school's content, plus my country's shared content":

```python
Q(institution=inst) | Q(institution__isnull=True, country=inst.country)
```

**53 call sites across 15 files** use the current pattern:

```
curriculum/curriculum_pack.py     curriculum/knowledge_base.py
api/views/sessions.py             api/views/sync.py
api/views/offline_pack.py         tutoring/views.py
tutoring/conversational_tutor.py  tutoring/simple_tutor/warm_up.py
accounts/context_processors.py    support/tools.py
llm/prompts.py                    dashboard/views.py
dashboard/models.py               dashboard/material_tasks.py
desktop/packs.py
```

Editing 53 sites by hand and missing one is a cross-country leak. So the rule
is extracted, which CLAUDE.md already asks for — it names this pattern as past
the Rule of Three and says to extract a manager method when next touched.

It is **one function, not a manager method**, in a new `apps/accounts/tenancy.py`:

```python
def visible_to(queryset, institution):
    """Rows this school may see: its own, plus its country's shared rows."""
```

A manager method would mean touching the manager of all fifteen models that
carry an `institution` FK, across six apps — `accounts`, `curriculum`,
`dashboard`, `llm`, `media_library`, `tutoring`. A free function over a
queryset needs no model surgery, works for every one of them, and is a single
place for the guard test to point at.

**It does not replace `filter_by_institution`.** That helper exists in
`views.py:205`, has 36 callers, and answers a different question: when
`institution` is None it returns *everything*, which is the super admin's
aggregated view. The `Q()` pattern answers "what may this one school see".
Conflating them would give a school every other school's content. They stay
separate, and the spec is explicit about it because the names are close enough
to invite the mistake.

A guard test asserts `institution__isnull=True` appears nowhere outside
`tenancy.py`, so a new call site cannot quietly reintroduce the old semantics
— the same ratchet shape as the CSS migration's guards.

### 4. What the two roles see

**Country account** gets one new page, **Schools**: the list for their
country, with an *Add school* form and, per school, an *Invite admin* action.
Both reuse what exists — `Institution.objects.create` at `views.py:3679` and
the `StaffInvitation` flow. Everything else is today's dashboard with
`all_schools` filtered to their country, so the school switcher already works
and the aggregate views already handle `institution=None`.

**School admin** sees today's teacher dashboard plus the Staff and Settings
pages, which are currently super-admin-gated. No new page.

Flagged-session review needs no work for either: `resolve_flag` is
`@staff_required` and already scopes by `request.staff_ctx['institution']`, so
any staff member can resolve their own school's flags today.

### 5. Migration

Three migrations, in order, so the backfill is reversible and can be dry-run
against a copy of the production dump as CLAUDE.md requires:

1. Create `Country` and `CountryMembership`. Add `Institution.country` and
   `Course.country` as **nullable**. Add `SCHOOL_ADMIN` to the role choices.
2. Data migration: create `Seychelles` and a hidden `Platform`; attach the two
   real schools and every `institution=None` course to Seychelles; attach
   `global` and `eval-harness` to `Platform`. Refuse to proceed — raise, not
   warn — if any institution or course would be left without a country.
3. Make both FKs non-null.

Splitting 1 from 3 is what makes step 2 re-runnable against a dump without
fighting a constraint that cannot yet be satisfied.

### 6. Testing

Cross-tenant isolation is the point of the feature, so it is asserted per
surface rather than once. A Tanzania country account must not see a Seychelles
school, its students, its courses, its sessions, or its flagged sessions —
five tests, not one.

Beyond that:

* `visible_to` returns a school's own content and its country's shared
  content, and never another country's.
* The guard test: no `institution__isnull=True` outside the manager.
* A hidden country's institutions never appear in any school list.
* Each `can_*` flag is true for exactly the roles in the table above — a
  parametrised test over the four roles, so adding a fifth fails loudly.
* A school admin cannot reach the curriculum upload or lesson edit views.

## Out of scope

* A country-level equivalent of the super admin's platform-wide content. There
  is deliberately no cross-country tier.
* Any change to what a teacher can do.
* Multi-country accounts. `CountryMembership` allows the rows, but no UI
  offers a country switcher; if that is wanted it is a later, separate change.
* Retiring `User.is_staff` as the super-admin marker.
