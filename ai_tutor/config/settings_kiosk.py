"""Settings for the offline kiosk deployment on the Jetson.

Used by infra/systemd/ai-tutor.service. Everything comes from config.settings;
this module changes only what plain-HTTP serving on an isolated access point
requires.

Deliberately a separate module rather than an edit to config/settings.py, which
is load-bearing for the Azure production deployment (see CLAUDE.md). Nothing
here can affect that deployment — it is reached only by setting
DJANGO_SETTINGS_MODULE=ai_tutor.config.settings_kiosk.
"""
# The offline builds have no secret store and no operator to configure one, so
# they run on the shared development SECRET_KEY. config/settings.py refuses to
# boot on that key when DEBUG is False unless this is set, so it must be set
# BEFORE the import below — a star-import runs the whole module.
#
# Accepted knowingly, and the risk is bounded by where these serve: a single-purpose isolated WiFi access point with no internet path.
# An attacker who could forge a session cookie here would already be on that
# network. It is still worth replacing with a per-installation generated key —
# see memory/self_hosting_manual_plan.md.
import os as _os  # noqa: E402
_os.environ.setdefault('ALLOW_DEV_SECRET_KEY', '1')

from ai_tutor.config.settings import *  # noqa: F401,F403

# config/settings.py:402 turns these on whenever DEBUG is False, which is right
# for Azure (TLS terminated at the ingress) and fatal here. The hotspot has no
# TLS, and a browser never returns a Secure cookie over plain HTTP — so the
# login POST fails CSRF with 403, no session cookie is stored, and every page
# redirects to a login URL that does not exist. Measured exactly that way on
# 2026-07-27 before this module existed.
#
# The tradeoff is accepted knowingly: session and CSRF cookies travel in the
# clear on a single-purpose, isolated WiFi network with no internet path. That
# is a different risk profile from the public internet. If this ever serves over
# a shared or routable network, it needs real TLS instead.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# No reverse proxy in front of gunicorn here, so there is no X-Forwarded-Proto
# to trust. Leaving the production value set would let a client claim HTTPS by
# sending a header.
SECURE_PROXY_SSL_HEADER = None
SECURE_SSL_REDIRECT = False

# HSTS must NOT be sent from here. config/settings.py enables it whenever
# HTTPS_EDGE is true, which is its default — and this build serves plain HTTP.
#
# The damage is not theoretical and not recoverable: a browser that receives
# HSTS remembers it, and will then refuse to load http:// from this host for a
# year. There is no way to reach a student's laptop to undo that. Django's own
# check calls it "serious, irreversible" for exactly this reason.
#
# SECURE_SSL_REDIRECT is already forced False above for the same family of
# reason: redirecting to an https:// port nothing listens on serves nothing.
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False


# This IS a packaged offline build, so device-only settings (the school
# server address) are reachable from the in-app settings page. See
# config/settings.py::DESKTOP_BUILD.
DESKTOP_BUILD = True
