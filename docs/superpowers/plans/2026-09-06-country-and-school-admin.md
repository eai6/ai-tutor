# Country Accounts and School Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a country account that administers every school in its country, and a school admin that runs one school's people and settings.

**Architecture:** `Country` and `CountryMembership` are new; `SCHOOL_ADMIN` joins the existing `Membership.Role`. `institution=None` stops meaning "every school on the platform" and starts meaning "every school in this course's country", expressed once in `apps/accounts/tenancy.py` and enforced by a guard test. `get_staff_context` moves to `apps/accounts/scope.py` and grows from two role branches to four, all returning one shape whose `can_*` flags are what views actually ask.

**Tech Stack:** Django 5, Python 3.13, pytest-django, SQLite in development.

**Spec:** `docs/superpowers/specs/2026-09-06-country-and-school-admin-design.md`

## Global Constraints

- **Test command:** `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest <path> -q` — bare `pytest` cannot work on this box.
- **Views ask a capability flag, never a role.** `staff_ctx['can_manage_people']`, never `staff_ctx['role'] == 'school_admin'`.
- **`visible_to` is not `filter_by_institution`.** The latter lives at `views.py:205`, has 36 callers, and returns *everything* when institution is None — that is the super admin's aggregated view. Conflating them hands a school every other school's content.
- **Both country FKs default to the hidden `Platform` country, never Seychelles.** 117 `Institution.objects.create` sites (113 in tests) and 108 `Course.objects.create` sites pass no country. A forgotten country must land somewhere invisible, not in a real ministry's school list.
- **Migrations:** one logical change per file, descriptive names. Nullable → backfill → non-null, so the backfill can be dry-run against a production dump.
- **Every `authenticate()` call passes `request`** — `AxesStandaloneBackend` raises without it.
- **Commit trailer:** every commit ends with `Claude-Session: https://claude.ai/code/session_0116UfmwGDtPWYnzoZdNXSdp`.

---

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `ai_tutor/apps/accounts/tenancy.py` | `visible_to(queryset, institution)` — the one definition of what a school may see |
| `ai_tutor/apps/accounts/scope.py` | `get_staff_context(request)` — one function per role, one returned shape |
| `ai_tutor/apps/accounts/tests/test_tenancy.py` | the scoping rule, and the guard that it has no rivals |
| `ai_tutor/apps/accounts/tests/test_scope.py` | the capability table, parametrised over all four roles |
| `ai_tutor/apps/accounts/tests/test_cross_country.py` | isolation, asserted per surface |
| `ai_tutor/apps/dashboard/views_country.py` | the Schools page: list, add, invite an admin |
| `ai_tutor/templates/dashboard/country/schools.html` | that page |

**Modified**

| Path | Change |
|---|---|
| `ai_tutor/apps/accounts/models.py` | `Country`, `CountryMembership`, two FKs, `SCHOOL_ADMIN` |
| `ai_tutor/apps/dashboard/views.py:38-130` | `get_staff_context` deleted, re-exported from `scope.py` |
| `ai_tutor/apps/dashboard/urls.py` | the Schools routes |
| `ai_tutor/templates/dashboard/base.html` | a Schools nav item, behind `can_add_schools` |
| `ai_tutor/templates/dashboard/settings.html:423` | Manage Schools gate widens to `can_add_schools` |

---

## Task 1: The models

**Files:**
- Modify: `ai_tutor/apps/accounts/models.py`
- Create: `ai_tutor/apps/accounts/migrations/0029_add_country.py`
- Test: `ai_tutor/apps/accounts/tests/test_country_model.py`

**Interfaces:**
- Produces: `Country(name, slug, default_locale, is_hidden)` with `Country.get_platform()` returning the hidden default; `CountryMembership(user, country, is_active)`; `Institution.country`; `Course.country`; `Membership.Role.SCHOOL_ADMIN == 'school_admin'`.

- [x] **Step 1: Write the failing test**

```python
import pytest
from ai_tutor.apps.accounts.models import Country, Institution, Membership


@pytest.mark.django_db
def test_a_school_created_without_a_country_lands_in_platform():
    """113 test call sites create an Institution with no country.

    They must keep working, and they must not land in a real ministry's
    school list — so the default is the hidden Platform country.
    """
    school = Institution.objects.create(name='Alpha', slug='alpha')
    assert school.country == Country.get_platform()
    assert school.country.is_hidden is True


@pytest.mark.django_db
def test_get_platform_is_idempotent():
    assert Country.get_platform().pk == Country.get_platform().pk


@pytest.mark.django_db
def test_school_admin_is_a_role():
    assert Membership.Role.SCHOOL_ADMIN == 'school_admin'
```

