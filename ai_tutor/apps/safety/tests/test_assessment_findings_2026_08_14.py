"""Regression cover for the 2026-08-14 web assessment set (the nine ZAP/QA
reports under tests/security/).

Most of those reports re-raised the same handful of open web-app items across
three scan profiles. One class per fixed finding, so a failure names what it
re-opens. The Cloud Functions findings (C-01…C-14) from the manual review live
in a separate codebase and are covered by their own edits, not here.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase

from ai_tutor.apps.accounts.views import _password_errors
from ai_tutor.apps.safety.response_headers import (
    PERMISSIONS_POLICY,
    SecurityHeadersMiddleware,
)

BASE_DIR = Path(__file__).resolve().parents[4]
CSRF_JS = BASE_DIR / 'ai_tutor' / 'static' / 'js' / 'csrf.js'


class RegistrationPasswordPolicyTests(SimpleTestCase):
    """QA-03 / QAS-03 — registration ran a hand-rolled length check that skipped
    Django's common-password / all-numeric / similarity validators. Every form
    now routes through _password_errors, which runs the full chain."""

    def test_short_password_is_rejected(self):
        # Six characters used to pass student registration.
        self.assertTrue(_password_errors('aB3xz'))

    def test_all_numeric_password_is_rejected(self):
        self.assertTrue(_password_errors('83914756'))

    def test_common_password_is_rejected(self):
        self.assertTrue(_password_errors('password123'))

    def test_password_similar_to_username_is_rejected(self):
        self.assertTrue(_password_errors('miriamu2011', username='miriamu2011'))

    def test_a_strong_password_passes(self):
        self.assertEqual(_password_errors('7rue-Sunset-Kite'), [])


class RegistrationEndToEndTests(TestCase):
    """A weak password blocks account creation rather than sliding through."""

    def test_weak_student_password_creates_no_user(self):
        before = User.objects.count()
        response = Client().post('/student/register/', {
            'username': 'newpupil',
            'password': '123456',            # all-numeric, too short
            'password_confirm': '123456',
            'first_name': 'New',
        })
        self.assertEqual(response.status_code, 200)   # re-render, not redirect
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(User.objects.filter(username='newpupil').exists())


class MediaHardeningTests(SimpleTestCase):
    """QA-04 / F-02 — an SVG served inline from our own origin is a stored-XSS
    vector. The CSP middleware only writes on text/html, so /media/ carried no
    policy; SecurityHeadersMiddleware now sandboxes the script-executing types."""

    def _run(self, path, content_type):
        factory = RequestFactory()
        request = factory.get(path)
        request.user = AnonymousUser()
        middleware = SecurityHeadersMiddleware(
            lambda r: HttpResponse(b'x', content_type=content_type)
        )
        return middleware(request)

    def test_svg_media_is_sandboxed_and_download_only(self):
        response = self._run('/media/platform_logos/x.svg', 'image/svg+xml')
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertIn("default-src 'none'", response['Content-Security-Policy'])
        self.assertEqual(response['Content-Disposition'], 'attachment')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Cross-Origin-Resource-Policy'], 'same-origin')

    def test_image_media_stays_inline(self):
        # A student's own lesson image must keep rendering in an <img>; it gets
        # nosniff but no forced download and no strict CSP.
        response = self._run('/media/uploads/photo.png', 'image/png')
        self.assertNotIn('Content-Disposition', response)
        self.assertNotIn('Content-Security-Policy', response)
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def test_non_media_paths_are_untouched(self):
        response = self._run('/tutor/', 'text/html')
        self.assertNotIn('Content-Disposition', response)


class CsrfCookieAgeTests(SimpleTestCase):
    """QA-09 / QAS-08 / F-07 — the CSRF cookie no longer lives for a year."""

    def test_age_is_bounded_to_a_school_day(self):
        from django.conf import settings
        self.assertEqual(settings.CSRF_COOKIE_AGE, 60 * 60 * 12)


class PermissionsPolicyTests(SimpleTestCase):
    """O-5 — the withdrawn FLoC token was dead weight; it is gone."""

    def test_interest_cohort_removed(self):
        self.assertNotIn('interest-cohort', PERMISSIONS_POLICY)

    def test_still_denies_camera_and_grants_microphone(self):
        self.assertIn('camera=()', PERMISSIONS_POLICY)
        self.assertIn('microphone=(self)', PERMISSIONS_POLICY)


class JavaScriptDisclosureTests(SimpleTestCase):
    """QA-07 / QAS-06 / F-04 / Z-04 — the public csrf.js comment named prior
    assessment findings and the count of request-input sinks. It no longer
    discloses security posture."""

    DISCLOSURE = re.compile(
        r'\bF-0\d\b|assessment|pentest|vulnerab|\d+\s+places where', re.IGNORECASE
    )

    def test_no_assessment_vocabulary_in_shipped_csrf_js(self):
        text = CSRF_JS.read_text()
        hit = self.DISCLOSURE.search(text)
        self.assertIsNone(
            hit, f'csrf.js discloses assessment detail: {hit.group(0) if hit else ""!r}'
        )


class TermsAcceptOpenRedirectTests(TestCase):
    """F-05 — the terms interstitial validated `next` with a bare
    startswith('/'), which lets a protocol-relative //host through as an open
    redirect. It now uses url_has_allowed_host_and_scheme."""

    def setUp(self):
        from ai_tutor.apps.accounts.models import PlatformTerms
        self.user = User.objects.create_user('terms', password='CorrectHorse1!')
        # A data migration may already seed the v1/en-us row, so reuse whatever
        # active terms exist and only create one if there are none (the
        # interstitial redirects straight to the catalog when no terms are live).
        if PlatformTerms.active(locale='en-us') is None:
            terms, _ = PlatformTerms.objects.get_or_create(
                version=1, locale='en-us', defaults={'body': 'terms'},
            )
            PlatformTerms.objects.filter(pk=terms.pk).update(is_active=True)

    def test_protocol_relative_next_is_not_reflected(self):
        self.client.force_login(self.user)
        response = self.client.get('/terms/accept/?next=//evil.example/x')
        self.assertNotContains(response, 'evil.example')

    def test_offsite_next_on_accept_lands_internally(self):
        self.client.force_login(self.user)
        response = self.client.post(
            '/terms/accept/',
            {'accept_terms': 'on', 'next': '//evil.example/x'},
        )
        self.assertNotIn('evil.example', response.get('Location', ''))
