"""Regression cover for the 2026-08-13 security assessment.

One class per finding, so a failure names the finding it re-opens. F-01 has its
own file (test_request_validation.py) because it needed more than a few cases.

The cookie-attribute checks run settings in a subprocess with DEBUG=False, for
the same reason test_https_edge.py does: the flags are evaluated once at import
time and override_settings cannot re-run that.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase, override_settings

BASE_DIR = Path(__file__).resolve().parents[4]
STATIC_DIR = BASE_DIR / 'ai_tutor' / 'static'
TEMPLATE_DIR = BASE_DIR / 'ai_tutor' / 'templates'


def _settings_under(env: dict) -> dict:
    """Import settings in a clean subprocess and read the cookie flags back."""
    probe = (
        'import json;from django.conf import settings;'
        'print(json.dumps({'
        "'session_secure': settings.SESSION_COOKIE_SECURE,"
        "'csrf_secure': settings.CSRF_COOKIE_SECURE,"
        "'language_secure': settings.LANGUAGE_COOKIE_SECURE,"
        "'session_httponly': settings.SESSION_COOKIE_HTTPONLY,"
        "'csrf_httponly': settings.CSRF_COOKIE_HTTPONLY,"
        "'language_httponly': settings.LANGUAGE_COOKIE_HTTPONLY,"
        "'session_samesite': settings.SESSION_COOKIE_SAMESITE,"
        "'csrf_samesite': settings.CSRF_COOKIE_SAMESITE,"
        "'language_samesite': settings.LANGUAGE_COOKIE_SAMESITE,"
        '}))'
    )
    environ = {
        **os.environ,
        'DJANGO_SETTINGS_MODULE': 'ai_tutor.config.settings',
        'ALLOW_DEV_SECRET_KEY': '1',
        **env,
    }
    result = subprocess.run(
        [sys.executable, '-c', f'import django;django.setup();{probe}'],
        cwd=BASE_DIR, env=environ, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


class CookieHardeningTests(SimpleTestCase):
    """F-05 and F-06 — every cookie declares Secure, HttpOnly and SameSite."""

    def test_production_sets_all_three_on_all_three_cookies(self):
        flags = _settings_under({'DEBUG': 'False', 'HTTPS_EDGE': 'true'})
        for cookie in ('session', 'csrf', 'language'):
            with self.subTest(cookie=cookie):
                self.assertTrue(flags[f'{cookie}_secure'])
                self.assertTrue(flags[f'{cookie}_httponly'])
                self.assertEqual(flags[f'{cookie}_samesite'], 'Lax')

    def test_plain_http_deployments_keep_secure_off(self):
        """The kiosk and desktop builds serve HTTP; Secure there breaks login."""
        flags = _settings_under({'DEBUG': 'False', 'HTTPS_EDGE': 'false'})
        for cookie in ('session', 'csrf', 'language'):
            with self.subTest(cookie=cookie):
                self.assertFalse(flags[f'{cookie}_secure'])
                # HttpOnly and SameSite do not depend on TLS and stay on.
                self.assertTrue(flags[f'{cookie}_httponly'])
                self.assertEqual(flags[f'{cookie}_samesite'], 'Lax')


class CsrfTokenSourceTests(SimpleTestCase):
    """F-05 — nothing may read the CSRF token from document.cookie again.

    This is the test that keeps CSRF_COOKIE_HTTPONLY safe to leave on. A new
    fetch() call that copies the old pattern would 403 at runtime and pass every
    other test in the suite; it fails here instead.
    """

    def test_no_template_or_script_reads_document_cookie(self):
        offenders = []
        for root in (TEMPLATE_DIR, STATIC_DIR):
            for path in root.rglob('*'):
                if path.suffix not in ('.html', '.js') or not path.is_file():
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if 'document.cookie' not in line:
                        continue
                    # Prose about the rule is not a breach of it.
                    stripped = line.strip()
                    if stripped.startswith(('*', '//', '{#', '#')):
                        continue
                    offenders.append(f'{path.relative_to(BASE_DIR)}:{number}')
        self.assertEqual(offenders, [], (
            'These read document.cookie. The CSRF cookie is HttpOnly, so the '
            'token must come from window.csrfToken() in static/js/csrf.js.'
        ))

    def test_both_base_templates_publish_the_token_and_the_helper(self):
        for base in ('base.html', 'dashboard/base.html'):
            with self.subTest(base=base):
                text = (TEMPLATE_DIR / base).read_text()
                self.assertIn('name="csrf-token"', text)
                self.assertIn("static 'js/csrf.js'", text)


class ContentSecurityPolicyTests(TestCase):
    """F-02 — the policy, the nonce, and the two switches that tighten it."""

    def test_report_only_by_default(self):
        response = Client().get('/student/login/')
        self.assertIn('Content-Security-Policy-Report-Only', response.headers)
        self.assertNotIn('Content-Security-Policy', response.headers)

    def test_a_fresh_nonce_appears_in_both_header_and_page(self):
        response = Client().get('/student/login/')
        policy = response.headers['Content-Security-Policy-Report-Only']
        body = response.content.decode()

        # Legacy mode: 'unsafe-inline' still in force, nonce present in the page
        # but not yet in the policy, so flipping CSP_ENFORCE alone behaves
        # exactly as it did before the nonce was introduced.
        self.assertIn("script-src 'self' 'unsafe-inline'", policy)
        self.assertNotIn('nonce-', policy)

        nonces = re.findall(r'<script[^>]*\bnonce="([^"]+)"', body)
        self.assertTrue(nonces, 'no nonced inline script in the rendered page')
        self.assertEqual(len(set(nonces)), 1, 'one nonce per request')

    def test_nonce_differs_between_requests(self):
        client = Client()
        first = re.search(r'<script[^>]*\bnonce="([^"]+)"',
                          client.get('/student/login/').content.decode())
        second = re.search(r'<script[^>]*\bnonce="([^"]+)"',
                           client.get('/student/login/').content.decode())
        self.assertNotEqual(first.group(1), second.group(1))

    def test_strict_scripts_swaps_unsafe_inline_for_the_nonce(self):
        from ai_tutor.apps.safety.csp import ContentSecurityPolicyMiddleware

        with override_settings():
            os.environ['CSP_STRICT_SCRIPTS'] = '1'
            try:
                middleware = ContentSecurityPolicyMiddleware(lambda r: None)
            finally:
                del os.environ['CSP_STRICT_SCRIPTS']

        class FakeRequest:
            csp_nonce = 'testnonce'

        policy = middleware._policy_for(FakeRequest())
        self.assertIn("script-src 'self' 'nonce-testnonce'", policy)
        self.assertNotIn("'unsafe-inline'", policy.split(';')[0])

    def test_every_inline_script_carries_a_nonce(self):
        """A new inline <script> without one goes dark the day CSP is enforced."""
        pattern = re.compile(r'^\s*<script(?![^>]*\bsrc=)(?![^>]*\bnonce=)[^>]*>',
                             re.MULTILINE)
        offenders = []
        for path in TEMPLATE_DIR.rglob('*.html'):
            for match in pattern.finditer(path.read_text()):
                line = path.read_text()[:match.start()].count('\n') + 1
                offenders.append(f'{path.relative_to(BASE_DIR)}:{line}')
        self.assertEqual(offenders, [], (
            'inline <script> without nonce="{{ request.csp_nonce }}"'
        ))


class TemplateSyntaxTrapTests(SimpleTestCase):
    """Two traps that this change set fell into, so they stay fallen-out-of.

    Both are invisible to a status-code assertion and to DOM inspection — the
    page returns 200 either way. Only rendering it and looking caught them.
    """

    def test_no_multiline_hash_comments(self):
        r"""``{# ... #}`` is single-line only; a multi-line one renders as text.

        A three-line ``{# #}`` note added to base.html printed itself across the
        top of every page in the application. Django's lexer only matches the
        comment token within one line — anything longer needs
        ``{% comment %}``.
        """
        offenders = []
        for path in TEMPLATE_DIR.rglob('*.html'):
            text = path.read_text()
            for match in re.finditer(r'\{#', text):
                rest = text[match.start():]
                close = rest.find('#}')
                if close == -1 or '\n' in rest[:close]:
                    line = text[:match.start()].count('\n') + 1
                    offenders.append(f'{path.relative_to(BASE_DIR)}:{line}')
        self.assertEqual(offenders, [], (
            '{# #} does not span lines — Django renders it as page text. '
            'Use {% comment %}...{% endcomment %}.'
        ))

    def test_no_closing_script_tag_inside_inline_script(self):
        """The HTML parser ends a <script> at the first closing tag it sees.

        Including one inside a JavaScript comment, which is how a note
        mentioning a closing script tag truncated the settings page's script
        block and threw 'Unexpected end of input'.
        """
        offenders = []
        pattern = re.compile(r'^\s*(//|\*|/\*).*</script\s*>', re.MULTILINE | re.IGNORECASE)
        for path in TEMPLATE_DIR.rglob('*.html'):
            text = path.read_text()
            for match in pattern.finditer(text):
                line = text[:match.start()].count('\n') + 1
                offenders.append(f'{path.relative_to(BASE_DIR)}:{line}')
        self.assertEqual(offenders, [], (
            'A closing script tag inside a JS comment still ends the block. '
            "Write it broken up, or describe it in words."
        ))


class CspReportEndpointTests(TestCase):
    """F-02 — the collector accepts real reports and shrugs off everything else."""

    URL = '/csp-report/'

    def test_accepts_a_report_uri_document(self):
        response = self.client.post(
            self.URL,
            data=json.dumps({'csp-report': {
                'document-uri': 'https://example.test/page',
                'violated-directive': 'script-src',
                'blocked-uri': 'inline',
            }}),
            content_type='application/csp-report',
        )
        self.assertEqual(response.status_code, 204)

    def test_get_is_rejected(self):
        self.assertEqual(self.client.get(self.URL).status_code, 405)

    def test_malformed_json_does_not_error(self):
        response = self.client.post(self.URL, data='{not json',
                                    content_type='application/csp-report')
        self.assertEqual(response.status_code, 204)

    def test_oversized_body_is_dropped_unparsed(self):
        response = self.client.post(
            self.URL,
            data=json.dumps({'csp-report': {'document-uri': 'x' * 20000}}),
            content_type='application/csp-report',
        )
        self.assertEqual(response.status_code, 204)

    def test_unexpected_content_type_is_ignored(self):
        response = self.client.post(self.URL, data='hi', content_type='text/plain')
        self.assertEqual(response.status_code, 204)


class ResponseHeaderTests(TestCase):
    """F-12 — Permissions-Policy and Cross-Origin-Resource-Policy."""

    def test_headers_present_on_application_responses(self):
        response = Client().get('/student/login/')
        self.assertEqual(response['Cross-Origin-Resource-Policy'], 'same-site')
        policy = response['Permissions-Policy']
        # Denied outright.
        for capability in ('camera', 'geolocation', 'payment', 'usb'):
            self.assertIn(f'{capability}=()', policy)
        # Used by the product, so granted to this origin only.
        self.assertIn('microphone=(self)', policy)
        self.assertIn('display-capture=(self)', policy)


class AuthenticatedCacheTests(TestCase):
    """F-09 — a signed-in page must not survive in the browser cache."""

    def setUp(self):
        self.user = User.objects.create_user('cachetest', password='CorrectHorse1!')

    def test_signed_in_html_is_no_store(self):
        self.client.force_login(self.user)
        response = self.client.get('/tutor/')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])

    def test_public_marketing_pages_are_left_alone(self):
        # The landing page carries no personal data and no CSRF-bearing form to
        # protect, so it stays cacheable.
        response = Client().get('/')
        self.assertNotIn('no-store', response.get('Cache-Control', ''))

    def test_anonymous_auth_pages_are_no_store(self):
        # 2026-08 assessment (QA-06 / QAS-05 / F-04): login, registration and
        # password-reset pages must not linger in a shared-device cache even for
        # an anonymous visitor — they render a CSRF token and redisplay any
        # personal data just submitted.
        for path in ('/student/login/', '/student/register/',
                     '/staff/login/', '/staff/register/', '/password-reset/'):
            response = Client().get(path)
            self.assertIn('no-store', response.get('Cache-Control', ''), path)


class BruteForceLockoutTests(TestCase):
    """F-04 — repeated failures lock the (IP, username) pair out."""

    def setUp(self):
        from axes.helpers import get_cache
        get_cache().clear()
        User.objects.create_user('lockme', password='CorrectHorse1!')

    def tearDown(self):
        from axes.helpers import get_cache
        get_cache().clear()

    def test_login_locks_after_the_configured_failure_limit(self):
        from django.conf import settings

        client = Client()
        for _ in range(settings.AXES_FAILURE_LIMIT - 1):
            response = client.post('/student/login/',
                                   {'username': 'lockme', 'password': 'wrong'})
            self.assertEqual(response.status_code, 200)

        response = client.post('/student/login/',
                               {'username': 'lockme', 'password': 'wrong'})
        self.assertEqual(response.status_code, settings.AXES_HTTP_RESPONSE_CODE)

    def test_the_correct_password_does_not_reopen_a_locked_account(self):
        from django.conf import settings

        client = Client()
        for _ in range(settings.AXES_FAILURE_LIMIT):
            client.post('/student/login/', {'username': 'lockme', 'password': 'wrong'})

        response = client.post('/student/login/',
                               {'username': 'lockme', 'password': 'CorrectHorse1!'})
        self.assertEqual(response.status_code, settings.AXES_HTTP_RESPONSE_CODE)

    def test_a_clean_login_is_unaffected(self):
        response = Client().post('/student/login/',
                                 {'username': 'lockme', 'password': 'CorrectHorse1!'})
        self.assertEqual(response.status_code, 302)

    def test_the_mobile_api_login_is_covered_too(self):
        """authenticate() there had no request, which django-axes rejects."""
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'lockme', 'password': 'CorrectHorse1!'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)


class LanguageEndpointTests(TestCase):
    """F-08 — the stock set_language view validates both of its inputs."""

    def _post(self, **payload):
        client = Client()
        client.get('/')
        payload['csrfmiddlewaretoken'] = client.cookies['csrftoken'].value
        return client.post('/i18n/setlang/', payload,
                           HTTP_REFERER='http://testserver/')

    def test_supported_language_is_accepted(self):
        response = self._post(language='en', next='/')
        self.assertEqual(response.cookies['django_language'].value, 'en')

    def test_unsupported_language_sets_no_cookie(self):
        response = self._post(language='xx', next='/')
        self.assertNotIn('django_language', response.cookies)

    def test_semicolon_cannot_smuggle_cookie_attributes(self):
        response = self._post(language='en;evil=1', next='/')
        self.assertNotIn('django_language', response.cookies)

    def test_next_cannot_leave_the_site(self):
        for target in ('https://evil.example/', '//evil.example/'):
            with self.subTest(target=target):
                response = self._post(language='en', next=target)
                self.assertNotIn('evil.example', response['Location'])

    def test_the_cookie_is_hardened(self):
        response = self._post(language='en', next='/')
        cookie = response.cookies['django_language']
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')


class AdminMountTests(TestCase):
    """F-04 — the admin path is configurable, and defaults to where it was."""

    def test_default_is_unchanged(self):
        from django.conf import settings
        self.assertEqual(settings.ADMIN_URL, 'admin/')
        self.assertEqual(Client().get('/admin/login/').status_code, 200)


class ProductionJavaScriptTests(SimpleTestCase):
    """F-11 — no developer markers in the assets we ship."""

    MARKERS = re.compile(r'\b(TODO|FIXME|XXX|HACK|BUG)\b')

    def test_no_developer_markers_in_shipped_javascript(self):
        offenders = []
        for path in STATIC_DIR.rglob('*.js'):
            # Third-party bundles are shipped as published; we do not edit them.
            if path.name.endswith('.min.js'):
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if self.MARKERS.search(line):
                    offenders.append(f'{path.relative_to(BASE_DIR)}:{number}')
        self.assertEqual(offenders, [], (
            'Developer markers reach production JavaScript. Comments naming '
            'known bugs point a reader at code paths worth probing (F-11).'
        ))


class StaticAssetCorsTests(SimpleTestCase):
    """F-03 — WhiteNoise must not label static files world-readable."""

    def test_wildcard_origin_is_disabled(self):
        from django.conf import settings
        self.assertFalse(settings.WHITENOISE_ALLOW_ALL_ORIGINS)

    def test_cors_is_still_scoped_to_the_api(self):
        from django.conf import settings
        self.assertEqual(settings.CORS_URLS_REGEX, r'^/api/.*$')
        self.assertFalse(getattr(settings, 'CORS_ALLOW_ALL_ORIGINS', False))
