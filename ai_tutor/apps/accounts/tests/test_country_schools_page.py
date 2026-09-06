"""The country account's Schools page."""

import pytest
from django.contrib.auth import get_user_model

from ai_tutor.apps.accounts.models import (
    Country, CountryMembership, Institution, Membership)

User = get_user_model()


@pytest.fixture
def world(db):
    sc = Country.objects.create(name='Seychelles', slug='sc', default_locale='en-us')
    tz = Country.objects.create(name='Tanzania', slug='tz', default_locale='sw')
    a = Institution.objects.create(name='Anse Royale', slug='anse-royale', country=sc)
    b = Institution.objects.create(name='Beau Vallon', slug='beau-vallon', country=sc)
    c = Institution.objects.create(name='Dodoma Sec', slug='dodoma-sec', country=tz)
    return sc, tz, a, b, c


def _ministry_of(country, username='m'):
    user = User.objects.create_user(username=username, password='pw')
    CountryMembership.objects.create(user=user, country=country)
    return user


@pytest.mark.django_db
def test_the_page_lists_only_this_countrys_schools(client, world):
    sc, _, a, b, c = world
    client.force_login(_ministry_of(sc))

    body = client.get('/dashboard/schools/').content.decode()

    assert a.name in body and b.name in body
    assert c.name not in body


@pytest.mark.django_db
def test_a_created_school_lands_in_the_creators_country(client, world):
    sc, _, _, _, _ = world
    client.force_login(_ministry_of(sc, 'm2'))

    client.post('/dashboard/schools/create/', {'name': 'New High', 'slug': 'new-high'})

    assert Institution.objects.get(slug='new-high').country == sc


@pytest.mark.django_db
def test_a_created_school_takes_its_countrys_language(client, world):
    _, tz, _, _, _ = world
    client.force_login(_ministry_of(tz, 'm3'))

    client.post('/dashboard/schools/create/', {'name': 'Mwanza Sec', 'slug': 'mwanza'})

    assert Institution.objects.get(slug='mwanza').default_locale == 'sw'


@pytest.mark.django_db
def test_a_slug_is_made_from_the_name_when_left_blank(client, world):
    sc, _, _, _, _ = world
    client.force_login(_ministry_of(sc, 'm4'))

    client.post('/dashboard/schools/create/', {'name': 'Praslin Secondary', 'slug': ''})

    assert Institution.objects.filter(slug='praslin-secondary').exists()


@pytest.mark.django_db
def test_a_duplicate_short_name_is_refused_not_crashed(client, world):
    sc, _, a, _, _ = world
    client.force_login(_ministry_of(sc, 'm5'))
    before = Institution.objects.count()

    resp = client.post('/dashboard/schools/create/',
                       {'name': 'Another', 'slug': a.slug}, follow=True)

    assert resp.status_code == 200
    assert Institution.objects.count() == before


@pytest.mark.django_db
def test_a_teacher_cannot_reach_the_page(client, world):
    _, _, a, _, _ = world
    user = User.objects.create_user(username='t2', password='pw')
    Membership.objects.create(user=user, institution=a, role=Membership.Role.STAFF)
    client.force_login(user)

    assert client.get('/dashboard/schools/').status_code in (302, 403)


@pytest.mark.django_db
def test_a_teacher_cannot_post_a_school(client, world):
    _, _, a, _, _ = world
    user = User.objects.create_user(username='t3', password='pw')
    Membership.objects.create(user=user, institution=a, role=Membership.Role.STAFF)
    client.force_login(user)

    client.post('/dashboard/schools/create/', {'name': 'Sneaky', 'slug': 'sneaky'})

    assert not Institution.objects.filter(slug='sneaky').exists()


@pytest.mark.django_db
def test_a_school_admin_cannot_reach_the_page(client, world):
    """A school admin runs one school; it does not add more."""
    _, _, a, _, _ = world
    user = User.objects.create_user(username='sa', password='pw')
    Membership.objects.create(
        user=user, institution=a, role=Membership.Role.SCHOOL_ADMIN)
    client.force_login(user)

    assert client.get('/dashboard/schools/').status_code in (302, 403)


@pytest.mark.django_db
def test_the_counts_are_the_schools_own(client, world):
    sc, _, a, b, _ = world
    client.force_login(_ministry_of(sc, 'm6'))
    for i in range(3):
        u = User.objects.create_user(username=f's{i}', password='pw')
        Membership.objects.create(user=u, institution=a, role=Membership.Role.STUDENT)
    other = User.objects.create_user(username='elsewhere', password='pw')
    Membership.objects.create(user=other, institution=b, role=Membership.Role.STUDENT)

    resp = client.get('/dashboard/schools/')
    counts = {s.name: s.student_count for s in resp.context['schools']}

    assert counts[a.name] == 3
    assert counts[b.name] == 1