- [x] **Step 2: Run to verify it fails**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest ai_tutor/apps/accounts/tests/test_country_model.py -q`
Expected: FAIL, `ImportError: cannot import name 'Country'`.

- [x] **Step 3: Add the models**

```python
class Country(models.Model):
    """A country the platform has been adopted in.

    `is_hidden` carries the two institutions that are not schools — `global`
    and `eval-harness`. Without it every school list would special-case two
    slugs, which is the kind of rule that gets applied at some call sites and
    not others.
    """
    PLATFORM_SLUG = 'platform'

    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    default_locale = models.CharField(
        max_length=10, default='en-us', choices=django_settings.LANGUAGES)
    is_hidden = models.BooleanField(
        default=False,
        help_text="Hidden countries never appear in a school or country list.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'countries'

    def __str__(self):
        return self.name

    @classmethod
    def get_platform(cls):
        """The hidden country a record falls back to when none is given.

        Deliberately NOT Seychelles: a school created without a country is a
        bug, and it must surface somewhere invisible rather than inside a real
        ministry's list.
        """
        country, _ = cls.objects.get_or_create(
            slug=cls.PLATFORM_SLUG,
            defaults={'name': 'Platform', 'is_hidden': True},
        )
        return country


def default_country():
    """Module-level so migrations can serialise the reference."""
    return Country.get_platform().pk


class CountryMembership(models.Model):
    """Links a user to a country they administer.

    No role field: a country account is one thing. A second country role
    would be the third case, and the point at which generalising is
    warranted rather than speculative.
    """
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='country_memberships')
    country = models.ForeignKey(
        Country, on_delete=models.CASCADE, related_name='memberships')
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'country']
        ordering = ['country', 'user']

    def __str__(self):
        return f"{self.user.username} - {self.country.name}"
```

On `Institution`, after `slug`:

```python
    country = models.ForeignKey(
        'Country', on_delete=models.PROTECT, related_name='institutions',
        default=default_country)
```

On `Membership.Role`, between STAFF and STUDENT:

```python
        SCHOOL_ADMIN = 'school_admin', 'School Admin'
```

- [x] **Step 4: Add the same FK to `Course`**

In `ai_tutor/apps/curriculum/models.py`, on `Course`:

```python
    country = models.ForeignKey(
        'accounts.Country', on_delete=models.PROTECT, related_name='courses',
        default='ai_tutor.apps.accounts.models.default_country',
        help_text=(
            "Which country's schools may see this course. Redundant when "
            "`institution` is set — the country is reachable through the "
            "school — but a shared course has no school to reach it through, "
            "and a query that branches on whether the FK is null is exactly "
            "the rule that gets applied inconsistently."))
```

Import `default_country` at the top of the module rather than naming it as a string.

- [x] **Step 5: Reject a course whose country disagrees with its school**

```python
    def clean(self):
        super().clean()
        if self.institution_id and self.country_id:
            if self.institution.country_id != self.country_id:
                raise ValidationError({
                    'country': "A course's country must match its school's.",
                })
```

- [x] **Step 6: Make the migrations**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python manage.py makemigrations accounts curriculum`
Rename them to `0029_add_country.py` and `0035_add_course_country.py`.

- [x] **Step 7: Run the tests**

Expected: PASS.

- [x] **Step 8: Run the wider suite to prove the default protects existing tests**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest ai_tutor/apps/accounts ai_tutor/apps/dashboard -q`
Expected: no new failures. If any test fails on a missing country, the default is not wired.

- [x] **Step 9: Commit**

---

## Task 2: The backfill

**Files:**
- Create: `ai_tutor/apps/accounts/migrations/0030_backfill_seychelles.py`
- Test: `ai_tutor/apps/accounts/tests/test_backfill.py`

**Interfaces:**
- Consumes: `Country`, `Institution.country`, `Course.country` from Task 1.
- Produces: a `seychelles` country holding every real school and every previously platform-wide course.

- [x] **Step 1: Write the failing test**

```python
import pytest
from django.core.management import call_command
from ai_tutor.apps.accounts.models import Country, Institution


