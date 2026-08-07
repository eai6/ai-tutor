"""S3 media storage plus a range-capable, school-network-friendly server.

Why this exists
---------------
School networks allowlist ``www.seselai.sc`` only — they block ``*.amazonaws.com``
the same way they block YouTube — and the bucket is private with no anonymous
access. So media must be SERVED from our own domain rather than handed out as
a bucket URL or a presigned link.

This module:
  * ``S3MediaStorage`` — django-storages S3 backend whose ``.url()`` returns
    ``/media/<name>`` (our domain) instead of an S3 URL.
  * ``serve_media`` — streams ``/media/<path>`` from the private bucket with
    HTTP Range support so videos can be scrubbed. Falls back to ``MEDIA_ROOT``
    for anything not in the bucket, which keeps local development working.
"""
from __future__ import annotations

import mimetypes
import os
import re

from django.conf import settings
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    StreamingHttpResponse,
)

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK_BYTES = 8 * 1024 * 1024


def _media_url_prefix() -> str:
    u = settings.MEDIA_URL or "media/"
    if not u.startswith("/"):
        u = "/" + u
    if not u.endswith("/"):
        u = u + "/"
    return u


# django-storages is only installed where S3 media is used. Guard the import so
# this module stays importable in a bare dev environment; the class below is
# only instantiated when settings.USE_S3_MEDIA is true.
try:
    from storages.backends.s3 import S3Storage as _S3Storage
except Exception:  # pragma: no cover - package absent in local dev
    _S3Storage = object


class S3MediaStorage(_S3Storage):
    """Media stored in S3; URLs point back at our own domain so they work on
    school networks and stay behind the WAF (we serve via serve_media, never a
    bucket URL or a presigned link)."""

    def __init__(self, **kwargs):
        super().__init__(
            bucket_name=settings.AWS_MEDIA_BUCKET,
            region_name=settings.AWS_MEDIA_REGION,
            querystring_auth=False,
            file_overwrite=False,
            **kwargs,
        )

    def url(self, name, *a, **k):
        return _media_url_prefix() + str(name).lstrip("/")


def _s3_client():
    """boto3 S3 client. Credentials come from the ECS task role."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_MEDIA_REGION)


def _filesystem_fallback(path: str):
    """Serve from MEDIA_ROOT for anything not in the bucket."""
    root = os.path.abspath(settings.MEDIA_ROOT)
    full = os.path.abspath(os.path.join(root, path))
    # Compare against root + separator, not a bare prefix match: with a bare
    # ``startswith(root)`` a sibling directory that merely shares the prefix
    # (``/app/media-old`` against a root of ``/app/media``) escapes the check.
    if not (full == root or full.startswith(root + os.sep)):
        raise Http404(path)
    if not os.path.isfile(full):
        raise Http404(path)
    return FileResponse(open(full, "rb"))


def serve_media(request, path):
    """Stream `/media/<path>` from S3 (range-aware), fall back to MEDIA_ROOT."""
    path = path.lstrip("/")
    bucket = settings.AWS_MEDIA_BUCKET
    client = _s3_client()
    try:
        head = client.head_object(Bucket=bucket, Key=path)
    except Exception:
        # Not in the bucket (or S3 unreachable) → try the local filesystem.
        return _filesystem_fallback(path)

    size = head["ContentLength"]
    # Prefer the extension guess when S3 has no stored type OR a generic one.
    # Bulk-migrated files land as ``application/octet-stream``, which would make
    # browsers download PDFs instead of rendering them inline.
    stored_ct = head.get("ContentType")
    guessed_ct = mimetypes.guess_type(path)[0]
    if guessed_ct and (not stored_ct or stored_ct == "application/octet-stream"):
        content_type = guessed_ct
    else:
        content_type = stored_ct or "application/octet-stream"

    range_header = request.headers.get("Range", "")
    start, end = 0, size - 1
    is_range = False
    if range_header:
        m = _RANGE_RE.match(range_header)
        if not m:
            return HttpResponseBadRequest("Invalid Range")
        g0, g1 = m.group(1), m.group(2)
        if g0 == "" and g1 == "":
            return HttpResponseBadRequest("Invalid Range")
        if g0 == "":  # suffix: last N bytes
            length = int(g1)
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(g0)
            end = int(g1) if g1 else size - 1
        end = min(end, size - 1)
        if start > end:
            resp = HttpResponse(status=416)
            resp["Content-Range"] = f"bytes */{size}"
            return resp
        is_range = True

    length = end - start + 1
    get_kwargs = {"Bucket": bucket, "Key": path}
    if is_range:
        get_kwargs["Range"] = f"bytes={start}-{end}"
    body = client.get_object(**get_kwargs)["Body"]

    resp = StreamingHttpResponse(
        body.iter_chunks(_CHUNK_BYTES),
        status=206 if is_range else 200,
        content_type=content_type,
    )
    resp["Accept-Ranges"] = "bytes"
    resp["Content-Length"] = str(length)
    if is_range:
        resp["Content-Range"] = f"bytes {start}-{end}/{size}"
    return resp
