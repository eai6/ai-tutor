"""M4 acceptance tests — fill_in_blank deterministic grader.

Real production shape (geography-heavy):
    answer_data = {
        'blanks': ['education'],
        'text_template': 'The three main components of the HDI are health,
            ___, and standard of living.',
        'accept_alternatives': [['Education', 'EDUCATION', 'school']],
    }

Multi-blank example:
    answer_data = {
        'blanks': ['corrosion', 'attrition'],
        'text_template': '... corrasion, hydraulic action, ___, and ___.',
        'accept_alternatives': [
            ['corrosion', 'CORROSION'],
            ['attrition', 'ATTRITION'],
        ],
    }

See memory/simple_tutor_engine_milestones.md (M4).
"""
from types import SimpleNamespace
from unittest import TestCase

from ai_tutor.apps.tutoring.simple_tutor.grader import (
    Verdict,
    grade_answer,
    _grade_fill_in_blank,
    _parse_blank_list,
    _blank_matches,
)


def _fib(*, blanks, alternatives=None, computed=None):
    """Fill-in-blank question stand-in."""
    ad = {'blanks': blanks}
    if alternatives is not None:
        ad['accept_alternatives'] = alternatives
    if computed is not None:
        ad['computed'] = computed
    return SimpleNamespace(
        pk=77,
        question_type='fill_in_blank',
        question_text='Complete: ___',
        correct_answer='',
        answer_data=ad,
    )


# ============================================================================
# Single-blank happy paths
# ============================================================================


class SingleBlankTest(TestCase):
    def test_exact_match(self):
        q = _fib(blanks=['education'])
        r = _grade_fill_in_blank(q, 'education')
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertEqual(r.tier, 'fill_blank')

    def test_case_insensitive(self):
        q = _fib(blanks=['education'])
        r = _grade_fill_in_blank(q, 'Education')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_uppercase(self):
        q = _fib(blanks=['education'])
        r = _grade_fill_in_blank(q, 'EDUCATION')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_with_whitespace(self):
        q = _fib(blanks=['education'])
        r = _grade_fill_in_blank(q, '  education  ')
        self.assertEqual(r.verdict, Verdict.CORRECT)


class AlternativesTest(TestCase):
    def test_accepts_alternative(self):
        q = _fib(blanks=['erosion'], alternatives=[['Erosion', 'The wearing away']])
        r = _grade_fill_in_blank(q, 'The wearing away')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_alternative_case_insensitive(self):
        q = _fib(blanks=['less'], alternatives=[['less', 'lower']])
        r = _grade_fill_in_blank(q, 'LOWER')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_wrong_answer_not_in_alternatives(self):
        q = _fib(blanks=['education'], alternatives=[['Education', 'school']])
        r = _grade_fill_in_blank(q, 'health')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Multi-blank questions
# ============================================================================


class MultiBlankTest(TestCase):
    """Real example: 'corrasion, hydraulic action, ___, and ___' →
    ['corrosion', 'attrition']
    """

    def test_all_correct_comma_separated(self):
        q = _fib(blanks=['corrosion', 'attrition'])
        r = _grade_fill_in_blank(q, 'corrosion, attrition')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_all_correct_list_input(self):
        # When posted as JSON, input is a list
        q = _fib(blanks=['corrosion', 'attrition'])
        r = _grade_fill_in_blank(q, ['corrosion', 'attrition'])
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_all_correct_newline_separated(self):
        q = _fib(blanks=['corrosion', 'attrition'])
        r = _grade_fill_in_blank(q, 'corrosion\nattrition')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_partial_credit(self):
        q = _fib(blanks=['corrosion', 'attrition'])
        r = _grade_fill_in_blank(q, 'corrosion, abrasion')   # 2nd wrong
        self.assertEqual(r.verdict, Verdict.PARTIAL)
        self.assertEqual(r.per_criterion_scores['blank_0'], 1.0)
        self.assertEqual(r.per_criterion_scores['blank_1'], 0.0)
        self.assertTrue(r.needs_followup)

    def test_none_correct(self):
        q = _fib(blanks=['corrosion', 'attrition'])
        r = _grade_fill_in_blank(q, 'abrasion, plucking')
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_wrong_blank_count_falls_back_to_containment(self):
        """When the student supplies one blank but two were expected,
        the grader falls back to containment matching — one match
        scores PARTIAL credit rather than INCORRECT. Lets free-form
        prose answers ("1000 metres or 1 km") get partial credit
        instead of a blanket fail when the slot count is wrong.
        """
        q = _fib(blanks=['corrosion', 'attrition'])
        r = _grade_fill_in_blank(q, 'corrosion')
        self.assertEqual(r.verdict, Verdict.PARTIAL)
        self.assertIn('1/2', r.justification)

    def test_all_blanks_appear_in_prose_answer(self):
        """Containment fallback: student writes both expected values in
        a single string instead of slot-separated form → CORRECT.
        """
        q = _fib(blanks=['1000', '1'])
        r = _grade_fill_in_blank(q, '1000 metres or 1 km')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_three_blank_with_alts(self):
        # Real-shape example: waves/winds/currents
        q = _fib(
            blanks=['Waves', 'winds', 'currents'],
            alternatives=[
                ['wave action', 'Wave action'],
                ['wind', 'Wind'],
                ['ocean currents', 'Ocean currents', 'currents'],
            ],
        )
        r = _grade_fill_in_blank(q, 'wave action, wind, ocean currents')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Edge cases