@pytest.mark.django_db
def test_the_synthetic_institutions_stay_hidden():
    """`global` and `eval-harness` are not schools. A ministry must never
    see them in its list."""
    for slug in ('global', 'eval-harness'):
        inst = Institution.objects.filter(slug=slug).first()
        if inst:
            assert inst.country.is_hidden is True


@pytest.mark.django_db
def test_every_real_school_has_a_visible_country():
    for inst in Institution.objects.exclude(slug__in=('global', 'eval-harness')):
        assert inst.country is not None
```

- [x] **Step 2: Run to verify it fails.** Expected: FAIL — no Seychelles country exists.

- [x] **Step 3: Write the data migration**

```python
SYNTHETIC = ('global', 'eval-harness')


def forwards(apps, schema_editor):
    Country = apps.get_model('accounts', 'Country')
    Institution = apps.get_model('accounts', 'Institution')
    Course = apps.get_model('curriculum', 'Course')

    platform, _ = Country.objects.get_or_create(
        slug='platform', defaults={'name': 'Platform', 'is_hidden': True})
    seychelles, _ = Country.objects.get_or_create(
        slug='seychelles',
        defaults={'name': 'Seychelles', 'default_locale': 'en-us'})

    Institution.objects.filter(slug__in=SYNTHETIC).update(country=platform)
    Institution.objects.exclude(slug__in=SYNTHETIC).update(country=seychelles)

    # Content that was platform-wide becomes Seychelles-wide. Nothing is
    # visible to a future Tanzania account until deliberately created there.
    Course.objects.filter(institution__isnull=True).update(country=seychelles)
    for course in Course.objects.filter(institution__isnull=False).select_related('institution'):
        Course.objects.filter(pk=course.pk).update(country=course.institution.country_id)

    stranded = (Institution.objects.filter(country__isnull=True).count()
                + Course.objects.filter(country__isnull=True).count())
    if stranded:
        raise RuntimeError(
            f"{stranded} rows would be left without a country. "
            "Refusing to continue — a row with no country is invisible to "
            "every scoped query, which looks like data loss.")


def backwards(apps, schema_editor):
    """Reversible: the FKs are still nullable at this point."""
    apps.get_model('accounts', 'Institution').objects.update(country=None)
    apps.get_model('curriculum', 'Course').objects.update(country=None)
```

- [x] **Step 4: Run the tests.** Expected: PASS.

- [x] **Step 5: Prove it reverses**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python manage.py migrate accounts 0029 && DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python manage.py migrate accounts`
Expected: both directions succeed.

- [x] **Step 6: Commit**

---

## Task 3: The scoping rule

**Files:**
- Create: `ai_tutor/apps/accounts/tenancy.py`
- Test: `ai_tutor/apps/accounts/tests/test_tenancy.py`

**Interfaces:**
- Produces: `visible_to(queryset, institution)` — returns rows whose `institution` is that school, plus rows with no institution whose `country` matches the school's. With `institution=None` it returns the queryset unchanged, because the caller is already in an aggregated context.

- [x] **Step 1: Write the failing test**

```python
import pytest
from ai_tutor.apps.accounts.models import Country, Institution
from ai_tutor.apps.accounts.tenancy import visible_to
from ai_tutor.apps.curriculum.models import Course


@pytest.fixture
def two_countries(db):
    sc = Country.objects.create(name='Seychelles', slug='sc')
    tz = Country.objects.create(name='Tanzania', slug='tz')
    return sc, tz


@pytest.mark.django_db
def test_a_school_sees_its_own_and_its_countrys_shared_content(two_countries):
    sc, tz = two_countries
    school = Institution.objects.create(name='A', slug='a', country=sc)
    mine = Course.objects.create(title='Mine', institution=school, country=sc)
    shared = Course.objects.create(title='National', institution=None, country=sc)
    theirs = Course.objects.create(title='Theirs', institution=None, country=tz)

    seen = set(visible_to(Course.objects.all(), school).values_list('pk', flat=True))
    assert seen == {mine.pk, shared.pk}
    assert theirs.pk not in seen


@pytest.mark.django_db
def test_another_countrys_school_content_is_never_visible(two_countries):
    sc, tz = two_countries
    mine = Institution.objects.create(name='A', slug='a', country=sc)
    theirs = Institution.objects.create(name='B', slug='b', country=tz)
    Course.objects.create(title='Theirs', institution=theirs, country=tz)

    assert visible_to(Course.objects.all(), mine).count() == 0


@pytest.mark.django_db
def test_none_means_aggregated_and_filters_nothing(two_countries):
    sc, _ = two_countries
    school = Institution.objects.create(name='A', slug='a', country=sc)
    Course.objects.create(title='X', institution=school, country=sc)
    assert visible_to(Course.objects.all(), None).count() == 1
```

