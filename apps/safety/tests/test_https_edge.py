"""Secure-cookie hardening follows the edge, not the deployment.

Azure terminates TLS at the App Gateway and must keep hardening exactly as
before. The AWS deployment runs HTTP-only on the ALB's own hostname until it
has a domain, and browsers refuse to send Secure cookies over plain HTTP — so
leaving them on there silently bounces every login back to the login page.

These read settings out of a subprocess rather than asserting on the imported
module, because the flag is evaluated once at import time and override_settings
cannot re-run that.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parents[3]

_PROBE = (
    "import json;from django.conf import settings;"
    "print(json.dumps({"
    "'https_edge': settings.HTTPS_EDGE,"
    "'session': settings.SESSION_COOKIE_SECURE,"
    "'csrf': settings.CSRF_COOKIE_SECURE,"
    "'proxy_header': settings.SECURE_PROXY_SSL_HEADER,"
    "}))"
)


def _settings_under(env: dict) -> dict:
    environ = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DEBUG": "False",
    }
    environ.pop("HTTPS_EDGE", None)
    environ.update(env)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(BASE_DIR), env=environ, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"probe failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


class HttpsEdgeTests(SimpleTestCase):

    def test_default_keeps_cookies_secure_so_azure_is_unaffected(self):
        """Azure sets nothing, so the default must stay hardened."""
        s = _settings_under({})

        self.assertTrue(s["https_edge"])
        self.assertTrue(s["session"])
        self.assertTrue(s["csrf"])

    def test_https_edge_false_relaxes_cookies_for_the_http_only_alb(self):
        s = _settings_under({"HTTPS_EDGE": "false"})

        self.assertFalse(s["https_edge"])
        self.assertFalse(s["session"])
        self.assertFalse(s["csrf"])

    def test_falsey_spellings_are_all_accepted(self):
        for value in ("0", "false", "False", "no", "off", "OFF"):
            with self.subTest(value=value):
                self.assertFalse(_settings_under({"HTTPS_EDGE": value})["session"])

    def test_explicit_true_hardens(self):
        for value in ("true", "1", "yes"):
            with self.subTest(value=value):
                self.assertTrue(_settings_under({"HTTPS_EDGE": value})["session"])

    def test_the_proxy_ssl_header_is_set_either_way(self):
        """Both App Gateway and the ALB overwrite X-Forwarded-Proto, so
        trusting it is safe regardless of whether TLS is terminated yet."""
        for env in ({}, {"HTTPS_EDGE": "false"}):
            with self.subTest(env=env or "default"):
                self.assertEqual(
                    _settings_under(env)["proxy_header"],
                    ["HTTP_X_FORWARDED_PROTO", "https"],
                )
