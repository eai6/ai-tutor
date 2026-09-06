"""The multi-tenancy rule, and the guard that keeps it to one definition."""

import pathlib
import re

import pytest

from ai_tutor.apps.accounts.models import Country, Institution
from ai_tutor.apps.accounts.tenancy import visible_to
from ai_tutor.apps.curriculum.models import Course


@pytest.fixture
def two_countries(db):
    return (
        Country.objects.create(name='Seychelles', slug='sc'),
        Country.objects.create(name='Tanzania', slug='tz'),
    )


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


@pytest.mark.django_db
def test_a_related_path_scopes_the_same_way(two_countries):
    """Many call sites reach the FK through a relation."""
    from ai_tutor.apps.curriculum.models import Unit

    sc, tz = two_countries
    mine = Institution.objects.create(name='A', slug='a', country=sc)
    theirs = Institution.objects.create(name='B', slug='b', country=tz)
    ours = Course.objects.create(title='Ours', institution=mine, country=sc)
    Unit.objects.create(course=ours, title='U1', order_index=1)
    hers = Course.objects.create(title='Hers', institution=theirs, country=tz)
    Unit.objects.create(course=hers, title='U2', order_index=1)

    seen = visible_to(Unit.objects.all(), mine,
                      field='course__institution', country_field='course__country')
    assert seen.count() == 1


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parents[4]
RULE = re.compile(r'institution__isnull\s*=\s*True')

# Only tenancy.py may spell the rule out.
ALLOWED = {'ai_tutor/apps/accounts/tenancy.py'}


@pytest.mark.xfail(strict=True, reason="true once the 53 call sites are converted")
def test_nothing_outside_tenancy_writes_the_rule_by_hand():
    """The rule has one definition.

    53 call sites used to spell it out, and a site that keeps the old spelling
    keeps the old MEANING — content shared with every country rather than one.
    That is a cross-border leak, and it is invisible until someone from
    another country looks.
    """
    offenders = []
    for path in sorted((REPO / 'ai_tutor' / 'apps').rglob('*.py')):
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWED or '/tests/' in rel or '/migrations/' in rel:
            continue
        if RULE.search(path.read_text(errors='ignore')):
            offenders.append(rel)
    assert offenders == [], (
        f"{len(offenders)} file(s) still spell the tenancy rule out by hand:\n  "
        + "\n  ".join(offenders)
    )
