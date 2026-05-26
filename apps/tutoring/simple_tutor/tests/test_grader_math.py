"""M3 acceptance tests — Tier-1 math grader.

Tests against the REAL production shape for short_numeric / math questions:
    question.answer_data = {
        'unit': '°',                    # informational suffix
        'computed': 150.0,              # raw float — used by tolerance fallback
        'model_answer': '150°',         # formatted string — used by math-verify
        'keywords': ['150'],            # legacy alt forms (currently unused)
        'parameters': {...},            # source parameters
    }

Real samples queried from staging on 2026-05-26 (498-question prod corpus):
    angles, currency (SCR), area (cm²/m²), algebra coefficients, probability,
    quadratic expansions, profit/loss, trade calculations.

Also covers multi-line student working — students often show their steps
through an algebra problem before stating the final answer. math-verify
handles this natively; tests assert it.

See memory/simple_tutor_engine_milestones.md (M3).
"""
from types import SimpleNamespace
from unittest import TestCase

from apps.tutoring.simple_tutor.grader import (
    GradeResult,
    Verdict,
    grade_answer,
    _grade_math,
    _extract_last_number,
    _spoken_to_numeric,
    _sympy_symbolic_equal,
)


def _math_q(*, model_answer: str = '', computed=None, unit: str = '',
            correct_answer: str = '', question_type: str = 'short_numeric'):
    """Build a math question stand-in mirroring production shape."""
    answer_data = {}
    if model_answer:
        answer_data['model_answer'] = model_answer
    if computed is not None:
        answer_data['computed'] = computed
    if unit:
        answer_data['unit'] = unit
    return SimpleNamespace(
        pk=99,
        question_type=question_type,
        question_text='Compute something.',
        correct_answer=correct_answer,
        answer_data=answer_data,
    )


# ============================================================================
# Production sample: angles (most common math question type)
# ============================================================================


class AngleQuestionsTest(TestCase):
    """Real shape: 'Two angles around a point are 100° and 110°. Find y.'
    answer_data = {'unit': '°', 'computed': 150.0, 'model_answer': '150°'}
    """

    def test_bare_number_matches_unit_answer(self):
        q = _math_q(model_answer='150°', computed=150.0, unit='°')
        r = _grade_math(q, '150')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_with_degree_symbol(self):
        q = _math_q(model_answer='150°', computed=150.0, unit='°')
        r = _grade_math(q, '150°')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_with_deg_word(self):
        q = _math_q(model_answer='150°', computed=150.0, unit='°')
        r = _grade_math(q, '150 degrees')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_wrong_angle(self):
        q = _math_q(model_answer='150°', computed=150.0, unit='°')
        r = _grade_math(q, '140')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Production sample: currency
# ============================================================================


class CurrencyQuestionsTest(TestCase):
    """Real shape: 'Fisherman buys net for SCR 300, sells for 320. Profit?'
    answer_data = {'unit': 'SCR', 'computed': 20.0, 'model_answer': '20 SCR'}
    """

    def test_bare_number_currency(self):
        q = _math_q(model_answer='20 SCR', computed=20.0, unit='SCR')
        r = _grade_math(q, '20')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_with_currency_suffix(self):
        q = _math_q(model_answer='20 SCR', computed=20.0, unit='SCR')
        r = _grade_math(q, '20 SCR')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_simple_interest_calc(self):
        """SCR 6000 at 3% for 1 year → SCR 180."""
        q = _math_q(model_answer='180 SCR', computed=180.0, unit='SCR')
        r = _grade_math(q, '180')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Production sample: area
# ============================================================================


class AreaQuestionsTest(TestCase):
    """Real shape: 'A trapezium with parallel sides 12 cm and 4 cm and
    height 4 cm... split into two triangles.'
    answer_data = {'unit': 'cm²', 'computed': 32.0, 'model_answer': '32 cm²'}
    """

    def test_bare_area(self):
        q = _math_q(model_answer='32 cm²', computed=32.0, unit='cm²')
        r = _grade_math(q, '32')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_garden_area_m2(self):
        q = _math_q(model_answer='64 m²', computed=64.0, unit='m²')
        for student in ('64', '64 m²', '64m²'):
            with self.subTest(student=student):
                r = _grade_math(q, student)
                self.assertEqual(r.verdict, Verdict.CORRECT,
                                 f"{student!r} should match 64 m²")


