"""LocaleResolverMiddleware — per-request locale resolution chain.

Resolves the active UI locale for each request using the platform's
multi-locale architecture (memory/multi_locale_architecture_research.md):

    1. course.locale         — when the request is for a chat tutor view,
                                the active course's locale wins. (NOTE:
                                this branch is handled inside the chat
                                tutor view itself via
                                ``translation.activate(course.locale)``
                                because we don't have access to view
                                kwargs in process_request. Middleware
                                covers everything else.)
    2. student.preferred_locale  — explicit per-user override.
    3. institution.default_locale — school-level default.
    4. settings.LANGUAGE_CODE      — global fallback (currently 'en-us').

Wired into MIDDLEWARE AFTER AuthenticationMiddleware (needs
``request.user``) and BEFORE Django's own LocaleMiddleware (so we set
the active language before Django's middleware uses it).

Pairs with a `try/finally` activation in the chat tutor view for the
"course wins in-session" branch — that view calls
``translation.activate(session.course.locale)`` explicitly so the chat
shell renders in the lesson's language regardless of student/school
preference. See ``apps/tutoring/views.py::chat_tutor_interface``.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import translation
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


def _resolve_locale(request) -> str:
    """Return the locale string to activate for this request.

    Defensive — every branch wraps the DB lookup in a try/except so a
    malformed user / missing profile / missing institution never 500s
    the request. Fall through to ``settings.LANGUAGE_CODE`` on any
    error.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return settings.LANGUAGE_CODE

    # 1. Explicit student preference.
    try:
        profile = getattr(user, 'student_profile', None)
        if profile is not None and profile.preferred_locale:
            return profile.preferred_locale
    except Exception:  # noqa: BLE001 — defensive, never 500 on locale
        logger.debug("locale resolver: student_profile read failed", exc_info=True)

    # 2. Institution default. A user may have multiple memberships
    # (staff in school A, student in school B). Pick the first active
    # one — institution-locale collisions are not a real risk in the
    # pilot (one student per school per pilot).
    try:
        membership = user.memberships.filter(is_active=True).first()
        if membership is not None and membership.institution_id is not None:
            inst_locale = membership.institution.default_locale
            if inst_locale:
                return inst_locale
    except Exception:  # noqa: BLE001
        logger.debug("locale resolver: membership lookup failed", exc_info=True)

    # 3. Global fallback.
    return settings.LANGUAGE_CODE


class LocaleResolverMiddleware(MiddlewareMixin):
    """Activates the resolved locale in ``process_request`` and ensures
    it is deactivated in ``process_response`` so worker-thread state
    doesn't leak between requests.

    The chat tutor view overrides this within its own scope by calling
    ``translation.activate(course.locale)`` directly — that activation
    wins for the duration of the view, and our ``process_response``
    deactivates everything at the end.
    """

    def process_request(self, request):
        locale = _resolve_locale(request)
        translation.activate(locale)
        request.LANGUAGE_CODE = translation.get_language()
        return None

    def process_response(self, request, response):
        # Always deactivate, even when the view re-activated a
        # different language for itself. This prevents locale bleed
        # between requests on the same worker thread.
        translation.deactivate()
        return response

    def process_exception(self, request, exception):
        # Belt-and-braces: deactivate on exception so a 500 doesn't
        # leave the worker thread holding a non-default locale.
        translation.deactivate()
        return None
