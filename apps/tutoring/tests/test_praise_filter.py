"""Praise filter is DISABLED as of 2026-05-06.

Pilot transcripts showed that the post-process strip kept injecting
stock opener phrases ("Let's check this one together…", "What was
your first move on this?") that Sonnet then echoed turn-after-turn.
Each new opener pool became the next leak. The fix was to disable
the strip entirely; the function signature stays for backward
compatibility with all existing call sites.

This file used to have ~17 tests covering strip behavior, opener
rotation, context-specific replacements, and edge cases. All of
them are now obsolete — the function is a deliberate no-op. We
keep one regression test asserting the no-op behavior so anyone
re-enabling the filter has to consciously update this file.
"""

import unittest

from apps.tutoring.praise_filter import strip_praise_if_wrong


class TestStripPraiseDisabled(unittest.TestCase):
    """Sanity that the function returns input unchanged regardless of
    arguments. If you flip this back on, this whole file needs a
    rewrite — see the module docstring for context."""

    def test_no_op_on_wrong_answer(self):
        text = "Brilliant! You've nailed it."
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertEqual(result, text)
        self.assertFalse(modified)

    def test_no_op_on_correct(self):
        text = "Right! Perfect work."
        result, modified = strip_praise_if_wrong(text, is_correct=True)
        self.assertEqual(result, text)
        self.assertFalse(modified)

    def test_no_op_on_bare_correct(self):
        text = "Exactly! 275 is right!"
        result, modified = strip_praise_if_wrong(
            text, is_correct=False,
            context="bare_correct", student_input="275",
        )
        self.assertEqual(result, text)
        self.assertFalse(modified)

    def test_no_op_on_empty_text(self):
        result, modified = strip_praise_if_wrong("", is_correct=False)
        self.assertEqual(result, "")
        self.assertFalse(modified)


if __name__ == "__main__":
    unittest.main()
