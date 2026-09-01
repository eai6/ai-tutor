"""Registration flows must actually sign the new account in.

Regression cover for a bug that reached production: every flow that creates a
user itself and then calls ``login(request, user)`` raised

    ValueError: You have multiple authentication backends configured and
    therefore must provide the `backend` argument or set the `backend`
    attribute on the user.

``login()`` only infers the backend from ``user.backend``, which Django sets
in ``authenticate()``. A user that came from ``User.objects.create_user()``
has never been near ``authenticate()``, so the attribute is absent — and once
``AUTHENTICATION_BACKENDS`` gained ``AxesStandaloneBackend`` alongside
``ModelBackend``, Django could no longer guess and started raising.

The failure mode is nasty: the account is committed to the database, then the
request 500s. The student sees an error, tries again, and is told their
username is already taken.

Login views were unaffected — they call ``authenticate()`` first, so their
users carry ``.backend``. Only the three self-created-user paths broke, and
two of them had no test at all, which is why this shipped.
"""
import secrets

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from ai_tutor.apps.accounts.models import (
    Institution,
    Membership,
    StaffInvitation,
)


class StudentSelfRegistrationTests(TestCase):
    """accounts.views.student_register"""

    def setUp(self):
        self.institution = Institution.objects.create(
            name='Anse Royale Secondary', slug='anse-royale', is_active=True,
        )
        self.client = Client()

    def _payload(self, **overrides):
        payload = {
            'first_name': 'Ana',
            'last_name': 'Silva',
            'username': 'ana.silva',
            'email': 'ana.silva@example.com',
            'password': 'Str0ngPassw0rd!x',
            'password_confirm': 'Str0ngPassw0rd!x',
            'school': str(self.institution.id),
            'grade_level': 'S3',
            'student_id': 'S123',
            'accept_terms': 'on',
        }
        payload.update(overrides)
        return payload

    def test_registration_does_not_raise(self):
        """The whole point: this used to raise ValueError from login()."""
        response = self.client.post(
            reverse('accounts:student_register'), self._payload(),
        )
        self.assertIn(response.status_code, (301, 302))

    def test_the_student_is_signed_in_afterwards(self):
        self.client.post(reverse('accounts:student_register'), self._payload())
        self.assertIn(
            '_auth_user_id', self.client.session,
            'account was created but the student was left signed out',
        )
        user = User.objects.get(username='ana.silva')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_the_account_and_membership_are_created(self):
        self.client.post(reverse('accounts:student_register'), self._payload())
        user = User.objects.get(username='ana.silva')
        self.assertEqual(user.first_name, 'Ana')
        self.assertTrue(
            Membership.objects.filter(
                user=user, institution=self.institution, role='student',
            ).exists()
        )

    def test_a_half_finished_signup_does_not_strand_an_account(self):
        """The bug's real damage: the row was committed, then the request
        died, so retrying reported 'username already taken'."""
        self.client.post(reverse('accounts:student_register'), self._payload())
        self.assertTrue(User.objects.filter(username='ana.silva').exists())
        self.assertIn('_auth_user_id', self.client.session)


class StaffInvitationRegistrationTests(TestCase):
    """accounts.views.staff_register — the invitation-token path."""

    def setUp(self):
        self.institution = Institution.objects.create(
            name='Anse Royale Secondary', slug='anse-royale-2', is_active=True,
        )
        self.invitation = StaffInvitation.objects.create(
            institution=self.institution,
            email='teacher@example.com',
            # token has no model-level default; the invite view generates one.
            token=secrets.token_urlsafe(32),
        )
        self.client = Client()

    def _payload(self):
        return {
            'first_name': 'Aline',
            'last_name': 'Mahoune',
            'username': 'a.mahoune',
            'password': 'Str0ngPassw0rd!x',
            'password_confirm': 'Str0ngPassw0rd!x',
        }

    def test_registration_does_not_raise(self):
        response = self.client.post(
            reverse('accounts:staff_register', args=[self.invitation.token]),
            self._payload(),
        )
        self.assertIn(response.status_code, (301, 302))

    def test_the_teacher_is_signed_in_afterwards(self):
        self.client.post(
            reverse('accounts:staff_register', args=[self.invitation.token]),
            self._payload(),
        )
        self.assertIn(
            '_auth_user_id', self.client.session,
            'account was created but the teacher was left signed out',
        )
        user = User.objects.get(username='a.mahoune')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_the_invitation_is_consumed(self):
        self.client.post(
            reverse('accounts:staff_register', args=[self.invitation.token]),
            self._payload(),
        )
        self.invitation.refresh_from_db()
        self.assertTrue(self.invitation.is_used)
