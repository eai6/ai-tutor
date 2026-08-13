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

from ai_tutor.apps.tutoring.simple_tutor.grader import (
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
    return patch('ai_tutor.apps.tutoring.simple_tutor.grader._llm_grade', _fake)


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
        # '2' is positional shorthand, not an option's text or value here, so
        # neither fast path resolves it. Unresolved is not wrong.
        r = _grade_mcq(_mcq('B'), '2')
        self.assertNotEqual(r.verdict, Verdict.INCORRECT)
        self.assertTrue(r.needs_followup, 'signals "unresolved", not "wrong"')

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

        with patch('ai_tutor.apps.tutoring.simple_tutor.grader._llm_grade', _fake):
            r = grade_answer(question=_mcq('B'), student_answer='B')
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertEqual(r.tier, 'mcq')
        self.assertEqual(called['n'], 0, 'a correct letter must not cost a call')

    def test_no_grader_reachable_reports_unresolved_not_wrong(self):
        """No provider, or every provider failed. Grading must never block —
        and must not invent a verdict either."""
        with patch('ai_tutor.apps.tutoring.simple_tutor.grader._llm_grade',
                   lambda *a, **k: None):
            r = grade_answer(question=_mcq('B'), student_answer='sort of the second one')
        self.assertNotEqual(r.verdict, Verdict.INCORRECT)
        self.assertTrue(r.needs_followup)


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
    """Unresolvable is NOT wrong.

    These asserted INCORRECT until 2026-08-06. That contract was the bug: a
    student who typed an option verbatim was told they were wrong with
    "no A-D letter extractable", because nothing had graded them. Students do
    not always answer with a letter, so this path is ordinary — the grader now
    says "unresolved" and the engine keeps the question live.
    """

    def test_unrelated_text(self):
        r = _grade_mcq(_mcq('B'), "I don't know")
        self.assertNotEqual(r.verdict, Verdict.INCORRECT)
        self.assertTrue(r.needs_followup)

    def test_two_letters_in_text(self):
        # "I'm between A or B" — genuinely ambiguous, must not be guessed.
        r = _grade_mcq(_mcq('B'), "I'm between A or B")
        self.assertNotEqual(r.verdict, Verdict.INCORRECT)
        self.assertTrue(r.needs_followup)

    def test_random_punctuation(self):
        r = _grade_mcq(_mcq('B'), '???')
        self.assertTrue(r.needs_followup)


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
        self.assertTrue(r.needs_followup, 'unresolved, escalate rather than guess')


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
    """A resolved MCQ grade is definite; an UNRESOLVED one asks for followup."""

    def test_correct_no_followup(self):
        r = _grade_mcq(_mcq('B'), 'B')
        self.assertFalse(r.needs_followup)

    def test_incorrect_no_followup(self):
        r = _grade_mcq(_mcq('B'), 'A')
        self.assertFalse(r.needs_followup)

    def test_unparseable_DOES_want_followup(self):
        """Changed 2026-08-06. This used to assert needs_followup=False, i.e.
        "treat it as INCORRECT and move on". That is what told a student they
        were wrong when nothing had graded them."""
        r = _grade_mcq(_mcq('B'), 'nonsense')
        self.assertTrue(r.needs_followup)


# ValueToLetterCycle8Test was removed with the value matcher it covered
# (2026-08-06). A bare value like "225" now escalates to the LLM —
# see NonLetterRepliesEscalateTest.


class ExactOptionFastPathTest(TestCase):
    """An option quoted verbatim must not cost an LLM call.

    Measured on device sessions 2026-08-06: "39" — the exact text of option B —
    took a full LLM round trip, and "The scale of the map", an option quoted
    verbatim, never reached a grader at all and was returned as INCORRECT at
    confidence 0.6 with "no A-D letter extractable". Offline that model call is
    on the same local model the tutor is already waiting on, which is what made
    grading feel slow.

    The line is recognising vs interpreting. Exact equality is recognising.
    Fuzzy matching (distinctive-substring, LCS overlap) is interpreting, was
    deleted for marking correct answers wrong, and is not coming back — the
    paraphrase tests below hold that line.
    """

    def _q(self, correct, **opts):
        return SimpleNamespace(
            pk=1, question_type='mcq', question_text='Which?',
            correct_answer=correct, answer_data={},
            option_a=opts.get('a', ''), option_b=opts.get('b', ''),
            option_c=opts.get('c', ''), option_d=opts.get('d', ''),
        )

    def test_exact_option_text_resolves_without_the_llm(self):
        q = self._q('B', a='The easting or horizontal distance',
                    b='The northing or vertical distance',
                    c='The scale of the map', d='The longitude lines')
        r = _grade_mcq(q, 'The scale of the map')
        self.assertEqual(r.tier, 'mcq')
        self.assertEqual(r.confidence, 1.0, 'resolved, so grade_answer must not escalate')
        self.assertEqual(r.verdict, Verdict.INCORRECT)   # option C, correct is B

    def test_exact_match_is_case_and_space_insensitive(self):
        q = self._q('C', a='Imports', b='Exports', c='  ThE  ScAlE ', d='Other')
        self.assertEqual(_grade_mcq(q, 'the scale').verdict, Verdict.CORRECT)

    def test_numeric_option_value_resolves_without_the_llm(self):
        q = self._q('B', a='47', b='39', c='3 and 9', d='4 and 7')
        r = _grade_mcq(q, '39')
        self.assertEqual(r.tier, 'mcq')
        self.assertEqual(r.confidence, 1.0)
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_unresolvable_is_not_reported_as_wrong(self):
        """A parse failure is not a verdict.

        Device session 22: a student typed an option verbatim, nothing graded
        it, and they were told INCORRECT with "no A-D letter extractable".
        Students do not always reply with a letter — that path is ordinary,
        and telling them they are wrong when the grader could not be reached
        is worse than saying nothing.
        """
        q = self._q('B', a='47', b='39', c='3 and 9', d='4 and 7')
        r = _grade_mcq(q, 'the second pair I think, not sure')
        self.assertNotEqual(r.verdict, Verdict.INCORRECT,
                            'must not assert wrongness it did not establish')
        self.assertTrue(r.needs_followup, 'engine must see this as unresolved')

    def test_a_paraphrase_still_escalates(self):
        """The case the fuzzy matchers got wrong. It must reach the LLM, not
        be resolved by a string rule."""
        q = self._q('C', a='Two digits', b='Three digits',
                    c='Four digits (two for easting, two for northing)',
                    d='Six digits')
        r = _grade_mcq(q, 'two for easting, then two for northing')
        self.assertTrue(r.needs_followup, 'must escalate, not be guessed at')
        self.assertNotEqual(r.verdict, Verdict.CORRECT)

    def test_reasoning_then_answering_still_escalates(self):
        q = self._q('B', a='47', b='39', c='3 and 9', d='4 and 7')
        self.assertTrue(_grade_mcq(q, '39 is the easting value').needs_followup)

    def test_two_options_with_the_same_value_are_not_guessed(self):
        """Ambiguity falls through rather than picking one."""
        from ai_tutor.apps.tutoring.simple_tutor.grader import _exact_option_match
        q = self._q('A', a='39', b='39', c='x', d='y')
        self.assertIsNone(_exact_option_match(q, '39'))
        self.assertTrue(_grade_mcq(q, '39').needs_followup)

    def test_a_bare_number_does_not_loosely_match_a_longer_one(self):
        """"3" must not match an option reading "3947"."""
        from ai_tutor.apps.tutoring.simple_tutor.grader import _exact_option_match
        q = self._q('A', a='3947', b='4756', c='x', d='y')
        self.assertIsNone(_exact_option_match(q, '3'))
