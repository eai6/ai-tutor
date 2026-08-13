"""Draining the outbox to the cloud.

Runs on the DEVICE. Everything here must survive the network simply not being
there, which is the normal case rather than the exception — a classroom laptop
may go weeks between connections.

Three rules the rest of this module exists to keep:

1. **Never block the student.** Enqueueing is a local insert; the worker runs
   on its own thread. A lesson behaves identically whether or not a server
   exists.
2. **Never lose work.** The queue is a table (``SyncOutbox``), so closing the
   lid mid-lesson costs nothing.
3. **Never double-write.** Every row carries a ``client_uuid`` the server
   enforces uniqueness on, so a retry after a lost response is harmless.

Uses ``requests``, deliberately not boto3 or any cloud SDK: AI-Tutor.spec
excludes those from the bundle because "an offline build never calls them", and
a sync client is not a reason to put a cloud SDK on a classroom laptop.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Poll interval when the queue is empty. Long on purpose — this is a background
# chore on a machine whose battery matters, not a latency-sensitive path.
IDLE_SECONDS = 60
BATCH_SIZE = 20
REQUEST_TIMEOUT = 20          # fail fast to "offline" rather than hanging

_worker_started = False
_worker_lock = threading.Lock()


# ── Enqueue ─────────────────────────────────────────────────────────────

def enqueue(kind: str, payload: dict, server_user_id: int | None = None):
    """Add one item to the outbox. Called from the tutoring path.

    Swallows its own errors. A failure to queue telemetry must never surface as
    a failed lesson — the student is mid-sentence and the server is optional.
    """
    from ai_tutor.apps.desktop.models import SyncOutbox
    try:
        return SyncOutbox.objects.create(
            kind=kind, payload=payload, server_user_id=server_user_id,
        )
    except Exception:                                    # noqa: BLE001
        logger.warning('[Sync] could not enqueue %s', kind, exc_info=True)
        return None


def server_user_id_for(user) -> int | None:
    """The server's id for a local user, via their claimed roster entry.

    None for a self-registered student. Those rows still sync — a teacher
    reconciles them — because dropping a term's work to keep the data tidy is
    the wrong trade.
    """
    entry = getattr(user, 'roster_entry', None)
    return entry.server_user_id if entry else None


# ── Send ────────────────────────────────────────────────────────────────

def _backoff(attempt: int) -> timedelta:
    """Exponential with jitter, capped at five minutes.

    The jitter is not decoration. A classroom of thirty laptops regains wifi at
    the same moment, and un-jittered retries would arrive as one synchronised
    stampede every time.
    """
    base = min(30 * (2 ** attempt), 300)
    return timedelta(seconds=base + random.uniform(0, 10))


def _endpoint(path: str, state=None) -> str | None:
    """Absolute URL on this device's server, or None if it has no server.

    Reads DeviceState rather than the setting alone: a packaged desktop
    application cannot be handed an environment variable, so the server is
    normally chosen on the connection screen after install. The setting still
    takes precedence for scripted rollouts — see DeviceState.server_url.
    """
    from ai_tutor.apps.desktop.models import DeviceState

    if state is None:
        state = DeviceState.load()
    base = state.effective_server_url
    return f'{base}{path}' if base else None


def _refresh_access_token(state) -> bool:
    """Trade the refresh token for a new access token. True if it worked.

    Quiet on failure: an expired refresh is not an error to alarm anyone with,
    it just means the student signs in again next time they are at the machine.
    """
    import requests

    if not state.refresh_token:
        return False
    url = _endpoint('/api/v1/auth/refresh/', state)
    if not url:
        return False
    try:
        response = requests.post(url, json={'refresh': state.refresh_token},
                                 timeout=REQUEST_TIMEOUT)
    except Exception:                                        # noqa: BLE001
        return False
    if response.status_code != 200:
        return False
    access = (response.json() or {}).get('access') or ''
    if not access:
        return False
    state.access_token = access
    state.save(update_fields=['access_token'])
    return True


def send_one(item) -> bool:
    """Push one outbox row. True if it is done with (delivered or duplicate)."""
    import requests
    from ai_tutor.apps.desktop.models import DeviceState, SyncOutbox

    state = DeviceState.load()
    token = state.access_token or ''
    url = _endpoint('/api/v1/sessions/upload/', state)
    if not url or not token:
        # No server, or nobody signed in yet. Both are normal: the work stays
        # queued and goes up whenever someone does sign in.
        return False

    # The upload endpoint takes the session directly, authenticated as the
    # student who did it. No device identity, no server_user_id — the token
    # already says who this is, and the server will not accept a claim to be
    # anyone else.
    body = {'client_uuid': str(item.client_uuid), **item.payload}

    item.attempt_count += 1
    item.last_attempt_at = timezone.now()

    try:
        response = requests.post(
            url, json=body, timeout=REQUEST_TIMEOUT,
            headers={'Authorization': f'Bearer {token}'},
        )
        if response.status_code == 401 and _refresh_access_token(state):
            # Access tokens are short-lived and a device is often asleep for
            # longer than one lasts, so this is the common path, not an edge.
            response = requests.post(
                url, json=body, timeout=REQUEST_TIMEOUT,
                headers={'Authorization': f'Bearer {state.access_token}'},
            )
    except Exception as exc:                              # noqa: BLE001
        # Offline, DNS failure, captive portal. Not an error worth alarming
        # anyone about — it is the expected state.
        item.last_error = f'{type(exc).__name__}: {exc}'[:500]
        item.next_attempt_at = timezone.now() + _backoff(item.attempt_count)
        item.save(update_fields=['attempt_count', 'last_attempt_at',
                                 'last_error', 'next_attempt_at'])
        return False

    if response.status_code in (200, 201, 409):
        # 409 = the server already has it. From our side that is success: the
        # work is safely there and retrying would achieve nothing.
        item.status = SyncOutbox.Status.SENT
        item.sent_at = timezone.now()
        item.last_error = ''
        item.save(update_fields=['status', 'sent_at', 'last_error',
                                 'attempt_count', 'last_attempt_at'])
        return True

    if response.status_code in (401, 403):
        # The sign-in is no longer good — password changed, account disabled,
        # refresh expired. Retrying cannot fix it and hammering a server that
        # is refusing us is worse than stopping. The student signs in again.
        item.status = SyncOutbox.Status.FAILED
        item.last_error = f'HTTP {response.status_code}: sign in again'
        item.save(update_fields=['status', 'last_error', 'attempt_count',
                                 'last_attempt_at'])
        logger.warning('[Sync] rejected (%s) — student must sign in again',
                       response.status_code)
        return True

    item.last_error = f'HTTP {response.status_code}: {response.text[:200]}'
    if item.attempt_count >= SyncOutbox.MAX_ATTEMPTS:
        item.status = SyncOutbox.Status.FAILED
    else:
        item.next_attempt_at = timezone.now() + _backoff(item.attempt_count)
    item.save(update_fields=['status', 'last_error', 'attempt_count',
                             'last_attempt_at', 'next_attempt_at'])
    return False


def drain(limit: int = BATCH_SIZE) -> dict:
    """Send whatever is due. Returns counts; never raises."""
    from ai_tutor.apps.desktop.models import SyncOutbox

    now = timezone.now()
    due = SyncOutbox.objects.filter(
        status=SyncOutbox.Status.PENDING,
    ).filter(
        models_q_due(now)
    ).order_by('created_at')[:limit]

    sent = failed = 0
    for item in due:
        try:
            if send_one(item):
                sent += 1
            else:
                failed += 1
        except Exception:                                 # noqa: BLE001
            logger.warning('[Sync] send failed for %s', item.client_uuid, exc_info=True)
            failed += 1
    return {'sent': sent, 'deferred': failed}


def models_q_due(now):
    """Rows whose backoff has elapsed (or that have never been tried)."""
    from django.db.models import Q
    return Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)


# ── Worker ──────────────────────────────────────────────────────────────

def _loop():
    while True:
        try:
            result = drain()
            if result['sent']:
                logger.info('[Sync] pushed %s item(s)', result['sent'])
        except Exception:                                 # noqa: BLE001
            # The worker must outlive any single failure. A dead sync thread is
            # invisible until someone notices weeks of missing data.
            logger.warning('[Sync] drain cycle failed', exc_info=True)
        time.sleep(IDLE_SECONDS)


def start_worker():
    """Start the background drainer once per process."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    thread = threading.Thread(target=_loop, name='sync-outbox', daemon=True)
    thread.start()
    logger.info('[Sync] outbox worker started')
