"""M2 acceptance tests — Tier-1 MCQ grader in
``apps.tutoring.simple_tutor.grader``.

See memory/simple_tutor_engine_milestones.md (M2).

These tests use lightweight ``SimpleNamespace`` stand-ins for
ExitTicketQuestion to keep them DB-free + fast. The grader only reads
``question_type``, ``correct_answer``, and ``option_{a,b,c,d}`` — those
attributes are all the public-API surface.
"""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest import TestCase

from apps.tutoring.simple_tutor.grader import (
    GradeResult,
    Verdict,
    grade_answer,
    _grade_mcq,
)


def _mcq(correct: str, options: dict | None = None) -> SimpleNamespace:
    """Build an MCQ-shaped question stand-in."""
    return SimpleNamespace(
        pk=42,
        question_type='mcq',
        question_text='Which is correct?',
        correct_answer=correct,
        option_a=(options or {}).get('A', 'Alpha'),
        option_b=(options or {}).get('B', 'Beta'),
        option_c=(options or {}).get('C', 'Gamma'),
        option_d=(options or {}).get('D', 'Delta'),
    )


# ============================================================================
# Happy paths
# ============================================================================


class ExactLetterMatchTest(TestCase):
    """Pre-extracted letter matches correct_answer."""

    def test_exact_uppercase(self):
        r = _grade_mcq(_mcq('B'), 'B')
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertEqual(r.confidence, 1.0)
        self.assertEqual(r.tier, 'mcq')

    def test_lowercase(self):
        r = _grade_mcq(_mcq('B'), 'b')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_with_period(self):
        r = _grade_mcq(_mcq('C'), 'C.')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_with_paren(self):
        r = _grade_mcq(_mcq('C'), 'C)')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_with_whitespace(self):
        r = _grade_mcq(_mcq('A'), '  A  ')
        self.assertEqual(r.verdict, Verdict.CORRECT)


class PrefixedFormsTest(TestCase):
    """LLM-extracted answers may carry a small prefix."""

    def test_option_letter(self):
        r = _grade_mcq(_mcq('B'), 'Option B')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_answer_colon_letter(self):
        r = _grade_mcq(_mcq('D'), 'Answer: D')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_lowercase_option(self):
        r = _grade_mcq(_mcq('A'), 'option a')
        self.assertEqual(r.verdict, Verdict.CORRECT)


class NumericFormsTest(TestCase):
    """Student typed a number instead of a letter (1=A, 2=B, ...)."""

    def test_bare_number(self):
        # "2" → "B"
        r = _grade_mcq(_mcq('B'), '2')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_option_number(self):
        r = _grade_mcq(_mcq('C'), 'Option 3')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_wrong_number(self):
        # "1" maps to "A" but correct is "B"
        r = _grade_mcq(_mcq('B'), '1')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


class FullOptionTextMatchTest(TestCase):
    """Student typed the option's full text instead of the letter."""

    def test_exact_text(self):
        q = _mcq('B', {'A': 'Imports', 'B': 'Exports'})
        r = _grade_mcq(q, 'Exports')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_case_insensitive(self):
        q = _mcq('B', {'A': 'Imports', 'B': 'Exports'})
        r = _grade_mcq(q, 'exports')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_wrong_text(self):
        q = _mcq('B', {'A': 'Imports', 'B': 'Exports'})
        r = _grade_mcq(q, 'Imports')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Wrong answers
# ============================================================================


class WrongLetterTest(TestCase):
    def test_wrong_uppercase(self):
        r = _grade_mcq(_mcq('B'), 'A')
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertEqual(r.confidence, 1.0)

    def test_wrong_lowercase(self):
        r = _grade_mcq(_mcq('B'), 'd')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Edge cases — empty, unparseable, multi-letter
# ============================================================================


class EmptyAnswerTest(TestCase):
    def test_empty_string(self):
        r = _grade_mcq(_mcq('B'), '')
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertEqual(r.confidence, 1.0)
        self.assertIn('empty', r.justification.lower())

    def test_whitespace_only(self):
        r = _grade_mcq(_mcq('B'), '   ')
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertIn('empty', r.justification.lower())


