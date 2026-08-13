"""Put finished sessions into the outbox.

This is the producer half of sync. Without it the outbox is always empty, the
worker has nothing to send, and a device that is correctly configured still
delivers nothing — which is exactly the state this file was written to fix.

Wired as a post_save signal on TutorSession (registered in apps.py) rather than
called from the tutoring engine, so that `apps.tutoring` stays unaware of the
desktop build. The dependency runs one way: desktop knows about tutoring.

Only active when settings.DESKTOP_BUILD is true. On a server, sessions are
already where they need to be and queueing them would be nonsense.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

#: Marks a session as queued, so a later save cannot enqueue it twice. Stored in
#: engine_state because the desktop's TutorSession is the stock model — adding a
#: column for a desktop-only concern would put it on every server too.
SYNCED_KEY = 'desktop_sync_uuid'


def payload_for(session) -> dict:
    """The body the server's /api/v1/devices/sync/ expects for one session."""
    turns = []
    for turn in session.turns.order_by('created_at', 'id'):
        turns.append({
            'role': turn.role,
            'content': turn.content,
            # Per-turn identity, so a partially-applied retry cannot duplicate
            # individual turns. The first turn's uuid is overwritten by the
            # server with the outbox uuid; see device_sync.
            'client_uuid': str(uuid.uuid4()),
        })
    return {
        'lesson_id': session.lesson_id,
        'status': session.status,
        'turns': turns,
    }


def enqueue_session(session) -> bool:
    """Queue one finished session. True if it was newly queued.

    Idempotent: a session already marked as queued is skipped, so repeated
    saves after completion do not pile up duplicates in the outbox.
    """
    from apps.desktop import sync

    state = session.engine_state or {}
    if state.get(SYNCED_KEY):
        return False

    server_user_id = sync.server_user_id_for(session.student)
    item = sync.enqueue('session', payload_for(session),
                        server_user_id=server_user_id)
    if item is None:
        # enqueue() already logged. Not marking it means the next save retries,
        # which is the behaviour we want for a transient DB failure.
        return False

    # Mark via queryset update, not session.save(): we are inside that model's
    # post_save, and saving again would re-enter this handler.
    from apps.tutoring.models import TutorSession
    state[SYNCED_KEY] = str(item.client_uuid)
    TutorSession.objects.filter(pk=session.pk).update(engine_state=state)
    # ...and on the instance in memory. Without this the guard only works
    # across processes: the engine saves a completed session more than once
    # (status first, then engine_state), and each of those saves would see the
    # stale in-memory copy and queue the session again. Observed: 2 outbox rows
    # for one lesson.
    session.engine_state = state

    logger.info('[Sync] queued session %s (%d turns)', session.pk,
                len(item.payload.get('turns') or []))
    return True


def on_session_saved(sender, instance, created, **kwargs):
    """Queue a session the moment it is finished.

    Deliberately fail-soft: a sync problem must never surface as a failed
    lesson. The student has finished their work either way, and it is already
    saved locally.
    """
    from apps.tutoring.models import TutorSession

    # Note the absence of a `created` check. The status test already excludes
    # brand-new active sessions, and skipping creates would silently drop a
    # session written as completed in one step — which is exactly what an
    # importer or a replayed lesson does.
    if instance.status != TutorSession.Status.COMPLETED:
        return
    try:
        enqueue_session(instance)
    except Exception:                                        # noqa: BLE001
        logger.warning('[Sync] could not queue session %s', instance.pk,
                       exc_info=True)
