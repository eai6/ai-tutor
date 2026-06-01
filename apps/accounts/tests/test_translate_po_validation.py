"""Regression tests for the translate_po command's strict key validation.

Background: on 2026-06-01 the dashboard rendered nonsensical pt-mz
translations — "All Students" → "Sou Estudante" ("I'm a Student"),
"Total Students" → "Sou Estudante" too, "Courses" → "Horas:", and 232
other cross-wired pairs. Root cause: translate_po's assignment loop
iterated Claude's response dict and looked for a matching entry by
msgid, then broke on the first empty-msgstr match. When Claude's
response keys did not 1:1 match the requested msgids (truncation,
case-folding, paraphrasing, or partial responses), the loop happily
landed wrong msgstrs.

These tests pin the new contract:

  1. A response with extra/missing keys raises — the entire batch is
     dropped, NO msgstrs are written.
  2. A response whose keys match the requested msgids exactly assigns
     each msgstr by exact-string lookup (no fuzzy fallback, no break-on-
     first-match).

Run against the BROKEN code (the previous loop) and they fail —
confirming they would have caught the 2026-06-01 corruption.
"""
from io import StringIO
from unittest.mock import patch, MagicMock

from django.core.management import call_command
from django.test import TestCase
from django.conf import settings


class TranslatePoKeyValidationTest(TestCase):
    """Smoke-tests the strict key-set validation."""

    def setUp(self):
        # Build a throw-away .po so the test doesn't touch the real
        # locale catalogue. Tempdir + a 3-entry .po is enough.
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self._locale_root = Path(self._tmp.name) / "locale" / "xx_TEST" / "LC_MESSAGES"
        self._locale_root.mkdir(parents=True)
        self._po_path = self._locale_root / "django.po"
        self._po_path.write_text(
            'msgid ""\n'
            'msgstr ""\n'
            '"Content-Type: text/plain; charset=UTF-8\\n"\n'
            '\n'
            '#: templates/dashboard/home.html:1\n'
            'msgid "All Students"\n'
            'msgstr ""\n'
            '\n'
            '#: templates/dashboard/home.html:2\n'
            'msgid "Total Students"\n'
            'msgstr ""\n'
            '\n'
            '#: templates/dashboard/home.html:3\n'
            'msgid "Courses"\n'
            'msgstr ""\n'
        )
        # Patch settings.BASE_DIR so the command looks in our tmpdir.
        self._base_dir_patch = patch.object(
            settings, "BASE_DIR", Path(self._tmp.name),
        )
        self._base_dir_patch.start()

    def tearDown(self):
        self._base_dir_patch.stop()
        self._tmp.cleanup()

    def _read_msgstrs(self):
        """Return {msgid: msgstr} from the .po file on disk."""
        import re
        content = self._po_path.read_text()
        pairs = re.findall(r'msgid "([^"]+)"\nmsgstr "([^"]*)"', content)
        return {msgid: msgstr for msgid, msgstr in pairs}

    @patch("anthropic.Anthropic")
    def test_extra_or_missing_keys_drops_batch(self, m_anthropic):
        """Claude returns extra/missing/paraphrased keys → drop the
        whole batch. No partial writes. This is the case that would
        have prevented the 2026-06-01 corruption."""
        # Simulate the 2026-06-01 failure mode: Claude returns a single
        # paraphrased key ("Students") instead of the three requested
        # ones, and an unrelated bonus key ("I'm a Student"). The
        # previous loop would have assigned "Sou Estudante" to the
        # first empty match. The hardened loop must drop the batch
        # entirely.
        m_response = MagicMock()
        m_block = MagicMock()
        m_block.type = "text"
        m_block.text = (
            '{"Students": "Alunos", "I\'m a Student": "Sou Estudante"}'
        )
        m_response.content = [m_block]
        m_anthropic.return_value.messages.create.return_value = m_response

        # Run the command. It must NOT crash, but it must NOT write
        # any msgstrs from this mismatched response.
        out = StringIO()
        err = StringIO()
        call_command(
            "translate_po",
            "--locale", "xx_TEST",
            "--no-compile",
            stdout=out,
            stderr=err,
        )

        # All three msgstrs MUST still be empty — the batch was dropped.
        after = self._read_msgstrs()
        self.assertEqual(after.get("All Students"), "")
        self.assertEqual(after.get("Total Students"), "")
        self.assertEqual(after.get("Courses"), "")

        # And the stderr must surface the key-set mismatch loudly.
        self.assertIn("key-set mismatch", err.getvalue())

    @patch("anthropic.Anthropic")
    def test_exact_key_match_assigns_by_exact_lookup(self, m_anthropic):
        """Claude returns exactly the requested msgids as keys → each
        msgstr is assigned by exact-string lookup. No cross-wiring is
        possible because the assignment iterates the REQUEST, not the
        response dict."""
        m_response = MagicMock()
        m_block = MagicMock()
        m_block.type = "text"
        m_block.text = (
            '{"All Students": "Todos os alunos", '
            '"Total Students": "Total de Alunos", '
            '"Courses": "Cursos"}'
        )
        m_response.content = [m_block]
        m_anthropic.return_value.messages.create.return_value = m_response

        out = StringIO()
        err = StringIO()
        call_command(
            "translate_po",
            "--locale", "xx_TEST",
            "--no-compile",
            stdout=out,
            stderr=err,
        )

        after = self._read_msgstrs()
        self.assertEqual(after["All Students"], "Todos os alunos")
        self.assertEqual(after["Total Students"], "Total de Alunos")
        self.assertEqual(after["Courses"], "Cursos")
