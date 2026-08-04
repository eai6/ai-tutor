"""Server-side pre-grading (qwen_mt30 board, 2026-08-03).

qwen3-4b-Instruct-2507 skipped the expected tool on ~80% of Call 1s, flat
from the first turn of every session — so grades landed late (Call-2 repair)
or never, and every student-facing reply was written before the verdict
existed. Pre-grading grades strict-shaped answers server-side BEFORE Call 1
and hands the verdict to the model in a <last_grade> block.
"""
from django.test import TestCase as DjangoTestCase

from apps.tutoring.models import InFlightQuestion
from apps.tutoring.simple_tutor.engine import _pre_grade_answer, _turn_verdict
from apps.tutoring.simple_tutor.prompts import (
    _render_last_grade_block,
    build_system_prompt,
)
from apps.tutoring.simple_tutor.tests.test_engine import _make_session
from apps.tutoring.simple_tutor.tools import handle_record_answer


def _slot(session, ref='B'):
    return InFlightQuestion.objects.create(
        session=session,
        question_text='Which lists the compass points clockwise from North?',
        question_type='mcq',
        options=['wrong one', 'right one', 'also wrong', 'nope'],
        reference_answer=ref, source='catalog',
    )


class PreGradeAnswerTest(DjangoTestCase):

    def test_correct_answer_grades_and_clears_slot(self):
        session, _ = _make_session()
        _slot(session)
        result = _pre_grade_answer(session, 'b')
        self.assertTrue(result['recorded'])
        self.assertEqual(result['verdict'], 'correct')
        self.assertTrue(result['pre_graded'])
        self.assertEqual(result['student_answer'], 'b')
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())
        self.assertTrue(session.engine_state.get('_pre_graded_this_turn'))

    def test_wrong_answer_bumps_attempts_and_keeps_slot(self):
        session, _ = _make_session()
        _slot(session)
        result = _pre_grade_answer(session, 'a')
        self.assertEqual(result['verdict'], 'incorrect')
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.attempt_count, 1)
        # Same-turn hint guard armed too.
        self.assertTrue(session.engine_state.get('_graded_incorrect_this_turn'))

    def test_no_slot_returns_none(self):
        session, _ = _make_session()
        self.assertIsNone(_pre_grade_answer(session, 'b'))

    def test_seeded_result_is_the_turn_verdict(self):
        session, _ = _make_session()
        _slot(session)
        result = _pre_grade_answer(session, 'b')
        tool_results = [{'tool': 'record_answer', 'result': result}]
        self.assertEqual(_turn_verdict(tool_results), 'correct')


class DoubleGradeGuardTest(DjangoTestCase):

    def test_model_record_answer_is_refused_after_pre_grade(self):
        session, _ = _make_session()
        _slot(session)
        _pre_grade_answer(session, 'a')          # wrong → slot survives
        second = handle_record_answer(session, extracted_answer='a')
        self.assertFalse(second['recorded'])
        self.assertIn('already graded', second['error'])
        # Attempt count did NOT double-bump.
        self.assertEqual(
            InFlightQuestion.objects.get(session=session).attempt_count, 1)

    def test_next_turn_grades_normally_after_flag_cleared(self):
        session, _ = _make_session()
        _slot(session)
        _pre_grade_answer(session, 'a')
        es = session.engine_state
        es.pop('_pre_graded_this_turn', None)     # what respond() does at turn start
        session.engine_state = es
        session.save(update_fields=['engine_state'])
        result = handle_record_answer(session, extracted_answer='b')
        self.assertTrue(result['recorded'])
        self.assertEqual(result['verdict'], 'correct')


class LastGradeBlockTest(DjangoTestCase):

    def test_correct_render(self):
        out = _render_last_grade_block({
            'recorded': True, 'verdict': 'correct',
            'question_text': 'Q?', 'student_answer': 'b',
            'attempt_count_before': 0,
        })
        self.assertIn('<verdict>correct</verdict>', out)
        self.assertIn('pose the NEXT question', out)
        self.assertNotIn('record_answer and do not affirm', out)

    def test_incorrect_render_keeps_question_open(self):
        out = _render_last_grade_block({
            'recorded': True, 'verdict': 'incorrect',
            'question_text': 'Q?', 'student_answer': 'a',
            'attempt_count_before': 1,
        })
        self.assertIn('<verdict>incorrect</verdict>', out)
        self.assertIn('<wrong_attempts>2</wrong_attempts>', out)
        self.assertIn('pose no new question this turn', out)

    def test_absent_for_none_or_unrecorded(self):
        self.assertEqual(_render_last_grade_block(None), '')
        self.assertEqual(_render_last_grade_block({'recorded': False}), '')

    def test_block_lands_in_system_prompt(self):
        session, _ = _make_session()
        slot = _slot(session)
        blocks, _tools = build_system_prompt(
            session=session, step=None, in_flight_question=slot,
            pre_grade={'recorded': True, 'verdict': 'incorrect',
                       'question_text': slot.question_text,
                       'student_answer': 'a', 'attempt_count_before': 0},
        )
        joined = '\n'.join(b['text'] for b in blocks)
        self.assertIn('<last_grade>', joined)
        # Rendered before the length budget (query-adjacent ordering).
        self.assertLess(joined.index('<last_grade>'),
                        joined.index('<reply_length>'))


