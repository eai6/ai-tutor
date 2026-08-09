"""Device enrolment and token authentication.

A device credential is not a user credential: one laptop pushes work for a whole
class, and the students it syncs for may never have logged in online. The
properties worth pinning are the security ones — single use, hashed at rest,
revocable, and not leaking which codes exist.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import Device, Institution


@pytest.fixture(autouse=True)
def _reset_throttle():
    """DRF throttles by client IP via the cache, and every test here shares one.

    Without this the 11th enrolment in the suite gets a 429 — which is the
    throttle working, but it makes later tests fail for the wrong reason. There
    is a dedicated test below that asserts the limit still bites.
    """
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def institution(db):
    return Institution.objects.create(name='Test School', slug='test-school')


@pytest.fixture
def pending(institution):
    return Device.objects.create(
        institution=institution,
        enrolment_code=Device.generate_code(),
        name='Lab laptop 3',
    )


@pytest.mark.django_db
class TestCodeGeneration:
    def test_avoids_visually_ambiguous_characters(self):
        """A mistyped code in a classroom is a support call."""
        for _ in range(50):
            code = Device.generate_code()
            assert not set(code) & set('01OIL')

    def test_codes_are_unique_enough(self):
        assert len({Device.generate_code() for _ in range(200)}) == 200


@pytest.mark.django_db
class TestEnrol:
    def test_exchanges_a_code_for_a_token(self, client, pending):
        r = client.post(reverse('api:device_enrol'),
                        {'code': pending.enrolment_code,
                         'device_id': '11111111-2222-3333-4444-555555555555',
                         'name': 'Lab laptop 3'},
                        content_type='application/json')
        assert r.status_code == 201
        body = r.json()
        assert body['token']
        assert body['institution_id'] == pending.institution_id

        pending.refresh_from_db()
        assert pending.status == Device.Status.ACTIVE
        assert pending.enrolled_at is not None

    def test_the_token_is_stored_hashed(self, client, pending):
        r = client.post(reverse('api:device_enrol'), {'code': pending.enrolment_code},
                        content_type='application/json')
        raw = r.json()['token']
        pending.refresh_from_db()
        assert pending.token_hash and pending.token_hash != raw
        assert pending.token_hash == Device.hash_token(raw)

    def test_a_code_is_single_use(self, client, pending):
        code = pending.enrolment_code
        assert client.post(reverse('api:device_enrol'), {'code': code},
                           content_type='application/json').status_code == 201
        second = client.post(reverse('api:device_enrol'), {'code': code},
                             content_type='application/json')
        assert second.status_code == 400

    def test_an_unknown_code_looks_the_same_as_a_used_one(self, client, pending):
        """Different messages would tell someone guessing which codes exist."""
        used_code = pending.enrolment_code
        client.post(reverse('api:device_enrol'), {'code': used_code},
                    content_type='application/json')
        used = client.post(reverse('api:device_enrol'), {'code': used_code},
                           content_type='application/json')
        unknown = client.post(reverse('api:device_enrol'), {'code': 'ZZZZ-9999'},
                              content_type='application/json')
        assert used.status_code == unknown.status_code == 400
        assert used.json()['detail'] == unknown.json()['detail']

    def test_code_is_case_insensitive(self, client, pending):
        r = client.post(reverse('api:device_enrol'),
                        {'code': pending.enrolment_code.lower()},
                        content_type='application/json')
        assert r.status_code == 201

    def test_missing_code_is_rejected(self, client, db):
        r = client.post(reverse('api:device_enrol'), {}, content_type='application/json')
        assert r.status_code == 400


@pytest.mark.django_db
class TestDeviceTokenAuth:
    def _enrol(self, client, pending):
        r = client.post(reverse('api:device_enrol'), {'code': pending.enrolment_code},
                        content_type='application/json')
        return r.json()['token']

    def test_a_valid_token_authenticates(self, client, pending):
        token = self._enrol(client, pending)
        r = client.get(reverse('api:device_check'), HTTP_AUTHORIZATION=f'Device {token}')
        assert r.status_code == 200 and r.json()['ok'] is True

    def test_a_revoked_device_is_refused(self, client, pending):
        token = self._enrol(client, pending)
        pending.refresh_from_db()
        pending.status = Device.Status.REVOKED
        pending.save()
        r = client.get(reverse('api:device_check'), HTTP_AUTHORIZATION=f'Device {token}')
        assert r.status_code == 401
        # The operator of a revoked laptop should learn it was revoked, not
        # that something is broken.
        assert 'revoked' in r.json()['detail'].lower()

    def test_a_garbage_token_is_refused(self, client, pending):
        r = client.get(reverse('api:device_check'), HTTP_AUTHORIZATION='Device nonsense')
        assert r.status_code == 401

    def test_no_token_is_refused(self, client, db):
        assert client.get(reverse('api:device_check')).status_code == 401

    def test_bearer_scheme_is_not_accepted_as_a_device(self, client, pending):
        """Device and Bearer must not be interchangeable."""
        token = self._enrol(client, pending)
        r = client.get(reverse('api:device_check'), HTTP_AUTHORIZATION=f'Bearer {token}')
        assert r.status_code == 401

    def test_last_seen_is_recorded(self, client, pending):
        token = self._enrol(client, pending)
        pending.refresh_from_db()
        assert pending.last_seen_at is None
        client.get(reverse('api:device_check'), HTTP_AUTHORIZATION=f'Device {token}')
        pending.refresh_from_db()
        assert pending.last_seen_at is not None


@pytest.mark.django_db
class TestEnrolThrottle:
    def test_repeated_guessing_is_rate_limited(self, client, institution):
        """An 8-character code from a 31-character alphabet must not be free to
        guess. Proven rather than assumed — the throttle silently not applying
        is exactly the kind of thing that goes unnoticed."""
        statuses = []
        for i in range(14):
            r = client.post(reverse('api:device_enrol'), {'code': f'ZZZZ-{i:04d}'},
                            content_type='application/json')
            statuses.append(r.status_code)
        assert 429 in statuses, f'never throttled: {statuses}'
        assert statuses.count(400) <= 10
