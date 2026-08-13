"""Tests for LocaleResolverMiddleware — the resolution chain.

Chain (first non-empty wins):
  1. course.locale (in-session)  ← handled in the chat view itself
  2. StudentProfile.preferred_locale
  3. Institution.default_locale (via active Membership)
  4. settings.LANGUAGE_CODE

The middleware only handles tiers 2–4; the chat tutor view activates
course.locale within its own scope.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.utils import translation

from ai_tutor.apps.accounts.locale_middleware import LocaleResolverMiddleware, _resolve_locale
from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile


class LocaleResolutionChainTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = LocaleResolverMiddleware(get_response=lambda r: HttpResponse())

    def _request(self, user):
        req = self.factory.get('/dashboard/')
        req.user = user
        return req

    def test_anonymous_uses_global_default(self):
        req = self._request(AnonymousUser())
        self.assertEqual(_resolve_locale(req), settings.LANGUAGE_CODE)

    def test_student_preferred_wins_over_institution(self):
        inst = Institution.objects.create(
            name='Moz School', slug='moz', default_locale='pt-mz',
        )
        user = User.objects.create_user('alice', password='x')
        Membership.objects.create(user=user, institution=inst, role='student')
        StudentProfile.objects.create(user=user, preferred_locale='en-us')

        req = self._request(user)
        # preferred_locale 'en-us' beats institution 'pt-mz'.
        self.assertEqual(_resolve_locale(req), 'en-us')

    def test_institution_default_used_when_no_student_preference(self):
        inst = Institution.objects.create(
            name='Moz School 2', slug='moz2', default_locale='pt-mz',
        )
        user = User.objects.create_user('bob', password='x')
        Membership.objects.create(user=user, institution=inst, role='student')
        # No StudentProfile.preferred_locale set.
        StudentProfile.objects.create(user=user)

        req = self._request(user)
        self.assertEqual(_resolve_locale(req), 'pt-mz')

    def test_fallback_when_no_membership(self):
        user = User.objects.create_user('charlie', password='x')
        StudentProfile.objects.create(user=user)
        req = self._request(user)
        self.assertEqual(_resolve_locale(req), settings.LANGUAGE_CODE)

    def test_no_locale_bleed_between_requests(self):
        """Activate pt-mz for one request, then a vanilla anonymous
        request should NOT see pt-mz active — process_response
        deactivates."""
        inst = Institution.objects.create(
            name='Moz School 3', slug='moz3', default_locale='pt-mz',
        )
        user = User.objects.create_user('dora', password='x')
        Membership.objects.create(user=user, institution=inst, role='student')
        StudentProfile.objects.create(user=user)

        # First request: pt-mz user.
        req1 = self._request(user)
        self.middleware.process_request(req1)
        self.assertEqual(translation.get_language(), 'pt-mz')
        self.middleware.process_response(req1, HttpResponse())

        # Second request: anonymous — must not inherit pt-mz.
        req2 = self._request(AnonymousUser())
        self.middleware.process_request(req2)
        # Whatever the global fallback resolves to, it should not be pt-mz.
        self.assertNotEqual(translation.get_language(), 'pt-mz')
        self.middleware.process_response(req2, HttpResponse())

    def test_process_exception_deactivates(self):
        """A 500 mid-request must still deactivate so the worker
        thread isn't poisoned."""
        inst = Institution.objects.create(
            name='Moz School 4', slug='moz4', default_locale='pt-mz',
        )
        user = User.objects.create_user('eve', password='x')
        Membership.objects.create(user=user, institution=inst, role='student')
        StudentProfile.objects.create(user=user)

        req = self._request(user)
        self.middleware.process_request(req)
        self.assertEqual(translation.get_language(), 'pt-mz')

        self.middleware.process_exception(req, RuntimeError('boom'))

        # After exception handling, the locale should be reset.
        self.assertNotEqual(translation.get_language(), 'pt-mz')
