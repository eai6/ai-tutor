"""The PUBLIC download page for the desktop app, served from the cloud.

Deliberately separate from ``apps/desktop/views.py``. That module runs on the
DEVICE, is bound to loopback, and is unauthenticated because nothing else can
reach it. This module runs on the public internet. Keeping them in one file
would put an "unauthenticated on purpose" comment next to a view that faces the
world, which is exactly the confusion that gets a route opened by mistake.

Why redirect instead of proxy: the installers are ~280 MB each. Streaming them
through Django would occupy a gunicorn worker for the length of a download on a
school connection — a handful of downloads would starve the tutor. S3 serves
the bytes; we only serve the link.

Public by design. No ``@login_required`` — the whole point is that someone who
has never logged in can install the app.
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import cache_control
from django.shortcuts import redirect

logger = logging.getLogger(__name__)

# The files the page offers. Anything not in here 404s rather than becoming an
# open redirect into the bucket.
INSTALLERS = {
    'macos': 'AI-Tutor-macos-arm64.dmg',
    'windows': 'AI-Tutor-windows-x64.zip',
}

_PREFIX = 'public/desktop/latest'
_SERVER_PREFIX = 'public/server/latest'


def server_artefacts() -> dict[str, str]:
    """Downloadable server artefacts, keyed by a name safe to put in a URL.

    Empty until SERVER_WHEEL_VERSION is set, because the filename must carry
    the real version: pip parses the version out of the wheel's name, so a
    stable alias like ai_tutor-latest-py3-none-any.whl is not installable.
    """
    version = getattr(settings, 'SERVER_WHEEL_VERSION', '')
    if not version:
        return {}
    return {
        'wheel': f'ai_tutor-{version}-py3-none-any.whl',
        'sdist': f'ai_tutor-{version}.tar.gz',
    }


def _bucket_url(filename: str, prefix: str = _PREFIX) -> str:
    bucket = getattr(settings, 'AWS_DOWNLOADS_BUCKET', '')
    region = getattr(settings, 'AWS_MEDIA_REGION', 'us-east-1')
    return f'https://{bucket}.s3.{region}.amazonaws.com/{prefix}/{filename}'


# GET and HEAD: require_GET rejects HEAD with 405, and link previewers,
# download managers and uptime checks all probe with HEAD first.
@require_http_methods(["GET", "HEAD"])
@cache_control(max_age=300, public=True)
def download_page(request):
    """The page itself — small, cacheable, no auth."""
    configured = bool(getattr(settings, 'AWS_DOWNLOADS_BUCKET', ''))
    artefacts = server_artefacts() if configured else {}
    wheel = artefacts.get('wheel', '')
    return render(request, 'downloads/index.html', {
        'configured': configured,
        'version': getattr(settings, 'DESKTOP_APP_VERSION', ''),
        # Empty until a wheel has been published, in which case the page offers
        # Docker and the release list rather than a link that 404s.
        'server_wheel': wheel,
        'server_wheel_url': _bucket_url(wheel, _SERVER_PREFIX) if wheel else '',
    })


@require_http_methods(["GET", "HEAD"])
def download_installer(request, platform: str):
    """302 to the installer in S3.

    Keyed on a platform name from INSTALLERS rather than taking a filename from
    the URL: a user-supplied path here would let anyone redirect through our
    domain to an arbitrary key in the bucket.
    """
    filename = INSTALLERS.get(platform)
    if not filename or not getattr(settings, 'AWS_DOWNLOADS_BUCKET', ''):
        raise Http404('unknown platform')
    return redirect(_bucket_url(filename))


@require_http_methods(["GET", "HEAD"])
def download_server(request, artefact: str):
    """302 to a server artefact (the wheel or the sdist) in S3.

    Allowlisted for the same reason as the desktop installers: taking a
    filename from the URL would let anyone redirect through this domain to an
    arbitrary key in the bucket.
    """
    filename = server_artefacts().get(artefact)
    if not filename or not getattr(settings, 'AWS_DOWNLOADS_BUCKET', ''):
        raise Http404('unknown artefact')
    return redirect(_bucket_url(filename, _SERVER_PREFIX))


# ── The self-hosting manual ────────────────────────────────────────────────

#: Rendered once per process. The document ships inside the wheel and cannot
#: change at runtime, so re-parsing 683 lines of Markdown per request would buy
#: nothing.
_MANUAL_HTML: str | None = None


def _manual_path() -> Path | None:
    """Where the manual lives, in either layout, or None.

    Installed from a wheel it sits inside the package (force-included at build
    time). Running from a checkout — which is how the Docker image runs, since
    the Dockerfile copies the repo — it is still at the repo root.

    Anchored on PACKAGE_DIR's parent rather than BASE_DIR: BASE_DIR follows
    AI_TUTOR_DATA_DIR, which points at writable state and holds no documents.
    """
    from django.conf import settings

    package = Path(settings.PACKAGE_DIR)
    for candidate in (package / 'docs' / 'self-hosting.md',
                      package.parent / 'docs' / 'self-hosting.md'):
        if candidate.exists():
            return candidate
    return None


def render_manual() -> str:
    """The manual as HTML, or '' if this build did not ship it.

    Rendered server-side rather than by a browser script: the page must work
    for someone reading it on a locked-down ministry laptop, and the content
    is ours — there is no user input here to sanitise.
    """
    global _MANUAL_HTML
    if _MANUAL_HTML is not None:
        return _MANUAL_HTML

    path = _manual_path()
    if path is None:
        logger.warning('[downloads] self-hosting manual not found; '
                       '/self-hosting/ will render without it')
        _MANUAL_HTML = ''
        return _MANUAL_HTML

    from markdown_it import MarkdownIt

    # commonmark + tables, NOT 'gfm-like': that preset turns on linkify, which
    # needs linkify-it-py and raises at import if it is absent.
    md = MarkdownIt('commonmark').enable(['table', 'strikethrough'])
    tokens = md.parse(path.read_text(encoding='utf-8'))
    _add_heading_ids(tokens)
    _MANUAL_HTML = md.renderer.render(tokens, md.options, {})
    return _MANUAL_HTML


def _slug(text: str) -> str:
    """GitHub's heading-anchor slug.

    Matched to GitHub deliberately: the manual is written and reviewed on
    GitHub, and its own cross-references ("see section 7") are GitHub-style
    anchors. Any other scheme would render the document with its internal
    links quietly broken.
    """
    import re
    text = text.strip().lower()
    text = re.sub(r'[^\w\s-]', '', text)     # drops '.', '—', '/', etc.
    return re.sub(r'\s', '-', text)


def _add_heading_ids(tokens) -> None:
    """Give every heading an id, which markdown-it does not do on its own.

    Without this the page renders perfectly and every anchor on it is dead —
    including the buttons at the top of the page.
    """
    seen: dict[str, int] = {}
    for i, tok in enumerate(tokens):
        if tok.type != 'heading_open':
            continue
        inline = tokens[i + 1]
        base = _slug(inline.content)
        if not base:
            continue
        seen[base] = seen.get(base, 0) + 1
        tok.attrSet('id', base if seen[base] == 1 else f'{base}-{seen[base] - 1}')


@require_http_methods(["GET", "HEAD"])
def self_hosting(request):
    """The whole self-hosting manual, plus the artefacts it refers to.

    Separate from /download/ because the audiences do not overlap: that page is
    for a teacher installing on one classroom machine, this is for whoever runs
    a ministry's servers. Mixing them made both longer and neither clearer.
    """
    configured = bool(getattr(settings, 'AWS_DOWNLOADS_BUCKET', ''))
    artefacts = server_artefacts() if configured else {}
    wheel = artefacts.get('wheel', '')
    return render(request, 'downloads/self_hosting.html', {
        'manual_html': render_manual(),
        'server_wheel': wheel,
        'server_wheel_url': _bucket_url(wheel, _SERVER_PREFIX) if wheel else '',
        'sdist': artefacts.get('sdist', ''),
    })
