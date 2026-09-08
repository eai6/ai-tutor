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

# Only tenancy.py may spell the rule out — plus the places where "no
# institution" genuinely still means "the platform", each named with why.
ALLOWED = {
    'ai_tutor/apps/accounts/tenancy.py',
    # PromptPack and ModelConfig are platform CONFIGURATION, not curriculum.
    # A platform-wide prompt pack is deliberately platform-wide; scoping it
    # by country would leave a country with no prompts at all.
    'ai_tutor/apps/llm/prompts.py',
    'ai_tutor/apps/tutoring/conversational_tutor.py',
    # A shared TeachingMaterialUpload has no institution and therefore no
    # country of its own, so there is nothing to scope the match by. Creating
    # one is a super-admin action, not a route a country account can reach.
    'ai_tutor/apps/dashboard/material_tasks.py',
}

# views.py reaches PromptPack twice for the same reason as llm/prompts.py.
# It is not in ALLOWED because the rest of the file must stay covered, so
# those two lines carry a marker instead.
CONFIG_MARKER = 'platform configuration, not curriculum'


@pytest.mark.django_db
def test_a_model_with_no_country_gets_no_country_lookup(two_countries):
    """/dashboard/curriculum/ raised FieldError for every teacher.

    `visible_q`'s shared half asks for `country__in`, and a
    TeachingMaterialUpload has no country column — the query could not be
    built at all, so the page 500'd rather than leaking anything. `in_country_q`
    had guarded this from the start; its other half had not.
    """
    from ai_tutor.apps.dashboard.models import TeachingMaterialUpload
    from ai_tutor.apps.accounts.tenancy import scope_q, visible_q

    sc, _tz = two_countries
    school = Institution.objects.create(name='S', slug='s', country=sc)

    # Told what it is filtering, it drops the half the model cannot answer.
    guarded = visible_q(school, model=TeachingMaterialUpload)
    assert 'country' not in str(guarded)
    assert TeachingMaterialUpload.objects.filter(guarded).count() == 0

    # And the same through scope_q, which is what the views call.
    assert TeachingMaterialUpload.objects.filter(
        scope_q(school, sc, model=TeachingMaterialUpload)).count() == 0

    # A model that does carry a country keeps its shared half.
    assert 'country' in str(visible_q(school, model=Course))


@pytest.mark.django_db
def test_the_curriculum_page_opens_for_a_teacher(client, two_countries):
    """The regression as a reader meets it, not as a Q object."""
    from django.contrib.auth.models import User
    from django.urls import reverse

    from ai_tutor.apps.accounts.models import Membership

    sc, _tz = two_countries
    school = Institution.objects.create(name='S', slug='s', country=sc)
    teacher = User.objects.create_user(username='t', password='Correct-Horse-Battery-92')
    Membership.objects.create(user=teacher, institution=school,
                              role=Membership.Role.STAFF)
    client.force_login(teacher)

    assert client.get(reverse('dashboard:curriculum_list')).status_code == 200


def _offending_lines(text):
    """Lines that spell the rule out and do not claim the configuration
    exemption on the line itself or the one above it."""
    lines = text.split('\n')
    out = []
    for i, line in enumerate(lines):
        if not RULE.search(line):
            continue
        # Six lines back: a multi-line reason plus the import it follows.
        context = ' '.join(lines[max(0, i - 6):i + 1])
        if CONFIG_MARKER in context:
            continue
        out.append(i + 1)
    return out


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
        hits = _offending_lines(path.read_text(errors='ignore'))
        if hits:
            offenders.append(f"{rel}:{','.join(str(h) for h in hits)}")
    assert offenders == [], (
        f"{len(offenders)} file(s) still spell the tenancy rule out by hand:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# The second guard: the aggregated view
# ---------------------------------------------------------------------------

def test_no_aggregated_query_forgets_the_country():
    """`institution=None` no longer means the platform.

    `filter_by_institution` and `get_scoped_object_or_404` fall back to the
    whole platform when both institution and country are None, because that
    IS the super admin's view. A country account reaches the same views with
    no school selected, so a call that omits `country=` shows it every other
    country's data — and reads as correct, because it is what the code said
    for years.
    """
    import ast

    path = REPO / 'ai_tutor' / 'apps' / 'dashboard' / 'views.py'
    tree = ast.parse(path.read_text())
    helpers = {'filter_by_institution', 'get_scoped_object_or_404'}

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name in helpers:
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id in helpers
                    and not any(k.arg == 'country' for k in call.keywords)):
                offenders.append(f'{call.func.id} at views.py:{call.lineno}')

    assert offenders == [], (
        f"{len(offenders)} scoped quer(ies) do not pass a country:\n  "
        + "\n  ".join(offenders)
    )


def test_the_dashboard_does_not_hand_write_the_scope_branch():
    """The same rule again, in the shape the dashboard used to write it.

        if institution is not None:
            course = get_object_or_404(Course, visible_q(institution), id=n)
        else:
            course = get_object_or_404(Course, id=n)

    26 sites had that `else`, and every one of them showed a country account
    the whole platform's curriculum. `scope_q` is the one expression that
    covers all three scopes, so `visible_q` should not appear in the
    dashboard's views at all.
    """
    path = REPO / 'ai_tutor' / 'apps' / 'dashboard' / 'views.py'
    text = path.read_text()
    lines = text.split('\n')
    offenders = [
        f'views.py:{i + 1}' for i, line in enumerate(lines)
        if 'visible_q(' in line and not line.lstrip().startswith('#')
    ]
    assert offenders == [], (
        "the dashboard reaches for visible_q instead of scope_q at:\n  "
        + "\n  ".join(offenders)
    )


def test_every_institution_branch_considers_the_country():
    """A third shape of the same mistake, and the one left to write.

    `if institution is not None:` is not wrong by itself — it is how a view
    asks "is one school selected". It becomes a leak when the other side of
    the branch, written or implied, means "the whole platform". So every one
    of them has to mention the country within sight, either by handling it or
    by resolving through `scope_q`.
    """
    path = REPO / 'ai_tutor' / 'apps' / 'dashboard' / 'views.py'
    lines = path.read_text().split('\n')

    offenders = []
    for i, line in enumerate(lines):
        if line.strip() != 'if institution is not None:':
            continue
        # Twelve lines: enough for a three-way branch that assigns an id and
        # then applies it, which is the longest of these in the file.
        window = ' '.join(lines[i:i + 12])
        if 'country' in window or 'scope_q' in window:
            continue
        offenders.append(f'views.py:{i + 1}')

    assert offenders == [], (
        f"{len(offenders)} institution branch(es) ignore the country:\n  "
        + "\n  ".join(offenders)
    )
