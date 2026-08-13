"""LOGIN_URL must point at a route that exists.

Regression cover for a bug that sat in production: LOGIN_URL was
'/accounts/login/', but ai_tutor.apps.accounts.urls is mounted at the root, so the real
route is '/login/'. Django never validates LOGIN_URL, so every @login_required
view redirected logged-out users to a 404 — hit by anyone with an expired
session, a bookmarked dashboard URL, or a shared link. Normal front-door
traffic never touches LOGIN_URL, which is why it survived so long.
"""
from __future__ import annotations

from django.conf import settings
from django.test import TestCase
from django.urls import Resolver404, resolve


class LoginUrlResolvesTests(TestCase):

    def test_login_url_setting_resolves_to_a_real_view(self):
        login_url = str(settings.LOGIN_URL)
        try:
            match = resolve(login_url.split("?")[0])
        except Resolver404:
            self.fail(
                f"LOGIN_URL={login_url!r} does not resolve to any view. Every "
                "@login_required view sends logged-out users here, so they get "
                "a 404 instead of a login page."
            )
        self.assertTrue(callable(match.func))

    def test_logout_redirect_url_resolves(self):
        """Currently unused — logout_view redirects explicitly — but it should
        not be left pointing somewhere that 404s."""
        target = str(settings.LOGOUT_REDIRECT_URL).split("?")[0]
        try:
            resolve(target)
        except Resolver404:
            self.fail(f"LOGOUT_REDIRECT_URL={target!r} does not resolve")


class ProtectedViewRedirectTests(TestCase):
    """The end-to-end behaviour a logged-out user actually experiences."""

    def test_dashboard_redirects_a_logged_out_user_to_a_page_that_exists(self):
        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 302)
        landing = self.client.get(response["Location"], follow=True)
        self.assertNotEqual(
            landing.status_code, 404,
            f"logged-out /dashboard/ redirected to {response['Location']!r}, "
            "which 404s — this is the production bug this test exists for",
        )
        self.assertEqual(landing.status_code, 200)

    def test_the_next_parameter_survives_the_redirect(self):
        """login_view reads ?next= to route dashboard traffic at the staff
        form. That logic is dead if next is dropped."""
        response = self.client.get("/dashboard/")

        self.assertIn("next=", response["Location"])
