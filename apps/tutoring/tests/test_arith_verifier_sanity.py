"""Tests for the verify_arithmetic_claims sanity filter.

Production session 255 (2026-05-12) showed the verifier emitting
phantom corrections every turn:

- {claimed: '20', correct: '20', expression: '3x + 20 = 80'}
  → claimed == correct (no-op correction)
- {claimed: '6.66667', correct: '20', expression: '3x + 20 = 80'}
  → expression is an EQUATION with variable x, not arithmetic

Both passed through to validator → arithmetic_violation flag →
regen runs → can't fix what isn't broken → cycles exhausted →
shipped dirty. The fix is at the source: drop corrections where
claimed equals correct (numerically), and drop expressions
containing single-letter algebraic variables.
"""
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from apps.llm.client import LLMResponse
from apps.tutoring.llm_arithmetic_verifier import (
    _is_real_correction,
    verify_arithmetic_claims,
)


def _llm_response_with_corrections(corrections_payload: list) -> LLMResponse:
    import json
    return LLMResponse(
        content=json.dumps({"corrections": corrections_payload}),
        tokens_in=1, tokens_out=1, model="test", stop_reason="end_turn",
    )


class IsRealCorrectionTest(SimpleTestCase):
    def test_noop_dropped(self):
        # claimed == correct
        self.assertFalse(_is_real_correction("20", "20", "100 + 120 + 80"))

    def test_numeric_equivalence_noop_dropped(self):
        # "8" and "8.0" are the same value
        self.assertFalse(_is_real_correction("8", "8.0", "100 / 12.5"))
        self.assertFalse(_is_real_correction("20.00", "20", "100 + 120 + 80"))

    def test_real_correction_kept(self):
        # 95 + 70 = 165, not 175 — real arithmetic mistake
        self.assertTrue(_is_real_correction("175", "165", "95 + 70"))

    def test_algebraic_equation_dropped(self):
        # Expression contains a single-letter variable (x) — that's
        # an equation to solve, not arithmetic to verify.
        self.assertFalse(_is_real_correction("33", "20", "3x + 20 = 80"))
        self.assertFalse(_is_real_correction("14", "14", "5y = 70"))
        self.assertFalse(_is_real_correction("9", "5", "x + 5 = 14"))

    def test_units_kept_as_arithmetic(self):
        # "kg" and "km" are multi-letter — should NOT trigger the
        # algebraic-variable filter.
        self.assertTrue(_is_real_correction(
            "175", "165",
            "95 km + 70 km = 165",
        ))
        self.assertTrue(_is_real_correction(
            "100", "120",
            "50 kg + 70 kg = 120 kg",
        ))

    def test_empty_values_dropped(self):
        self.assertFalse(_is_real_correction("", "20", "100 + 120"))
        self.assertFalse(_is_real_correction("20", "", "100 + 120"))


class VerifyArithmeticClaimsFiltersFalsePositivesTest(SimpleTestCase):
    """End-to-end: when the LLM emits a phantom correction, the
    verifier strips it before returning."""

    def test_noop_correction_filtered_out(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response_with_corrections([
            # Phantom — claimed == correct
            {"expression": "100 + 120 + 80", "claimed": "300", "correct": "300"},
        ])
        _text, corrections = verify_arithmetic_claims(
            "Three angles sum to 100° + 120° + 80° = 300°.",
            llm_client=llm,
        )
        self.assertEqual(corrections, [])

    def test_algebraic_equation_filtered_out(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response_with_corrections([
            # Phantom — verifier tried to "solve" 3x + 20 = 80
            {"expression": "3x + 20 = 80", "claimed": "33", "correct": "20"},
            {"expression": "5y = 70", "claimed": "14", "correct": "14"},
        ])
        _text, corrections = verify_arithmetic_claims(
            "Solve 3x + 20 = 80 and 5y = 70.",
            llm_client=llm,
        )
        self.assertEqual(corrections, [])

    def test_real_correction_passes_through(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response_with_corrections([
            # Real mistake: 95 + 70 = 165 not 175
            {"expression": "95 + 70", "claimed": "175", "correct": "165"},
        ])
        _text, corrections = verify_arithmetic_claims(
            "First add 95 + 70 = 175, so x = 360 - 175 = 185.",
            llm_client=llm,
        )
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["claimed"], "175")
        self.assertEqual(corrections[0]["correct"], "165")

    def test_mixed_real_and_phantom_keeps_only_real(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response_with_corrections([
            # Phantom — claimed == correct
            {"expression": "100 + 120 + 80", "claimed": "300", "correct": "300"},
            # Phantom — equation with variable
            {"expression": "5x = 100", "claimed": "20", "correct": "20"},
            # REAL — actual arithmetic mistake
            {"expression": "75 + 95", "claimed": "180", "correct": "170"},
        ])
        _text, corrections = verify_arithmetic_claims(
            "Add 75 + 95 = 180 km total.",
            llm_client=llm,
        )
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["expression"], "75 + 95")
