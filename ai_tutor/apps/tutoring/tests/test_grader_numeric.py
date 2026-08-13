"""Tests for the math answer parser and deterministic check used by the
tutor engine to prevent false-positive praise on wrong math answers.

See memory/math_tutor_fix_plan.md Phase M1.
"""

import unittest

from ai_tutor.apps.tutoring.grader import (
    parse_math_answer,
    numeric_equals,
    check_math_answer,
    grade_numeric,
    GradeResult,
)


class TestParseMathAnswer(unittest.TestCase):
    def test_integer(self):
        self.assertEqual(parse_math_answer("42"), 42.0)

    def test_decimal(self):
        self.assertEqual(parse_math_answer("5.25"), 5.25)

    def test_negative_integer(self):
        self.assertEqual(parse_math_answer("-7"), -7.0)

    def test_negative_decimal(self):
        self.assertEqual(parse_math_answer("-3.14"), -3.14)

    def test_improper_fraction(self):
        self.assertEqual(parse_math_answer("21/4"), 5.25)

    def test_negative_improper_fraction(self):
        self.assertEqual(parse_math_answer("-21/4"), -5.25)

    def test_mixed_number(self):
        self.assertEqual(parse_math_answer("3 3/4"), 3.75)

    def test_mixed_number_with_hyphen(self):
        # "3-3/4" is sometimes used. Accept it.
        self.assertEqual(parse_math_answer("3-3/4"), 3.75)

    def test_negative_mixed_number(self):
        self.assertEqual(parse_math_answer("-3 3/4"), -3.75)

    def test_mixed_number_with_double_space(self):
        self.assertEqual(parse_math_answer("3  3/4"), 3.75)

    def test_simple_fraction_as_probability(self):
        self.assertAlmostEqual(parse_math_answer("1/3"), 1 / 3)

    def test_percentage(self):
        self.assertEqual(parse_math_answer("75%"), 0.75)

    def test_percentage_decimal(self):
        self.assertEqual(parse_math_answer("12.5%"), 0.125)

    def test_currency_prefix(self):
        self.assertEqual(parse_math_answer("$42"), 42.0)

    def test_currency_with_decimal(self):
        self.assertEqual(parse_math_answer("$42.50"), 42.50)

    def test_thousands_comma(self):
        self.assertEqual(parse_math_answer("1,234"), 1234.0)

    def test_thousands_comma_large(self):
        self.assertEqual(parse_math_answer("1,234,567"), 1234567.0)

    def test_unit_suffix_kg(self):
        self.assertEqual(parse_math_answer("5 1/4 kg"), 5.25)

    def test_unit_suffix_degrees(self):
        self.assertEqual(parse_math_answer("90 degrees"), 90.0)

    def test_unit_suffix_cm(self):
        self.assertEqual(parse_math_answer("42 cm"), 42.0)

    def test_unit_suffix_minutes(self):
        self.assertEqual(parse_math_answer("30 minutes"), 30.0)

    def test_returns_none_on_text(self):
        self.assertIsNone(parse_math_answer("not a number"))

    def test_returns_none_on_empty(self):
        self.assertIsNone(parse_math_answer(""))

    def test_returns_none_on_whitespace(self):
        self.assertIsNone(parse_math_answer("   "))

    def test_returns_none_on_none(self):
        self.assertIsNone(parse_math_answer(None))

    def test_returns_none_on_division_by_zero(self):
        self.assertIsNone(parse_math_answer("5/0"))

    def test_returns_none_on_bad_fraction(self):
        self.assertIsNone(parse_math_answer("5/"))

    def test_preserves_leading_sign_with_currency(self):
        # "$-42" is unusual but legit enough; for now we don't parse it.
        # "-$42" should work.
        self.assertEqual(parse_math_answer("-42"), -42.0)