- [x] **Step 2: Run to verify it fails.** Expected: FAIL, `ModuleNotFoundError: ai_tutor.apps.accounts.tenancy`.

- [x] **Step 3: Write it**

```python
"""What one school may see.

The single definition of the multi-tenancy rule CLAUDE.md calls critical.

It is a function over a queryset rather than a manager method because fifteen
models across six apps carry an `institution` FK — accounts, curriculum,
dashboard, llm, media_library, tutoring — and a manager method would mean
surgery on every one of them.

It is NOT `dashboard.views.filter_by_institution`. That helper has 36 callers
and answers a different question: when institution is None it returns
everything, which is the super admin's aggregated view. This one answers
"what may this one school see". The names are close enough to invite the
mistake, so they are kept apart deliberately.
"""

from django.db.models import Q


def visible_to(queryset, institution, field='institution'):
    """Rows *institution* may see: its own, plus its country's shared rows.

    A shared row is one with no institution. Before countries existed those
    were visible to every school on the platform; they are now visible to
    every school in the same country, which is what stops a national
    curriculum leaking across a border.

    `institution=None` means the caller is already aggregating — a super
    admin looking at everything — so nothing is filtered.
    """
    if institution is None:
        return queryset
    shared = {f"{field}__isnull": True, "country": institution.country_id}
    return queryset.filter(Q(**{field: institution}) | Q(**shared))
```

- [x] **Step 4: Run the tests.** Expected: PASS.

- [x] **Step 5: Write the guard test**

```python
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[4]
ALLOWED = {'ai_tutor/apps/accounts/tenancy.py'}


def test_nothing_outside_tenancy_writes_the_rule_by_hand():
    """The rule has one definition.

    53 call sites used to spell it out, and a site that keeps the old spelling
    keeps the old meaning — content shared with every country rather than one.
    """
    offenders = []
    for path in (REPO / 'ai_tutor' / 'apps').rglob('*.py'):
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWED or '/tests/' in rel or '/migrations/' in rel:
            continue
        if re.search(r'institution__isnull\s*=\s*True', path.read_text()):
            offenders.append(rel)
    assert offenders == [], (
        f"{len(offenders)} file(s) still spell the tenancy rule out by hand")
```

- [x] **Step 6: Run it.** Expected: FAIL, listing 15 files. That is the worklist for Task 4.

- [x] **Step 7: Commit** — the guard fails; note the count in the commit body.

---

## Task 4: Convert the 53 call sites

**Files:**
- Modify: the 15 files the guard test names.
- Test: `ai_tutor/apps/accounts/tests/test_tenancy.py` (the guard turns green)

**Interfaces:**
- Consumes: `visible_to(queryset, institution, field='institution')` from Task 3.

- [x] **Step 1: List the work**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest ai_tutor/apps/accounts/tests/test_tenancy.py -q`
The failure names every file.

- [x] **Step 2: Convert one file, starting with `curriculum/knowledge_base.py`**

The shape to look for, and what it becomes:

```python
# before
qs = Course.objects.filter(Q(institution=inst) | Q(institution__isnull=True))
# after
from ai_tutor.apps.accounts.tenancy import visible_to
qs = visible_to(Course.objects.all(), inst)
```

Where the FK is reached through a relation, pass the path:

```python
# before
Q(course__institution=inst) | Q(course__institution__isnull=True)
# after
visible_to(qs, inst, field='course__institution')
```

Note the `country` lookup in `visible_to` is not prefixed — a related
`country` needs its own path, so for those sites write the filter inline and
add the file to `ALLOWED` with a comment saying why.

- [x] **Step 3: Run that app's tests after each file**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest ai_tutor/apps/<app> -q`
Expected: PASS before moving to the next file.

- [x] **Step 4: Repeat for all 15 files.**

- [x] **Step 5: Run the guard.** Expected: PASS.

