"""Regression tests for finding F-01 — NUL byte crashes the public forms.

The captured payload from the assessment is used verbatim, because the point of
these tests is that THAT request stops returning 500. It never reaches a view
now, so they run against SQLite even though the crash itself only ever happened
on PostgreSQL: psycopg is what refuses to encode 0x00, and the whole fix is
making sure the string never gets that far.
"""
from __future__ import annotations

import json

from django.test import Client, SimpleTestCase, TestCase

# The exact username ZAP sent, decoded from the report's evidence block.
ZAP_PAYLOAD = '">\x00<scrIpt>alert(1);</scRipt>'

PUBLIC_FORMS = [
    '/student/login/',
    '/staff/login/',
    '/student/register/',
    '/staff/register/',
]


class NulByteRejectedTests(TestCase):
    """Every endpoint the assessment crashed now answers 400."""

    def test_nul_in_username_is_rejected_on_every_captured_endpoint(self):
        client = Client()
        for path in PUBLIC_FORMS:
            with self.subTest(path=path):
                response = client.post(path, {
                    'username': ZAP_PAYLOAD,
                    'password': 'ZAP',
                })
                self.assertEqual(response.status_code, 400)

    def test_nul_in_email_is_rejected(self):
        client = Client()
        for path in ('/student/register/', '/staff/register/'):
            with self.subTest(path=path):
                response = client.post(path, {
                    'username': 'zap',
                    'email': 'zap\x00@example.com',
                    'password': 'ZAP',
                })
                self.assertEqual(response.status_code, 400)

    def test_nul_in_query_string_is_rejected(self):
        response = Client().get('/student/login/', {'next': '/tutor/\x00'})
        self.assertEqual(response.status_code, 400)

    def test_nul_in_json_body_is_rejected(self):
        response = Client().post(
            '/api/v1/auth/login/',
            data='{"username": "a\\u0000b", "password": "x"}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['error'],
                         'Invalid characters in request.')

    def test_raw_nul_in_json_body_is_rejected(self):
        response = Client().post(
            '/api/v1/auth/login/',
            data=b'{"username": "a\x00b", "password": "x"}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_clean_login_still_reaches_the_view(self):
        """The guard must not turn a normal failed login into a 400."""
        response = Client().post('/student/login/', {
            'username': 'nobody',
            'password': 'wrong',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Invalid username or password.')

    def test_response_body_does_not_echo_the_payload(self):
        """A 400 that reflected the input would be a new XSS surface."""
        response = Client().post('/student/login/', {
            'username': ZAP_PAYLOAD,
            'password': 'ZAP',
        })
        self.assertNotIn(b'scrIpt', response.content)
        self.assertNotIn(b'alert', response.content)


class ControlCharacterStrippingTests(SimpleTestCase):
    """C0 controls other than tab/newline/CR are removed, not rejected."""

    def test_form_feed_is_stripped_and_the_request_proceeds(self):
        from ai_tutor.apps.safety.request_validation import clean_querydict
        from django.http import QueryDict

        source = QueryDict('notes=line+one\x0cline+two', mutable=False)
        nul, stripped, cleaned = clean_querydict(source)

        self.assertEqual(nul, [])
        self.assertEqual(stripped, ['notes'])
        self.assertEqual(cleaned['notes'], 'line oneline two')

    def test_tab_newline_and_carriage_return_survive(self):
        from ai_tutor.apps.safety.request_validation import clean_querydict
        from django.http import QueryDict

        source = QueryDict(mutable=True)
        source['notes'] = 'a\tb\r\nc'
        nul, stripped, cleaned = clean_querydict(source)

        self.assertEqual(nul, [])
        self.assertEqual(stripped, [])
        self.assertIsNone(cleaned)

    def test_clean_input_allocates_no_copy(self):
        from ai_tutor.apps.safety.request_validation import clean_querydict
        from django.http import QueryDict

        nul, stripped, cleaned = clean_querydict(QueryDict('a=1&b=hello+world'))

        self.assertEqual((nul, stripped), ([], []))
        self.assertIsNone(cleaned)

    def test_nul_is_reported_rather_than_stripped(self):
        from ai_tutor.apps.safety.request_validation import clean_querydict
        from django.http import QueryDict

        source = QueryDict(mutable=True)
        source['username'] = ZAP_PAYLOAD
        nul, stripped, cleaned = clean_querydict(source)

        self.assertEqual(nul, ['username'])
        self.assertEqual(stripped, [])
        self.assertIsNone(cleaned)
