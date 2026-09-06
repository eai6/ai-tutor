"""The state the backfill migration has to leave the database in.

These assert against the migrated database rather than by calling the
migration, because what matters is the end state a deployment reaches.
"""

import pytest

from ai_tutor.apps.accounts.models import Country, Institution

SYNTHETIC = ('global', 'eval-harness')


@pytest.mark.django_db
def test_seychelles_exists_and_is_visible():
    sc = Country.objects.filter(slug='seychelles').first()
    assert sc is not None, "the backfill did not create Seychelles"
    assert sc.is_hidden is False


@pytest.mark.django_db
def test_the_synthetic_institutions_stay_hidden():
    """`global` and `eval-harness` are not schools. A ministry must never see
    them in its list, and is_hidden is what keeps them out without every
    call site special-casing two slugs."""
    for slug in SYNTHETIC:
        inst = Institution.objects.filter(slug=slug).first()
        if inst is not None:
            assert inst.country.is_hidden is True, slug


@pytest.mark.django_db
def test_no_row_is_left_without_a_country():
    """A row with no country is invisible to every scoped query, which looks
    like data loss rather than a permissions bug."""
    from ai_tutor.apps.curriculum.models import Course

    assert not Institution.objects.filter(country__isnull=True).exists()
    assert not Course.objects.filter(country__isnull=True).exists()
