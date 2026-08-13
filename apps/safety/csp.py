"""Content-Security-Policy header.

Ships REPORT-ONLY by default. A blocking policy that gets one directive wrong
takes the dashboard down, and the templates are not ready for a strict one:

    86 templates, of which
    32  contain an inline <script> block
    55  contain a <style> block
    206 inline event handlers (onclick=, onchange=, ...)
    2226 inline style="" attributes

Every one of those needs 'unsafe-inline' to run, so script-src cannot be
tightened until they are refactored into static files. What the policy CAN do
today is close the directives that need no template changes at all, because the
templates reference no external host — the only absolute URL anywhere in them is
the SVG xmlns. Those directives are the ones that matter against injected
<script src="//evil">, an injected <base>, a form re-pointed at another origin,
and clickjacking.

Enforcement is a deliberate follow-up, not a default:

    CSP_ENFORCE=1     send Content-Security-Policy instead of the -Report-Only
                      variant. Do this only after watching a real deployment
                      with the browser console open and seeing no violations.

There is deliberately no report-uri. Collecting reports means exposing an
unauthenticated POST endpoint that accepts attacker-controlled JSON from any
browser on the internet — a new piece of attack surface, added to a build whose
point is to reduce it. Violations are visible in the browser console, which is
where the person doing the refactor is looking anyway.
"""
import os

# Kept as a tuple of (directive, value) rather than a dict so the order in the
# emitted header is stable and diffable between runs.
_POLICY = (
    # Nothing loads from anywhere but this origin unless a directive below
    # widens it. The templates reference no CDN, so this holds today.
    ('default-src', "'self'"),

    # Not tightenable yet — see the counts in the module docstring. Listed
    # explicitly rather than inherited from default-src so that the day the
    # inline handlers are gone, the fix is deleting two words on one line.
    ('script-src', "'self' 'unsafe-inline'"),
    ('style-src', "'self' 'unsafe-inline'"),

    # data: for inline SVG and base64 thumbnails; blob: for figures generated
    # client-side and for recorded audio playback in the tutor.
    ('img-src', "'self' data: blob:"),
    ('media-src', "'self' data: blob:"),
    ('font-src', "'self' data:"),

    # XHR/fetch/WebSocket targets. Same-origin only: the browser never talks to
    # an LLM provider directly, the server does.
    ('connect-src', "'self'"),

    # These four need no template change and are the real value of shipping
    # this at all.
    ('object-src', "'none'"),      # no Flash/Java/embed vector
    ('base-uri', "'self'"),        # an injected <base> cannot re-root every relative URL
    ('form-action', "'self'"),     # a defaced form cannot POST credentials elsewhere
    ('frame-ancestors', "'none'"),  # clickjacking; also what X-Frame-Options does, but CSP wins where both are understood
)

POLICY_STRING = '; '.join(f'{name} {value}' for name, value in _POLICY)


class ContentSecurityPolicyMiddleware:
    """Attach the CSP header to HTML responses."""

    def __init__(self, get_response):
        self.get_response = get_response
        enforce = os.getenv('CSP_ENFORCE', '').lower() in ('1', 'true', 'yes', 'on')
        self.header = (
            'Content-Security-Policy' if enforce
            else 'Content-Security-Policy-Report-Only'
        )

    def __call__(self, request):
        response = self.get_response(request)

        # Only HTML executes a policy. Skipping everything else keeps the header
        # off image and JSON responses, where it is dead weight on every request.
        content_type = response.headers.get('Content-Type', '')
        if not content_type.startswith('text/html'):
            return response

        # Never overwrite a policy a view set for itself — a view that narrowed
        # its own policy knows something this middleware does not.
        if 'Content-Security-Policy' in response.headers:
            return response

        response.headers[self.header] = POLICY_STRING
        return response
