"""What a school admin reaches, and what it must not.

A school admin runs one school: its people and its settings. It does not run
its content, and it does not run the platform. The second half of this file is
the more important half — a role that can manage people is one POST away from
managing the wrong people.
"""

import pytest
from django.contrib.auth import get_user_model

from ai_tutor.apps.accounts.models import (
    Country, CountryMembership, Institution, Membership, StaffInvitation)

User = get_user_model()


@pytest.fixture
def world(db):
    sc = Country.objects.create(name='Seychelles', slug='sc')
    tz = Country.objects.create(name='Tanzania', slug='tz')
    a = Institution.objects.create(name='A', slug='a', country=sc)
    b = Institution.objects.create(name='B', slug='b', country=sc)
    c = Institution.objects.create(name='C', slug='c', country=tz)
    return sc, tz, a, b, c


def _admin_of(school, username='sa'):
    user = User.objects.create_user(username=username, password='pw')
    Membership.objects.create(
        user=user, institution=school, role=Membership.Role.SCHOOL_ADMIN)
    return user


def _teacher_at(school, username='t'):
    user = User.objects.create_user(username=username, password='pw')
    Membership.objects.create(
        user=user, institution=school, role=Membership.Role.STAFF)
    return user


# ---------------------------------------------------------------------------
# Reach
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_school_admin_reaches_the_staff_page(client, world):
    _, _, a, _, _ = world
    client.force_login(_admin_of(a))
    assert client.get('/dashboard/staff/').status_code == 200


@pytest.mark.django_db
def test_a_school_admin_cannot_reach_curriculum_upload(client, world):
    """People and settings, not content. The staff context records that
    keeping uploads away from non-super-admins was deliberate."""
    _, _, a, _, _ = world
    client.force_login(_admin_of(a, 'sa2'))
    assert client.get('/dashboard/curriculum/upload/').status_code in (302, 403)


@pytest.mark.django_db
def test_a_teacher_still_cannot_reach_the_staff_page(client, world):
    _, _, a, _, _ = world
    client.force_login(_teacher_at(a))
    resp = client.get('/dashboard/staff/')
    assert resp.status_code in (302, 403)


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_it_cannot_promote_anyone_to_platform_admin(client, world):
    """The one action that would hand out more access than the actor holds."""
    _, _, a, _, _ = world
    client.force_login(_admin_of(a, 'sa3'))
    victim = _teacher_at(a, 'victim')

    client.post('/dashboard/staff/', {'action': 'toggle_admin', 'user_id': victim.id})

    victim.refresh_from_db()
    assert victim.is_staff is False


@pytest.mark.django_db
def test_it_cannot_create_a_platform_admin(client, world):
    _, _, a, _, _ = world
    client.force_login(_admin_of(a, 'sa4'))

    client.post('/dashboard/staff/', {
        'action': 'create_admin',
        'admin_email': 'new@example.com',
        'admin_password': 'correct-horse-battery-staple-92',
    })

    assert not User.objects.filter(email='new@example.com').exists()


@pytest.mark.django_db
def test_it_cannot_deactivate_a_teacher_at_another_school(client, world):
    _, _, a, b, _ = world
    client.force_login(_admin_of(a, 'sa5'))
    outsider = _teacher_at(b, 'outsider')

    client.post('/dashboard/staff/', {'action': 'toggle_user', 'user_id': outsider.id})

    outsider.refresh_from_db()
    assert outsider.is_active is True


@pytest.mark.django_db
def test_it_cannot_delete_a_teacher_at_another_school(client, world):
    _, _, a, b, _ = world
    client.force_login(_admin_of(a, 'sa6'))
    outsider = _teacher_at(b, 'outsider2')

    client.post(f'/dashboard/staff/{outsider.id}/delete/')

    assert User.objects.filter(pk=outsider.pk).exists()


@pytest.mark.django_db
def test_it_cannot_reset_another_schools_teachers_password(client, world):
    _, _, a, b, _ = world
    client.force_login(_admin_of(a, 'sa7'))
    outsider = _teacher_at(b, 'outsider3')
    before = User.objects.get(pk=outsider.pk).password

    resp = client.post(f'/dashboard/staff/{outsider.id}/reset-password/show/')

    assert resp.status_code == 403
    assert User.objects.get(pk=outsider.pk).password == before


