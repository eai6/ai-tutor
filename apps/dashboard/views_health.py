"""Health check endpoint.

Used by Azure Container Apps probes + by humans confirming which
version is live. The ``version`` field is read from the repo-root
``VERSION`` file at module import time — for the released build,
this is the value at the tagged commit (e.g. "0.1.0" for v0.1.0).

Rollback verification recipe:
    curl https://<env-url>/health/ | jq .version
"""
from __future__ import annotations

import os

from django.conf import settings
from django.db import connection
from django.http import JsonResponse


def _read_version() -> str:
    """Read the VERSION file at the repo root, falling back to 'unknown'."""
    try:
        path = os.path.join(settings.BASE_DIR, "VERSION")
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or "unknown"
    except (OSError, AttributeError):
        return "unknown"


# Read once at import; ContainerApp restarts pick up the new value.
_VERSION = _read_version()


def health_check(request):
    payload = {
        "status": "ok",
        "version": _VERSION,
        # Surface the active language for the request. After M4 ships
        # the LocaleResolverMiddleware this reflects per-request
        # resolution; today it's the global LANGUAGE_CODE.
        "language": settings.LANGUAGE_CODE,
    }
    try:
        connection.ensure_connection()
        return JsonResponse(payload)
    except Exception as e:
        payload.update(status="error", detail=str(e))
        return JsonResponse(payload, status=503)
