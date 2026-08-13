"""Unit tests for the coherence judge.

Pins the behaviour of `apps/tutoring/judges/coherence.py`:
  - skip gates (empty / no client / too short)
  - parses violations from a well-formed LLM response
  - fail-soft on malformed JSON
  - production-style self-contradiction example produces a flag
"""

import json
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from ai_tutor.apps.llm.client import LLMResponse
from ai_tutor.apps.tutoring.judges.coherence import (
    CoherenceResult,
    run_coherence_judge,
)


def _llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, tokens_in=1, tokens_out=1,
        model="test", stop_reason="end_turn",
    )


def _long(text: str, target: int = 250) -> str:
    """Pad a string past the coherence judge's length pre-gate
    (>= 200 chars + >= 2 sentences) without changing its meaning."""
    if len(text) >= target:
        return text
    pad = " The student is in a tutoring session. We will continue."
    while len(text) < target:
        text = text + pad
    return text


class CoherenceJudgeSkipGatesTest(SimpleTestCase):
    def test_empty_response_skipped(self):
        llm = MagicMock()
        result = run_coherence_judge("", llm_client=llm)
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "empty_response")
        llm.generate.assert_not_called()

    def test_no_llm_client_skipped(self):
        result = run_coherence_judge(
            _long("Some long enough response. Another sentence to clear the gate."),
            llm_client=None,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_llm_client")

    def test_too_short_response_skipped(self):
        llm = MagicMock()
        # Single sentence < 200 chars — can't plausibly contradict itself.
        result = run_coherence_judge("Great work!", llm_client=llm)
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "too_short_for_contradiction")
        llm.generate.assert_not_called()

    def test_long_single_sentence_still_skipped(self):
        """Length alone isn't enough — also need >= 2 sentences."""
        llm = MagicMock()
        text = "x" * 400  # no sentence terminators at all
        result = run_coherence_judge(text, llm_client=llm)
        self.assertTrue(result.skipped)
        llm.generate.assert_not_called()


class CoherenceJudgeParseTest(SimpleTestCase):
    def test_clean_response_returns_no_violations(self):
        """Well-formed JSON with empty violations array → no findings."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({"violations": []}))
        result = run_coherence_judge(
            _long("Good work on that calculation. Now let's try a similar problem."),
            llm_client=llm,
        )
        self.assertFalse(result.skipped)
        self.assertEqual(result.violations, [])
        llm.generate.assert_called_once()

    def test_parses_violations_from_llm(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "violations": [
                "introduces 3 angles then poses a 2-angle question",
                "praises student then says 'not quite'",
            ],
        }))
        result = run_coherence_judge(
            _long("Now let's explore three angles. Find the other angle when one is 42."),
            llm_client=llm,
        )
        self.assertFalse(result.skipped)
        self.assertEqual(len(result.violations), 2)
        self.assertIn("3 angles", result.violations[0])

    def test_truncates_violation_strings_at_200(self):
        llm = MagicMock()
        long_v = "x" * 500
        llm.generate.return_value = _llm_response(json.dumps({
            "violations": [long_v],
        }))
        result = run_coherence_judge(
            _long("Good. Now let's continue."),
            llm_client=llm,
        )
        self.assertEqual(len(result.violations), 1)
        self.assertLessEqual(len(result.violations[0]), 200)

    def test_caps_violations_at_max(self):
        """max_violations=4 default — extra entries dropped."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "violations": [f"v{i}" for i in range(10)],
        }))
        result = run_coherence_judge(
            _long("Good. Now let's continue."),
            llm_client=llm,
        )
        self.assertEqual(len(result.violations), 4)

    def test_strips_markdown_fence(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response(
            "```json\n" + json.dumps({"violations": ["fenced violation"]}) + "\n```"
        )
        result = run_coherence_judge(
            _long("Good. Now let's continue."),
            llm_client=llm,
        )
        self.assertFalse(result.skipped)
        self.assertEqual(result.violations, ["fenced violation"])


class CoherenceJudgeFailSoftTest(SimpleTestCase):
    def test_malformed_json_fails_soft(self):
        """Bad LLM output ⇒ skipped + skip_reason llm_error*. Never raises."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response("not valid json at all")
        result = run_coherence_judge(
            _long("Good. Now let's continue."),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("llm_error"))
        self.assertEqual(result.violations, [])

    def test_llm_exception_fails_soft(self):
        llm = MagicMock()
        llm.generate.side_effect = RuntimeError("network down")
        result = run_coherence_judge(
            _long("Good. Now let's continue."),
            llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("llm_error"))

    def test_violations_not_a_list_returns_empty(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "violations": "not a list",
        }))
        result = run_coherence_judge(
            _long("Good. Now let's continue."),
            llm_client=llm,
        )
        # Doesn't crash; just no violations recorded.
        self.assertFalse(result.skipped)
        self.assertEqual(result.violations, [])


class CoherenceJudgePromptContractTest(SimpleTestCase):
    """Pilot 2026-05-12: tutor authored "To solve x + 15 = 25, what
    operation..." when the posed problem said the RESULT was 40. The
    coherence judge prompt must explicitly list this scaffold-vs-posed
    mismatch as a violation pattern so the judge has a chance of
    catching it on real traffic."""

    def test_prompt_names_scaffold_mismatch_pattern(self):
        from ai_tutor.apps.tutoring.judges.coherence import _SYSTEM
        self.assertIn("SCAFFOLD-vs-POSED MISMATCH", _SYSTEM)
        # The illustrative example uses the same numbers from the
        # pilot transcript so the judge has a concrete shape to match.
        self.assertIn("x + 15 = 40", _SYSTEM)
        self.assertIn("x + 15 = 25", _SYSTEM)
