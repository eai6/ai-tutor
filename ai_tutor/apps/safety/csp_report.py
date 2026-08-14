"""Collector for CSP violation reports.

Only reachable when CSP_REPORT=1 puts a report-uri in the policy; the route
itself is always mounted, because a 404 on an unwired path is not worth a
conditional in config/urls.py.

Finding F-02 asked for this so that the CSP rollout could do the step it had
skipped: watch real browsers for one to two weeks, see what a strict policy
would break, and only then enforce. Without a collector, "no violations" means
"nobody was looking".

The reason this endpoint did not already exist is written into apps/safety/csp.py
and has not gone away: it is an unauthenticated POST that accepts
attacker-controlled JSON from any browser on the internet, added to a build
whose purpose is to reduce attack surface. So it is built to be boring:

  * POST only, and only the two content types browsers actually send.
  * Body capped well below anything a real report reaches. Oversized bodies are
    dropped without being parsed.
  * Rate limited per client IP through the cache, so a flood costs one cache
    increment rather than a log line and a JSON parse.
  * No database write, no model, no admin page. Reports go to the log stream
    that already carries everything else, where the container's log pipeline
    picks them up.
  * Only known fields are logged, each truncated. The whole document is
    attacker-controlled; echoing it verbatim into a log is how a report ends up
    executing in whatever renders that log.
  * Always answers 204 regardless of what happened. The sender is a browser
    that ignores the response, and an error code would only tell a prober which
    inputs are interesting.
"""
from __future__ import annotations

import json
import logging

from django.core.cache import cache
from django.http import HttpResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt

from .client_ip import get_client_ip

logger = logging.getLogger(__name__)

# A real violation report is a few hundred bytes. 8 KB is generous and still
# far below anything worth parsing twice.
MAX_REPORT_BYTES = 8 * 1024

# Per-IP ceiling. A page with a broken policy can fire a report per blocked
# element on every load, so this is about keeping one misconfigured deployment
# from filling the log stream, not about stopping abuse on its own.
MAX_REPORTS_PER_MINUTE = 30

_ACCEPTED_CONTENT_TYPES = frozenset({
    'application/csp-report',   # report-uri
    'application/reports+json',  # report-to
    'application/json',         # what some browsers send anyway
})

# Everything else in the document is ignored. These four say which directive
# fired and where, which is all the refactor backlog needs.
_LOGGED_FIELDS = (
    'effective-directive',
    'violated-directive',
    'blocked-uri',
    'document-uri',
)

_MAX_FIELD_CHARS = 200


@csrf_exempt
def csp_report(request):
    """Accept one CSP violation report. Always 204."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    if request.content_type not in _ACCEPTED_CONTENT_TYPES:
        return HttpResponse(status=204)

    if not _within_rate_limit(request):
        return HttpResponse(status=204)

    try:
        length = int(request.META.get('CONTENT_LENGTH') or 0)
    except (TypeError, ValueError):
        return HttpResponse(status=204)
    if length <= 0 or length > MAX_REPORT_BYTES:
        return HttpResponse(status=204)

    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=204)

    for report in _reports_in(payload):
        logger.warning('CSP violation: %s', _summarise(report))

    return HttpResponse(status=204)


def _within_rate_limit(request) -> bool:
    ip = get_client_ip(request) or 'unknown'
    key = f'csp-report:{ip}'
    try:
        count = cache.get_or_set(key, 0, timeout=60)
        cache.set(key, count + 1, timeout=60)
    except Exception:
        # A cache backend that is down must not turn into a 500 on a public
        # endpoint. Accept the report and move on.
        return True
    return count < MAX_REPORTS_PER_MINUTE


def _reports_in(payload):
    """Yield the individual report bodies out of either wire format.

    report-uri sends ``{"csp-report": {...}}``; report-to sends a list of
    ``{"type": ..., "body": {...}}``. Anything else yields nothing.
    """
    if isinstance(payload, dict):
        body = payload.get('csp-report')
        if isinstance(body, dict):
            yield body
        return
    if isinstance(payload, list):
        for entry in payload[:10]:
            if isinstance(entry, dict) and isinstance(entry.get('body'), dict):
                yield entry['body']


def _summarise(report: dict) -> str:
    parts = []
    for field in _LOGGED_FIELDS:
        value = report.get(field)
        if isinstance(value, str) and value:
            parts.append(f'{field}={value[:_MAX_FIELD_CHARS]!r}')
    return ' '.join(parts) or '(no recognised fields)'