- [x] **Step 6: Run the whole suite.**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest ai_tutor -q`
Expected: no new failures.

- [x] **Step 7: Commit**

---

## Task 5: The scope module

**Files:**
- Create: `ai_tutor/apps/accounts/scope.py`
- Modify: `ai_tutor/apps/dashboard/views.py:38-130`
- Test: `ai_tutor/apps/accounts/tests/test_scope.py`

**Interfaces:**
- Produces: `get_staff_context(request)` returning `{membership, institution, role, role_label, all_schools, is_aggregated, unreviewed_flag_count, can_edit_content, can_upload_curriculum, can_regenerate_courses, can_add_schools, can_manage_people, country}` or `None`.
- `role` is one of `'superadmin' | 'country' | 'school_admin' | 'staff'`.

- [x] **Step 1: Write the failing test**

```python
import pytest
from django.contrib.auth import get_user_model
from ai_tutor.apps.accounts.models import (
    Country, CountryMembership, Institution, Membership)
from ai_tutor.apps.accounts.scope import get_staff_context

User = get_user_model()

CAPABILITIES = {
    'superadmin':   dict(can_add_schools=True,  can_manage_people=True,  can_edit_content=True),
    'country':      dict(can_add_schools=True,  can_manage_people=True,  can_edit_content=True),
    'school_admin': dict(can_add_schools=False, can_manage_people=True,  can_edit_content=False),
    'staff':        dict(can_add_schools=False, can_manage_people=False, can_edit_content=False),
}


@pytest.fixture
def world(db):
    sc = Country.objects.create(name='Seychelles', slug='sc')
    tz = Country.objects.create(name='Tanzania', slug='tz')
    a = Institution.objects.create(name='A', slug='a', country=sc)
    b = Institution.objects.create(name='B', slug='b', country=sc)
    c = Institution.objects.create(name='C', slug='c', country=tz)
    return sc, tz, a, b, c


def _request(rf, user):
    request = rf.get('/dashboard/')
    request.user = user
    request.session = {}
    return request


@pytest.mark.parametrize('role', sorted(CAPABILITIES))
@pytest.mark.django_db
def test_each_role_has_exactly_its_capabilities(rf, world, role):
    """Parametrised so a fifth role cannot be added without deciding this."""
    sc, _, a, _, _ = world
    user = User.objects.create_user(username=role, password='x')
    if role == 'superadmin':
        user.is_staff = True
        user.save()
    elif role == 'country':
        CountryMembership.objects.create(user=user, country=sc)
    elif role == 'school_admin':
        Membership.objects.create(user=user, institution=a, role=Membership.Role.SCHOOL_ADMIN)
    else:
        Membership.objects.create(user=user, institution=a, role=Membership.Role.STAFF)

    ctx = get_staff_context(_request(rf, user))
    assert ctx['role'] == role
    for flag, expected in CAPABILITIES[role].items():
        assert ctx[flag] is expected, f"{role}.{flag}"


@pytest.mark.django_db
def test_a_country_account_sees_only_its_own_countrys_schools(rf, world):
    sc, _, a, b, c = world
    user = User.objects.create_user(username='ministry', password='x')
    CountryMembership.objects.create(user=user, country=sc)

    ctx = get_staff_context(_request(rf, user))
    assert {s.pk for s in ctx['all_schools']} == {a.pk, b.pk}
    assert c.pk not in {s.pk for s in ctx['all_schools']}


@pytest.mark.django_db
def test_a_hidden_country_never_appears_in_a_school_list(rf, world):
    sc, _, a, b, _ = world
    Institution.objects.create(name='H', slug='h', country=Country.get_platform())
    user = User.objects.create_user(username='m2', password='x')
    CountryMembership.objects.create(user=user, country=sc)
    ctx = get_staff_context(_request(rf, user))
    assert all(s.country.is_hidden is False for s in ctx['all_schools'])
