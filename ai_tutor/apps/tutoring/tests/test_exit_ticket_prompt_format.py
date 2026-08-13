"""Regression guard for the exit-ticket prompt templates.

Both MATH_EXIT_TICKET_PROMPT and EXIT_TICKET_PROMPT are run through
``str.format()`` at runtime. Any literal `{name}` in the prompt body —
typically added as documentation in worked-example or failure-mode
sections — gets interpreted as a placeholder and raises KeyError if
not in the kwargs whitelist. That kills the regen silently, with the
exception swallowed by the surrounding handler.

History (2026-05-03 incident):
  - CONCEPTUAL INTEGRITY + COMMON FAILURE MODES sections shipped
    with single-brace `{a}` / `{n}` / `{angle}` references as
    documentation. Every exit-ticket regen for a math lesson then
    crashed with `KeyError: 'a'` before the LLM call. The regen
    came back as an empty replace (transaction rollback retained
    old questions, lesson timestamp updated, looked stale to the
    teacher) — invisible failure mode.

This test catches the same trap on the next change.
"""

from django.test import TestCase

from ai_tutor.apps.tutoring.management.commands.generate_exit_tickets import (
    EXIT_TICKET_PROMPT,
    MATH_EXIT_TICKET_PROMPT,
)


# Must match the kwargs the engine passes — see
# apps/curriculum/content_generator.py::generate_exit_ticket_for_lesson
ALLOWED_FORMAT_KWARGS = {
    'lesson_title': 'Test Lesson',
    'lesson_objective': 'Test objective',
    'subject': 'Mathematics',
    'exam_context': '',
    'seychelles_context': '',
}


class ExitTicketPromptFormatTest(TestCase):
    def test_math_prompt_formats_with_engine_kwargs(self):
        try:
            out = MATH_EXIT_TICKET_PROMPT.format(**ALLOWED_FORMAT_KWARGS)
        except KeyError as e:
            self.fail(
                f"MATH_EXIT_TICKET_PROMPT.format() raised KeyError({e!r}). "
                f"Likely a documentation example with single-brace "
                f"{{name}} that should be {{{{name}}}} to escape the "
                f"placeholder. See test docstring for context."
            )
        self.assertIn('Test Lesson', out)

    def test_general_prompt_formats_with_engine_kwargs(self):
        try:
            out = EXIT_TICKET_PROMPT.format(**ALLOWED_FORMAT_KWARGS)
        except KeyError as e:
            self.fail(
                f"EXIT_TICKET_PROMPT.format() raised KeyError({e!r}). "
                f"Likely a documentation example with single-brace "
                f"{{name}} that should be {{{{name}}}}."
            )
        self.assertIn('Test Lesson', out)

    def test_math_prompt_keeps_double_brace_examples_intact(self):
        """The properly-escaped {{a}} placeholders in the worked
        examples should render as `{a}` after format() — that's what
        the LLM sees and what the renderer expects when parsing the
        emitted templates."""
        out = MATH_EXIT_TICKET_PROMPT.format(**ALLOWED_FORMAT_KWARGS)
        # Worked example #1 (sum of angles around a point) has
        # template_text "...{a}°, {b}°, and x°..." after escape.
        self.assertIn('{a}°', out)
        self.assertIn('{b}°', out)
        # The {{answer}} slot should also pass through correctly.
        self.assertIn('{answer}', out)
