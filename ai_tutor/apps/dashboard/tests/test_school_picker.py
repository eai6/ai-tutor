"""The school picker in the dashboard header.

"Global (All Schools)" is a container row — Institution.get_global(), slug
'global' — that exists so an upload made in All-Schools mode has a non-null FK
to hang media and extracted skills off. It is not a school: no members, no
roster, nothing to teach. Listing it among the real ones put a second "All
Schools" halfway down an alphabetical list, below Belonie and above La Digue.

The student catalog (tutoring/views.py) and the accounts school list already
excluded it; the dashboard did not.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership


@pytest.fixture
def schools(db):
    Institution.objects.create(name='Belonie Secondary', slug='belonie')
    Institution.objects.create(name='La Digue Secondary', slug='la-digue')
    return Institution.get_global()


@pytest.fixture
def superadmin(db):
    return User.objects.create_user('root', 'r@example.com', 'pw', is_staff=True)


@pytest.mark.django_db
class TestTheGlobalRowIsNotASchool:

    def test_it_is_not_offered_in_the_picker(self, client, superadmin, schools):
        client.force_login(superadmin)
        response = client.get(reverse('dashboard:home'))

        names = [s.name for s in response.context['all_schools']]
        assert names == ['Belonie Secondary', 'La Digue Secondary']
        assert 'Global (All Schools)' not in response.content.decode()

    def test_all_schools_is_still_there(self, client, superadmin, schools):
        """The aggregated option is a real one — it is only the duplicate in
        the middle of the list that goes."""
        client.force_login(superadmin)
        response = client.get(reverse('dashboard:home'))
        assert response.context['is_aggregated'] is True
        assert 'All Schools' in response.content.decode()

    def test_a_session_already_on_it_falls_back_to_aggregated(
            self, client, superadmin, schools):
        """Someone who picked it before the change should land on All Schools,
        not wedge on an option the picker no longer offers."""
        client.force_login(superadmin)
        session = client.session
        session['selected_school_id'] = str(schools.id)
        session.save()

        response = client.get(reverse('dashboard:home'))
        assert response.context['institution'] is None
        assert response.context['is_aggregated'] is True

    def test_a_real_school_still_selects(self, client, superadmin, schools):
        belonie = Institution.objects.get(slug='belonie')
        client.force_login(superadmin)
        session = client.session
        session['selected_school_id'] = str(belonie.id)
        session.save()

        response = client.get(reverse('dashboard:home'))
        assert response.context['institution'] == belonie
        assert response.context['is_aggregated'] is False

    def test_a_regular_teacher_never_saw_it_anyway(self, client, db, schools):
        """Their list comes from their own memberships, and nobody is a member
        of the global row. Held so a future refactor that unifies the two
        branches does not reintroduce it."""
        belonie = Institution.objects.get(slug='belonie')
        teacher = User.objects.create_user('teach', 't@example.com', 'pw')
        Membership.objects.create(user=teacher, institution=belonie,
                                  role=Membership.Role.STAFF)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:home'))
        assert schools not in list(response.context['all_schools'])