# ============================================================================
# Number-representation equivalence (fraction / decimal / percent)
# ============================================================================


class NumberRepresentationsTest(TestCase):
    """math-verify handles fraction = decimal = percent natively."""

    def test_decimal_matches_fraction_ref(self):
        # 'Convert 4/6 to a decimal' → 0.666667
        q = _math_q(model_answer='0.666667', computed=0.6666666666666666)
        r = _grade_math(q, '2/3')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_percent_matches_decimal_ref(self):
        q = _math_q(model_answer='0.5', computed=0.5)
        r = _grade_math(q, '50%')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_fraction_matches_fraction_ref(self):
        q = _math_q(model_answer='1/2', computed=0.5)
        r = _grade_math(q, '0.5')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Algebra — symbolic equivalence
# ============================================================================


class AlgebraicEquivalenceTest(TestCase):
    """For algebra questions where the answer is an expression, not a number."""

    def test_factored_form(self):
        q = _math_q(model_answer='2(x+1)', computed=None)
        r = _grade_math(q, '2x + 2')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_expanded_form(self):
        q = _math_q(model_answer='2x + 2', computed=None)
        r = _grade_math(q, '2(x+1)')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_wrong_algebra(self):
        q = _math_q(model_answer='x + 1', computed=None)
        r = _grade_math(q, 'x + 2')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Multi-line student working
# ============================================================================


class MultiLineWorkingTest(TestCase):
    """Students often show steps before the final answer. math-verify
    handles this natively — extracts the final answer from the working.
    """

    def test_trapezium_working_with_units(self):
        """Real example: student shows full trapezium-area calc."""
        q = _math_q(model_answer='32 cm²', computed=32.0, unit='cm²')
        student = (
            "Area of trapezium = (1/2)(a+b)h\n"
            "= (1/2)(12+4)(4)\n"
            "= (1/2)(16)(4)\n"
            "= 32 cm²"
        )
        r = _grade_math(q, student)
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_algebra_working_with_x_equals(self):
        """Student solves a linear equation, states final x value."""
        q = _math_q(model_answer='-1', computed=-1.0)
        student = (
            "5x + 5 = 1x + 1\n"
            "5x - 1x = 1 - 5\n"
            "4x = -4\n"
            "x = -1"
        )
        r = _grade_math(q, student)
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_multiline_wrong_final_rejected(self):
        """Multi-line WRONG answer must still be INCORRECT — the right
        intermediate value in the working does NOT save a wrong final.
        Defends against false-positives when the student writes "180 - 80 = 110"
        but the correct answer is 100.
        """
        q = _math_q(model_answer='100°', computed=100.0, unit='°')
        student = (
            "Two angles on straight line = 180°\n"
            "One is 80°\n"
            "180 - 80 = 110\n"
            "So the other is 110°"
        )
        r = _grade_math(q, student)
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Numeric tolerance fallback
# ============================================================================


class NumericToleranceFallbackTest(TestCase):
    """math-verify is precision-strict (0.6667 ≠ 0.6666666...) so we fall
    back to numeric tolerance of 0.01 against ``computed``.
    """

    def test_rounded_decimal_matches_computed(self):
        q = _math_q(model_answer='0.666667', computed=0.6666666666666666)
        r = _grade_math(q, '0.67')
        # math-verify says no, fallback should accept (|0.67 - 0.666...| < 0.01)
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertIn('tolerance', r.justification)
        self.assertEqual(r.confidence, 0.95)

    def test_too_imprecise_rejected(self):
        q = _math_q(model_answer='0.666667', computed=0.6666666666666666)
        # 0.6 vs 0.6666... → |diff| = 0.066 > tolerance
        r = _grade_math(q, '0.6')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Empty / garbage / unparseable
# ============================================================================


