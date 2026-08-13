"""Tests for /api/v1/auth/* and /api/v1/me/."""

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from ai_tutor.apps.accounts.models import Institution, Membership


class AuthEndpointsTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.institution = Institution.objects.create(name='Test', slug='test')
        self.user = User.objects.create_user(username='alice', password='pw-123456')
        Membership.objects.create(
            user=self.user, institution=self.institution, role='student',
        )

    def test_login_returns_tokens_and_user(self):
        resp = self.client.post(
            '/api/v1/auth/login/',
            {'username': 'alice', 'password': 'pw-123456'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIn('access', body)
        self.assertIn('refresh', body)
        self.assertEqual(body['user']['username'], 'alice')
        self.assertEqual(len(body['user']['memberships']), 1)
        self.assertEqual(body['user']['memberships'][0]['institution_slug'], 'test')

    def test_login_rejects_wrong_password(self):
        resp = self.client.post(
            '/api/v1/auth/login/',
            {'username': 'alice', 'password': 'nope'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_register_new_student_with_institution(self):
        resp = self.client.post(
            '/api/v1/auth/register/',
            {
                'username': 'bob',
                'password': 'pw-7654321',
                'email': 'bob@example.com',
                'institution_slug': 'test',
                'school': 'Test',
                'grade_level': 'S3',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertIn('access', body)
        self.assertEqual(body['user']['username'], 'bob')
        self.assertTrue(User.objects.filter(username='bob').exists())

    def test_register_rejects_duplicate_username(self):
        resp = self.client.post(
            '/api/v1/auth/register/',
            {'username': 'alice', 'password': 'pw-12345678'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_register_rejects_unknown_institution(self):
        resp = self.client.post(
            '/api/v1/auth/register/',
            {
                'username': 'carol', 'password': 'pw-12345678',
                'institution_slug': 'no-such-school',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_me_requires_auth(self):
        resp = self.client.get('/api/v1/me/')
        self.assertEqual(resp.status_code, 401)

    def test_me_returns_current_user_with_jwt(self):
        login = self.client.post(
            '/api/v1/auth/login/',
            {'username': 'alice', 'password': 'pw-123456'},
            format='json',
        )
        token = login.json()['access']
        resp = self.client.get(
            '/api/v1/me/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['username'], 'alice')

    def test_refresh_rotates_access_token(self):
        login = self.client.post(
            '/api/v1/auth/login/',
            {'username': 'alice', 'password': 'pw-123456'},
            format='json',
        )
        refresh = login.json()['refresh']
        resp = self.client.post(
            '/api/v1/auth/refresh/',
            {'refresh': refresh},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('access', resp.json())

    def test_logout_blacklists_refresh(self):
        login = self.client.post(
            '/api/v1/auth/login/',
            {'username': 'alice', 'password': 'pw-123456'},
            format='json',
        )
        body = login.json()
        access = body['access']
        refresh = body['refresh']
        resp = self.client.post(
            '/api/v1/auth/logout/',
            {'refresh': refresh},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {access}',
        )
        self.assertEqual(resp.status_code, 204)
        # Reuse should now fail
        reuse = self.client.post(
            '/api/v1/auth/refresh/', {'refresh': refresh}, format='json',
        )
        self.assertEqual(reuse.status_code, 401)
