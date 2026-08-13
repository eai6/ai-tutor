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

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import cache_control
from django.shortcuts import redirect

# The files the page offers. Anything not in here 404s rather than becoming an
# open redirect into the bucket.
INSTALLERS = {
    'macos': 'AI-Tutor-macos-arm64.dmg',
    'windows': 'AI-Tutor-windows-x64.zip',
}

_PREFIX = 'public/desktop/latest'


def _bucket_url(filename: str) -> str:
    bucket = getattr(settings, 'AWS_DOWNLOADS_BUCKET', '')
    region = getattr(settings, 'AWS_MEDIA_REGION', 'us-east-1')
    return f'https://{bucket}.s3.{region}.amazonaws.com/{_PREFIX}/{filename}'


# GET and HEAD: require_GET rejects HEAD with 405, and link previewers,
# download managers and uptime checks all probe with HEAD first.
@require_http_methods(["GET", "HEAD"])
@cache_control(max_age=300, public=True)
def download_page(request):
    """The page itself — small, cacheable, no auth."""
    configured = bool(getattr(settings, 'AWS_DOWNLOADS_BUCKET', ''))
    return render(request, 'downloads/index.html', {
        'configured': configured,
        'version': getattr(settings, 'DESKTOP_APP_VERSION', ''),
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