```

- [x] **Step 2: Run to verify it fails.** Expected: FAIL, `ModuleNotFoundError`.

- [x] **Step 3: Write `scope.py`**

Move the existing body of `get_staff_context` (`views.py:38-130`) verbatim into
two functions, `_superadmin_context` and `_staff_context`, keeping every
comment. Add two:

```python
def _country_context(request, membership, selected):
    """A super admin bounded by one country."""
    schools = list(
        Institution.objects.filter(
            country=membership.country, is_active=True,
        ).exclude(country__is_hidden=True).order_by('name'))
    institution = None
    if selected and selected != 'all':
        institution = next((s for s in schools if str(s.pk) == str(selected)), None)
    return {
        'membership': None,
        'country': membership.country,
        'institution': institution,
        'role': 'country',
        'role_label': _('Country'),
        'all_schools': schools,
        'is_aggregated': institution is None,
        'unreviewed_flag_count': _safety_flag_count(institution),
        'can_edit_content': True,
        'can_upload_curriculum': True,
        'can_regenerate_courses': True,
        'can_add_schools': True,
        'can_manage_people': True,
    }


def _school_admin_context(request, membership):
    """Runs one school: its people and its settings, not its content."""
    return {
        'membership': membership,
        'country': membership.institution.country,
        'institution': membership.institution,
        'role': 'school_admin',
        'role_label': _('School Admin'),
        'all_schools': [],
        'is_aggregated': False,
        'unreviewed_flag_count': _safety_flag_count(membership.institution),
        'can_edit_content': False,
        'can_upload_curriculum': False,
        'can_regenerate_courses': False,
        'can_add_schools': False,
        'can_manage_people': True,
    }
```

Dispatch in this order — most privileged first, because a user may hold more
than one of these:

```python
def get_staff_context(request):
    selected = request.session.get('selected_school_id')
    if request.user.is_staff:
        return _superadmin_context(request, selected)
    country_membership = CountryMembership.objects.filter(
        user=request.user, is_active=True).select_related('country').first()
    if country_membership:
        return _country_context(request, country_membership, selected)
    admin = Membership.objects.filter(
        user=request.user, role=Membership.Role.SCHOOL_ADMIN, is_active=True,
    ).select_related('institution', 'institution__country').first()
    if admin:
        return _school_admin_context(request, admin)
    return _staff_context(request, selected)
```

Add `'can_add_schools': True, 'can_manage_people': True` to the superadmin
dict and `False, False` to the staff dict, plus `'country'` to both.

- [x] **Step 4: Re-export from views.py so no caller changes**

Replace the deleted body at `views.py:38` with:

```python
from ai_tutor.apps.accounts.scope import get_staff_context  # noqa: F401
```

- [x] **Step 5: Run the tests.** Expected: PASS.

- [x] **Step 6: Run the dashboard suite.** Expected: no new failures.

- [x] **Step 7: Commit**

---

## Task 6: Give the school admin its pages

**Files:**
- Modify: `ai_tutor/apps/dashboard/views.py` — the `is_superuser` gates on the staff and settings views
- Modify: `ai_tutor/templates/dashboard/settings.html:423`
- Test: `ai_tutor/apps/accounts/tests/test_school_admin_access.py`

**Interfaces:**
- Consumes: `staff_ctx['can_manage_people']`, `staff_ctx['can_add_schools']`.

- [x] **Step 1: Write the failing test**

```python
@pytest.mark.django_db
def test_a_school_admin_reaches_the_staff_page(client, world):
    sc, _, a, _, _ = world
    user = User.objects.create_user(username='sa', password='pw')
    Membership.objects.create(user=user, institution=a, role=Membership.Role.SCHOOL_ADMIN)
    client.force_login(user)
    assert client.get('/dashboard/staff/').status_code == 200


@pytest.mark.django_db
def test_a_school_admin_cannot_reach_curriculum_upload(client, world):
    """People and settings, not content. views.py:107-112 records that
    keeping uploads away from non-super-admins was deliberate."""
    sc, _, a, _, _ = world
    user = User.objects.create_user(username='sa2', password='pw')
    Membership.objects.create(user=user, institution=a, role=Membership.Role.SCHOOL_ADMIN)
    client.force_login(user)
    assert client.get('/dashboard/curriculum/upload/').status_code in (302, 403)


@pytest.mark.django_db
def test_a_teacher_still_cannot_reach_the_staff_page(client, world):
    sc, _, a, _, _ = world
    user = User.objects.create_user(username='t', password='pw')
    Membership.objects.create(user=user, institution=a, role=Membership.Role.STAFF)
    client.force_login(user)
    assert client.get('/dashboard/staff/').status_code in (302, 403)
