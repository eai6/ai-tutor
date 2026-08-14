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

The 2026-08-13 assessment raised this as F-02: a policy that is deployed but
switched off, on an application with no other XSS containment. Two switches were
added in response, and they are deliberately separate, because they fail in
different ways:

    CSP_ENFORCE=1     send Content-Security-Policy instead of the -Report-Only
                      variant. Do this only after watching a real deployment
                      with the browser console open and seeing no violations.

    CSP_STRICT_SCRIPTS=1
                      swap 'unsafe-inline' on script-src for a per-request
                      nonce. Every inline <script> block in the templates
                      already carries nonce="{{ request.csp_nonce }}", so those
                      keep running — but the 206 inline EVENT HANDLERS do not,
                      because a nonce cannot be attached to an onclick
                      attribute. That is the refactor this flag measures.

    CSP_REPORT=1      add report-uri pointing at /csp-report/ (see
                      apps/safety/csp_report.py).

Turn CSP_STRICT_SCRIPTS on with CSP_ENFORCE off, and the browser reports one
violation per surviving inline handler without blocking anything. That report
stream IS the remaining backlog, enumerated by the browser rather than by grep.
When it goes quiet, both flags can go on together.

The two are separate for a reason worth stating: CSP_ENFORCE on its own still
does exactly what it did before this change — the nonce attributes now in the
templates are inert while 'unsafe-inline' is in the policy, because a browser
only starts ignoring 'unsafe-inline' once a nonce appears in the same directive.
Nobody flipping the old switch gets a surprise.

On report collection: the original note here argued against a report-uri,
because collecting reports means an unauthenticated POST endpoint accepting
attacker-controlled JSON, which is new attack surface added to a build whose
point is to reduce it. That reasoning still stands, which is why CSP_REPORT
defaults to off and the endpoint that serves it is written defensively — capped
body, rate limited per IP, no database write. It exists so that the violation
census above can be collected from real users rather than from whoever happens
to have a console open.
"""
import os
import secrets

# Kept as a tuple of (directive, value) rather than a dict so the order in the
# emitted header is stable and diffable between runs.
_POLICY = (
    # Nothing loads from anywhere but this origin unless a directive below
    # widens it. The templates reference no CDN, so this holds today.
    ('default-src', "'self'"),

    # script-src is filled in per-request — see _script_src. style-src is not
    # tightenable at all yet: 2226 inline style="" attributes, and unlike
    # scripts there is no nonce that can be attached to an attribute.
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

REPORT_PATH = '/csp-report/'


def _env_flag(name: str) -> bool:
    return os.getenv(name, '').lower() in ('1', 'true', 'yes', 'on')


class ContentSecurityPolicyMiddleware:
    """Attach the CSP header to HTML responses."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.enforce = _env_flag('CSP_ENFORCE')
        self.strict_scripts = _env_flag('CSP_STRICT_SCRIPTS')
        self.report = _env_flag('CSP_REPORT')
        self.header = (
            'Content-Security-Policy' if self.enforce
            else 'Content-Security-Policy-Report-Only'
        )

    def __call__(self, request):
        # Generated before the view runs, because the template reads it while
        # rendering and the header is not written until afterwards. One
        # token_urlsafe call per request; 128 bits, which is what the spec asks
        # for and what makes guessing it not a strategy.
        request.csp_nonce = secrets.token_urlsafe(16)

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

        response.headers[self.header] = self._policy_for(request)
        return response

    def _script_src(self, request) -> str:
        """'unsafe-inline' or a nonce — never both, and not by accident.

        A browser that understands nonces ignores 'unsafe-inline' the moment one
        appears in the same directive. Emitting both would therefore not be a
        belt-and-braces policy; it would be the strict policy, arrived at
        without anyone deciding to.
        """
        if self.strict_scripts:
            return f"'self' 'nonce-{request.csp_nonce}'"
        return "'self' 'unsafe-inline'"

    def _policy_for(self, request) -> str:
        directives = [('script-src', self._script_src(request))]
        directives.extend(_POLICY)
        if self.report:
            # report-uri is deprecated but is still the only one Safari and
            # older Firefox honour; report-to needs a Reporting-Endpoints header
            # to go with it. Both, so every browser in a Seychelles classroom
            # reports something.
            directives.append(('report-uri', REPORT_PATH))
        return '; '.join(f'{name} {value}' for name, value in directives)
