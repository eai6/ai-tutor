"""Claiming a roster entry: binding a local login to a server identity.

This is the step that makes offline work attributable. A student picks their
name from the roster the pack shipped; from then on their local account carries
the server user id that sync will label their sessions with.

The cases worth pinning are the ones a classroom produces: two students racing
for the same name, a name already taken, and a device that has no pack yet.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.desktop.models import DeviceState, RosterEntry


@pytest.fixture
def provisioned(db):
    inst = Institution.objects.create(name='Test School', slug='test-school')
    state = DeviceState.load()
    state.institution_id = inst.id
    state.pack_version = 1
    state.save()
    RosterEntry.objects.create(server_user_id=501, username='alice',
                               display_name='Alice Adams', grade_level='S3')
    RosterEntry.objects.create(server_user_id=502, username='bob',
                               display_name='Bob Brown')
    return inst


@pytest.mark.django_db
class TestClaimPage:
    def test_lists_unclaimed_entries(self, client, provisioned):
        body = client.get(reverse('desktop:claim')).content.decode()
        assert 'Alice Adams' in body and 'Bob Brown' in body

    def test_hides_entries_already_claimed(self, client, provisioned):
        taken = User.objects.create_user(username='someone')
        RosterEntry.objects.filter(server_user_id=501).update(local_user=taken)
        body = client.get(reverse('desktop:claim')).content.decode()
        assert 'Alice Adams' not in body
        assert 'Bob Brown' in body

    def test_search_filters(self, client, provisioned):
        body = client.get(reverse('desktop:claim'), {'q': 'bob'}).content.decode()
        assert 'Bob Brown' in body and 'Alice Adams' not in body

    def test_unprovisioned_device_is_sent_to_setup(self, client, db):
        state = DeviceState.load()
        state.pack_version = None
        state.save()
        r = client.get(reverse('desktop:claim'))
        assert r.status_code == 302 and 'setup' in r['Location']


@pytest.mark.django_db
class TestClaimSubmit:
    def _claim(self, client, server_user_id=501, password='pencil'):
        return client.post(reverse('desktop:claim_submit'),
                           {'server_user_id': server_user_id, 'password': password})

    def test_creates_a_local_account_linked_to_the_server_id(self, client, provisioned):
        r = self._claim(client)
        assert r.status_code == 302
        entry = RosterEntry.objects.get(server_user_id=501)
        assert entry.local_user is not None
        assert entry.claimed_at is not None
        assert entry.local_user.first_name == 'Alice'
        assert entry.local_user.last_name == 'Adams'

    def test_the_student_is_signed_in(self, client, provisioned):
        self._claim(client)
        assert client.session.get('_auth_user_id')

    def test_membership_and_profile_are_created(self, client, provisioned):
        self._claim(client)
        user = RosterEntry.objects.get(server_user_id=501).local_user
        assert user.memberships.filter(institution=provisioned, is_active=True).exists()
        assert user.student_profile.grade_level == 'S3'

    def test_the_password_actually_works(self, client, provisioned):
        """A password that is set but not usable would strand the student at
        the next login, with no way back — the entry is already claimed."""
        self._claim(client, password='pencil')
        user = RosterEntry.objects.get(server_user_id=501).local_user
        assert client.login(username=user.username, password='pencil')

    def test_a_second_claim_of_the_same_name_is_refused(self, client, provisioned):
        self._claim(client)
        r = self._claim(client)
        assert r.status_code == 400
        assert b'already been set up' in r.content
        assert User.objects.filter(first_name='Alice').count() == 1

    def test_short_passwords_are_rejected_without_claiming(self, client, provisioned):
        r = self._claim(client, password='ab')
        assert r.status_code == 400
        assert RosterEntry.objects.get(server_user_id=501).local_user is None

    def test_username_collision_with_a_self_registered_account(self, client, provisioned):
        """Self-registration is still available, so the roster's username may
        already be taken locally. The claim must not fail on that."""
        User.objects.create_user(username='alice', password='x')
        r = self._claim(client)
        assert r.status_code == 302
        entry = RosterEntry.objects.get(server_user_id=501)
        assert entry.local_user.username != 'alice'
        assert entry.local_user_id is not None

    def test_unknown_server_user_id_is_refused(self, client, provisioned):
        r = self._claim(client, server_user_id=99999)
        assert r.status_code == 400
