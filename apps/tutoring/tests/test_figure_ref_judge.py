"""Unit tests for the figure-reference judge.

Pins behaviour of `apps/tutoring/judges/figure_ref.py`. Pure
deterministic regex check — no LLM calls.
"""

from django.test import SimpleTestCase

from apps.tutoring.judges.figure_ref import (
    FigureRefResult,
    run_figure_ref_judge,
)


class FigureRefSkipGatesTest(SimpleTestCase):
    def test_empty_response_skipped(self):
        result = run_figure_ref_judge("", attached_media_count=0)
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "empty_response")

    def test_figure_attached_skipped(self):
        """When a figure WAS attached this turn, references are
        legitimate — the figure_vision judge handles whether it
        actually matches. figure_ref short-circuits."""
        result = run_figure_ref_judge(
            "Looking at the diagram, you can see…",
            attached_media_count=1,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "figure_attached")
        self.assertEqual(result.issues, [])

    def test_no_figure_reference_skipped(self):
        """Plain prose with no diagram/figure phrasing."""
        result = run_figure_ref_judge(
            "Two angles on a straight line sum to 180°. Try this one: 180 - 42 = ?",
            attached_media_count=0,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_figure_reference")
        self.assertEqual(result.issues, [])


class FigureRefDetectionTest(SimpleTestCase):
    def test_flags_diagram_reference_with_no_attachment(self):
        """The core production failure mode: tutor says
        'looking at the diagram' but no figure was attached."""
        result = run_figure_ref_judge(
            "Looking at the diagram, you can see two angles. Sum is 180°.",
            attached_media_count=0,
        )
        self.assertFalse(result.skipped)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("looking at the diagram", result.issues[0].lower())

    def test_flags_each_distinct_phrase_only_once(self):
        """If the same phrase appears multiple times, dedupe."""
        result = run_figure_ref_judge(
            "Looking at the diagram, see angle a. "
            "Looking at the diagram again, see angle b.",
            attached_media_count=0,
        )
        self.assertFalse(result.skipped)
        self.assertEqual(len(result.issues), 1)

    def test_caps_issues_at_three(self):
        """Defensive cap — even with many distinct phrases, list stays short."""
        text = (
            "Looking at the diagram, "
            "see in the figure, "
            "in the image, "
            "from the diagram, "
            "shown in the figure, "
            "the diagram above shows…"
        )
        result = run_figure_ref_judge(text, attached_media_count=0)
        self.assertLessEqual(len(result.issues), 3)

    def test_in_question_flagged_when_reference_inside_question(self):
        """Reference embedded in a `?` sentence → in_question=True
        (more critical for regen)."""
        result = run_figure_ref_judge(
            "Now: looking at the diagram, what is the value of x?",
            attached_media_count=0,
        )
        self.assertFalse(result.skipped)
        self.assertTrue(result.in_question)
        self.assertIn("inside a question", result.issues[0])

    def test_in_question_flagged_when_followed_by_fill_in(self):
        """Fill-in marker `___°` after the reference also counts."""
        result = run_figure_ref_judge(
            "In the diagram the missing angle is ___°.",
            attached_media_count=0,
        )
        self.assertFalse(result.skipped)
        self.assertTrue(result.in_question)

    def test_in_question_false_when_reference_purely_narrative(self):
        result = run_figure_ref_judge(
            "Looking at the diagram, you can see how rays divide the angle.",
            attached_media_count=0,
        )
        self.assertFalse(result.skipped)
        # No `?`, no fill-in marker → narrative reference.
        self.assertFalse(result.in_question)

    def test_phrases_case_insensitive(self):
        result = run_figure_ref_judge(
            "LOOKING AT THE DIAGRAM, see angle a.",
            attached_media_count=0,
        )
        self.assertFalse(result.skipped)
        self.assertEqual(len(result.issues), 1)


class FigureRefDoesNotOverFlagTest(SimpleTestCase):
    """Cases that look figure-ish but should NOT trigger the judge."""

    def test_imagine_phrasing_does_not_flag(self):
        """`imagine` / `picture yourself` are figurative, not deictic."""
        result = run_figure_ref_judge(
            "Imagine you're at the beach watching the horizon. "
            "Picture yourself drawing a line on the sand.",
            attached_media_count=0,
        )
        # Neither "imagine" nor "picture yourself" is in the phrase set.
        self.assertTrue(result.skipped)

    def test_pure_math_question_does_not_flag(self):
        result = run_figure_ref_judge(
            "If one angle is 50°, what is the adjacent angle?",
            attached_media_count=0,
        )
        self.assertTrue(result.skipped)
