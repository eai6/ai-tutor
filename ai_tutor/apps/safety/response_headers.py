"""Defence-in-depth response headers, and no-store for signed-in pages.

Covers two findings from the 2026-08-13 assessment:

**F-12 — modern headers absent.** The header set was already good (HSTS,
X-Frame-Options, X-Content-Type-Options, Referrer-Policy,
Cross-Origin-Opener-Policy). Two were missing and are added here.

*Permissions-Policy* is not a copy of the report's suggested value, because that
value would have broken the product. This application genuinely uses two
capabilities: the tutor records voice answers through ``getUserMedia({audio})``
(templates/tutoring/chat_tutor.html) and the feedback widget takes a screenshot
through ``getDisplayMedia`` (templates/_includes/feedback_button.html). Both are
granted to ``self`` and nothing else, so an injected third-party frame still
cannot reach them. Everything the app does not use — camera included, since the
microphone path asks for audio only — is denied outright. For a platform used by
minors, an explicit ``camera=()`` is worth having written down.

*Cross-Origin-Resource-Policy* is ``same-site`` rather than ``same-origin`` so
that a future subdomain split (media on its own host, for instance) does not
have to revisit it. Static assets get the same value from
``static_headers.py`` — WhiteNoise short-circuits above this middleware, so it
never sees them.

**F-09 — sensitive HTML cacheable.** The instances the scanner caught were
public marketing pages, where caching is fine. The finding was really about
what it could not test: the authenticated dashboards, on the shared and
school-managed devices this platform runs on, where a rendered page left in the
disk cache is readable by the next person at that machine with no credentials.

Narrower than the report's blanket ``never_cache`` on every authenticated
response, deliberately. Applying no-store to *every* response for a logged-in
user also kills caching of media — figures, recorded audio, lesson images — all
of which are served through our own domain to authenticated students and are
expensive to re-fetch from S3 on every page view. Rendered personal data lives
in HTML and JSON; that is what gets the header. A view that already set its own
Cache-Control keeps it, because a view that thought about caching knows more
than this middleware does.
"""
from __future__ import annotations

PERMISSIONS_POLICY = ', '.join((
    # Used by the product, same-origin only.
    'microphone=(self)',        # voice answers in the tutor
    'display-capture=(self)',   # screenshot in the feedback widget
    # Not used. Denied to everyone including this origin.
    'camera=()',
    'geolocation=()',
    'payment=()',
    'usb=()',
    'serial=()',
    'midi=()',
    'accelerometer=()',
    'gyroscope=()',
    'magnetometer=()',
    'interest-cohort=()',
))

CROSS_ORIGIN_RESOURCE_POLICY = 'same-site'

NO_STORE = 'no-store, no-cache, must-revalidate, private'

# Response bodies that can carry a rendered view of someone's personal data.
# Images, audio and video are the student's own lesson media and stay cacheable.
_PRIVATE_CONTENT_TYPES = ('text/html', 'application/json')


class SecurityHeadersMiddleware:
    """Add the missing defence-in-depth headers; keep signed-in pages out of caches."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # setdefault, not assignment: a view that set its own policy narrowed it
        # for a reason this middleware cannot see.
        response.setdefault('Permissions-Policy', PERMISSIONS_POLICY)
        response.setdefault('Cross-Origin-Resource-Policy', CROSS_ORIGIN_RESOURCE_POLICY)

        if self._is_private_render(request, response):
            response['Cache-Control'] = NO_STORE
            # Some intermediaries still honour only the HTTP/1.0 spelling.
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'

        return response

    @staticmethod
    def _is_private_render(request, response) -> bool:
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return False
        if 'Cache-Control' in response.headers:
            return False
        content_type = response.headers.get('Content-Type', '')
        return content_type.startswith(_PRIVATE_CONTENT_TYPES)
