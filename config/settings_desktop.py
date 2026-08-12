"""Django settings for the offline desktop build.

Used by ``desktop_server.py``; the server sets
``DJANGO_SETTINGS_MODULE=config.settings_desktop``.

A separate module rather than more branches inside ``config/settings.py``:
that file is load-bearing for production (CLAUDE.md flags it as
confirm-before-editing), and the desktop's needs are the *opposite* of the
server's on several axes. Keeping the divergence in one small file makes it
reviewable, and leaves the production path untouched.

Plan: memory/desktop_offline_app_plan.md
"""
# The offline builds have no secret store and no operator to configure one, so
# they run on the shared development SECRET_KEY. config/settings.py refuses to
# boot on that key when DEBUG is False unless this is set, so it must be set
# BEFORE the import below — a star-import runs the whole module.
#
# Accepted knowingly, and the risk is bounded by where these serve: loopback only (127.0.0.1), never a network address.
# An attacker who could forge a session cookie here would already be on that
# network. It is still worth replacing with a per-installation generated key —
# see memory/self_hosting_manual_plan.md.
import os as _os  # noqa: E402
_os.environ.setdefault('ALLOW_DEV_SECRET_KEY', '1')

from config.settings import *  # noqa: F401,F403

# ── Cookies over loopback HTTP ──────────────────────────────────────────
#
# config/settings.py:404-408 turns on SESSION_COOKIE_SECURE and
# CSRF_COOKIE_SECURE whenever DEBUG is False — correct for a public HTTPS
# deployment, wrong here. The desktop app serves plain HTTP on 127.0.0.1, and
# a browser will not STORE a cookie marked Secure over http://. The CSRF token
# therefore never persists and every POST fails with
# "Forbidden (403) — CSRF verification failed", including the login form.
#
# Turning these off does not weaken anything meaningful: the server binds to
# loopback only, so the traffic never leaves the machine and there is no
# network path for an eavesdropper to be on.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Same reasoning — there is no TLS-terminating proxy in front of the desktop
# app, so trusting a forwarded-proto header would let any local process claim
# the connection was HTTPS.
SECURE_PROXY_SSL_HEADER = None
SECURE_SSL_REDIRECT = False

# ── Host / origin ───────────────────────────────────────────────────────
#
# The port is assigned by the OS at startup, so the origin cannot be enumerated
# ahead of time. Both entries are loopback-only, which is the actual security
# boundary here.
ALLOWED_HOSTS = ['127.0.0.1', 'localhost']
CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1', 'http://localhost']

# ── Offline invariants ──────────────────────────────────────────────────
#
# The desktop build ships the ONNX encoder, never torch. Pinned in settings
# rather than left to an env var so a launcher that forgets to export it does
# not silently fall back to a dependency that is not installed.
EMBEDDING_BACKEND = 'onnx'

DEBUG = False

# ── Per-installation data location ──────────────────────────────────────
#
# config/settings.py:198 hardcodes MEDIA_ROOT = BASE_DIR / 'media' and never
# consults the environment, so exporting MEDIA_ROOT has no effect on the
# server build — a real trap, because it fails silently: the pack importer
# writes figures into the source tree instead of the device's data directory
# and nothing complains. The desktop build keeps media beside its database in
# the platform application-data directory (see desktop_server.data_dir).
import os as _os  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_media = _os.environ.get('MEDIA_ROOT')
if _media:
    MEDIA_ROOT = _Path(_media)

# STATIC_ROOT must be writable, and in a frozen build BASE_DIR is inside the
# application bundle — read-only on macOS, and under Program Files on Windows.
# collectstatic runs on every launch (desktop_server._first_run_setup), so
# leaving it at BASE_DIR/staticfiles makes a packaged app fail at startup with
# a permission error, while working perfectly from source. Put it beside the
# database instead.
import sys as _sys  # noqa: E402

if getattr(_sys, 'frozen', False) or _os.environ.get('AI_TUTOR_STATIC_ROOT'):
    _static = _os.environ.get('AI_TUTOR_STATIC_ROOT')
    if _static:
        STATIC_ROOT = _Path(_static)
    elif _media:
        STATIC_ROOT = _Path(_media).parent / 'staticfiles'
    else:
        STATIC_ROOT = _Path.home() / '.ai-tutor' / 'staticfiles'
    _Path(STATIC_ROOT).mkdir(parents=True, exist_ok=True)
