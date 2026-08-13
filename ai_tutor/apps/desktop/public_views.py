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


def _artefact_context() -> dict:
    configured = bool(getattr(settings, 'AWS_DOWNLOADS_BUCKET', ''))
    artefacts = server_artefacts() if configured else {}
    wheel = artefacts.get('wheel', '')
    return {
        'server_wheel': wheel,
        'server_wheel_url': _bucket_url(wheel, _SERVER_PREFIX) if wheel else '',
        'sdist': artefacts.get('sdist', ''),
    }


@require_http_methods(["GET", "HEAD"])
def self_hosting(request):
    """How to deploy, and nothing else.

    Separate from /download/ because the audiences do not overlap: that page is
    for a teacher installing on one classroom machine, this is for whoever runs
    a ministry's servers.

    Deliberately only the commands. An earlier version rendered the whole
    683-line manual here — AWS cost tables, three architecture diagrams, a
    Pulumi walkthrough — and buried the handful of commands someone actually
    needs under everything they do not. docs/self-hosting.md remains the long
    form for anyone with the repository.
    """
    return render(request, 'downloads/self_hosting.html', _artefact_context())
