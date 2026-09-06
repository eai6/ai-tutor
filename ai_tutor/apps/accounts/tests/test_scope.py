"""Who a signed-in staff user is, and what that lets them do."""

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


@pytest.mark.django_db
def test_the_most_privileged_role_wins(rf, world):
    """One person may hold several of these at once."""
    sc, _, a, _, _ = world
    user = User.objects.create_user(username='both', password='x')
    CountryMembership.objects.create(user=user, country=sc)
    Membership.objects.create(user=user, institution=a, role=Membership.Role.SCHOOL_ADMIN)

    assert get_staff_context(_request(rf, user))['role'] == 'country'


@pytest.mark.django_db
def test_a_user_with_no_staff_role_at_all_gets_nothing(rf, world):
    user = User.objects.create_user(username='student', password='x')
    assert get_staff_context(_request(rf, user)) is None


@pytest.mark.django_db
def test_a_country_account_can_narrow_to_one_of_its_schools(rf, world):
    sc, _, a, b, _ = world
    user = User.objects.create_user(username='m3', password='x')
    CountryMembership.objects.create(user=user, country=sc)

    request = _request(rf, user)
    request.session['selected_school_id'] = str(b.pk)
    ctx = get_staff_context(request)
    assert ctx['institution'] == b
    assert ctx['is_aggregated'] is False


@pytest.mark.django_db
def test_a_country_account_cannot_select_another_countrys_school(rf, world):
    """The school switcher posts an id; the id is not to be trusted."""
    sc, _, _, _, c = world
    user = User.objects.create_user(username='m4', password='x')
    CountryMembership.objects.create(user=user, country=sc)

    request = _request(rf, user)
    request.session['selected_school_id'] = str(c.pk)
    ctx = get_staff_context(request)
    assert ctx['institution'] is None
