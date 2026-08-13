"""M1 bootstrap tests for the multi-locale platform foundation.

Covers the foundation milestone of the Mozambique localization plan
(memory/portuguese_mozambique_pilot_plan.md). No actual translations
yet — these tests just verify the plumbing is wired correctly so
M2/M3/M4 can build on top.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase


class I18nSettingsTest(TestCase):
    def test_languages_includes_both_locales(self):
        codes = dict(settings.LANGUAGES)
        self.assertIn("en-us", codes)
        self.assertIn("pt-mz", codes)

    def test_locale_paths_directory_exists(self):
        self.assertGreaterEqual(len(settings.LOCALE_PATHS), 1)
        locale_dir = Path(settings.LOCALE_PATHS[0])
        self.assertTrue(
            locale_dir.exists(),
            f"LOCALE_PATHS[0] {locale_dir} does not exist",
        )

    def test_pt_mz_skeleton_present(self):
        locale_dir = Path(settings.LOCALE_PATHS[0])
        po = locale_dir / "pt_MZ" / "LC_MESSAGES" / "django.po"
        po_js = locale_dir / "pt_MZ" / "LC_MESSAGES" / "djangojs.po"
        self.assertTrue(po.exists(), f"missing skeleton: {po}")
        self.assertTrue(po_js.exists(), f"missing skeleton: {po_js}")

    def test_locale_middleware_in_middleware_list(self):
        self.assertIn(
            "django.middleware.locale.LocaleMiddleware", settings.MIDDLEWARE
        )

    def test_locale_middleware_after_session_middleware(self):
        mw = list(settings.MIDDLEWARE)
        session_idx = mw.index(
            "django.contrib.sessions.middleware.SessionMiddleware"
        )
        locale_idx = mw.index("django.middleware.locale.LocaleMiddleware")
        self.assertGreater(
            locale_idx,
            session_idx,
            "LocaleMiddleware must come after SessionMiddleware",
        )

    def test_locale_middleware_before_common_middleware(self):
        mw = list(settings.MIDDLEWARE)
        locale_idx = mw.index("django.middleware.locale.LocaleMiddleware")
        common_idx = mw.index("django.middleware.common.CommonMiddleware")
        self.assertLess(
            locale_idx,
            common_idx,
            "LocaleMiddleware must come before CommonMiddleware",
        )


class JavaScriptCatalogTest(TestCase):
    def test_jsi18n_endpoint_returns_javascript(self):
        client = Client()
        response = client.get("/jsi18n/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"].lower())


class HealthCheckLanguageTest(TestCase):
    def test_health_check_surfaces_language(self):
        client = Client()
        response = client.get("/health/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("language", data)
        self.assertEqual(data["language"], settings.LANGUAGE_CODE)