@pytest.mark.django_db
def test_it_cannot_invite_staff_into_another_school(client, world):
    _, _, a, b, _ = world
    client.force_login(_admin_of(a, 'sa8'))

    client.post('/dashboard/staff/', {
        'action': 'invite_staff',
        'invite_school_id': b.id,
        'invite_email': 'someone@example.com',
    })

    assert not StaffInvitation.objects.filter(institution=b).exists()


@pytest.mark.django_db
def test_it_cannot_revoke_another_schools_invite(client, world):
    _, _, a, b, _ = world
    client.force_login(_admin_of(a, 'sa9'))
    inv = StaffInvitation.objects.create(
        institution=b, email='x@example.com', role='staff', token='tok-1')

    client.post('/dashboard/staff/', {'action': 'revoke_invite', 'invite_id': inv.id})

    assert StaffInvitation.objects.filter(pk=inv.pk).exists()


@pytest.mark.django_db
def test_the_find_box_does_not_enumerate_the_platform(client, world):
    """The box searches every account by design — it exists to diagnose
    "this person cannot log in". Unscoped, it would list every user."""
    _, _, a, b, _ = world
    client.force_login(_admin_of(a, 'sa10'))
    _teacher_at(b, 'findme')

    resp = client.get('/dashboard/staff/?find=findme')

    # Asserted on the results, not the body: the box echoes the query back
    # into its own input, so the name is on the page either way.
    assert resp.context['found_users'] == []


@pytest.mark.django_db
def test_the_people_list_stops_at_the_school_boundary(client, world):
    _, _, a, b, _ = world
    client.force_login(_admin_of(a, 'sa11'))
    _teacher_at(a, 'mine')
    _teacher_at(b, 'theirs')

    body = client.get('/dashboard/staff/').content.decode()
    assert 'mine' in body
    assert 'theirs' not in body


# ---------------------------------------------------------------------------
# A country account is a super admin bounded by one country
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_country_account_manages_its_own_countrys_teachers(client, world):
    sc, _, a, b, c = world
    user = User.objects.create_user(username='ministry', password='pw')
    CountryMembership.objects.create(user=user, country=sc)
    client.force_login(user)
    _teacher_at(a, 'in-country')
    _teacher_at(c, 'abroad')

    body = client.get('/dashboard/staff/').content.decode()
    assert 'in-country' in body
    assert 'abroad' not in body


@pytest.mark.django_db
def test_a_country_account_cannot_create_a_platform_admin(client, world):
    sc, _, _, _, _ = world
    user = User.objects.create_user(username='ministry2', password='pw')
    CountryMembership.objects.create(user=user, country=sc)
    client.force_login(user)

    client.post('/dashboard/staff/', {
        'action': 'create_admin',
        'admin_email': 'nope@example.com',
        'admin_password': 'correct-horse-battery-staple-92',
    })

    assert not User.objects.filter(email='nope@example.com').exists()


@pytest.mark.django_db
def test_a_country_account_adds_schools_into_its_own_country(client, world):
    sc, _, _, _, _ = world
    user = User.objects.create_user(username='ministry3', password='pw')
    CountryMembership.objects.create(user=user, country=sc)
    client.force_login(user)

    client.post('/dashboard/settings/', {
        'action': 'add_school',
        'school_name': 'New High',
        'school_slug': 'new-high',
        'school_timezone': 'UTC',
    })

    assert Institution.objects.get(slug='new-high').country == sc


@pytest.mark.django_db
def test_a_country_account_cannot_deactivate_another_countrys_school(client, world):
    sc, _, _, _, c = world
    user = User.objects.create_user(username='ministry4', password='pw')
    CountryMembership.objects.create(user=user, country=sc)
    client.force_login(user)

    client.post('/dashboard/settings/', {'action': 'toggle_school', 'school_id': c.id})

    c.refresh_from_db()
    assert c.is_active is True


@pytest.mark.django_db
def test_a_teacher_cannot_add_a_school(client, world):
    _, _, a, _, _ = world
    client.force_login(_teacher_at(a, 't3'))

    client.post('/dashboard/settings/', {
        'action': 'add_school',
        'school_name': 'Sneaky',
        'school_slug': 'sneaky',
        'school_timezone': 'UTC',
    })

    assert not Institution.objects.filter(slug='sneaky').exists()
