"""The CSRF rejection page.

Sessions here last two weeks while CSRF_COOKIE_AGE is twelve hours, so a
signed-in user routinely outlives the token in a page they left open. Django's
stock 403 says "CSRF verification failed" and offers nothing else; these tests
pin the three things that make the replacement usable, and the one thing it
must not do.
"""
import re

import pytest
from django.conf import settings as dj
from django.contrib.auth import get_user_model
from django.middleware.csrf import _get_new_csrf_string, _mask_cipher_secret
from django.test import Client
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution

User = get_user_model()
BACKEND = 'django.contrib.auth.backends.ModelBackend'


def _stale_post(client, url, referer):
    """POST a token the cookie no longer matches — the real-world failure."""
    body = client.get(url).content.decode()
    token = re.findall(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)[0]
    client.cookies[dj.CSRF_COOKIE_NAME] = _mask_cipher_secret(_get_new_csrf_string())
    return client.post(
        url,
        {'action': 'account', 'csrfmiddlewaretoken': token,
         'first_name': 'A', 'last_name': 'B',
         'email': 'root@example.com', 'preferred_locale': ''},
        HTTP_REFERER=referer,
    )


@pytest.fixture
def admin_client_csrf(db):
    Institution.objects.create(name='Alpha', slug='alpha', is_active=True)
    user = User.objects.create_user(
        username='root', email='root@example.com', password='pw', is_staff=True)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user, backend=BACKEND)
    return client


def test_the_request_is_still_rejected(admin_client_csrf):
    """The page is an explanation, not an exemption."""
    url = reverse('dashboard:settings')
    r = _stale_post(admin_client_csrf, url, f'http://testserver{url}')
    assert r.status_code == 403


def test_it_says_what_happened_and_that_nothing_saved(admin_client_csrf):
    url = reverse('dashboard:settings')
    text = _stale_post(admin_client_csrf, url, f'http://testserver{url}').content.decode()
    assert 'Your session timed out' in text
    assert 'Your changes were not saved' in text


def test_it_offers_the_way_back(admin_client_csrf):
    url = reverse('dashboard:settings')
    text = _stale_post(admin_client_csrf, url, f'http://testserver{url}').content.decode()
    assert f'href="http://testserver{url}"' in text


def test_an_offsite_referer_is_never_linked(admin_client_csrf):
    """Referer is client-supplied, so it is a redirect target only after the
    same-host check. Without this the page is an open redirect anyone can reach
    by sending a bad token."""
    url = reverse('dashboard:settings')
    text = _stale_post(
        admin_client_csrf, url, 'https://evil.example.com/harvest').content.decode()
    assert 'evil.example.com' not in text
    assert 'Back to the form' not in text


def test_the_reason_string_is_not_shown(admin_client_csrf):
    """It distinguishes "no cookie" from "wrong cookie", which is an oracle,
    and it means nothing to the reader. It belongs in the log."""
    url = reverse('dashboard:settings')
    text = _stale_post(admin_client_csrf, url, f'http://testserver{url}').content.decode()
    assert 'CSRF token from POST incorrect' not in text
    assert 'CSRF cookie' not in text


def test_the_page_is_not_cached(admin_client_csrf):
    """It exists because a stale page was submitted; a cached copy is how the
    reader meets it again on a request that would have worked."""
    url = reverse('dashboard:settings')
    r = _stale_post(admin_client_csrf, url, f'http://testserver{url}')
    assert r['Cache-Control'] == 'no-store'


def test_the_setting_points_at_this_view():
    assert dj.CSRF_FAILURE_VIEW == 'ai_tutor.apps.safety.csrf_failure.csrf_failure'