```

- [x] **Step 2: Run to verify it fails.** Expected: the school admin is redirected.

- [x] **Step 3: Swap the role checks for flag checks**

In the staff-list, invite, delete-staff and settings views, replace

```python
if not request.user.is_superuser:
```

with

```python
if not request.staff_ctx['can_manage_people']:
```

Leave every `is_superuser` gate on a curriculum, lesson or regeneration view
exactly as it is.

- [x] **Step 4: Widen the Manage Schools gate**

`settings.html:423` — the comment says "superadmin only". Change the
surrounding condition to `{% if staff_ctx.can_add_schools %}` and update the
comment to say what it now means.

- [x] **Step 5: Run the tests.** Expected: PASS.

- [x] **Step 6: Commit**

---

## Task 7: The country Schools page

**Files:**
- Create: `ai_tutor/apps/dashboard/views_country.py`
- Create: `ai_tutor/templates/dashboard/country/schools.html`
- Modify: `ai_tutor/apps/dashboard/urls.py`, `ai_tutor/templates/dashboard/base.html`
- Test: `ai_tutor/apps/accounts/tests/test_country_schools_page.py`

**Interfaces:**
- Produces: `dashboard:country_schools` (GET) and `dashboard:country_school_create` (POST).

- [x] **Step 1: Write the failing test**

```python
@pytest.mark.django_db
def test_the_page_lists_only_this_countrys_schools(client, world):
    sc, _, a, b, c = world
    user = User.objects.create_user(username='m', password='pw')
    CountryMembership.objects.create(user=user, country=sc)
    client.force_login(user)
    body = client.get('/dashboard/schools/').content.decode()
    assert a.name in body and b.name in body
    assert c.name not in body


@pytest.mark.django_db
def test_a_created_school_lands_in_the_creators_country(client, world):
    sc, _, _, _, _ = world
    user = User.objects.create_user(username='m2', password='pw')
    CountryMembership.objects.create(user=user, country=sc)
    client.force_login(user)
    client.post('/dashboard/schools/create/', {'name': 'New High', 'slug': 'new-high'})
    assert Institution.objects.get(slug='new-high').country == sc


@pytest.mark.django_db
def test_a_teacher_cannot_reach_the_page(client, world):
    sc, _, a, _, _ = world
    user = User.objects.create_user(username='t2', password='pw')
    Membership.objects.create(user=user, institution=a, role=Membership.Role.STAFF)
    client.force_login(user)
    assert client.get('/dashboard/schools/').status_code in (302, 403)
```

- [x] **Step 2: Run to verify it fails.** Expected: 404, no such route.

- [x] **Step 3: Write the views**

```python
@staff_required
def country_schools(request):
    ctx = request.staff_ctx
    if not ctx['can_add_schools']:
        messages.error(request, _("You don't have access to that."))
        return redirect('dashboard:home')
    return render(request, 'dashboard/country/schools.html', {
        'staff_ctx': ctx,
        'schools': ctx['all_schools'],
        'country': ctx.get('country'),
    })


@staff_required
@require_POST
def country_school_create(request):
    ctx = request.staff_ctx
    if not ctx['can_add_schools']:
        messages.error(request, _("You don't have access to that."))
        return redirect('dashboard:home')
    name = (request.POST.get('name') or '').strip()
    slug = slugify(request.POST.get('slug') or name)
    if not name or not slug:
        messages.error(request, _("A school needs a name."))
        return redirect('dashboard:country_schools')
    if Institution.objects.filter(slug=slug).exists():
        messages.error(request, _("That short name is already taken."))
        return redirect('dashboard:country_schools')
    # The creator's country, never a form field: a country account may only
    # ever add schools to its own country, and a hidden input would be a
    # cross-tenant hole.
    country = ctx.get('country') or Country.get_platform()
    Institution.objects.create(name=name, slug=slug, country=country,
                               default_locale=country.default_locale)
    messages.success(request, _("%(name)s added.") % {'name': name})
    return redirect('dashboard:country_schools')
```

- [x] **Step 4: Add the routes**

```python
    path('schools/', views_country.country_schools, name='country_schools'),
    path('schools/create/', views_country.country_school_create, name='country_school_create'),
```

- [x] **Step 5: Write the template**

A page in the existing dashboard shell: a heading, a table of schools with
their student counts, and an add form. Utilities only — no new stylesheet,
no `<style>` block, no literal colour. Follow `dashboard/students/list.html`
for the table shape.

- [x] **Step 6: Add the nav item**

In `base.html`, beside Students and Classes:

```django
{% if staff_ctx.can_add_schools %}
<a href="{% url 'dashboard:country_schools' %}" class="nav-link …">
    {% icon "school" %}<span>{% trans "Schools" %}</span>
