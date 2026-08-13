"""Tests for verify_calculations — the post-generation arithmetic
verifier that fixes wrong sums in tutor responses.

Production transcript bugs that motivated the N-term sum coverage:
  "60 + 80 + 75 + 70 + 75 = 220 ✓"   (5 terms — actually 360)
  "90 + 85 + 100 + 85 = 270 ✓"        (4 terms — actually 360)
  "60 + 80 + 75 + 70 = 225"           (4 terms — actually 285)
"""

import unittest

from ai_tutor.apps.tutoring.math_tools import verify_calculations


class TestVerifyCalculations(unittest.TestCase):
    def test_two_term_addition_correct(self):
        text, corrections = verify_calculations("3 + 4 = 7")
        self.assertEqual(corrections, [])
        self.assertEqual(text, "3 + 4 = 7")

    def test_two_term_addition_wrong_fixed(self):
        text, corrections = verify_calculations("3 + 4 = 8")
        self.assertEqual(len(corrections), 1)
        self.assertIn("3 + 4 = 7", text)

    def test_four_term_sum_wrong_fixed(self):
        """The transcript bug: tutor wrote `90 + 85 + 100 + 85 = 270`
        when the right total is 360."""
        text, corrections = verify_calculations("90 + 85 + 100 + 85 = 270")
        self.assertEqual(len(corrections), 1)
        self.assertIn("90 + 85 + 100 + 85 = 360", text)

    def test_five_term_sum_wrong_fixed(self):
        """The transcript bug: `60 + 80 + 75 + 70 + 75 = 220` is actually 360."""
        text, corrections = verify_calculations("60 + 80 + 75 + 70 + 75 = 220")
        self.assertEqual(len(corrections), 1)
        self.assertIn("60 + 80 + 75 + 70 + 75 = 360", text)

    def test_four_term_sum_correct_unchanged(self):
        """When the long sum is correct, it should pass through untouched."""
        text, corrections = verify_calculations("60 + 80 + 75 + 70 = 285")
        self.assertEqual(corrections, [])
        self.assertEqual(text, "60 + 80 + 75 + 70 = 285")

    def test_mixed_signs_n_term(self):
        """N-term verifier handles mixed + and - operators left-to-right."""
        text, corrections = verify_calculations("100 - 20 + 5 + 10 = 100")
        # 100 - 20 + 5 + 10 = 95, not 100
        self.assertEqual(len(corrections), 1)
        self.assertIn("= 95", text)

    def test_n_term_inside_prose(self):
        """Verifier should fix arithmetic embedded in narrative prose
        (this is how it appears in tutor worked examples)."""
        prose = "Step 5: Check 60 + 80 + 75 + 70 + 75 = 220 ✓ — looks good!"
        text, corrections = verify_calculations(prose)
        self.assertEqual(len(corrections), 1)
        self.assertIn("= 360", text)

    def test_three_term_chain_unchanged_when_correct(self):
        """Existing chain handler still works — regression guard."""
        text, corrections = verify_calculations("8 × 2.5 + 15 = 35")
        self.assertEqual(corrections, [])

    def test_three_term_chain_wrong_fixed(self):
        """Existing chain handler still works — regression guard."""
        text, corrections = verify_calculations("8 × 2.5 + 15 = 30")
        self.assertEqual(len(corrections), 1)
        self.assertIn("= 35", text)

    def test_no_arithmetic_passthrough(self):
        text, corrections = verify_calculations("Walk me through your steps.")
        self.assertEqual(corrections, [])
        self.assertEqual(text, "Walk me through your steps.")


if __name__ == "__main__":
    unittest.main()
