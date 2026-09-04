"""Unit tests for the figure-vision judge.

Pins behaviour of `apps/tutoring/judges/figure_vision.py`. The judge
makes a vision LLM call ONLY when:
  - a figure is attached
  - the response poses a figure-dependent question
  - llm_client + image_reader are both supplied
  - the image_reader actually returns bytes

All four gates are exercised.

── Where the seam is ────────────────────────────────────────────────────
The judge does not call `llm_client.generate` any more. It wraps the
client with `_instructor_helper.get_instructor_from_client` and calls
`chat.completions.create(response_model=_FigureVisionVerdict, ...)`,
passing the figure as an `instructor.Image` rather than a hand-built
Anthropic image block. The wire format and the malformed-JSON path
belong to instructor now.

These tests used to hand a MagicMock's `.generate` raw JSON. That seam
is gone: `get_instructor_from_client` reads `client.config.provider`,
which on a MagicMock is not one instructor knows, so every test here
fell through to `skip_reason='instructor_unavailable'` — including the
ones asserting a misalignment comes back with its reason.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from django.test import SimpleTestCase
from pydantic import ValidationError

from ai_tutor.apps.tutoring.judges import figure_vision as fv_mod
from ai_tutor.apps.tutoring.judges.figure_vision import (
    FigureVisionResult,
    _FigureVisionVerdict,
    run_figure_vision_judge,
)


@contextmanager
def _vision(**fields):
    """Stand in for one instructor vision round-trip. Yields the patched
    `chat.completions.create` so a test can assert on the call."""
    client = MagicMock()
    client.chat.completions.create.return_value = _FigureVisionVerdict(**fields)
    with patch.object(fv_mod, 'get_instructor_from_client', return_value=client):
        yield client.chat.completions.create


@contextmanager
def _vision_raises(exc):
    client = MagicMock()
    client.chat.completions.create.side_effect = exc
    with patch.object(fv_mod, 'get_instructor_from_client', return_value=client):
        yield client.chat.completions.create


@contextmanager
def _never_called():
    """For the skip gates: the judge must not reach the model.

    `llm.generate.assert_not_called()` used to carry this and now passes
    vacuously — the judge never calls generate on any path — so the
    assertion moves to the call that would actually spend a token.
    """
    client = MagicMock()
    with patch.object(fv_mod, 'get_instructor_from_client', return_value=client):
        yield client.chat.completions.create


def _ok_reader():
    """Image reader that returns plausible bytes."""
    return lambda url: ("ZmFrZS1pbWFnZS1ieXRlcw==", "image/png")


def _empty_reader():
    """Image reader that returns no bytes — simulates a fetch failure."""
    return lambda url: ("", "")


def _figure(url: str = "/media/figures/test.png") -> dict:
    return {"url": url, "description": "test figure"}


class FigureVisionSkipGatesTest(SimpleTestCase):
    """Each gate must stop the judge BEFORE the vision call — that call
    is the expensive one, so `create.assert_not_called()` is the point of
    every test here."""

    def test_no_attached_media_skipped(self):
        with _never_called() as create:
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_attached_media")
        create.assert_not_called()

    def test_empty_response_skipped(self):
        with _never_called() as create:
            result = run_figure_vision_judge(
                "",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "empty_response")
        create.assert_not_called()

    def test_no_figure_question_skipped(self):
        """Response has a figure attached but doesn't pose a question
        that depends on it — pure narrative. Skip the (expensive) call."""
        with _never_called() as create:
            result = run_figure_vision_judge(
                "Great work on that calculation. Let's continue.",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_figure_question")
        create.assert_not_called()

    def test_no_llm_client_skipped(self):
        result = run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=None,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_llm_client_or_reader")

    def test_no_image_reader_skipped(self):
        with _never_called() as create:
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[_figure()],
                image_reader=None,
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_llm_client_or_reader")
        create.assert_not_called()

    def test_media_with_no_url_skipped(self):
        with _never_called() as create:
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[{"url": "", "description": "blank"}],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "media_no_url")
        create.assert_not_called()

    def test_image_fetch_empty_skipped(self):
        """Reader succeeds but returns empty bytes — skip without LLM call."""
        with _never_called() as create:
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[_figure()],
                image_reader=_empty_reader(),
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "image_fetch_empty")
        create.assert_not_called()

    def test_image_reader_exception_fails_soft(self):
        def _bad_reader(url):
            raise IOError("disk on fire")

        with _never_called() as create:
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[_figure()],
                image_reader=_bad_reader,
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("image_read_error"))
        create.assert_not_called()

    def test_instructor_unavailable_skips_rather_than_blocking(self):
        with patch.object(fv_mod, 'get_instructor_from_client',
                          return_value=None):
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "instructor_unavailable")


class FigureVisionParseTest(SimpleTestCase):
    def test_aligned_response_parsed(self):
        with _vision(aligned=True,
                     figure_summary="diagram shows two angles on a line",
                     mismatch_reason="") as create:
            result = run_figure_vision_judge(
                "Looking at the diagram, what is the missing angle?",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertFalse(result.skipped)
        self.assertIs(result.aligned, True)
        self.assertIn("two angles", result.figure_summary)
        self.assertEqual(result.mismatch_reason, "")
        create.assert_called_once()

    def test_misaligned_response_carries_reason(self):
        with _vision(aligned=False,
                     figure_summary="diagram shows 2 angles",
                     mismatch_reason="question describes 3 angles, figure shows 2"):
            result = run_figure_vision_judge(
                "In the figure, three angles 30°, 62°, w sum to 180°. What is w?",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertFalse(result.skipped)
        self.assertIs(result.aligned, False)
        self.assertIn("3 angles", result.mismatch_reason)

    def test_null_aligned_preserved(self):
        """LLM returns aligned=null when it can't tell — don't infer."""
        with _vision(aligned=None, figure_summary="blurry", mismatch_reason=""):
            result = run_figure_vision_judge(
                "In the diagram, what do you see?",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertFalse(result.skipped)
        self.assertIsNone(result.aligned)

    def test_message_carries_the_image_and_the_text_prompt(self):
        """The vision call must include both the image and the text
        prompt. The image is an instructor.Image now rather than a
        hand-built Anthropic image block, so the assertion is on the
        content list holding one of each, not on block `type` keys."""
        from instructor.processing.multimodal import Image

        with _vision(aligned=True, figure_summary="ok",
                     mismatch_reason="") as create:
            run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        kwargs = create.call_args.kwargs
        self.assertIs(kwargs["response_model"], _FigureVisionVerdict)

        messages = kwargs["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        content = messages[1]["content"]
        self.assertEqual(len(content), 2)
        self.assertIsInstance(content[0], Image)
        self.assertIn("Looking at the diagram", content[1])

    def test_the_figure_description_reaches_the_prompt(self):
        with _vision(aligned=True, figure_summary="ok",
                     mismatch_reason="") as create:
            run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[{"url": "/media/f.png",
                                 "description": "two angles on a straight line"}],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        text = create.call_args.kwargs["messages"][1]["content"][1]
        self.assertIn("two angles on a straight line", text)

    def test_the_verdict_fields_are_length_capped(self):
        """The cap lives on the schema, so an over-long field never
        reaches the judge at all. The judge slices to the same lengths
        afterwards, which is belt-and-braces rather than the thing doing
        the work — worth stating so nobody "simplifies" the schema and
        expects the slice to hold the line.
        """
        with pytest.raises(ValidationError):
            _FigureVisionVerdict(figure_summary="s" * 201)
        with pytest.raises(ValidationError):
            _FigureVisionVerdict(mismatch_reason="m" * 301)

        # At the cap, both survive intact through the judge.
        with _vision(aligned=False, figure_summary="s" * 200,
                     mismatch_reason="m" * 300):
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertEqual(len(result.figure_summary), 200)
        self.assertEqual(len(result.mismatch_reason), 300)


class FigureVisionFailSoftTest(SimpleTestCase):
    def test_structured_call_error_fails_soft(self):
        """Instructor raises when it cannot coerce the response."""
        with _vision_raises(ValueError("could not coerce response")):
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("vision_error"))

    def test_llm_exception_fails_soft(self):
        with _vision_raises(RuntimeError("vision API down")):
            result = run_figure_vision_judge(
                "Looking at the diagram, what is x?",
                attached_media=[_figure()],
                image_reader=_ok_reader(),
                llm_client=MagicMock(),
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "vision_error: RuntimeError")


class FigureVisionResultShapeTest(SimpleTestCase):
    def test_default_result_cannot_tell_and_is_not_skipped(self):
        result = FigureVisionResult()
        self.assertIsNone(result.aligned)
        self.assertEqual(result.mismatch_reason, "")
        self.assertFalse(result.skipped)


class FigureVisionGatingTest(SimpleTestCase):
    """The `_has_figure_question` gate decides whether the (expensive)
    vision call fires. Lock its behaviour in."""

    def test_question_about_diagram_triggers(self):
        from ai_tutor.apps.tutoring.judges.figure_vision import _has_figure_question
        self.assertTrue(_has_figure_question(
            "Looking at the diagram, what is x?"
        ))

    def test_fill_in_referencing_figure_triggers(self):
        from ai_tutor.apps.tutoring.judges.figure_vision import _has_figure_question
        self.assertTrue(_has_figure_question(
            "In the figure the missing angle is ___°."
        ))

    def test_question_without_figure_phrase_skips(self):
        from ai_tutor.apps.tutoring.judges.figure_vision import _has_figure_question
        self.assertFalse(_has_figure_question(
            "What is 180 minus 42?"
        ))

    def test_figure_phrase_without_question_skips(self):
        from ai_tutor.apps.tutoring.judges.figure_vision import _has_figure_question
        self.assertFalse(_has_figure_question(
            "The diagram shows two angles on a line."
        ))
