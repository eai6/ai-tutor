"""Unit tests for the figure-vision judge.

Pins behaviour of `apps/tutoring/judges/figure_vision.py`. The judge
makes a vision LLM call ONLY when:
  - a figure is attached
  - the response poses a figure-dependent question
  - llm_client + image_reader are both supplied
  - the image_reader actually returns bytes

All four gates are exercised.
"""

import json
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from ai_tutor.apps.llm.client import LLMResponse
from ai_tutor.apps.tutoring.judges.figure_vision import (
    FigureVisionResult,
    run_figure_vision_judge,
)


def _llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, tokens_in=1, tokens_out=1,
        model="test-vision", stop_reason="end_turn",
    )


def _ok_reader():
    """Image reader that returns plausible bytes."""
    return lambda url: ("ZmFrZS1pbWFnZS1ieXRlcw==", "image/png")


def _empty_reader():
    """Image reader that returns no bytes — simulates a fetch failure."""
    return lambda url: ("", "")


def _figure(url: str = "/media/figures/test.png") -> dict:
    return {"url": url, "description": "test figure"}


class FigureVisionSkipGatesTest(SimpleTestCase):
    def test_no_attached_media_skipped(self):
        llm = MagicMock()
        result = run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_attached_media")
        llm.generate.assert_not_called()

    def test_empty_response_skipped(self):
        llm = MagicMock()
        result = run_figure_vision_judge(
            "",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "empty_response")

    def test_no_figure_question_skipped(self):
        """Response has a figure attached but doesn't pose a question
        that depends on it — pure narrative. Skip the (expensive) call."""
        llm = MagicMock()
        result = run_figure_vision_judge(
            "Great work on that calculation. Let's continue.",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_figure_question")
        llm.generate.assert_not_called()

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
        llm = MagicMock()
        result = run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[_figure()],
            image_reader=None,
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_llm_client_or_reader")
        llm.generate.assert_not_called()

    def test_media_with_no_url_skipped(self):
        llm = MagicMock()
        result = run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[{"url": "", "description": "blank"}],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "media_no_url")

    def test_image_fetch_empty_skipped(self):
        """Reader succeeds but returns empty bytes — skip without LLM call."""
        llm = MagicMock()
        result = run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[_figure()],
            image_reader=_empty_reader(),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "image_fetch_empty")
        llm.generate.assert_not_called()

    def test_image_reader_exception_fails_soft(self):
        llm = MagicMock()
        def _bad_reader(url):
            raise IOError("disk on fire")
        result = run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[_figure()],
            image_reader=_bad_reader,
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("image_read_error"))


class FigureVisionParseTest(SimpleTestCase):
    def test_aligned_response_parsed(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "aligned": True,
            "figure_summary": "two angles 120° and 60° on a straight line",
            "mismatch_reason": "",
        }))
        result = run_figure_vision_judge(
            "Looking at the diagram, what is the missing angle?",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertFalse(result.skipped)
        self.assertIs(result.aligned, True)
        self.assertIn("two angles", result.figure_summary)
        self.assertEqual(result.mismatch_reason, "")
        llm.generate.assert_called_once()

    def test_misaligned_response_carries_reason(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "aligned": False,
            "figure_summary": "diagram shows 2 angles",
            "mismatch_reason": "question describes 3 angles, figure shows 2",
        }))
        result = run_figure_vision_judge(
            "In the figure, three angles 30°, 62°, w sum to 180°. What is w?",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertFalse(result.skipped)
        self.assertIs(result.aligned, False)
        self.assertIn("3 angles", result.mismatch_reason)

    def test_null_aligned_preserved(self):
        """LLM returns aligned=null when it can't tell — don't infer."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "aligned": None,
            "figure_summary": "blurry",
            "mismatch_reason": "",
        }))
        result = run_figure_vision_judge(
            "In the diagram, what do you see?",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertFalse(result.skipped)
        self.assertIsNone(result.aligned)

    def test_message_has_image_block_and_text_block(self):
        """The vision call must include both the image and the text
        prompt — confirm the message shape."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "aligned": True, "figure_summary": "ok", "mismatch_reason": "",
        }))
        run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        call = llm.generate.call_args
        messages = call.kwargs.get("messages") or []
        self.assertEqual(len(messages), 1)
        content_blocks = messages[0]["content"]
        self.assertIsInstance(content_blocks, list)
        block_types = [b["type"] for b in content_blocks]
        self.assertIn("image", block_types)
        self.assertIn("text", block_types)


class FigureVisionFailSoftTest(SimpleTestCase):
    def test_malformed_json_fails_soft(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response("totally not json")
        result = run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("vision_error"))

    def test_llm_exception_fails_soft(self):
        llm = MagicMock()
        llm.generate.side_effect = RuntimeError("vision API down")
        result = run_figure_vision_judge(
            "Looking at the diagram, what is x?",
            attached_media=[_figure()],
            image_reader=_ok_reader(),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("vision_error"))


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