# ============================================================================


class FillBlankEdgesTest(TestCase):
    def test_empty_student_answer(self):
        q = _fib(blanks=['education'])
        r = _grade_fill_in_blank(q, '')
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_none_input(self):
        q = _fib(blanks=['education'])
        r = _grade_fill_in_blank(q, None)
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_malformed_question_raises(self):
        q = SimpleNamespace(
            pk=1, question_type='fill_in_blank', answer_data={},  # no 'blanks'
        )
        with self.assertRaisesRegex(ValueError, 'blanks'):
            _grade_fill_in_blank(q, 'education')


# ============================================================================
# Math fill_in_blank → routes to math grader (via dispatcher)
# ============================================================================


class MathFillBlankRoutingTest(TestCase):
    """When fill_in_blank has 'computed' in answer_data, it's a math
    question and should route through the math grader (not fill_blank).
    """

    def test_math_fill_blank_routes_to_math_grader(self):
        # Real shape: "Simple interest on SCR 6000 at 3% for 1 year is ___."
        # answer_data = {'blanks': ['180 SCR'], 'computed': [180.0], ...}
        q = SimpleNamespace(
            pk=2,
            question_type='fill_in_blank',
            question_text='...',
            correct_answer='',
            answer_data={
                'blanks': ['180 SCR'],
                'computed': [180.0],
                'model_answer': '180 SCR',
            },
        )
        r = grade_answer(question=q, student_answer='180')
        # Routed to math grader — accepts "180" against computed=180.0
        self.assertEqual(r.tier, 'math')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Helper unit tests
# ============================================================================


class ParseBlankListTest(TestCase):
    def test_passthrough_list(self):
        self.assertEqual(_parse_blank_list(['a', 'b']), ['a', 'b'])

    def test_strips_list_items(self):
        self.assertEqual(_parse_blank_list(['  a  ', ' b ']), ['a', 'b'])

    def test_comma_separated(self):
        self.assertEqual(_parse_blank_list('a, b, c'), ['a', 'b', 'c'])

    def test_newline_separated(self):
        self.assertEqual(_parse_blank_list('a\nb\nc'), ['a', 'b', 'c'])

    def test_newline_beats_comma(self):
        # "a, b\nc, d" → split on newline first
        self.assertEqual(_parse_blank_list('a, b\nc, d'), ['a, b', 'c, d'])

    def test_single_string(self):
        self.assertEqual(_parse_blank_list('hello'), ['hello'])

    def test_empty(self):
        self.assertEqual(_parse_blank_list(''), [])
        self.assertEqual(_parse_blank_list(None), [])
        self.assertEqual(_parse_blank_list([]), [])


class BlankMatchesTest(TestCase):
    def test_exact_match(self):
        self.assertTrue(_blank_matches('education', 'education', []))

    def test_case_insensitive(self):
        self.assertTrue(_blank_matches('EDUCATION', 'education', []))

    def test_whitespace_stripped(self):
        self.assertTrue(_blank_matches('  education  ', 'education', []))

    def test_alternative_match(self):
        self.assertTrue(_blank_matches('school', 'education', ['school']))

    def test_no_match(self):
        self.assertFalse(_blank_matches('health', 'education', ['school']))

    def test_empty_given_no_match(self):
        self.assertFalse(_blank_matches('', 'education', []))
        self.assertFalse(_blank_matches('   ', 'education', []))