class EmptyAndGarbageTest(TestCase):
    def test_empty_string(self):
        q = _math_q(model_answer='42', computed=42.0)
        r = _grade_math(q, '')
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertEqual(r.confidence, 1.0)
        self.assertIn('empty', r.justification.lower())

    def test_whitespace_only(self):
        q = _math_q(model_answer='42', computed=42.0)
        r = _grade_math(q, '   \n  ')
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_garbage_text(self):
        q = _math_q(model_answer='42', computed=42.0)
        r = _grade_math(q, "I don't know how to do this")
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_off_topic_response(self):
        q = _math_q(model_answer='32', computed=32.0)
        r = _grade_math(q, 'The answer to that question is 42')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Defensive errors
# ============================================================================


class MalformedQuestionTest(TestCase):
    def test_no_answer_data_no_correct_answer_raises(self):
        q = _math_q()  # empty answer_data, empty correct_answer
        with self.assertRaisesRegex(ValueError, 'no model_answer'):
            _grade_math(q, '42')

    def test_correct_answer_fallback_when_no_answer_data(self):
        # Legacy / sparse question: only correct_answer is set
        q = _math_q(correct_answer='42')
        r = _grade_math(q, '42')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Algebra OPERATIONS — factoring, expansion, simplification
# ============================================================================


class AlgebraFactoringTest(TestCase):
    """Factoring is the inverse of expansion — both forms must grade
    equivalent. math-verify alone is too strict; sympy.simplify(a-b)==0
    via latex2sympy is the fallback that closes this gap.
    """

    def test_difference_of_squares_factor_to_expanded(self):
        # Q: "Factorise x² - 100" → "(x+10)(x-10)"
        q = _math_q(model_answer='(x+10)(x-10)', computed=None)
        # Student wrote the EXPANDED form — equivalent
        r = _grade_math(q, 'x^2 - 100')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_difference_of_squares_expanded_to_factor(self):
        q = _math_q(model_answer='x^2 - 100', computed=None)
        r = _grade_math(q, '(x+10)(x-10)')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_factor_order_independent(self):
        # (x+10)(x-10) == (x-10)(x+10) — commutative
        q = _math_q(model_answer='(x+10)(x-10)', computed=None)
        r = _grade_math(q, '(x-10)(x+10)')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_factor_trinomial(self):
        # x²+3x+2 = (x+1)(x+2)
        q = _math_q(model_answer='(x+1)(x+2)', computed=None)
        r = _grade_math(q, 'x^2 + 3x + 2')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_factor_common(self):
        # 2x+6 = 2(x+3)
        q = _math_q(model_answer='2(x+3)', computed=None)
        r = _grade_math(q, '2x + 6')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_wrong_factor(self):
        q = _math_q(model_answer='(x+5)(x-5)', computed=None)
        r = _grade_math(q, '(x+3)(x-3)')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


class AlgebraExpansionTest(TestCase):
    """Expansion / multiplying out."""

    def test_expand_simple(self):
        q = _math_q(model_answer='2x + 2', computed=None)
        r = _grade_math(q, '2(x+1)')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_expand_diff_squares(self):
        q = _math_q(model_answer='x^2 - 4', computed=None)
        r = _grade_math(q, '(x+2)(x-2)')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_expand_square(self):
        # (x+1)² = x² + 2x + 1
        q = _math_q(model_answer='x^2 + 2x + 1', computed=None)
        r = _grade_math(q, '(x+1)^2')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_multiply_polynomials(self):
        q = _math_q(model_answer='x^2 + 5x + 6', computed=None)
        r = _grade_math(q, '(x+2)(x+3)')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_wrong_expansion(self):
        q = _math_q(model_answer='x^2 + 5x + 6', computed=None)
        r = _grade_math(q, '(x+2)(x+4)')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


class AlgebraicArithmeticTest(TestCase):
    """Adding/subtracting expressions."""

    def test_add_expressions(self):
        q = _math_q(model_answer='2x + 4', computed=None)
        r = _grade_math(q, '(x+1) + (x+3)')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_subtract_expressions(self):
        q = _math_q(model_answer='x - 1', computed=None)
        r = _grade_math(q, '(2x+3) - (x+4)')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Angle COMPUTATIONS — student showing arithmetic
