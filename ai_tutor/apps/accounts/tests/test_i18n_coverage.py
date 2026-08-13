"""Coverage tests for the M2 i18n string wrapping.

Verifies that:
  - The wrapped student-facing templates render at en-us AND pt-mz
    without TemplateSyntaxError (with empty .po → identity translation,
    output should be equivalent to today's English).
  - The audit script finds 0 candidate unwrapped strings in scope
    (ignoring known false positives like example email formats).
  - The .po file exists and contains at least the expected msgid count.

Part of M2d of memory/portuguese_mozambique_pilot_plan.md. Pairs with
``scripts/list_unwrapped_strings.py``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.contrib.auth.models import AnonymousUser
from django.utils import translation


# WhiteNoise's CompressedManifestStaticFilesStorage (prod) raises when
# {% static %} lookups happen for files that aren't in the manifest
# (e.g. when manage.py collectstatic hasn't been run in test mode).
# Swap to the vanilla backend for these tests so we exercise the
# wrapping itself, not the static-files infrastructure.
_TEST_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


# Templates wrapped in M2a + M2b. Each entry includes the context
# kwargs needed to render the template at all (avoids False-positive
# render failures on missing variables).
_WRAPPED_TEMPLATES: list[tuple[str, dict]] = [
    ('base.html', {}),
    ('accounts/login.html', {}),
    ('accounts/student_login.html', {}),
    (
        'accounts/student_register.html',
        {'school_choices': [], 'grade_choices': []},
    ),
    ('accounts/landing.html', {}),
    (
        'accounts/settings.html',
        {
            'user_obj': None, 'membership': None,
            'student_profile': None, 'is_staff_user': False,
            'password_form': None, 'school_choices': [],
            'grade_choices': [],
        },
    ),
    (
        'tutoring/chat_tutor.html',
        {
            'lesson': type('L', (), {'title': 'Test', 'id': 1})(),
            'is_staff_user': False, 'allow_duration_picker': False,
            'is_math_lesson': False,
        },
    ),
    (
        'tutoring/catalog.html',
        {
            'subjects': [], 'is_staff_viewer': False,
            'is_staff_user': False, 'weekly_assignments': [],
            'active_sessions': [],
        },
    ),
]


@override_settings(STORAGES=_TEST_STORAGES)
class WrappedTemplatesRenderAtBothLocalesTest(TestCase):
    """Every M2-wrapped template must render at both en-us and pt-mz
    without raising. The empty .po means the rendered text reads as
    English at both locales — coverage gaps fall through, but they
    don't break the page."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()

    def _render(self, code: str, template: str, ctx: dict):
        translation.activate(code)
        try:
            req = self.factory.get('/')
            req.user = AnonymousUser()
            ctx = {**ctx, 'request': req, 'LANGUAGE_CODE': code}
            render_to_string(template, ctx, request=req)
        finally:
            translation.deactivate()

    def test_all_wrapped_templates_render_at_en_us(self):
        for tpl, ctx in _WRAPPED_TEMPLATES:
            with self.subTest(template=tpl):
                self._render('en-us', tpl, ctx)

    def test_all_wrapped_templates_render_at_pt_mz(self):
        for tpl, ctx in _WRAPPED_TEMPLATES:
            with self.subTest(template=tpl):
                self._render('pt-mz', tpl, ctx)


class PoFileTest(TestCase):
    """The pt_MZ catalog must exist and carry at least the strings
    M2 wrapped (~135). Threshold is conservative — drops can indicate
    a regression in the wrap or in the makemessages call."""

    PO_PATH = Path(settings.BASE_DIR) / 'locale' / 'pt_MZ' / 'LC_MESSAGES' / 'django.po'

    def test_po_file_exists(self):
        self.assertTrue(
            self.PO_PATH.exists(),
            f"Expected populated .po at {self.PO_PATH}",
        )

    def test_po_has_expected_msgid_count(self):
        """≥ 100 msgids — sanity check that makemessages was actually
        run after the wrapping. The exact count fluctuates as new
        strings get wrapped; the floor catches regressions where
        someone deletes wraps en masse."""
        content = self.PO_PATH.read_text()
        # Subtract 1 for the header msgid "" line.
        msgid_count = content.count('\nmsgid "') - 0
        self.assertGreaterEqual(
            msgid_count, 100,
            f"Expected ≥ 100 msgids in {self.PO_PATH}, got {msgid_count}. "
            "Did `python manage.py makemessages -l pt_MZ` run after wrapping?",
        )


class AuditScriptTest(TestCase):
    """Run scripts/list_unwrapped_strings.py and assert it returns 0
    candidates outside the known false-positive allowlist (example
    email placeholders like your.email@example.com).
    """

    SCRIPT_PATH = Path(settings.BASE_DIR) / 'scripts' / 'list_unwrapped_strings.py'

    # Lines containing these substrings are intentionally NOT wrapped —
    # they're example values shown TO the user in a placeholder, not
    # localizable UI strings.
    _ALLOWED_FALSE_POSITIVES = (
        'your.email@example.com',
        'you@example.com',
    )

    def test_audit_finds_no_unwrapped_strings_outside_allowlist(self):
        result = subprocess.run(
            ['python', str(self.SCRIPT_PATH)],
            cwd=settings.BASE_DIR,
            capture_output=True, text=True, check=True,
        )
        unexpected = []
        for line in result.stdout.splitlines():
            if 'L' not in line or ':' not in line:
                continue  # header or summary line
            if any(token in line for token in self._ALLOWED_FALSE_POSITIVES):
                continue
            if '===' in line:
                continue
            if line.strip().startswith('templates/'):
                continue  # file-name header line
            if 'candidate' in line.lower():
                continue
            unexpected.append(line.strip())
        self.assertEqual(
            unexpected, [],
            "Audit found unwrapped student-facing strings outside the "
            "allowlist:\n" + "\n".join(unexpected),
        )
