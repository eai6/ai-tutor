"""The country account's own front door: creating one, and signing in.

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
    """Post the country sign-up form. *country* is a Country or a bare ISO
    code, so a test can ask for one the platform has no row for yet."""
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
# Creating a country account
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
def test_a_country_is_claimed_once(client, countries):
    """The gate that replaced the approval queue. A country account reaches
    every school in its country, so a second sign-up naming a country someone
    already holds would be a way in, not a second seat."""
    _, tz = countries
    _request(client, tz)

    # A fresh client: the first sign-up left the other one signed in, and the
    # view turns an authenticated visitor around before it reads the form.
    second = client.__class__()
    resp = _request(second, tz, username='ministry2',
                    email='second@moe.example')

    assert resp.status_code == 200
    assert not User.objects.filter(username='ministry2').exists()
    assert any('already has an account' in str(e) for e in resp.context['errors'])
    assert CountryMembership.objects.filter(country=tz).count() == 1


@pytest.mark.django_db
def test_an_account_left_inactive_still_holds_its_country(client, countries):
    """A ministry waiting to be let in is still that country's claim. Letting
    someone register around it would hand them the country."""
    _, tz = countries
    waiting = User.objects.create_user(username='waiting', password=PW, is_active=False)
    CountryMembership.objects.create(user=waiting, country=tz, is_active=False)

    resp = _request(client, tz)

    assert not User.objects.filter(username='ministry').exists()
    assert any('already has an account' in str(e) for e in resp.context['errors'])


@pytest.mark.django_db
def test_a_country_row_nobody_holds_is_not_a_claim(client, countries):
    """Seychelles has had a `Country` row since the migrations. A row is not
    an account, and offering the country and then refusing it would be the
    worst of both."""
    sc, _ = countries
    assert not CountryMembership.objects.filter(country=sc).exists()

    _request(client, sc)

    assert CountryMembership.objects.get(user__username='ministry').country == sc


@pytest.mark.django_db
def test_choosing_a_country_with_no_row_yet_still_creates_one(client, countries):
    """The claim is read off CountryMembership, so a country the platform has
    never heard of has to stay registrable."""
    _request(client, 'MZ')

    created = Country.objects.get(iso_code='MZ')
    assert created.name == 'Mozambique'
    assert CountryMembership.objects.get(
        user__username='ministry').country == created


@pytest.mark.django_db
def test_an_existing_country_is_matched_not_duplicated(client, countries):
    _, tz = countries
    _request(client, tz)

    assert Country.objects.filter(iso_code='TZ').count() == 1
    assert CountryMembership.objects.get(user__username='ministry').country == tz


@pytest.mark.django_db
def test_signing_up_creates_a_live_account_against_its_country(client, countries):
    """No approval queue: both records are active, or the account looks signed
    up and still cannot sign in — `has_staff_access` reads both."""
    _, tz = countries
    _request(client, tz)

    user = User.objects.get(username='ministry')
    membership = CountryMembership.objects.get(user=user)
    assert user.is_active is True
    assert membership.is_active is True
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
# Signing in
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_signing_up_lands_straight_in_the_dashboard(client, countries):
    """The form used to end on a "we'll be in touch" page. It signs the team
    in instead — the whole point of dropping the approval gate."""
    _, tz = countries
    resp = _request(client, tz)

    assert resp.status_code == 302
    assert '/dashboard/' in resp['Location'] or '/terms/accept/' in resp['Location']
    assert client.session.get('_auth_user_id') == str(
        User.objects.get(username='ministry').id)


@pytest.mark.django_db
def test_it_can_sign_in_again_on_a_fresh_client(client, countries):
    """Being signed in by the sign-up itself is not the same as being able to
    come back tomorrow — that is what the two is_active flags decide."""
    _, tz = countries
    _request(client, tz)

    resp = client.__class__().post(reverse('accounts:country_login'),
                                   {'username': 'ministry', 'password': PW})
    assert resp.status_code == 302
    assert '/dashboard/' in resp['Location'] or '/terms/accept/' in resp['Location']


@pytest.mark.django_db
def test_a_new_country_account_does_not_queue_for_approval(client, countries):
    _, tz = countries
    _request(client, tz)

    admin = User.objects.create_user(username='root', password=PW, is_staff=True)
    admin_client = client.__class__()
    admin_client.force_login(admin)
    resp = admin_client.get(reverse('dashboard:staff_list'))

    assert not any(r['user'].username == 'ministry'
                   for r in resp.context['pending_approvals'])


@pytest.mark.django_db
def test_an_account_left_inactive_still_lists_and_still_hears_why(client, countries):
    """Accounts made before the gate came down are still inactive. The queue
    that lists them and the sign-in message that explains them both stay — the
    original regression here was a country account going unlisted because it
    has a CountryMembership and no Membership."""
    _, tz = countries
    legacy = User.objects.create_user(username='legacy', password=PW, is_active=False)
    CountryMembership.objects.create(user=legacy, country=tz, is_active=False)

    admin = User.objects.create_user(username='root', password=PW, is_staff=True)
    admin_client = client.__class__()
    admin_client.force_login(admin)
    resp = admin_client.get(reverse('dashboard:staff_list'))

    row = next((r for r in resp.context['pending_approvals']
                if r['user'].username == 'legacy'), None)
    assert row is not None, 'a country account with no Membership went unlisted'
    assert row['institutions'] == 'Tanzania'

    denied = client.post(reverse('accounts:country_login'),
                         {'username': 'legacy', 'password': PW})
    assert denied.status_code == 200
    assert 'awaiting approval' in denied.context['error'].lower()


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
def test_the_landing_page_sends_an_enterprise_reader_to_a_person(client, countries):
    """The card offers no form, and the page offers no enterprise door at all.
    A country is claimed once and reaches every school in it, so both the
    first account and the way back into it start with a conversation."""
    body = client.get(reverse('accounts:landing')).content.decode()

    assert 'mailto:' in body
    assert 'Contact us for onboarding' in body
    assert reverse('accounts:country_self_register') not in body
    assert reverse('accounts:country_login') not in body, 'no self-service door either'


@pytest.mark.django_db
def test_the_landing_page_calls_that_door_enterprise_hosting(client, countries):
    """The card is read by a ministry or a school group, neither of which
    thinks of itself as "Countries"."""
    body = client.get(reverse('accounts:landing')).content.decode()
    assert 'Enterprise Hosting' in body
    assert '>Countries<' not in body


@pytest.mark.django_db
def test_the_landing_page_no_longer_offers_to_take_a_request(client, countries):
    """There is nothing to request — the sign-up is the access."""
    body = client.get(reverse('accounts:landing')).content.decode()
    assert 'Request access' not in body


# ---------------------------------------------------------------------------
# The country team: the only other way into a country
# ---------------------------------------------------------------------------

def _country_client(client, country, **over):
    """Sign up for *country* and hand back the signed-in client."""
    _request(client, country, **over)
    return client


@pytest.mark.django_db
def test_the_holder_can_add_a_colleague(client, countries):
    """Self-registration claims a country once, so if this did not work the
    second person at the ministry would have no way in at all."""
    _, tz = countries
    holder = _country_client(client, tz)

    resp = holder.post(reverse('dashboard:staff_list'), {
        'action': 'create_country_member',
        'member_email': 'colleague@moe.example',
        'member_first_name': 'Neema', 'member_last_name': 'Mushi',
        'member_password': PW,
    })
    assert resp.status_code == 302

    member = User.objects.get(email='colleague@moe.example')
    assert member.is_active is True
    assert CountryMembership.objects.get(user=member, country=tz).is_active is True

    signed_in = client.__class__().post(reverse('accounts:country_login'),
                                        {'username': 'colleague@moe.example',
                                         'password': PW})
    assert signed_in.status_code == 302
    assert '/dashboard/' in signed_in['Location'] or '/terms/accept/' in signed_in['Location']


@pytest.mark.django_db
def test_the_colleague_is_scoped_to_the_same_country_and_no_further(client, countries):
    sc, tz = countries
    holder = _country_client(client, tz)
    holder.post(reverse('dashboard:staff_list'), {
        'action': 'create_country_member', 'member_email': 'c@moe.example',
        'member_first_name': 'Neema', 'member_password': PW,
    })

    member = User.objects.get(email='c@moe.example')
    assert member.is_staff is False
    assert not CountryMembership.objects.filter(user=member, country=sc).exists()


@pytest.mark.django_db
def test_a_school_admin_cannot_add_to_a_country_team(client, countries):
    """The capability, not the action name, is the gate — a school admin
    reaches this view because it holds `can_manage_people`."""
    sc, _ = countries
    school = Institution.objects.create(name='A', slug='a', country=sc)
    admin = User.objects.create_user(username='head', password=PW)
    Membership.objects.create(user=admin, institution=school,
                              role=Membership.Role.SCHOOL_ADMIN)
    client.force_login(admin)

    client.post(reverse('dashboard:staff_list'), {
        'action': 'create_country_member', 'member_email': 'x@moe.example',
        'member_first_name': 'X', 'member_password': PW,
    })

    assert not User.objects.filter(email='x@moe.example').exists()


@pytest.mark.django_db
def test_the_team_is_listed_and_can_be_deactivated(client, countries):
    """A colleague holds a CountryMembership and no Membership, so both the
    listing and `may_manage` have to know about them — an account that cannot
    be seen is one that cannot be revoked."""
    _, tz = countries
    holder = _country_client(client, tz)
    holder.post(reverse('dashboard:staff_list'), {
        'action': 'create_country_member', 'member_email': 'c@moe.example',
        'member_first_name': 'Neema', 'member_password': PW,
    })
    member = User.objects.get(email='c@moe.example')

    listed = holder.get(reverse('dashboard:staff_list'))
    row = next((r for r in listed.context['people']
                if r['user'].id == member.id), None)
    assert row is not None, 'the colleague was invisible on the page that made them'
    assert row['institutions'] == 'Tanzania'

    holder.post(reverse('dashboard:staff_list'),
                {'action': 'toggle_user', 'user_id': member.id})
    member.refresh_from_db()
    assert member.is_active is False
    assert CountryMembership.objects.get(user=member).is_active is False


@pytest.mark.django_db
def test_one_country_cannot_touch_another_countrys_team(client, countries):
    sc, tz = countries
    other = User.objects.create_user(username='sc-team', password=PW)
    CountryMembership.objects.create(user=other, country=sc)

    holder = _country_client(client, tz)
    holder.post(reverse('dashboard:staff_list'),
                {'action': 'toggle_user', 'user_id': other.id})

    other.refresh_from_db()
    assert other.is_active is True


# ---------------------------------------------------------------------------
# The platform admin: no country of its own, and reach into every one
# ---------------------------------------------------------------------------

@pytest.fixture
def superadmin(db):
    return User.objects.create_user(username='root', password=PW, is_staff=True)


@pytest.mark.django_db
def test_a_platform_admin_can_open_a_country_nobody_holds(client, countries, superadmin):
    """Self-registration claims a country once, so without this the people who
    run the platform could not open one at all."""
    client.force_login(superadmin)

    resp = client.post(reverse('dashboard:staff_list'), {
        'action': 'create_country_member', 'member_country': 'MZ',
        'member_email': 'moz@moe.example', 'member_first_name': 'Ana',
        'member_password': PW,
    })
    assert resp.status_code == 302

    created = Country.objects.get(iso_code='MZ')
    assert created.name == 'Mozambique'
    member = User.objects.get(email='moz@moe.example')
    assert CountryMembership.objects.get(user=member).country == created

    signed_in = client.__class__().post(reverse('accounts:country_login'),
                                        {'username': 'moz@moe.example',
                                         'password': PW})
    assert signed_in.status_code == 302


@pytest.mark.django_db
def test_a_platform_admin_lands_on_the_row_a_ministry_already_made(client, countries, superadmin):
    """Two rows for one country would split its schools between them. Both
    doors match on the ISO code for that reason."""
    _request(client, 'MZ')
    made = Country.objects.get(iso_code='MZ')

    admin_client = client.__class__()
    admin_client.force_login(superadmin)
    admin_client.post(reverse('dashboard:staff_list'), {
        'action': 'create_country_member', 'member_country': 'MZ',
        'member_email': 'second@moe.example', 'member_first_name': 'Ana',
        'member_password': PW,
    })

    assert Country.objects.filter(iso_code='MZ').count() == 1
    assert CountryMembership.objects.get(
        user__email='second@moe.example').country == made


@pytest.mark.django_db
def test_a_platform_admin_is_offered_every_country(client, countries, superadmin):
    client.force_login(superadmin)
    offered = dict(client.get(reverse('dashboard:staff_list')).context['country_choices'])

    assert len(offered) > 150
    assert 'MZ' in offered, 'a country with no row yet must still be openable'


@pytest.mark.django_db
def test_a_country_account_is_never_asked_which_country(client, countries):
    """It has one, and a posted country is user input — answering it would be
    a way out of the country the account is scoped to."""
    sc, tz = countries
    holder = _country_client(client, tz)

    assert holder.get(reverse('dashboard:staff_list')).context['country_choices'] == []

    holder.post(reverse('dashboard:staff_list'), {
        'action': 'create_country_member', 'member_country': sc.iso_code,
        'member_email': 'sneaky@moe.example', 'member_first_name': 'X',
        'member_password': PW,
    })

    member = User.objects.get(email='sneaky@moe.example')
    assert CountryMembership.objects.get(user=member).country == tz


@pytest.mark.django_db
def test_a_country_that_is_not_one_is_refused(client, countries, superadmin):
    client.force_login(superadmin)

    client.post(reverse('dashboard:staff_list'), {
        'action': 'create_country_member', 'member_country': 'ZZ',
        'member_email': 'nowhere@moe.example', 'member_first_name': 'X',
        'member_password': PW,
    })

    assert not User.objects.filter(email='nowhere@moe.example').exists()
    assert not Country.objects.filter(iso_code='ZZ').exists()


@pytest.mark.django_db
def test_a_platform_admin_sees_every_country_account(client, countries, superadmin):
    sc, tz = countries
    for name, country in (('sc-team', sc), ('tz-team', tz)):
        u = User.objects.create_user(username=name, password=PW)
        CountryMembership.objects.create(user=u, country=country)

    client.force_login(superadmin)
    rows = client.get(reverse('dashboard:staff_list')).context['people']
    listed = {r['user'].username: r for r in rows}

    assert listed['sc-team']['is_country'] is True
    assert listed['sc-team']['institutions'] == 'Seychelles'
    assert listed['tz-team']['institutions'] == 'Tanzania'
