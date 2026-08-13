"""Tests for Layer 2 — cross_check_question (Patterns B/C/D).

Coverage:
  - Pattern B (pure additive sum) — MCQ + short_answer
  - Pattern C (multiplication chain)
  - Pattern D (simple linear equation, both var positions)
  - Out-of-pattern (word problems) pass through
  - Tolerance + units (°)
  - Integration with is_broken (Layer 2 entries flag drop)
"""

from __future__ import annotations

import unittest

from ai_tutor.apps.tutoring.question_validator import (
    cross_check_question,
    is_broken,
)


# ============================================================================
# Pattern B — pure additive sum
# ============================================================================


class TestPatternB_PureSum(unittest.TestCase):
    def test_mcq_correct_answer_passes(self):
        # Stem computes 275; correct option B = "275" → no audit.
        q = {
            "question_type": "mcq",
            "question": "What is 95 + 70 + 110?",
            "option_a": "165",
            "option_b": "275",
            "option_c": "285",
            "option_d": "360",
            "correct": "B",
        }
        self.assertIsNone(cross_check_question(q))
        self.assertIsNone(is_broken(q))

    def test_mcq_wrong_correct_answer_caught(self):
        # Stem computes 275; correct option marked as A (165) → audit.
        q = {
            "question_type": "mcq",
            "question": "What is 95 + 70 + 110?",
            "option_a": "165",
            "option_b": "275",
            "option_c": "285",
            "option_d": "360",
            "correct": "A",  # WRONG — A is 165 but stem computes 275
        }
        audit = cross_check_question(q, question_index=3)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["question_index"], 3)
        self.assertEqual(audit["pattern"], "sum")
        self.assertEqual(audit["computed"], 275.0)
        self.assertEqual(audit["claimed"], 165.0)
        # is_broken should also flag it (Layer 2 wired in)
        self.assertIsNotNone(is_broken(q))

    def test_short_answer_caught(self):
        q = {
            "question_type": "short_answer",
            "question": "Compute 95 + 70 + 110.",
            "correct_answer": "165",  # wrong, stem says 275
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["pattern"], "sum")
        self.assertEqual(audit["computed"], 275.0)

    def test_with_degree_units_normalized(self):
        # Geometry phrasing — ° symbols don't break extraction.
        q = {
            "question_type": "short_answer",
            "question": "What is 95° + 70° + 110°?",
            "correct_answer": "165",
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["computed"], 275.0)

    def test_question_mark_terminator_works(self):
        q = {
            "question_type": "short_answer",
            "question": "Calculate 30 + 40 + 50?",
            "correct_answer": "100",  # wrong, 120
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["computed"], 120.0)

    def test_two_term_does_NOT_match(self):
        # Pattern B requires 3+ operands. "3 + 4 = ?" is not enough
        # to flag (could be a deliberate trivial question).
        q = {
            "question_type": "short_answer",
            "question": "What is 3 + 4?",
            "correct_answer": "8",  # wrong (would be 7) but skipped
        }
        self.assertIsNone(cross_check_question(q))


# ============================================================================
# Pattern C — multiplication chain
# ============================================================================


class TestPatternC_MultChain(unittest.TestCase):
    def test_simple_chain_caught(self):
        q = {
            "question_type": "short_answer",
            "question": "Calculate 8 × 7 × 3.",
            "correct_answer": "120",  # wrong, 168
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["pattern"], "mult")
        self.assertEqual(audit["computed"], 168.0)
        self.assertEqual(audit["claimed"], 120.0)

    def test_correct_chain_passes(self):
        q = {
            "question_type": "short_answer",
            "question": "Calculate 8 × 7 × 3.",
            "correct_answer": "168",
        }
        self.assertIsNone(cross_check_question(q))

    def test_ascii_asterisk_works(self):
        q = {
            "question_type": "short_answer",
            "question": "Calculate 4 * 5 * 2.",
            "correct_answer": "30",  # wrong, 40
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["computed"], 40.0)


# ============================================================================
# Pattern D — simple linear equation
# ============================================================================


