"""Countries, and the default that keeps 113 existing test call sites working."""

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


@pytest.mark.django_db
def test_a_country_cannot_be_deleted_while_it_holds_schools():
    """PROTECT, not CASCADE: deleting a country should fail loudly rather
    than take every school and student record with it."""
    from django.db.models import ProtectedError

    country = Country.objects.create(name='Testland', slug='testland')
    Institution.objects.create(name='Beta', slug='beta', country=country)
    with pytest.raises(ProtectedError):
        country.delete()


@pytest.mark.django_db
def test_one_membership_per_user_per_country():
    from django.contrib.auth import get_user_model
    from django.db import IntegrityError

    from ai_tutor.apps.accounts.models import CountryMembership

    user = get_user_model().objects.create_user(username='m', password='x')
    country = Country.objects.create(name='Testland', slug='testland')
    CountryMembership.objects.create(user=user, country=country)
    with pytest.raises(IntegrityError):
        CountryMembership.objects.create(user=user, country=country)
