"""What a staff member may change about their own account.

Two things moved out of self-service on the dashboard settings page:

  * **School.** Re-pointing a membership re-scopes every query the account
    makes, so it belongs to whoever administers the school.
  * **Account deletion.** A staff account owns session records, safety flags
    and progress rows for real students.

Both were removed from the form AND refused by the view. The template half is
the label; these tests exist because only the view half is the control.
"""
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership

User = get_user_model()
BACKEND = 'django.contrib.auth.backends.ModelBackend'
URL = 'dashboard:settings'


@pytest.fixture
def teacher(db):
    home = Institution.objects.create(name='Alpha', slug='alpha', is_active=True)
    other = Institution.objects.create(name='Beta', slug='beta', is_active=True)
    user = User.objects.create_user(
        username='teach', email='t@example.com', password='pw')
    membership = Membership.objects.create(
        user=user, institution=home, role='staff', is_active=True)
    client = Client()
    client.force_login(user, backend=BACKEND)
    return {'user': user, 'membership': membership, 'other': other, 'client': client}


class TestSchool:

    def test_it_is_shown_but_not_editable(self, teacher):
        body = teacher['client'].get(reverse(URL)).content.decode()
        assert 'Alpha' in body
        assert 'id="school"' not in body
        assert 'name="school"' not in body

    def test_a_posted_school_is_ignored(self, teacher):
        """The control, not the label. Nothing renders the field any more, so a
        POST carrying one did not come from a form a person filled in."""
        teacher['client'].post(reverse(URL), {
            'action': 'account', 'first_name': 'T', 'last_name': 'X',
            'email': 't@example.com', 'preferred_locale': '',
            'school': str(teacher['other'].id),
        })
        teacher['membership'].refresh_from_db()
        assert teacher['membership'].institution.slug == 'alpha'

    def test_the_rest_of_the_profile_still_saves(self, teacher):
        """Removing one field must not break the form it sat in."""
        teacher['client'].post(reverse(URL), {
            'action': 'account', 'first_name': 'Grace', 'last_name': 'Miller',
            'email': 'grace@example.com', 'preferred_locale': '',
        })
        teacher['user'].refresh_from_db()
        assert teacher['user'].first_name == 'Grace'
        assert teacher['user'].email == 'grace@example.com'


class TestAccountDeletion:

    def test_there_is_no_delete_button(self, teacher):
        body = teacher['client'].get(reverse(URL)).content.decode()
        assert 'value="delete_account"' not in body

    def test_it_says_who_to_ask(self, teacher):
        body = teacher['client'].get(reverse(URL)).content.decode()
        assert 'Contact your school administrator to have it removed' in body

    def test_a_posted_delete_is_refused(self, teacher):
        teacher['client'].post(reverse(URL), {'action': 'delete_account'})
        assert User.objects.filter(pk=teacher['user'].pk).exists()

    def test_the_card_no_longer_shouts(self, teacher):
        """Red "Danger Zone" is the treatment for a card holding a destructive
        button. With the button gone it holds a sentence."""
        body = teacher['client'].get(reverse(URL)).content.decode()
        assert 'Danger Zone' not in body
        assert 'Deleting your account' in body


class TestSuperadmin:

    def test_school_is_not_editable_there_either(self, db):
        """A superadmin has no membership, so the dropdown could never have
        saved anything for them — it wrote through membership, which is None."""
        Institution.objects.create(name='Alpha', slug='alpha', is_active=True)
        user = User.objects.create_user(
            username='root', email='r@example.com', password='pw', is_staff=True)
        client = Client()
        client.force_login(user, backend=BACKEND)

        body = client.get(reverse(URL)).content.decode()
        assert 'name="school"' not in body
        assert 'All schools' in body

    def test_the_role_is_named_exactly(self, db):
        """Not Membership.Role's "Staff (Teacher/Admin)", which names two roles
        and commits to neither."""
        inst = Institution.objects.create(name='Alpha', slug='alpha', is_active=True)
        admin = User.objects.create_user(
            username='root2', email='r2@example.com', password='pw', is_staff=True)
        teacher = User.objects.create_user(
            username='teach2', email='t2@example.com', password='pw')
        Membership.objects.create(
            user=teacher, institution=inst, role='staff', is_active=True)

        for user, expected in ((admin, 'Super Admin'), (teacher, 'Teacher')):
            client = Client()
            client.force_login(user, backend=BACKEND)
            for url in (reverse(URL), reverse('dashboard:home')):
                body = client.get(url).content.decode()
                assert expected in body, f'{user.username} @ {url}'
                assert 'Staff (Teacher/Admin)' not in body, f'{user.username} @ {url}'