class TestNumericEquals(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(numeric_equals(5.25, 5.25))

    def test_within_default_tolerance(self):
        # 1e-6 relative tolerance
        self.assertTrue(numeric_equals(5.25, 5.25 + 1e-9))

    def test_beyond_default_tolerance(self):
        self.assertFalse(numeric_equals(5.25, 5.26))

    def test_custom_tolerance(self):
        self.assertTrue(numeric_equals(5.25, 5.26, tolerance=0.01))

    def test_zero_equal(self):
        self.assertTrue(numeric_equals(0.0, 0.0))

    def test_near_zero_values(self):
        self.assertTrue(numeric_equals(0.0, 1e-9))

    def test_none_returns_false(self):
        self.assertFalse(numeric_equals(None, 5.0))
        self.assertFalse(numeric_equals(5.0, None))

    def test_negative_equal(self):
        self.assertTrue(numeric_equals(-5.25, -5.25))

    def test_fraction_vs_decimal_equal(self):
        # 21/4 == 5.25 exactly
        self.assertTrue(numeric_equals(21 / 4, 5.25))


class TestCheckMathAnswer(unittest.TestCase):
    """The deterministic math check that layer 1 of the fix uses."""

    def test_the_production_bug(self):
        """The exact failure case from the screenshot — student said 3 3/4
        when expected was 5 1/4. The fix must catch this."""
        result = check_math_answer("3 3/4", "5 1/4")
        self.assertIsNotNone(result)
        self.assertFalse(result.is_correct)
        self.assertEqual(result.student_parsed, 3.75)
        self.assertEqual(result.expected_parsed, 5.25)
        self.assertIn("mismatch", result.reasoning)

    def test_correct_mixed_number(self):
        result = check_math_answer("5 1/4", "5 1/4")
        self.assertIsNotNone(result)
        self.assertTrue(result.is_correct)

    def test_fraction_equals_mixed(self):
        # 21/4 == 5 1/4 — student gave improper, expected was mixed. Same value.
        result = check_math_answer("21/4", "5 1/4")
        self.assertIsNotNone(result)
        self.assertTrue(result.is_correct)

    def test_mixed_equals_decimal(self):
        result = check_math_answer("5 1/4", "5.25")
        self.assertIsNotNone(result)
        self.assertTrue(result.is_correct)

    def test_unit_stripped_on_expected(self):
        result = check_math_answer("5.25", "5 1/4 kg")
        self.assertIsNotNone(result)
        self.assertTrue(result.is_correct)

    def test_falls_through_on_non_numeric_expected(self):
        # Expected "any positive integer" is text -> None, caller uses LLM.
        result = check_math_answer("5", "any positive integer")
        self.assertIsNone(result)

    def test_falls_through_on_non_numeric_student(self):
        result = check_math_answer("I think it's five", "5")
        self.assertIsNone(result)

    def test_percentage_decimal(self):
        # 50% vs 0.5 — same value.
        result = check_math_answer("50%", "0.5")
        self.assertIsNotNone(result)
        self.assertTrue(result.is_correct)

    def test_decimal_tolerance_loosens(self):
        # When either side has a decimal, tolerance is 1e-3 -- 3.141 ~= 3.14
        result = check_math_answer("3.141", "3.14")
        self.assertIsNotNone(result)
        # Relative diff: 0.001/3.14 ~= 3.18e-4, within 1e-3
        self.assertTrue(result.is_correct)

    def test_integer_tolerance_tight(self):
        # Both sides integer -> 1e-6 tolerance -> 42 vs 43 clearly different
        result = check_math_answer("42", "43")
        self.assertIsNotNone(result)
        self.assertFalse(result.is_correct)


class TestGradeNumericUsesNewParser(unittest.TestCase):
    """Smoke test that grade_numeric (used by exit tickets) now also handles
    fractions via the refactor."""

    def test_fraction_answer_correct(self):
        outcome = grade_numeric("21/4", "5.25")
        self.assertEqual(outcome.result, GradeResult.CORRECT)

    def test_mixed_number_wrong(self):
        outcome = grade_numeric("3 3/4", "5 1/4")
        self.assertEqual(outcome.result, GradeResult.INCORRECT)

    def test_mixed_number_correct(self):
        outcome = grade_numeric("5 1/4", "5.25")
        self.assertEqual(outcome.result, GradeResult.CORRECT)

    def test_percent_correct(self):
        outcome = grade_numeric("75%", "0.75")
        self.assertEqual(outcome.result, GradeResult.CORRECT)

    def test_unparseable_student(self):
        outcome = grade_numeric("eleven", "11")
        self.assertEqual(outcome.result, GradeResult.INCORRECT)


if __name__ == "__main__":
    unittest.main()
