"""Whether this device can teach a lesson yet.

A desktop install arrives without weights — the tutor model, the retrieval
encoder — and acquires them afterwards. Until it has them, a lesson must not
start.

The alternative was to teach anyway and warn. That is worse than it sounds:
without the encoder ``_retrieve_kb`` catches its own failure and returns an
empty list, so the tutor keeps answering, ungrounded, and the session looks
identical to a healthy one in the chat, in the transcript, and to the teacher.
A gate turns an invisible quality failure into a visible, fixable setup step.

Desktop only. A hosted deployment has its assets by construction, and must not
inherit a new way to fail.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Long enough that opening a lesson does not re-probe Ollama every click, short
# enough that finishing an install is noticed without a restart.
_TTL_SECONDS = 10
_cache: dict = {'checked_at': 0.0, 'value': None}


def gate_enabled() -> bool:
    from django.conf import settings
    return bool(getattr(settings, 'DESKTOP_BUILD', False))


def lesson_prerequisites() -> tuple:
    """``(ready, missing)`` — missing is a list of human-readable labels.

    Fails closed. Anything that goes wrong while checking counts as not ready,
    because the failure modes here (no Ollama, no model list, unreadable data
    directory) are the same conditions that would break the lesson anyway.
    """
    if not gate_enabled():
        return True, []

    now = time.monotonic()
    if _cache['value'] is not None and (now - _cache['checked_at']) < _TTL_SECONDS:
        return _cache['value']

    missing = []
    try:
        from ai_tutor.apps.desktop import assets, provisioning

        if not provisioning.model_installed():
            missing.append('Tutor model')
        missing.extend(a.label for a in assets.missing_required())
    except Exception as exc:                         # noqa: BLE001
        logger.warning('lesson_prerequisites failed, treating as not ready: %s', exc)
        missing = missing or ['Setup could not be verified']

    result = (not missing, missing)
    _cache.update({'checked_at': now, 'value': result})
    return result


def invalidate() -> None:
    """Drop the cache — call after an install finishes."""
    _cache.update({'checked_at': 0.0, 'value': None})
