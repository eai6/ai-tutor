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
        # Every QuestionType.choices value is now supported by the grader
        # stack (M2 MCQ, M3 math, M4 fill_in_blank + embed gate, M5
        # verifier LLM, M5.5 matching). Use a deliberately fake type to
        # verify the dispatcher's catch-all branch still raises.
        q = SimpleNamespace(question_type='__not_a_real_type__')
        with self.assertRaisesRegex(NotImplementedError, '__not_a_real_type__'):
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


class ValueToLetterCycle8Test(TestCase):
    """Cycle-8: a bare value that uniquely matches one option grades as
    that option's letter — kills the 8-turn 'which letter?' nag loop
    (kimi refusal_chain: student computed 225, tutor demanded the letter
    for six more turns)."""

    def _q(self, opts, correct):
        return SimpleNamespace(
            option_a=opts[0], option_b=opts[1],
            option_c=opts[2], option_d=opts[3],
            correct_answer=correct,
        )

    def test_unique_value_match_grades_as_letter(self):
        q = self._q(['180°', '225°', '270°', '315°'], 'B')
        r = _grade_mcq(q, '225')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_value_match_beats_positional_numeric(self):
        # '2' is the VALUE of option B here — must not be read as
        # "option 2" (positionally also B, so make the value sit at D).
        q = self._q(['4', '6', '10', '2'], 'D')
        r = _grade_mcq(q, '2')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_positional_still_works_when_no_value_match(self):
        q = self._q(['red', 'blue', 'green', 'yellow'], 'B')
        r = _grade_mcq(q, 'option 2')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_prefixed_option_text_still_value_matches(self):
        q = self._q(['A) 11', 'B) 3.8', 'C) 31', 'D) 12'], 'A')
        r = _grade_mcq(q, '11')
        self.assertEqual(r.verdict, Verdict.CORRECT)