class TestPatternD_Linear(unittest.TestCase):
    def test_x_plus_a_equals_b(self):
        q = {
            "question_type": "short_answer",
            "question": "Solve x + 5 = 12.",
            "correct_answer": "17",  # wrong, 7
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["pattern"], "linear")
        self.assertEqual(audit["computed"], 7.0)

    def test_x_minus_a_equals_b(self):
        q = {
            "question_type": "short_answer",
            "question": "Solve x - 3 = 10.",
            "correct_answer": "7",  # wrong, 13
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["computed"], 13.0)

    def test_a_plus_x_equals_b(self):
        q = {
            "question_type": "short_answer",
            "question": "Solve 5 + x = 12.",
            "correct_answer": "5",  # wrong, 7
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["computed"], 7.0)

    def test_a_times_x_equals_b(self):
        q = {
            "question_type": "short_answer",
            "question": "Solve 3 × x = 21.",
            "correct_answer": "5",  # wrong, 7
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["computed"], 7.0)

    def test_correct_linear_passes(self):
        q = {
            "question_type": "short_answer",
            "question": "Solve x + 5 = 12.",
            "correct_answer": "7",
        }
        self.assertIsNone(cross_check_question(q))

    def test_arbitrary_var_letter(self):
        # Should work with any single letter, not just 'x'.
        q = {
            "question_type": "short_answer",
            "question": "Solve y - 4 = 10.",
            "correct_answer": "6",  # wrong, 14
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["computed"], 14.0)


# ============================================================================
# Out-of-pattern — word problems pass through
# ============================================================================


class TestOutOfPattern(unittest.TestCase):
    def test_word_problem_passes_through(self):
        q = {
            "question_type": "short_answer",
            "question": (
                "Pierre has 15 mangoes and gives away 7. "
                "How many remain?"
            ),
            "correct_answer": "8",  # actually right — but unverifiable
        }
        # Pattern parser doesn't fire — None means "we couldn't verify".
        self.assertIsNone(cross_check_question(q))

    def test_geometry_word_problem_unverified(self):
        q = {
            "question_type": "short_answer",
            "question": (
                "Three angles around a point are 95°, 70°, and 110°. "
                "Find x."
            ),
            "correct_answer": "85",
        }
        # No `+` connectors and no equation — unverifiable in v1.
        # (Layer 4 parametric templates close this gap.)
        self.assertIsNone(cross_check_question(q))

    def test_fill_in_blank_uses_pattern_a(self):
        # Sum-with-blank → Pattern A path (existing
        # verify_fill_in_blank logic). cross_check_question
        # delegates and reports a sum_with_blank audit on mismatch.
        q = {
            "question_type": "fill_in_blank",
            "question": "85° + 92° + 78° + ___ = 360°",
            "answer_data": {"blanks": [85]},   # wrong, 105
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["pattern"], "sum_with_blank")


# ============================================================================
# Tolerance + numeric edges
# ============================================================================


class TestTolerance(unittest.TestCase):
    def test_half_unit_tolerance_passes(self):
        # 275.3 vs 275 — within 0.5 tolerance.
        q = {
            "question_type": "short_answer",
            "question": "What is 95 + 70 + 110?",
            "correct_answer": "275.3",
        }
        self.assertIsNone(cross_check_question(q))

    def test_more_than_half_unit_caught(self):
        q = {
            "question_type": "short_answer",
            "question": "What is 95 + 70 + 110?",
            "correct_answer": "275.6",  # 0.6 off — flagged
        }
        audit = cross_check_question(q)
        self.assertIsNotNone(audit)


# ============================================================================
# is_broken integration
# ============================================================================


class TestIsBrokenIntegration(unittest.TestCase):
    def test_layer2_failure_drops_question(self):
        q = {
            "question_type": "short_answer",
            "question": "What is 30 + 40 + 50?",
            "correct_answer": "100",  # wrong, 120
        }
        reason = is_broken(q)
        self.assertIsNotNone(reason)
        self.assertIn("computes 120", reason)

    def test_correct_question_survives(self):
        q = {
            "question_type": "short_answer",
            "question": "What is 30 + 40 + 50?",
            "correct_answer": "120",
        }
        self.assertIsNone(is_broken(q))


if __name__ == "__main__":
    unittest.main()
