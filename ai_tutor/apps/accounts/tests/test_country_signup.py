"""The country account's own front door: requesting one, and signing in.

The roles landed before the door did. `Membership.Role.SCHOOL_ADMIN` and
`CountryMembership` were both created, tested and shipped while the login
gate still asked its own narrower question — `is_staff` or a Membership with
role STAFF — so neither role could actually sign in. Half of this file is
about that: a gate and a room have to agree on who may enter.
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from ai_tutor.apps.accounts.models import (
    Country, CountryMembership, Institution, Membership)
from ai_tutor.apps.accounts.scope import has_staff_access

User = get_user_model()

PW = 'Correct-Horse-Battery-92'


@pytest.fixture
def countries(db):
    """get_or_create, because the migrations already put Seychelles in the
    test database and `iso_code` is unique — a second row for it is exactly
    the split this field exists to prevent."""
    sc, _ = Country.objects.get_or_create(
        iso_code='SC', defaults={'name': 'Seychelles', 'slug': 'seychelles'})
    tz, _ = Country.objects.get_or_create(
        iso_code='TZ', defaults={'name': 'Tanzania', 'slug': 'tanzania'})
    return sc, tz


@pytest.fixture(autouse=True)
def _no_lockout(db):
    """django-axes counts failures across tests in one class otherwise."""
    from axes.models import AccessAttempt
    AccessAttempt.objects.all().delete()
    yield
    AccessAttempt.objects.all().delete()


def _request(client, country, **over):
    """*country* is a Country or a bare ISO code, so a test can ask for one
    the platform has no row for yet."""
    code = getattr(country, 'iso_code', country)
    payload = {
        'first_name': 'Amina', 'last_name': 'Juma', 'username': 'ministry',
        'email': 'amina@moe.example', 'country': code,
        'organisation': 'Ministry of Education',
        'password': PW, 'password_confirm': PW, 'accept_terms': 'on',
    }
    payload.update(over)
    return client.post(reverse('accounts:country_self_register'), payload)


# ---------------------------------------------------------------------------
# Every role can reach the dashboard it was built for
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('role', ['superadmin', 'country', 'school_admin', 'staff'])
def test_every_staff_role_can_sign_in(client, countries, role):
    """The regression this file exists for. Two of these four could not."""
    sc, _ = countries
    school = Institution.objects.create(name='A', slug='a', country=sc)
    user = User.objects.create_user(username=f'u-{role}', password=PW)
    if role == 'superadmin':
        user.is_staff = True
        user.save()
    elif role == 'country':
        CountryMembership.objects.create(user=user, country=sc)
    elif role == 'school_admin':
        Membership.objects.create(user=user, institution=school,
                                  role=Membership.Role.SCHOOL_ADMIN)
    else:
        Membership.objects.create(user=user, institution=school,
                                  role=Membership.Role.STAFF)

    assert has_staff_access(user) is True
    resp = client.post(reverse('accounts:staff_login'),
                       {'username': user.username, 'password': PW})
    assert resp.status_code == 302, resp.status_code
    assert '/dashboard/' in resp['Location'] or '/terms/accept/' in resp['Location']


@pytest.mark.django_db
def test_a_student_still_has_no_staff_access(client, countries):
    sc, _ = countries
    school = Institution.objects.create(name='A', slug='a', country=sc)
    user = User.objects.create_user(username='pupil', password=PW)
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STUDENT)

    assert has_staff_access(user) is False
    resp = client.post(reverse('accounts:staff_login'),
                       {'username': 'pupil', 'password': PW})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Requesting a country account
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_form_offers_every_country_not_only_the_ones_on_the_platform(client, countries):
    """A ministry arrives before its country is on the platform. Offering only
    the existing rows would mean the form worked for nobody new."""
    resp = client.get(reverse('accounts:country_self_register'))
    assert resp.status_code == 200
    offered = dict(resp.context['countries'])
    assert len(offered) > 150
    assert 'TZ' in offered and 'SC' in offered
    assert 'MZ' in offered, 'a country with no row yet must still be offerable'


@pytest.mark.django_db
def test_every_country_is_offered_with_its_flag(client, countries):
    resp = client.get(reverse('accounts:country_self_register'))
    offered = dict(resp.context['countries'])
    assert offered['TZ'] == '\U0001F1F9\U0001F1FF Tanzania'
    assert all(label[0] == '\U0001F1E6' or ord(label[0]) >= 0x1F1E6
               for label in offered.values()), 'every option leads with a flag'


@pytest.mark.django_db
def test_the_hidden_platform_country_is_never_offered(client, countries):
    """`Country.get_platform()` is the fallback a row lands on when it has no
    country, and it is not a country. It has no ISO code, so it cannot appear
    in a list built from ISO codes — this asserts that stays true."""
    platform = Country.get_platform()
    resp = client.get(reverse('accounts:country_self_register'))
    assert platform.iso_code in (None, '')
    assert platform.name not in dict(resp.context['countries']).values()


@pytest.mark.django_db
def test_choosing_a_country_with_no_row_yet_creates_one(client, countries):
    _request(client, 'MZ')

    created = Country.objects.get(iso_code='MZ')
    assert created.name == 'Mozambique'
    assert CountryMembership.objects.get(
        user__username='ministry').country == created


@pytest.mark.django_db
def test_a_second_ministry_joins_the_row_the_first_one_made(client, countries):
    """Two rows for one country would split its schools between them, each
    invisible to the other."""
    _request(client, 'MZ')
    _request(client, 'MZ', username='ministry2')

    assert Country.objects.filter(iso_code='MZ').count() == 1


@pytest.mark.django_db
def test_an_existing_country_is_matched_not_duplicated(client, countries):
    _, tz = countries
    _request(client, tz)

    assert Country.objects.filter(iso_code='TZ').count() == 1
    assert CountryMembership.objects.get(user__username='ministry').country == tz


@pytest.mark.django_db
def test_a_request_creates_an_inactive_account_against_its_country(client, countries):
    _, tz = countries
    _request(client, tz)

    user = User.objects.get(username='ministry')
    membership = CountryMembership.objects.get(user=user)
    assert user.is_active is False
    assert membership.is_active is False
    assert membership.country == tz


@pytest.mark.django_db
def test_a_request_cannot_invent_a_country(client, countries):
    """The country is the boundary every scoped query is drawn against, so a
    posted code that is not a real one is refused rather than resolved to
    something — and no Country row is created for it."""
    client.post(reverse('accounts:country_self_register'), {
        'first_name': 'Amina', 'last_name': 'Juma', 'username': 'ministry',
        'email': 'amina@moe.example', 'country': 'ZZ',
        'organisation': 'Ministry of Education',
        'password': PW, 'password_confirm': PW, 'accept_terms': 'on',
    })

    assert not User.objects.filter(username='ministry').exists()
    assert not Country.objects.filter(iso_code='ZZ').exists()


@pytest.mark.django_db
def test_a_request_is_refused_without_the_ministry_it_speaks_for(client, countries):
    resp = _request(client, countries[1], organisation='')
    assert not User.objects.filter(username='ministry').exists()
    assert any('ministry' in str(e).lower() for e in resp.context['errors'])


@pytest.mark.django_db
def test_a_weak_password_is_refused(client, countries):
    _request(client, countries[1], password='password', password_confirm='password')
    assert not User.objects.filter(username='ministry').exists()


@pytest.mark.django_db
def test_a_duplicate_username_is_refused(client, countries):
    User.objects.create_user(username='ministry', password=PW)
    _request(client, countries[1])
    assert User.objects.filter(username='ministry').count() == 1


# ---------------------------------------------------------------------------
# Signing in, before and after approval
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_it_cannot_sign_in_before_approval(client, countries):
    _, tz = countries
    _request(client, tz)

    resp = client.post(reverse('accounts:country_login'),
                       {'username': 'ministry', 'password': PW})
    assert resp.status_code == 200
    assert 'awaiting approval' in resp.context['error'].lower()


@pytest.mark.django_db
def test_it_signs_in_once_a_platform_admin_approves(client, countries):
    _, tz = countries
    _request(client, tz)
    user = User.objects.get(username='ministry')

    admin = User.objects.create_user(username='root', password=PW, is_staff=True)
    admin_client = client.__class__()
    admin_client.force_login(admin)
    admin_client.post(reverse('dashboard:staff_list'),
                      {'action': 'toggle_user', 'user_id': user.id})

    user.refresh_from_db()
    assert user.is_active is True
    # Both records, or the account looks approved and still cannot sign in.
    assert CountryMembership.objects.get(user=user).is_active is True

    resp = client.__class__().post(reverse('accounts:country_login'),
                                   {'username': 'ministry', 'password': PW})
    assert resp.status_code == 302
    assert '/dashboard/' in resp['Location'] or '/terms/accept/' in resp['Location']


@pytest.mark.django_db
def test_a_pending_request_reaches_the_approval_queue(client, countries):
    _, tz = countries
    _request(client, tz)

    admin = User.objects.create_user(username='root', password=PW, is_staff=True)
    admin_client = client.__class__()
    admin_client.force_login(admin)
    resp = admin_client.get(reverse('dashboard:staff_list'))

    row = next((r for r in resp.context['pending_approvals']
                if r['user'].username == 'ministry'), None)
    assert row is not None, 'a country account with no Membership went unlisted'
    assert row['institutions'] == 'Tanzania'


@pytest.mark.django_db
def test_a_teacher_is_turned_away_from_the_country_door_by_name(client, countries):
    """Not "invalid password" — that sends someone to reset a password that
    was never the problem."""
    sc, _ = countries
    school = Institution.objects.create(name='A', slug='a', country=sc)
    user = User.objects.create_user(username='teach', password=PW)
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STAFF)

    resp = client.post(reverse('accounts:country_login'),
                       {'username': 'teach', 'password': PW})
    assert resp.status_code == 200
    assert 'not a country account' in resp.context['error'].lower()


@pytest.mark.django_db
def test_a_country_account_lands_on_the_dashboard_not_the_catalogue(client, countries):
    sc, _ = countries
    user = User.objects.create_user(username='m', password=PW)
    CountryMembership.objects.create(user=user, country=sc)

    resp = client.post(reverse('accounts:country_login'),
                       {'username': 'm', 'password': PW})
    assert '/dashboard/' in resp['Location'] or '/terms/accept/' in resp['Location']


@pytest.mark.django_db
def test_the_landing_page_offers_the_country_door(client, countries):
    body = client.get(reverse('accounts:landing')).content.decode()
    assert reverse('accounts:country_login') in body
    assert reverse('accounts:country_self_register') in body