# ============================================================================


class AngleComputationsTest(TestCase):
    """Angle problems where the student computes the answer arithmetically
    rather than just typing the final number. math-verify evaluates the
    expression and matches it against the reference.
    """

    def test_complement_computed(self):
        # 90° - 30° = 60°
        q = _math_q(model_answer='60°', computed=60.0, unit='°')
        r = _grade_math(q, '90 - 30')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_supplement_computed(self):
        # 180° - 75° = 105°
        q = _math_q(model_answer='105°', computed=105.0, unit='°')
        r = _grade_math(q, '180 - 75')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_angles_around_point_division(self):
        # 360° / 5 = 72°
        q = _math_q(model_answer='72°', computed=72.0, unit='°')
        r = _grade_math(q, '360/5')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_triangle_interior_sum(self):
        # 60° + 60° + 60° = 180° (equilateral)
        q = _math_q(model_answer='180°', computed=180.0, unit='°')
        r = _grade_math(q, '60 + 60 + 60')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_quad_interior_sum(self):
        # Sum of interior angles of a quadrilateral
        q = _math_q(model_answer='360°', computed=360.0, unit='°')
        r = _grade_math(q, '4 * 90')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_pentagon_interior_each(self):
        # (n-2) * 180 / n for n=5 → 108
        q = _math_q(model_answer='108°', computed=108.0, unit='°')
        r = _grade_math(q, '(5-2)*180/5')
        self.assertEqual(r.verdict, Verdict.CORRECT)


class AngleMultiLineWorkingTest(TestCase):
    """Students show the full derivation. Common pattern in angle problems."""

    def test_regular_polygon_interior_working(self):
        q = _math_q(model_answer='108', computed=108.0)
        student = (
            "Sum of interior angles = (n-2) * 180\n"
            "= (5-2) * 180\n"
            "= 540\n"
            "Each angle = 540/5 = 108"
        )
        r = _grade_math(q, student)
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_co_interior_angle_working(self):
        # Two parallel lines + transversal — co-interior angles sum to 180°
        q = _math_q(model_answer='100°', computed=100.0, unit='°')
        student = (
            "Co-interior angles sum to 180°\n"
            "180 - 80 = 100\n"
            "So the other co-interior angle is 100°"
        )
        r = _grade_math(q, student)
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_supplement_working_wrong(self):
        # WRONG: student computes "110" but reference is 100
        q = _math_q(model_answer='100°', computed=100.0, unit='°')
        student = (
            "Angles on straight line sum to 180\n"
            "180 - 80 = 110\n"   # arithmetic error
            "Answer: 110°"
        )
        r = _grade_math(q, student)
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# _sympy_symbolic_equal helper tests
# ============================================================================


class SympySymbolicEqualTest(TestCase):
    """Direct unit tests for the symbolic-equivalence helper."""

    def test_equivalent_returns_true(self):
        self.assertTrue(_sympy_symbolic_equal('(x+1)(x+2)', 'x^2+3x+2'))

    def test_non_equivalent_returns_false(self):
        self.assertFalse(_sympy_symbolic_equal('x+1', 'x+2'))

    def test_unparseable_returns_none_or_false(self):
        # Multi-line working can't be parsed as a single algebra expression.
        # latex2sympy may parse the first line OR return None — either way
        # the functional contract holds: caller doesn't get True.
        result = _sympy_symbolic_equal('(x+1)(x+2)', 'line one\nline two')
        self.assertNotEqual(result, True)

    def test_garbage_returns_non_true(self):
        # latex2sympy is permissive — "asdf qwerty" parses as the product
        # asdf*qwerty (symbols). That's mathematically ≠ the reference
        # expression, so simplify(a-b) != 0 and we get False (not None).
        # Either False or None is acceptable; True would be a bug.
        result = _sympy_symbolic_equal('(x+1)(x+2)', 'asdf qwerty')
        self.assertNotEqual(result, True)

    def test_factor_to_expanded(self):
        # difference of squares
        self.assertTrue(_sympy_symbolic_equal('(x+10)(x-10)', 'x^2 - 100'))

    def test_implicit_multiplication(self):
        # latex2sympy handles 2x → 2*x
        self.assertTrue(_sympy_symbolic_equal('2x + 6', '2(x+3)'))


