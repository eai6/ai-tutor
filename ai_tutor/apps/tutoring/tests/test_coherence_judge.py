"""Unit tests for the coherence judge.

Pins the behaviour of `apps/tutoring/judges/coherence.py`:
  - skip gates (empty / no client / too short / instructor unavailable)
  - carries violations through, truncated and capped
  - fail-soft when the structured call raises
  - the prompt still names the pilot's scaffold-vs-posed pattern

── Where the seam is ────────────────────────────────────────────────────
The judge does not call `llm_client.generate` any more. It hands the
client to `_instructor_helper.get_instructor_from_client`, then asks
`structured_completion` for a typed `_CoherenceVerdict`, so the wire
format — the markdown fence, the malformed JSON, a `violations` that is
not a list — is instructor's problem now, not this module's.

These tests used to hand a MagicMock's `.generate` raw JSON. That seam
is gone: `get_instructor_from_client` reads `client.config.provider`,
which on a MagicMock is not a provider instructor knows, so every test
fell through to `skip_reason='instructor_unavailable'` — including the
ones asserting violations get parsed. They now stand on the two
functions the judge actually calls.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from django.test import SimpleTestCase
from pydantic import ValidationError

from ai_tutor.apps.tutoring.judges import coherence as coherence_mod
from ai_tutor.apps.tutoring.judges.coherence import (
    CoherenceResult,
    _CoherenceVerdict,
    run_coherence_judge,
)


@contextmanager
def _verdict(violations):
    """Stand in for one instructor round-trip. Yields the patched
    `structured_completion` so a test can assert it was (or was not)
    reached."""
    with patch.object(coherence_mod, 'get_instructor_from_client',
                      return_value=object()):
        with patch.object(coherence_mod, 'structured_completion',
                          return_value=_CoherenceVerdict(violations=violations)) as call:
            yield call


@contextmanager
def _raises(exc):
    with patch.object(coherence_mod, 'get_instructor_from_client',
                      return_value=object()):
        with patch.object(coherence_mod, 'structured_completion',
                          side_effect=exc) as call:
            yield call


@contextmanager
def _never_called():
    """For the skip gates: the judge must not reach the model at all.

    `llm.generate.assert_not_called()` used to carry this. It passes
    vacuously now — the judge never calls generate on any path — so the
    assertion has to move to the function that would actually spend a
    token.
    """
    with patch.object(coherence_mod, 'get_instructor_from_client',
                      return_value=object()):
        with patch.object(coherence_mod, 'structured_completion') as call:
            yield call


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
        with _never_called() as call:
            result = run_coherence_judge("", llm_client=MagicMock())
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "empty_response")
        call.assert_not_called()

    def test_no_llm_client_skipped(self):
        result = run_coherence_judge(
            _long("Some long enough response. Another sentence to clear the gate."),
            llm_client=None,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_llm_client")

    def test_too_short_response_skipped(self):
        # Single sentence < 200 chars — can't plausibly contradict itself.
        with _never_called() as call:
            result = run_coherence_judge("Great work!", llm_client=MagicMock())
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "too_short_for_contradiction")
        call.assert_not_called()

    def test_long_single_sentence_still_skipped(self):
        """Length alone isn't enough — also need >= 2 sentences."""
        with _never_called() as call:
            result = run_coherence_judge("x" * 400, llm_client=MagicMock())
        self.assertTrue(result.skipped)
        call.assert_not_called()

    def test_instructor_unavailable_skips_rather_than_blocking(self):
        with patch.object(coherence_mod, 'get_instructor_from_client',
                          return_value=None):
            result = run_coherence_judge(
                _long("Good. Now let's continue."), llm_client=MagicMock())
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "instructor_unavailable")
        self.assertEqual(result.violations, [])


class CoherenceJudgeParseTest(SimpleTestCase):
    def test_clean_response_returns_no_violations(self):
        with _verdict([]) as call:
            result = run_coherence_judge(
                _long("Good work on that calculation. Now let's try a similar problem."),
                llm_client=MagicMock(),
            )
        self.assertFalse(result.skipped)
        self.assertEqual(result.violations, [])
        call.assert_called_once()

    def test_parses_violations_from_llm(self):
        with _verdict([
            "introduces 3 angles then poses a 2-angle question",
            "praises student then says 'not quite'",
        ]):
            result = run_coherence_judge(
                _long("Now let's explore three angles. Find the other angle when one is 42."),
                llm_client=MagicMock(),
            )
        self.assertFalse(result.skipped)
        self.assertEqual(len(result.violations), 2)
        self.assertIn("3 angles", result.violations[0])

    def test_truncates_violation_strings_at_200(self):
        with _verdict(["x" * 500]):
            result = run_coherence_judge(
                _long("Good. Now let's continue."), llm_client=MagicMock())
        self.assertEqual(len(result.violations), 1)
        self.assertEqual(len(result.violations[0]), 200)

    def test_caps_violations_at_max(self):
        """max_violations=4 default — extra entries dropped."""
        with _verdict([f"v{i}" for i in range(10)]):
            result = run_coherence_judge(
                _long("Good. Now let's continue."), llm_client=MagicMock())
        self.assertEqual(len(result.violations), 4)

    def test_blank_violations_are_dropped(self):
        with _verdict(["", "   ", "a real one"]):
            result = run_coherence_judge(
                _long("Good. Now let's continue."), llm_client=MagicMock())
        self.assertEqual(result.violations, ["a real one"])

    def test_the_reviewed_response_reaches_the_prompt(self):
        text = _long("Angles sum to 180. Now find the third angle.")
        with _verdict([]) as call:
            run_coherence_judge(text, llm_client=MagicMock())
        self.assertIn("Angles sum to 180.",
                      call.call_args.kwargs['user_prompt'])

    def test_prior_exchanges_reach_the_prompt_when_given(self):
        with _verdict([]) as call:
            run_coherence_judge(
                _long("Good. Now let's continue."),
                llm_client=MagicMock(),
                prior_exchanges="TUTOR: the answer was 12",
            )
        prompt = call.call_args.kwargs['user_prompt']
        self.assertIn("PRIOR_EXCHANGES", prompt)
        self.assertIn("the answer was 12", prompt)


class CoherenceVerdictSchemaTest(SimpleTestCase):
    """Shape enforcement moved into the schema when the judge stopped
    parsing JSON by hand — a `violations` that is not a list of strings
    is rejected before the judge ever sees it."""

    def test_violations_must_be_a_list(self):
        with pytest.raises(ValidationError):
            _CoherenceVerdict(violations="not a list")

    def test_defaults_to_no_violations(self):
        self.assertEqual(_CoherenceVerdict().violations, [])


class CoherenceJudgeFailSoftTest(SimpleTestCase):
    def test_structured_call_error_fails_soft(self):
        """Instructor raises when it cannot coerce a response — the
        judge names it and lets the turn through. Never raises."""
        with _raises(ValueError("could not coerce response")):
            result = run_coherence_judge(
                _long("Good. Now let's continue."), llm_client=MagicMock())
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("llm_error"))
        self.assertEqual(result.violations, [])

    def test_llm_exception_fails_soft(self):
        with _raises(RuntimeError("network down")):
            result = run_coherence_judge(
                _long("Good. Now let's continue."), llm_client=MagicMock())
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "llm_error: RuntimeError")


class CoherenceResultShapeTest(SimpleTestCase):
    def test_default_result_is_clean_and_not_skipped(self):
        result = CoherenceResult()
        self.assertEqual(result.violations, [])
        self.assertFalse(result.skipped)
        self.assertEqual(result.skip_reason, "")


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
