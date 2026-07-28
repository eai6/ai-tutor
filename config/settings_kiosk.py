"""Settings for the offline kiosk deployment on the Jetson.

Used by infra/systemd/ai-tutor.service. Everything comes from config.settings;
this module changes only what plain-HTTP serving on an isolated access point
requires.

Deliberately a separate module rather than an edit to config/settings.py, which
is load-bearing for the Azure production deployment (see CLAUDE.md). Nothing
here can affect that deployment — it is reached only by setting
DJANGO_SETTINGS_MODULE=config.settings_kiosk.
"""
from config.settings import *  # noqa: F401,F403

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
