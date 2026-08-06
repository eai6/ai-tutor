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
from unittest.mock import patch

from apps.tutoring.simple_tutor.grader import (
    GradeResult,
    Verdict,
    grade_answer,
    _grade_mcq,
)


def _stub_llm(verdict='correct'):
    """Stand in for the LLM grader so escalation is testable with no provider."""
    def _fake(question, student_answer, *, qtype):
        return GradeResult(verdict=Verdict(verdict), confidence=1.0,
                           tier='mcq_llm', justification='stub')
    return patch('apps.tutoring.simple_tutor.grader._llm_grade', _fake)


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


class NonLetterRepliesEscalateTest(TestCase):
    """Anything that is not a bare letter is the LLM's call now.

    The string matchers that used to resolve "2", "Exports" and "225" were
    deleted 2026-08-06 — five layers of heuristics that still marked
    "two for easting, THEN two for northing" wrong against
    "Four digits (two for easting, two for northing)". The deterministic layer
    now defers and grade_answer escalates.
    """

    def test_deterministic_layer_defers_on_non_letter(self):
        r = _grade_mcq(_mcq('B'), '2')
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertEqual(r.confidence, 0.6, 'signals "unresolved", not "wrong"')
        self.assertIn('no A-D letter', r.justification)

    def test_grade_answer_escalates_a_bare_number(self):
        with _stub_llm('correct'):
            r = grade_answer(question=_mcq('B'), student_answer='2')
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertEqual(r.tier, 'mcq_llm')

    def test_grade_answer_escalates_option_text(self):
        q = _mcq('B', {'A': 'Imports', 'B': 'Exports'})
        with _stub_llm('correct'):
            r = grade_answer(question=q, student_answer='Exports')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_grade_answer_escalates_a_bare_value(self):
        q = _mcq('B', {'A': '180', 'B': '225', 'C': '270', 'D': '315'})
        with _stub_llm('correct'):
            r = grade_answer(question=q, student_answer='225')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_escalation_can_confirm_incorrect_too(self):
        """The LLM is not a rubber stamp — it returns incorrect as readily."""
        q = _mcq('B', {'A': 'Imports', 'B': 'Exports'})
        with _stub_llm('incorrect'):
            r = grade_answer(question=q, student_answer='Imports')
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_a_bare_letter_never_escalates(self):
        """58% of real replies are a bare letter. Those stay exact, free and
        reproducible — no LLM call."""
        called = {'n': 0}

        def _fake(question, student_answer, *, qtype):
            called['n'] += 1
            return None

        with patch('apps.tutoring.simple_tutor.grader._llm_grade', _fake):
            r = grade_answer(question=_mcq('B'), student_answer='B')
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertEqual(r.tier, 'mcq')
        self.assertEqual(called['n'], 0, 'a correct letter must not cost a call')

    def test_falls_back_to_defensive_incorrect_when_no_grader(self):
        """No provider, or every provider failed. Grading must never block."""
        with patch('apps.tutoring.simple_tutor.grader._llm_grade',
                   lambda *a, **k: None):
            r = grade_answer(question=_mcq('B'), student_answer='Exports')
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertEqual(r.confidence, 0.6)


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
    """A letter buried in a sentence used to be caught by a last-ditch regex
    that grabbed any lone A-D token. That heuristic is gone: it also fired on
    "I got A but then changed my mind" and on option text containing a stray
    letter. Reasoning-then-answering is exactly the shape the LLM should read.
    """

    def test_letter_inside_a_sentence_escalates(self):
        with _stub_llm('correct'):
            r = grade_answer(question=_mcq('B'),
                             student_answer="I think it's B because of trade balance")
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_deterministic_layer_does_not_guess_at_it(self):
        r = _grade_mcq(_mcq('B'), "I think it's B because of trade balance")
        self.assertEqual(r.confidence, 0.6, 'unresolved, escalate rather than guess')


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
            'justification': "letter 'B' matches",
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


# ValueToLetterCycle8Test was removed with the value matcher it covered
# (2026-08-06). A bare value like "225" now escalates to the LLM —
# see NonLetterRepliesEscalateTest.