# ============================================================================
# Quadratic / probability / other production samples
# ============================================================================


class ProductionSamplesTest(TestCase):
    """Spot-check questions taken verbatim from the staging DB."""

    def test_quadratic_coefficient(self):
        """'In 4x² + 8x + 21, what is the quadratic coefficient?' → 4"""
        q = _math_q(model_answer='4', computed=4.0)
        for student in ('4', '4.0', '4x²', '4 x²'):
            with self.subTest(student=student):
                r = _grade_math(q, student)
                # All should parse to "4" as the leading number
                self.assertEqual(r.verdict, Verdict.CORRECT,
                                 f"{student!r} should match 4")

    def test_function_machine_input(self):
        """Trivially asks for the input — students likely just type it."""
        q = _math_q(model_answer='12', computed=12.0)
        r = _grade_math(q, '12')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_circle_chord_distance(self):
        """'Diameter 12 cm, chord 10 cm. By how much is the chord shorter
        than the diameter?' → 2 cm"""
        q = _math_q(model_answer='2 cm', computed=2.0, unit='cm')
        r = _grade_math(q, '2')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_expand_y_y_plus_3_constant(self):
        """'Expand y(y+3). What is the constant term?' → 0"""
        q = _math_q(model_answer='0', computed=0.0)
        r = _grade_math(q, '0')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Dispatcher integration
# ============================================================================


class DispatcherTest(TestCase):
    def test_short_numeric_routes_to_math(self):
        q = _math_q(model_answer='150°', computed=150.0, unit='°',
                    question_type='short_numeric')
        r = grade_answer(question=q, student_answer='150')
        self.assertEqual(r.tier, 'math')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_math_qtype_also_routes(self):
        q = _math_q(model_answer='42', computed=42.0, question_type='math')
        r = grade_answer(question=q, student_answer='42')
        self.assertEqual(r.tier, 'math')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_numeric_qtype_also_routes(self):
        q = _math_q(model_answer='42', computed=42.0, question_type='numeric')
        r = grade_answer(question=q, student_answer='42')
        self.assertEqual(r.tier, 'math')


# ============================================================================
# Spoken-form answers (TTS/STT students)
# ============================================================================


