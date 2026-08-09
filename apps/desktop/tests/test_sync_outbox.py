"""The device-side outbox.

Its job is to survive: a laptop closed mid-lesson, weeks with no network, a
server that refuses. The failure mode to protect against is silent — a worker
that dies, or a queue held in memory, loses a term's work and nobody finds out
until a teacher asks why the dashboard is empty.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.desktop import sync as sync_mod
from apps.desktop.models import DeviceState, SyncOutbox


class FakeResponse:
    def __init__(self, status_code, text=''):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def enrolled(db, settings):
    settings.SYNC_SERVER_URL = 'https://example.test'
    state = DeviceState.load()
    state.sync_token = 'a-token'
    state.save()
    return state


@pytest.mark.django_db
class TestEnqueue:
    def test_enqueue_persists(self, db):
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        item = SyncOutbox.objects.get()
        assert item.status == SyncOutbox.Status.PENDING
        assert item.server_user_id == 42
        assert item.client_uuid                      # generated client-side

    def test_enqueue_never_raises_into_the_lesson(self, db):
        """A telemetry failure must not surface as a failed lesson."""
        with patch.object(SyncOutbox.objects, 'create', side_effect=RuntimeError('db gone')):
            assert sync_mod.enqueue('session', {}) is None


@pytest.mark.django_db
class TestDrain:
    def test_a_successful_push_marks_the_row_sent(self, enrolled):
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        with patch('requests.post', return_value=FakeResponse(201)):
            result = sync_mod.drain()
        assert result['sent'] == 1
        item = SyncOutbox.objects.get()
        assert item.status == SyncOutbox.Status.SENT
        assert item.sent_at is not None

    def test_409_counts_as_delivered(self, enrolled):
        """The server already has it — retrying achieves nothing."""
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        with patch('requests.post', return_value=FakeResponse(409)):
            sync_mod.drain()
        assert SyncOutbox.objects.get().status == SyncOutbox.Status.SENT

    def test_being_offline_defers_rather_than_failing(self, enrolled):
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        with patch('requests.post', side_effect=OSError('no route to host')):
            sync_mod.drain()
        item = SyncOutbox.objects.get()
        assert item.status == SyncOutbox.Status.PENDING     # still queued
        assert item.attempt_count == 1
        assert item.next_attempt_at is not None             # backoff set

    def test_backoff_defers_the_next_attempt(self, enrolled):
        """A row inside its backoff window must be skipped, or the worker
        hammers a server that just told it to wait."""
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        SyncOutbox.objects.update(next_attempt_at=timezone.now() + timedelta(minutes=5))
        with patch('requests.post') as post:
            sync_mod.drain()
        post.assert_not_called()

    def test_a_rejected_device_stops_retrying(self, enrolled):
        """401 cannot be fixed by trying again; hammering is worse."""
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        with patch('requests.post', return_value=FakeResponse(401)):
            sync_mod.drain()
        assert SyncOutbox.objects.get().status == SyncOutbox.Status.FAILED

    def test_gives_up_after_max_attempts(self, enrolled):
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        with patch('requests.post', return_value=FakeResponse(500, 'boom')):
            for _ in range(SyncOutbox.MAX_ATTEMPTS + 1):
                SyncOutbox.objects.update(next_attempt_at=None)   # skip the wait
                sync_mod.drain()
        item = SyncOutbox.objects.get()
        assert item.status == SyncOutbox.Status.FAILED
        assert item.attempt_count >= SyncOutbox.MAX_ATTEMPTS

    def test_nothing_is_sent_before_enrolment(self, db, settings):
        """No token yet: hold the work rather than dropping it."""
        settings.SYNC_SERVER_URL = 'https://example.test'
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        with patch('requests.post') as post:
            sync_mod.drain()
        post.assert_not_called()
        assert SyncOutbox.objects.get().status == SyncOutbox.Status.PENDING

    def test_the_authorization_header_uses_the_device_scheme(self, enrolled):
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=42)
        with patch('requests.post', return_value=FakeResponse(201)) as post:
            sync_mod.drain()
        headers = post.call_args.kwargs['headers']
        assert headers['Authorization'] == 'Device a-token'

    def test_drain_survives_one_bad_row(self, enrolled):
        """One poisoned item must not stop the queue behind it."""
        sync_mod.enqueue('session', {'lesson_id': 1}, server_user_id=1)
        sync_mod.enqueue('session', {'lesson_id': 2}, server_user_id=2)
        with patch.object(sync_mod, 'send_one',
                          side_effect=[RuntimeError('bad row'), True]):
            result = sync_mod.drain()
        assert result['sent'] == 1


@pytest.mark.django_db
class TestBackoff:
    def test_grows_and_is_capped(self):
        assert sync_mod._backoff(1).total_seconds() < sync_mod._backoff(4).total_seconds()
        assert sync_mod._backoff(50).total_seconds() <= 310      # 300 cap + jitter

    def test_is_jittered(self):
        """Thirty laptops regaining wifi together must not arrive as one
        synchronised stampede."""
        values = {sync_mod._backoff(3).total_seconds() for _ in range(20)}
        assert len(values) > 1