class UnparseableTest(TestCase):
    def test_unrelated_text(self):
        # "I don't know" — no A-D letter, no option text match → defensive
        # INCORRECT with lower confidence (signal that the engine may want
        # to treat this as confusion, not a definite wrong answer).
        r = _grade_mcq(_mcq('B'), "I don't know")
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertEqual(r.confidence, 0.6)
        self.assertIn('extractable', r.justification)

    def test_two_letters_in_text(self):
        # "I'm between A or B" — ambiguous. Should NOT match (more than
        # one letter present), low confidence.
        r = _grade_mcq(_mcq('B'), "I'm between A or B")
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertEqual(r.confidence, 0.6)

    def test_random_punctuation(self):
        r = _grade_mcq(_mcq('B'), '???')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


class MultiLetterNonAmbiguousTest(TestCase):
    def test_single_letter_in_long_text(self):
        # "I think it's B because..." — one letter, last-ditch pattern
        # should pick it up.
        r = _grade_mcq(_mcq('B'), "I think it's B because of trade balance")
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Defensive errors
# ============================================================================


class MalformedQuestionTest(TestCase):
    def test_no_correct_answer_raises(self):
        q = _mcq('')   # empty correct_answer
        with self.assertRaisesRegex(ValueError, 'correct_answer'):
            _grade_mcq(q, 'A')

    def test_bogus_correct_answer_raises(self):
        q = _mcq('Z')  # not A-D
        with self.assertRaises(ValueError):
            _grade_mcq(q, 'A')


# ============================================================================
# Dispatcher
# ============================================================================


class GradeAnswerDispatchTest(TestCase):
    def test_mcq_routes_to_mcq_grader(self):
        r = grade_answer(question=_mcq('B'), student_answer='B')
        self.assertEqual(r.tier, 'mcq')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_unsupported_type_raises_not_implemented(self):
        q = SimpleNamespace(question_type='short_answer')
        with self.assertRaisesRegex(NotImplementedError, 'short_answer'):
            grade_answer(question=q, student_answer='something')

    def test_math_type_routes_to_math_grader(self):
        # M3 is implemented — math type routes to the math grader and
        # returns a real GradeResult. We use a minimal math question
        # stand-in (model_answer in answer_data).
        q = SimpleNamespace(
            pk=1, question_type='math', question_text='2+2',
            correct_answer='',
            answer_data={'model_answer': '4', 'computed': 4.0},
        )
        r = grade_answer(question=q, student_answer='4')
        self.assertEqual(r.tier, 'math')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# GradeResult serialisation (used by SessionTurn.judge_outputs)
# ============================================================================


class GradeResultToDictTest(TestCase):
    def test_to_dict_roundtrip(self):
        r = _grade_mcq(_mcq('B'), 'B')
        d = r.to_dict()
        self.assertEqual(d, {
            'verdict': 'correct',
            'confidence': 1.0,
            'tier': 'mcq',
            'per_criterion_scores': {},
            'justification': "extracted 'B' matches correct_answer",
            'needs_followup': False,
        })

    def test_grade_result_is_frozen(self):
        r = _grade_mcq(_mcq('B'), 'B')
        with self.assertRaises(FrozenInstanceError):
            r.confidence = 0.0  # type: ignore[misc]


# ============================================================================
# needs_followup defaults
# ============================================================================


class NeedsFollowupDefaultTest(TestCase):
    """MCQ grader never returns needs_followup=True — there's no middle band."""

    def test_correct_no_followup(self):
        r = _grade_mcq(_mcq('B'), 'B')
        self.assertFalse(r.needs_followup)

    def test_incorrect_no_followup(self):
        r = _grade_mcq(_mcq('B'), 'A')
        self.assertFalse(r.needs_followup)

    def test_unparseable_no_followup(self):
        # Even the 0.6-confidence "no letter extractable" case has
        # needs_followup=False — the engine treats it as INCORRECT and
        # moves on; clarification mode is the LLM's job, not the grader's.
        r = _grade_mcq(_mcq('B'), 'nonsense')
        self.assertFalse(r.needs_followup)