</a>
{% endif %}
```

- [x] **Step 7: Run the tests.** Expected: PASS.

- [x] **Step 8: Screenshot the page and look at it**

Per CLAUDE.md, a UI change is not verified until it has been seen. Use
`scripts/shoot.py` or drive Chromium directly, and check the table renders,
the form submits, and the flash message appears.

- [x] **Step 9: Commit**

---

## Task 8: Cross-country isolation

**Files:**
- Create: `ai_tutor/apps/accounts/tests/test_cross_country.py`

**Interfaces:**
- Consumes: everything above.

- [x] **Step 1: Write the tests — one per surface, not one for all**

```python
"""A Tanzania account must not see Seychelles.

Asserted per surface rather than once, because each surface reaches the data
by a different path and a fix to one does not fix the others.
"""

@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_school(client, world): ...

@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_students(client, world): ...

@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_courses(client, world): ...

@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_sessions(client, world): ...

@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_flagged_sessions(client, world): ...

@pytest.mark.django_db
def test_switching_to_another_countrys_school_id_is_refused(client, world):
    """selected_school_id is user input. Setting it to a school in another
    country must not widen what the session can see."""
```

Each builds two countries with a school and data in each, logs in as the
Tanzania country account, and asserts the Seychelles record is absent from
both the rendered page and the underlying queryset.

- [x] **Step 2: Run them.** Expected: PASS — if any fails, it is a real leak, not a test bug.

- [x] **Step 3: Run the whole suite.**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest ai_tutor -q`

- [x] **Step 4: Commit**

---

## Self-Review

**Spec coverage.** Data model → Task 1. Migration → Tasks 1, 2. Content scoping
→ Tasks 3, 4. Permissions/`scope.py` → Task 5. School admin's pages → Task 6.
Country's Schools page → Task 7. Testing → Tasks 5, 6, 7, 8. The spec's
"flagged review needs no work" is honoured by not appearing as a task; its
isolation is still asserted in Task 8.

**Placeholder scan.** No TBD/TODO. Task 4 lists a shape to find and a
transformation rather than 53 literal diffs, because the guard test enumerates
the files at execution time and the surrounding code differs at each site; the
before/after pair and the related-path case are both given concretely. Task 7
Step 5 describes the template by pointing at an existing one to copy rather
than inlining ~80 lines of utilities.

**Type consistency.** `visible_to(queryset, institution, field='institution')`
is the same in Tasks 3 and 4. `Country.get_platform()` and `default_country()`
match between Task 1 and Task 7. The `can_*` flag names in Task 5's dicts,
Task 5's `CAPABILITIES` table, Task 6's checks and Task 7's guard are the same
four strings. `role` values `'superadmin' | 'country' | 'school_admin' |
'staff'` match between Task 5's dispatch and its parametrised test.

---

## What the plan got wrong

Recorded because the gaps were all of one kind: the plan trusted that
widening a gate was the whole job.

**Task 6 was a one-line gate swap in the plan.** It is the largest change in
the series. The staff page reaches every user on the platform and can create
platform super admins, so handing it to `can_manage_people` without scoping
every id it resolves would have been a privilege escalation, not a feature.
The plan also named the gates as `is_superuser`; they were `is_staff`.

**Task 8 was meant to be a test file.** Every one of its tests failed. The
plan assumed Tasks 3–7 had already closed the country boundary, but they
closed it only where a school was selected. `institution=None` meant "every
school on the platform" in 96 places, and a country account arrives at all of
them with no school selected. Task 8 became the fix as well as the proof.

**`can_edit_school_settings` was missing.** The spec grants a school admin
"School settings"; the plan's flag list had nothing for it, and folding it
into `can_manage_people` would have tied a school's name to its teachers'
accounts.

**The safety badge and the nav were not in any task.** The badge counted
flags from every country; the school admin's Staff page had no nav item at
all, so it was reachable only by typing the URL.

**One conversion was a false positive.** Task 4's shape-match hit
`Q(step=current_step) | Q(step__isnull=True)` in `simple_tutor/state.py`,
which is step scoping and not tenancy. 29 tests caught it.
