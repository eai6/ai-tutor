"""A CSRF rejection a person can recover from.

Django's stock failure page is a bare "Forbidden (403) — CSRF verification
failed. Request aborted." with DEBUG off. It is accurate and it is useless: it
names a mechanism the reader has never heard of, offers no way back, and
arrives at the exact moment their typed input is discarded.

The rejection is not rare and it is not usually an attack. Sessions here last
two weeks (Django's SESSION_COOKIE_AGE default) while CSRF_COOKIE_AGE is twelve
hours, deliberately — a 2026-08 assessment finding cut it down from a year. So a
signed-in teacher outlives several token generations in one sitting. Leave the
settings page open over lunch, load any other page in another tab (which mints a
fresh token), come back and save: the form still carries the token the old
cookie minted, the cookie no longer matches it, and Django answers 403
"CSRF token from POST incorrect".

What that person needs is the three things this page gives them: what happened
in words about their session rather than about tokens, that their work was not
saved, and a way back to the form. Nothing here weakens the check — the request
is still rejected. Only the explanation changes.

The reason string is shown to nobody. It distinguishes "no cookie" from "wrong
cookie", which is a probing oracle, and it means nothing to the reader anyway.
It is logged instead.
"""
from __future__ import annotations

import logging

from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme

from .client_ip import get_client_ip

logger = logging.getLogger(__name__)

TEMPLATE = 'safety/csrf_failure.html'


def _safe_return_url(request) -> str | None:
    """The page the rejected form was on, if it is one of ours.

    Referer is set by the client, so it is a redirect target only after the
    same host check every other back-link in this project gets. An off-site or
    malformed value is dropped rather than sanitised — the page reads fine with
    no button, and the alternative is an open redirect reachable by anyone who
    can make a browser send a bad token.
    """
    referer = request.META.get('HTTP_REFERER') or ''
    if not referer:
        return None
    if not url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return None
    return referer


def csrf_failure(request, reason='', template_name=TEMPLATE):
    """Render the recovery page. Signature is Django's CSRF_FAILURE_VIEW contract."""
    logger.warning(
        'CSRF failure on %s (reason=%s ip=%s authenticated=%s)',
        request.path, reason or 'unspecified', get_client_ip(request),
        getattr(request.user, 'is_authenticated', False),
    )
    response = render(
        request,
        template_name,
        {'return_url': _safe_return_url(request)},
        status=403,
    )
    # This page exists because a stale page was submitted. Letting a cache keep
    # a copy is how the reader gets it a second time on a request that would
    # have worked.
    response['Cache-Control'] = 'no-store'
    return response
