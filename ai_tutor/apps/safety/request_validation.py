"""Reject hostile control characters before they reach the data layer.

A NUL byte inside a form field crashed four public endpoints in the 2026-08-13
assessment (finding F-01). psycopg refuses to encode a Python string containing
0x00 — ``ValueError: A string literal cannot contain NUL (0x00) characters`` —
Django surfaces that as an unhandled exception and gunicorn turns it into a 500.
Six such crashes were captured against /student/login/, /staff/login/,
/student/register/ and /staff/register/: unauthenticated, deterministic, and
free to repeat.

The report's suggested fix was a form mixin carrying
``ProhibitNullCharactersValidator``. That does not apply here, because there is
no form layer to attach it to — ``apps/accounts/views.py`` reads ``request.POST``
directly and passes the result straight to ``authenticate()`` and
``User.objects.create_user()``, and the project has no ``forms.py`` anywhere.
The equivalent central place is one hop earlier, here, which also covers the
authenticated views and the JSON API. The report specifically flagged those as
the likely next place to find the same crash, and they would have been missed by
a mixin on the four public forms.

Two treatments, matching what the finding asked for:

  * **NUL is rejected** with 400. It can never be legitimate input — no keyboard
    produces it and no locale needs it — and it is the character that actually
    crashes the database driver.

  * **The remaining C0 controls** (0x01-0x1F less tab, newline, carriage return)
    are **stripped**. These crash nothing; they are log-injection and
    display-spoofing material that mostly arrives inside pasted text. Rejecting
    a teacher's pasted curriculum because it carried a form feed would be a
    worse bug than the one being fixed.

Field NAMES are logged, never values. The value is attacker-controlled and by
definition full of control characters, so logging it is how a scanner payload
ends up executing in whatever reads the log.

Why not rely on the WAF: the ALB rules are a perimeter control that this same
assessment showed blocks scanners, not a guarantee about any single byte. The
crash is in our code and the fix belongs in our code.
"""
from __future__ import annotations

import logging

from django.http import HttpResponseBadRequest, JsonResponse

from .client_ip import get_client_ip

logger = logging.getLogger(__name__)

NUL = '\x00'

# Every C0 control except the three that occur in real text: tab, newline and
# carriage return. str.translate takes a {codepoint: None} map to delete.
STRIPPABLE_CONTROLS = {c: None for c in range(0x20)}
for _keep in (0x09, 0x0A, 0x0D):
    STRIPPABLE_CONTROLS.pop(_keep)

_FORM_CONTENT_TYPES = frozenset({
    'application/x-www-form-urlencoded',
    'multipart/form-data',
})

# Only bodies we could hold in memory anyway are scanned. Anything larger is
# left alone, because the size limit that governs it is
# DATA_UPLOAD_MAX_MEMORY_SIZE and the view is where that surfaces.
MAX_SCANNED_BODY = 1024 * 1024

_METHODS_WITH_BODY = frozenset({'POST', 'PUT', 'PATCH'})


def clean_querydict(source):
    """Split one QueryDict into (nul_fields, stripped_fields, cleaned).

    ``cleaned`` is None when nothing needed rewriting, so the common case
    allocates nothing. Fields carrying a NUL are reported and left untouched —
    the request is about to be rejected whole, so there is nothing to clean.
    """
    nul_fields: list[str] = []
    stripped_fields: list[str] = []
    cleaned = None

    for key in source:
        values = source.getlist(key)
        if NUL in key or any(NUL in value for value in values):
            nul_fields.append(key)
            continue

        new_key = key.translate(STRIPPABLE_CONTROLS)
        new_values = [value.translate(STRIPPABLE_CONTROLS) for value in values]
        if new_key == key and new_values == values:
            continue

        stripped_fields.append(key)
        if cleaned is None:
            cleaned = source.copy()
        if new_key != key:
            del cleaned[key]
        cleaned.setlist(new_key, new_values)

    return nul_fields, stripped_fields, cleaned


class ControlCharacterMiddleware:
    """Reject NUL bytes, strip other C0 controls, on every inbound request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        nul_fields: list[str] = []
        stripped_fields: list[str] = []

        # Query string first: cheap, and it needs no body to have been read.
        nul, stripped, cleaned = clean_querydict(request.GET)
        nul_fields += [f'GET:{name}' for name in nul]
        stripped_fields += [f'GET:{name}' for name in stripped]
        if cleaned is not None:
            request.GET = cleaned

        content_type = request.content_type or ''
        if request.method == 'POST' and content_type in _FORM_CONTENT_TYPES:
            # Touching request.POST parses the body. That is not new work:
            # CsrfViewMiddleware already does it for every unsafe form request,
            # and Django caches the result, so the view still sees one parse.
            nul, stripped, cleaned = clean_querydict(request.POST)
            nul_fields += [f'POST:{name}' for name in nul]
            stripped_fields += [f'POST:{name}' for name in stripped]
            if cleaned is not None:
                request.POST = cleaned

            # An upload's NAME is stored in a text column exactly like a form
            # value, so it reaches psycopg by the same route. Contents are
            # bytes in a file field and are none of this middleware's business.
            for field, uploaded in request.FILES.items():
                if NUL in (uploaded.name or ''):
                    nul_fields.append(f'FILES:{field}')
        elif self._body_carries_nul(request):
            nul_fields.append('body')

        if nul_fields:
            logger.warning(
                'Rejected request carrying NUL bytes in %s (path=%s ip=%s)',
                ', '.join(sorted(nul_fields)), request.path, get_client_ip(request),
            )
            return self._bad_request(request)

        if stripped_fields:
            logger.info(
                'Stripped control characters from %s (path=%s ip=%s)',
                ', '.join(sorted(stripped_fields)), request.path,
                get_client_ip(request),
            )

        return self.get_response(request)

    @staticmethod
    def _body_carries_nul(request) -> bool:
        """True if a non-form body carries a NUL, raw or JSON-escaped.

        Matched on the raw bytes rather than by parsing. This runs on every API
        request, and ``json.loads`` over an attacker-supplied body is work the
        view should do once, behind its own auth check. Raw 0x00 and the
        six-byte ``\\u0000`` escape are the only two ways a NUL arrives in JSON.
        """
        if request.method not in _METHODS_WITH_BODY:
            return False
        try:
            length = int(request.META.get('CONTENT_LENGTH') or 0)
        except (TypeError, ValueError):
            return False
        if length <= 0 or length > MAX_SCANNED_BODY:
            return False
        try:
            body = request.body
        except Exception:
            # Stream already consumed, or over the upload limit. Either way the
            # view's own error handling owns the case and always has.
            return False
        return b'\x00' in body or b'\\u0000' in body

    @staticmethod
    def _bad_request(request):
        """400 in the shape the caller asked for.

        Deliberately says nothing about which field or which character. The
        sender knows what they sent; a legitimate user never reaches this.
        """
        accepts_json = (
            request.content_type == 'application/json'
            or request.headers.get('Accept', '').startswith('application/json')
        )
        if accepts_json:
            return JsonResponse({'error': 'Invalid characters in request.'}, status=400)
        return HttpResponseBadRequest(
            '<!doctype html><html lang="en"><head><title>Bad Request (400)</title>'
            '</head><body><h1>Bad Request (400)</h1>'
            '<p>The request contained characters that cannot be processed.</p>'
            '</body></html>'
        )