# ============================================================================
# it2 fixes — authored-reference solver + stale-slot guard
# ============================================================================


from apps.tutoring.simple_tutor.grader import _option_number
from apps.tutoring.simple_tutor.tools import (
    handle_pose_question,
    solve_authored_stem,
)
from django.test import SimpleTestCase


class SolveAuthoredStemTest(SimpleTestCase):

    def test_recognised_families(self):
        cases = [
            ("Four angles around a point are 70°, 85°, 90°, and x°. "
             "What is x?", "115"),
            ("What is 360° − 175°?", "185"),
            ("Two angles on a straight line are 113° and x°. Find x.", "67"),
            ("An angle of 74° has a vertically opposite angle. "
             "What is it?", "74"),
            ("A bearing of 135° points to which compass point?", "southeast"),
            ("What bearing corresponds to the compass point Southwest "
             "(SW)?", "225"),
            ("The probability of rain is 0.7. What is the probability that "
             "it does not rain?", "0.3"),
        ]
        for stem, want in cases:
            self.assertEqual(solve_authored_stem(stem), want, stem)

    def test_unrecognised_stems_decline(self):
        for stem in [
            "In a lottery with 200 tickets, 20 are winners. If you buy 25 "
            "tickets, how many winners do you expect to have?",
            "Which of these is a biological weathering agent?",
            "Angles around a point sum to how many degrees?",
        ]:
            self.assertIsNone(solve_authored_stem(stem), stem)


class AuthoredRefCorrectionTest(DjangoTestCase):
    """The it2 killer: reference_answer='175' for a stem whose true answer
    is 115, enforced by pre-grading for five straight attempts."""

    def test_wrong_authored_ref_is_corrected_at_pose(self):
        session, _ = _make_session()
        posed = handle_pose_question(
            session,
            question_text='Four angles around a point are 70°, 85°, 90°, '
                          'and x°. What is x?',
            question_type='short_numeric', reference_answer='175',
            source='inline_authored',
        )
        self.assertTrue(posed['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, '115')
        # And the student's correct answer now grades correct.
        result = handle_record_answer(session, extracted_answer='115')
        self.assertEqual(result['verdict'], 'correct')

    def test_correct_authored_ref_untouched(self):
        session, _ = _make_session()
        handle_pose_question(
            session,
            question_text='Two angles on a straight line are 113° and x°. '
                          'Find x.',
            question_type='short_numeric', reference_answer='67',
            source='inline_authored',
        )
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, '67')

    def test_numeric_ref_on_compass_question_retyped(self):
        session, _ = _make_session()
        handle_pose_question(
            session,
            question_text='A bearing of 135° points to which compass point?',
            question_type='short_numeric', reference_answer='135',
            source='inline_authored',
        )
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, 'southeast')
        self.assertEqual(slot.question_type, 'short_answer')


class StaleSlotPreGradeGuardTest(DjangoTestCase):
    """it2: the tutor asked 'what is 360 − 175?' in prose while the slot held
    the main question — the correct micro-answer '185' was graded against the
    main reference '175' twice."""

    def _slot_and_turn(self, session, tutor_text):
        from apps.tutoring.models import SessionTurn
        slot = InFlightQuestion.objects.create(
            session=session,
            question_text='Four angles around a point are 70°, 85°, 90°, '
                          'and x°. What is x?',
            question_type='short_numeric', reference_answer='115',
            source='inline_authored',
        )
        SessionTurn.objects.create(
            session=session, role=SessionTurn.Role.TUTOR, content=tutor_text)
        return slot

    def test_prose_microstep_skips_pre_grade(self):
        session, _ = _make_session()
        self._slot_and_turn(
            session, "Not quite. Let's simplify: what is 360° − 175°?")
        self.assertIsNone(_pre_grade_answer(session, '185'))
        # Slot untouched — no attempt bump for the micro-answer.
        self.assertEqual(
            InFlightQuestion.objects.get(session=session).attempt_count, 0)

    def test_reanchored_stem_pre_grades(self):
        session, _ = _make_session()
        self._slot_and_turn(
            session, "Try again: Four angles around a point are 70°, 85°, "
                     "90°, and x°. What is x?")
        result = _pre_grade_answer(session, '115')
        self.assertIsNotNone(result)
        self.assertEqual(result['verdict'], 'correct')


class OptionNumberUnitsTest(SimpleTestCase):
    def test_spelled_degrees_parse(self):
        self.assertEqual(_option_number('360 degrees'), 360.0)
        self.assertEqual(_option_number('360°'), 360.0)
        self.assertEqual(_option_number('45 deg'), 45.0)