class SpokenAnswersTest(TestCase):
    """Students using voice mode (ElevenLabs STT → Whisper) speak answers
    that come through as English word forms: 'twenty', 'one hundred and
    fifty degrees', 'two thirds'. Grader normalises before math-verify.
    """

    # --- Plain whole numbers --------------------------------------------

    def test_word_form_simple(self):
        q = _math_q(model_answer='20', computed=20.0)
        r = _grade_math(q, 'twenty')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_word_form_compound(self):
        q = _math_q(model_answer='35', computed=35.0)
        r = _grade_math(q, 'thirty five')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_word_form_with_and(self):
        # Common in spoken British English: "one hundred AND fifty"
        q = _math_q(model_answer='150', computed=150.0)
        r = _grade_math(q, 'one hundred and fifty')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_word_form_zero(self):
        q = _math_q(model_answer='0', computed=0.0)
        r = _grade_math(q, 'zero')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    # --- With units spoken aloud ----------------------------------------

    def test_word_form_with_degrees(self):
        q = _math_q(model_answer='150°', computed=150.0, unit='°')
        r = _grade_math(q, 'one hundred and fifty degrees')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_word_form_with_currency(self):
        q = _math_q(model_answer='20 SCR', computed=20.0, unit='SCR')
        r = _grade_math(q, 'twenty SCR')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_word_form_with_centimeters(self):
        q = _math_q(model_answer='64 cm²', computed=64.0, unit='cm²')
        r = _grade_math(q, 'sixty four centimeters squared')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    # --- Sign words ------------------------------------------------------

    def test_negative_word_form(self):
        q = _math_q(model_answer='-10', computed=-10.0)
        r = _grade_math(q, 'negative ten')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_minus_word_form(self):
        q = _math_q(model_answer='-17', computed=-17.0)
        r = _grade_math(q, 'minus seventeen')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_minus_with_decimal_via_dict_fraction(self):
        q = _math_q(model_answer='-0.5', computed=-0.5)
        r = _grade_math(q, 'negative one half')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    # --- Common spoken fractions ----------------------------------------

    def test_one_half_spoken(self):
        q = _math_q(model_answer='1/2', computed=0.5)
        r = _grade_math(q, 'one half')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_two_thirds_spoken(self):
        q = _math_q(model_answer='2/3', computed=2 / 3)
        r = _grade_math(q, 'two thirds')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_three_quarters_spoken(self):
        q = _math_q(model_answer='3/4', computed=0.75)
        r = _grade_math(q, 'three quarters')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_a_quarter_spoken(self):
        q = _math_q(model_answer='0.25', computed=0.25)
        r = _grade_math(q, 'a quarter')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    # --- Embedded numbers in noise text (LLM extraction failure mode) ---

    def test_answer_is_X_pattern(self):
        # If the tutor LLM mis-extracts and passes whole sentence in:
        q = _math_q(model_answer='42', computed=42.0)
        r = _grade_math(q, 'the answer is forty two')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_x_equals_X_pattern(self):
        q = _math_q(model_answer='5', computed=5.0)
        r = _grade_math(q, 'x equals five')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    # --- Wrong spoken answers stay wrong --------------------------------

    def test_wrong_spoken_number(self):
        q = _math_q(model_answer='150°', computed=150.0, unit='°')
        r = _grade_math(q, 'one hundred and forty')
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_wrong_spoken_fraction(self):
        q = _math_q(model_answer='2/3', computed=2 / 3)
        r = _grade_math(q, 'three quarters')   # ≠ 2/3
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# _spoken_to_numeric helper unit tests
# ============================================================================


class SpokenToNumericTest(TestCase):
    """Direct unit tests for the spoken→numeric preprocessor."""

    def test_passthrough_digits(self):
        # Idempotent on already-numeric input.
        self.assertEqual(_spoken_to_numeric('42'), '42')
        self.assertEqual(_spoken_to_numeric('0.5'), '0.5')

    def test_whole_word(self):
        self.assertEqual(_spoken_to_numeric('twenty'), '20')
        self.assertEqual(_spoken_to_numeric('one hundred'), '100')

    def test_negative_prefix_word(self):
        # word2number drops the sign; our preprocessor restores it.
        self.assertEqual(_spoken_to_numeric('negative ten'), '-10')
        self.assertEqual(_spoken_to_numeric('minus seventeen'), '-17')

    def test_fractions(self):
        self.assertEqual(_spoken_to_numeric('one half'), '1/2')
        self.assertEqual(_spoken_to_numeric('two thirds'), '2/3')
        self.assertEqual(_spoken_to_numeric('three quarters'), '3/4')

    def test_negative_fraction(self):
        self.assertEqual(_spoken_to_numeric('negative one half'), '-1/2')

    def test_empty(self):
        self.assertEqual(_spoken_to_numeric(''), '')

    def test_unparseable_passthrough(self):
        # No number at all — return original so math-verify still gets to try.
        result = _spoken_to_numeric('asdf qwerty')
        self.assertEqual(result, 'asdf qwerty')


# ============================================================================
# _extract_last_number helper
# ============================================================================


class ExtractLastNumberTest(TestCase):
    def test_bare_int(self):
        self.assertEqual(_extract_last_number('42'), 42.0)

    def test_bare_float(self):
        self.assertEqual(_extract_last_number('3.14'), 3.14)

    def test_negative(self):
        self.assertEqual(_extract_last_number('-7'), -7.0)

    def test_picks_last_when_multiple(self):
        # Multi-line working — last number is the answer
        self.assertEqual(_extract_last_number('5x = 10\nx = 2'), 2.0)

    def test_no_number(self):
        self.assertIsNone(_extract_last_number('I do not know'))

    def test_empty(self):
        self.assertIsNone(_extract_last_number(''))
        self.assertIsNone(_extract_last_number(None))  # type: ignore[arg-type]
