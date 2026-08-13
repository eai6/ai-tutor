"""Azure Blob media storage + a range-capable, school-network-friendly server.

Why this exists
---------------
Media is migrating off the SMB Azure File Share onto Azure Blob Storage, but
school networks only allow ``www.seselai.sc`` (they block ``*.blob.core...``
just like YouTube), and prod is now fully private behind the App Gateway WAF.
So media must be SERVED from our own domain, not from a blob URL.

This module:
  * ``AzureMediaStorage`` — django-storages Azure backend whose ``.url()``
    returns ``/media/<name>`` (our domain) instead of a public blob URL.
  * ``serve_media`` — streams ``/media/<path>`` from the PRIVATE blob container
    (key-authenticated, no anonymous access), with HTTP Range support so videos
    can be scrubbed. Falls back to the File Share mount for any file not yet
    migrated, so nothing breaks during the transition.

Everything is gated by ``settings.USE_BLOB_MEDIA`` (true only when the blob env
vars are present), and the Azure SDK is imported lazily so local/dev without the
package is unaffected.
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


def _media_url_prefix() -> str:
    u = settings.MEDIA_URL or "media/"
    if not u.startswith("/"):
        u = "/" + u
    if not u.endswith("/"):
        u = u + "/"
    return u


# django-storages is only installed where blob media is used (prod). Guard the
# import so importing this module never crashes a local/dev env without it; the
# class below is only ever instantiated when settings.USE_BLOB_MEDIA is true.
try:
    from storages.backends.azure_storage import AzureStorage as _AzureStorage
except Exception:  # pragma: no cover - package absent in local dev
    _AzureStorage = object


class AzureMediaStorage(_AzureStorage):
    """Media stored in Azure Blob; URLs point back at our own domain so they
    work on school networks and stay behind the WAF (we serve via serve_media,
    never a public blob URL)."""

    def __init__(self, **kwargs):
        super().__init__(
            account_name=settings.AZURE_BLOB_MEDIA_ACCOUNT,
            account_key=settings.AZURE_BLOB_MEDIA_KEY,
            azure_container=settings.AZURE_BLOB_MEDIA_CONTAINER,
            expiration_secs=None,
            **kwargs,
        )

    def url(self, name, *a, **k):
        return _media_url_prefix() + str(name).lstrip("/")


def _blob_client(path: str):
    """BlobClient for `media/<path>` — used for efficient ranged reads."""
    from azure.storage.blob import BlobServiceClient

    svc = BlobServiceClient(
        f"https://{settings.AZURE_BLOB_MEDIA_ACCOUNT}.blob.core.windows.net",
        credential=settings.AZURE_BLOB_MEDIA_KEY,
    )
    return svc.get_blob_client(
        container=settings.AZURE_BLOB_MEDIA_CONTAINER, blob=path
    )


def _filesystem_fallback(path: str):
    """Serve from the File Share mount (MEDIA_ROOT) for un-migrated files."""
    full = os.path.join(settings.MEDIA_ROOT, path)
    # Guard against path traversal.
    root = os.path.abspath(settings.MEDIA_ROOT)
    if not os.path.abspath(full).startswith(root) or not os.path.isfile(full):
        raise Http404(path)
    # Django's FileResponse handles Range + content-type itself.
    return FileResponse(open(full, "rb"))


def serve_media(request, path):
    """Stream `/media/<path>` from blob (range-aware), fall back to File Share."""
    path = path.lstrip("/")
    try:
        client = _blob_client(path)
        props = client.get_blob_properties()
    except Exception:
        # Not in blob (or blob unreachable) → try the File Share mount.
        return _filesystem_fallback(path)

    size = props.size
    # Prefer the extension-based guess when the blob has no stored content-type
    # OR a generic one. Bulk-migrated files (azcopy) land as
    # ``application/octet-stream``, which would make browsers download PDFs and
    # mis-handle media instead of rendering them inline.
    stored_ct = props.content_settings.content_type
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
    downloader = client.download_blob(offset=start, length=length)
    resp = StreamingHttpResponse(
        downloader.chunks(),
        status=206 if is_range else 200,
        content_type=content_type,
    )
    resp["Accept-Ranges"] = "bytes"
    resp["Content-Length"] = str(length)
    if is_range:
        resp["Content-Range"] = f"bytes {start}-{end}/{size}"
    return resp
