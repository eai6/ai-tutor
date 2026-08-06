"""Pick the tutoring model for a session, honouring the student's preference.

Exists because the offline desktop build can have *two* usable tutors — the
local qwen3-4b and, when the school has internet, a cloud model — and which one
to use is a student-level preference rather than a deployment constant.

Deliberately NOT done with ``TUTOR_MODEL_OVERRIDE``. That env var is
process-wide, so it cannot vary per student, and setting it is what previously
made the model choice in the dashboard read-only in practice (see the note in
``desktop_server.py`` DEFAULTS and ``engine.py:2022``).

Resolution:

    offline  -> the local model, always
    online   -> a cloud model, always
    auto     -> cloud if reachable, else local

``auto`` is the default and the reason this module has a reachability check:
on a classroom laptop the internet comes and goes, and a student mid-lesson
should not get an error because the link dropped. Falling back is the whole
point — a lesson that continues on a weaker model beats a lesson that stops.

Returns ``None`` to mean "no preference applies, use the normal
``ModelConfig.get_for('tutoring')`` path", which keeps the hosted platform
behaving exactly as it does today.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

LOCAL_PROVIDER = 'local_ollama'

# Reachability is cached because `auto` is evaluated on every turn and a
# DNS/TCP probe per turn is both slow and pointless — connectivity does not
# change on a per-turn timescale. Short enough that a school coming back online
# is noticed within a lesson.
_REACHABILITY_TTL_SECONDS = 60
_reachability_cache: dict = {'checked_at': 0.0, 'online': None}


def _cloud_reachable(timeout: float = 2.0) -> bool:
    """Cheap connectivity probe.

    Checks a TCP/HTTP endpoint rather than asking the provider SDK: a real
    completion call costs money and seconds, and the question here is only
    "is there internet", not "is the provider healthy". A provider that is up
    but erroring is handled by the engine's existing fail-soft path.
    """
    now = time.monotonic()
    cached = _reachability_cache
    if cached['online'] is not None and (now - cached['checked_at']) < _REACHABILITY_TTL_SECONDS:
        return cached['online']

    probe = os.environ.get('CONNECTIVITY_PROBE_URL', 'https://api.anthropic.com/')
    try:
        req = urllib.request.Request(probe, method='HEAD')
        with urllib.request.urlopen(req, timeout=timeout):
            online = True
    except Exception:
        # Any failure — DNS, TCP, TLS, HTTP error — counts as "not usable".
        # An HTTP 4xx from the probe still proves connectivity, but urlopen
        # raises on those, and treating them as offline only costs us a
        # fallback to a model that works.
        online = False

    _reachability_cache.update({'checked_at': now, 'online': online})
    return online


def _first_active(purpose: str, *, local: bool):
    from apps.llm.models import ModelConfig

    qs = ModelConfig.objects.filter(purpose=purpose, is_active=True)
    qs = qs.filter(provider=LOCAL_PROVIDER) if local else qs.exclude(provider=LOCAL_PROVIDER)
    # Order explicitly. Unordered .first() is whatever the DB returns, so
    # installing a second local model silently changed the default tutor for
    # every student who had not picked one — on the desktop that moved them
    # from the 4B to a 14B that was still downloading. Oldest row first keeps
    # the original default stable as models are added.
    return qs.order_by('id').first()


def local_options():
    """Every local model a student could be offered, ordered for display.

    The picker only appears when this returns more than one — on a device with
    a single local model there is nothing to choose and the control would be
    a dead input.
    """
    from apps.llm.models import ModelConfig
    return list(
        ModelConfig.objects
        .filter(purpose='tutoring', provider=LOCAL_PROVIDER, is_active=True)
        .order_by('model_name')
    )


def _resolve_local(profile):
    """The local model this student should get.

    Their explicit pick when it is still installed and active, otherwise the
    platform default. Checking `is_active`/provider again matters because a
    model can be retired after a student selected it — falling back beats
    handing the engine a config that no longer runs.
    """
    chosen = getattr(profile, 'offline_model', None)
    if chosen is not None:
        if (chosen.is_active
                and chosen.provider == LOCAL_PROVIDER
                and chosen.purpose == 'tutoring'):
            return chosen
        logger.info(
            "[TutorMode] student's offline model %s is no longer available — "
            "falling back to the default", getattr(chosen, 'model_name', '?'),
        )
    return _first_active('tutoring', local=True)


def resolve_for_session(session):
    """Return the ModelConfig this session should use, or None to defer.

    Never raises: a failure here must not take down a tutoring turn, so the
    caller falls back to the normal resolution path.
    """
    try:
        profile = getattr(getattr(session, 'student', None), 'student_profile', None)
        mode = getattr(profile, 'tutor_mode', None)
        if not mode:
            return None

        # An env override still wins where it is set (evals, chat.py, sweeps).
        # Those callers are pinning a specific model on purpose and a student
        # preference must not silently redirect them.
        if os.getenv('TUTOR_MODEL_OVERRIDE', '').strip():
            return None

        local = _resolve_local(profile)
        cloud = _first_active('tutoring', local=False)

        # Only one option configured — nothing to choose between, so let the
        # normal path handle it. This is the hosted platform's situation.
        #
        # EXCEPT when the student picked a specific local model: that is a
        # choice even with no cloud tutor to switch to, and deferring here
        # would silently ignore it on an offline-only install — which is
        # exactly the desktop build.
        if cloud is None:
            picked = getattr(profile, 'offline_model_id', None)
            return local if (picked and local is not None) else None
        if local is None:
            return None

        if mode == 'offline':
            return local
        if mode == 'online':
            return cloud

        if _cloud_reachable():
            return cloud
        logger.info(
            "[TutorMode] auto: cloud unreachable, falling back to %s/%s",
            local.provider, local.model_name,
        )
        return local
    except Exception as exc:                      # noqa: BLE001
        logger.warning("resolve_for_session failed, deferring: %s", exc)
        return None


def describe_for_student(profile) -> dict:
    """What the student settings page shows.

    ``available`` is False on a deployment with only one tutor, which is how
    the template knows to hide the control rather than offer a choice that
    does nothing.
    """
    try:
        options = local_options()
        local = _resolve_local(profile)
        cloud = _first_active('tutoring', local=False)
        return {
            'available': bool(local and cloud),
            'mode': getattr(profile, 'tutor_mode', 'auto'),
            'offline_model': local.model_name if local else None,
            'online_model': cloud.model_name if cloud else None,
            'online_reachable': _cloud_reachable() if cloud else False,
            # The model picker is a separate control with its own visibility
            # rule: it shows whenever a device has more than one local model,
            # even if there is no cloud tutor at all.
            'offline_options': options,
            'offline_options_available': len(options) > 1,
            'offline_model_id': getattr(profile, 'offline_model_id', None),
        }
    except Exception as exc:                      # noqa: BLE001
        logger.warning("describe_for_student failed: %s", exc)
        return {'available': False, 'mode': 'auto',
                'offline_options': [], 'offline_options_available': False}
