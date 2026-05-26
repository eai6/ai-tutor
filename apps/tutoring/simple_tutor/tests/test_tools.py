"""M8 acceptance tests — server-side tool handlers + flow primitives.

Per the simplified server-owned-state design
(auto-memory/feedback_server_owns_question_state.md):

  - record_answer does NOT take a question_id (server owns it)
  - pose_question and advance_step are dropped (server-driven)
  - All handlers return dicts (never raise) — conversation must flow
  - Auto-fallback grading + auto-step-advance are server-driven
"""
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.tutoring.models import (
    ExitTicket, ExitTicketQuestion, SessionTurn, TutorSession,
)
from apps.tutoring.simple_tutor.tools import (
    DEFAULT_STEP_TURN_CAP,
    auto_grade_if_missed,
    handle_advance_step,
    handle_record_answer,
    handle_request_figure,
    handle_redirect_off_topic,
    maybe_advance_step,
    pick_current_question,
    _looks_like_answer_attempt,
)

User = get_user_model()


_counter = {'n': 0}


def _make_session(*, n_questions=3, n_steps=2, with_objective=True):
    """Build a session + lesson + exit ticket with N MCQ questions.

    Steps and questions share the same ``enabling_objective`` so the
    enabling_objective-based filter in pick_current_question routes
    them to the current step. Pass ``with_objective=False`` to leave
    fields blank (tests the legacy/sparse-data fallback).
    """
    _counter['n'] += 1
    i = _counter['n']
    inst = Institution.objects.create(name=f'School {i}', slug=f'sch-{i}')
    user = User.objects.create_user(username=f'stu-{i}', password='x')
    course = Course.objects.create(
        title=f'Course {i}', institution=inst,
        grade_level='S3', is_published=True,
    )
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='L', objective='x',
        order_index=0, is_published=True,
    )
    objective_text = (
        f'objective-{i}' if with_objective else ''
    )
    for idx in range(n_steps):
        LessonStep.objects.create(
            lesson=lesson, teacher_script=f's{idx}',
            question='?', expected_answer='42',
            phase='engage', order_index=idx,
            enabling_objective=objective_text,
        )
    ticket = ExitTicket.objects.create(lesson=lesson)
    questions = []
    for j in range(n_questions):
        q = ExitTicketQuestion.objects.create(
            exit_ticket=ticket,
            question_type='mcq',
            question_text=f'Q{j+1}: which is correct?',
            option_a='alpha', option_b='beta',
            option_c='gamma', option_d='delta',
            correct_answer='B',
            enabling_objective=objective_text,
            order_index=j,
        )
        questions.append(q)
    session = TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson,
        engine='simple',
    )
    return session, questions


def _add_graded_turn(session, question, verdict='correct'):
    """Helper: add a tutor turn with a recorded grader verdict."""
    return SessionTurn.objects.create(
        session=session,
        role='tutor',
        content='turn',
        judge_outputs={
            'grader': {
                'verdict': verdict,
                'confidence': 1.0,
                'tier': 'mcq',
                'per_criterion_scores': {},
                'justification': 'test',
                'needs_followup': False,
                'question_id': question.pk,
            },
        },
    )


# ============================================================================
# pick_current_question
# ============================================================================


class PickCurrentQuestionTest(DjangoTestCase):

    def test_picks_first_question_when_none_graded(self):
        session, qs = _make_session(n_questions=3)
        q = pick_current_question(session)
        self.assertIsNotNone(q)
        self.assertEqual(q.pk, qs[0].pk)

    def test_skips_already_graded(self):
        session, qs = _make_session(n_questions=3)
        _add_graded_turn(session, qs[0])
        q = pick_current_question(session)
        self.assertEqual(q.pk, qs[1].pk)

    def test_returns_none_when_all_graded(self):
        session, qs = _make_session(n_questions=2)
        _add_graded_turn(session, qs[0])
        _add_graded_turn(session, qs[1])
        self.assertIsNone(pick_current_question(session))

    def test_returns_none_when_lesson_has_no_questions(self):
        session, _ = _make_session(n_questions=0)
        self.assertIsNone(pick_current_question(session))


# ============================================================================
# handle_record_answer
# ============================================================================


class HandleRecordAnswerTest(DjangoTestCase):

    def test_grades_against_current_question(self):
        session, qs = _make_session(n_questions=2)
        session.current_question_id = qs[0].pk
        session.save(update_fields=['current_question_id'])

        # Correct MCQ answer
        r = handle_record_answer(session, extracted_answer='B')
        self.assertTrue(r['recorded'])
        self.assertEqual(r['question_id'], qs[0].pk)
        self.assertEqual(r['verdict'], 'correct')
        self.assertEqual(r['tier'], 'mcq')

    def test_wrong_mcq(self):
        session, qs = _make_session(n_questions=2)
        session.current_question_id = qs[0].pk
        session.save(update_fields=['current_question_id'])
        r = handle_record_answer(session, extracted_answer='A')
        self.assertTrue(r['recorded'])
        self.assertEqual(r['verdict'], 'incorrect')

    def test_no_current_question_returns_error(self):
        """Handler does NOT raise — returns error dict so engine flow continues."""
        session, _ = _make_session()
        session.current_question_id = None
        session.save(update_fields=['current_question_id'])
        r = handle_record_answer(session, extracted_answer='B')
        self.assertFalse(r['recorded'])
        self.assertIn('no current question', r['error'])

    def test_stale_question_id_clears_pointer(self):
        """If current_question_id points to a deleted question, handler
        clears it AND returns an error dict (doesn't crash).
        """
        session, qs = _make_session()
        session.current_question_id = 999_999    # bogus
        session.save(update_fields=['current_question_id'])
        r = handle_record_answer(session, extracted_answer='B')
        self.assertFalse(r['recorded'])
        session.refresh_from_db()
        self.assertIsNone(session.current_question_id)


# ============================================================================
# handle_request_figure
# ============================================================================


class HandleRequestFigureTest(DjangoTestCase):

    def test_invalid_id_returns_error_dict(self):
        session, _ = _make_session()
        # Pass a clearly-invalid id; handler must return error, NOT raise
        r = handle_request_figure(session, figure_id=999_999)
        self.assertFalse(r['displayed'])
        self.assertIn('error', r)
        # The error message should mention either "not in catalog" or
        # the absence of the StepMedia model (some test envs).
        err_lower = str(r['error']).lower()
        self.assertTrue(
            'not in catalog' in err_lower or 'unavailable' in err_lower,
            f"unexpected error message: {r['error']!r}",
        )

    def test_invalid_id_does_not_raise(self):
        # The whole point — handler must NEVER raise on bad input
        session, _ = _make_session()
        try:
            handle_request_figure(session, figure_id=-1)
        except Exception as exc:
            self.fail(f"handle_request_figure raised: {exc!r}")


# ============================================================================
# handle_redirect_off_topic
# ============================================================================


class HandleRedirectOffTopicTest(DjangoTestCase):

    def test_increments_counter_in_engine_state(self):
        session, _ = _make_session()
        r = handle_redirect_off_topic(session, reason='asking about football')
        self.assertTrue(r['recorded'])
        self.assertEqual(r['off_topic_count'], 1)

        session.refresh_from_db()
        self.assertEqual(session.engine_state.get('off_topic_count'), 1)
        self.assertEqual(
            session.engine_state.get('last_off_topic_reason'),
            'asking about football',
        )

    def test_increments_across_calls(self):
        session, _ = _make_session()
        handle_redirect_off_topic(session, reason='r1')
        handle_redirect_off_topic(session, reason='r2')
        r = handle_redirect_off_topic(session, reason='r3')
        self.assertEqual(r['off_topic_count'], 3)

    def test_empty_reason_safe(self):
        session, _ = _make_session()
        r = handle_redirect_off_topic(session)
        self.assertTrue(r['recorded'])


# ============================================================================
# _looks_like_answer_attempt heuristic
# ============================================================================


class LooksLikeAnswerAttemptTest(DjangoTestCase):

    def test_short_factual_answer(self):
        self.assertTrue(_looks_like_answer_attempt('B'))
        self.assertTrue(_looks_like_answer_attempt('150°'))
        self.assertTrue(_looks_like_answer_attempt('Because tropical climates have heavy rainfall'))

    def test_clarifying_question_rejected(self):
        self.assertFalse(_looks_like_answer_attempt('What does export mean?'))
        self.assertFalse(_looks_like_answer_attempt('Why is that?'))
        self.assertFalse(_looks_like_answer_attempt('How do I solve this?'))
        self.assertFalse(_looks_like_answer_attempt('Can you explain again?'))
        self.assertFalse(_looks_like_answer_attempt('Tell me more about angles'))
        self.assertFalse(_looks_like_answer_attempt('Explain it differently'))

    def test_ends_with_question_mark_rejected(self):
        self.assertFalse(_looks_like_answer_attempt('Is it B?'))

    def test_empty_rejected(self):
        self.assertFalse(_looks_like_answer_attempt(''))
        self.assertFalse(_looks_like_answer_attempt('   '))

    def test_very_long_rejected(self):
        # >500 chars → likely a discussion not a focused answer
        self.assertFalse(_looks_like_answer_attempt('answer ' * 100))


# ============================================================================
# auto_grade_if_missed
# ============================================================================


class AutoGradeIfMissedTest(DjangoTestCase):

    def test_llm_called_record_answer_noop(self):
        session, qs = _make_session()
        session.current_question_id = qs[0].pk
        session.save(update_fields=['current_question_id'])
        result = auto_grade_if_missed(session, 'B', llm_called_record_answer=True)
        self.assertIsNone(result)

    def test_no_current_question_noop(self):
        session, _ = _make_session()
        session.current_question_id = None
        session.save(update_fields=['current_question_id'])
        result = auto_grade_if_missed(session, 'B', llm_called_record_answer=False)
        self.assertIsNone(result)

    def test_clarifying_question_noop(self):
        session, qs = _make_session()
        session.current_question_id = qs[0].pk
        session.save(update_fields=['current_question_id'])
        result = auto_grade_if_missed(
            session, 'What is an angle?', llm_called_record_answer=False,
        )
        self.assertIsNone(result)

    def test_answer_shaped_input_grades(self):
        session, qs = _make_session()
        session.current_question_id = qs[0].pk
        session.save(update_fields=['current_question_id'])
        result = auto_grade_if_missed(
            session, 'B', llm_called_record_answer=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict.value, 'correct')
        # tier override marks it as auto-fallback so analytics can tell
        self.assertEqual(result.tier, 'auto_fallback')
        self.assertIn('auto-fallback', result.justification)

    def test_wrong_answer_via_auto_grade(self):
        session, qs = _make_session()
        session.current_question_id = qs[0].pk
        session.save(update_fields=['current_question_id'])
        result = auto_grade_if_missed(
            session, 'A', llm_called_record_answer=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict.value, 'incorrect')

    def test_stale_question_id_no_crash(self):
        session, _ = _make_session()
        session.current_question_id = 999_999
        session.save(update_fields=['current_question_id'])
        # Must return None rather than crashing on the lookup
        result = auto_grade_if_missed(session, 'B', llm_called_record_answer=False)
        self.assertIsNone(result)


# ============================================================================
# maybe_advance_step
# ============================================================================


class HandleAdvanceStepTest(DjangoTestCase):
    """Soft-hint handler: LLM says 'student is ready', server bumps step.
    Even if the LLM forgets it, maybe_advance_step provides the safety net.
    """

    def test_advances_step_index(self):
        session, _ = _make_session(n_steps=3)
        r = handle_advance_step(session, reason='student got it')
        self.assertTrue(r['advanced'])
        self.assertEqual(r['new_step_index'], 1)
        self.assertTrue(r['has_next_step'])
        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 1)
        self.assertIsNone(session.current_question_id)

    def test_records_reason_in_engine_state(self):
        session, _ = _make_session()
        handle_advance_step(session, reason='clear evidence of mastery')
        session.refresh_from_db()
        hints = session.engine_state.get('advance_step_hints', [])
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]['reason'], 'clear evidence of mastery')

    def test_advance_past_last_step_returns_has_next_false(self):
        session, _ = _make_session(n_steps=2)
        # Advance to step 1
        handle_advance_step(session, reason='ok')
        # Advance to step 2 — past the last step (only indices 0, 1)
        r = handle_advance_step(session, reason='all done')
        self.assertTrue(r['advanced'])
        self.assertEqual(r['new_step_index'], 2)
        self.assertFalse(r['has_next_step'])


class MaybeAdvanceStepTest(DjangoTestCase):
    """Server-driven auto-advance. Two triggers:
      1. All questions for CURRENT step's enabling_objective are graded
      2. Soft turn cap exceeded on the current step
    """

    def test_does_not_advance_when_step_questions_remain(self):
        session, qs = _make_session(n_questions=3, n_steps=2)
        # No verdicts yet — questions 0, 1, 2 all un-graded
        advanced = maybe_advance_step(session)
        self.assertFalse(advanced)
        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 0)

    def test_advances_when_step_questions_all_graded(self):
        """When all current-step (enabling_objective) questions have
        verdicts, server advances to next step.
        """
        session, qs = _make_session(n_questions=3, n_steps=2)
        # Grade ALL three (they share the step's enabling_objective)
        for q in qs:
            _add_graded_turn(session, q)
        advanced = maybe_advance_step(session)
        self.assertTrue(advanced)
        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 1)
        self.assertIsNone(session.current_question_id)

    def test_advance_clears_current_question(self):
        session, qs = _make_session(n_questions=1)
        _add_graded_turn(session, qs[0])
        session.current_question_id = 42
        session.save(update_fields=['current_question_id'])
        maybe_advance_step(session)
        session.refresh_from_db()
        self.assertIsNone(session.current_question_id)

    def test_idempotent_when_no_more_to_advance(self):
        """Past the last step → no change on subsequent calls."""
        session, qs = _make_session(n_questions=1, n_steps=1)
        _add_graded_turn(session, qs[0])
        maybe_advance_step(session)   # advances to step 1
        advanced_again = maybe_advance_step(session)
        self.assertFalse(advanced_again)

    def test_turn_cap_forces_advance(self):
        """Even with ungraded questions remaining, server force-advances
        after the soft turn cap and logs `forced=True` in engine_state.
        """
        session, qs = _make_session(n_questions=3, n_steps=2)
        # No verdicts. Pile up student turns on the current step.
        step0 = session.lesson.steps.first()
        for _ in range(DEFAULT_STEP_TURN_CAP):
            SessionTurn.objects.create(
                session=session, role='student',
                content='x', step=step0,
            )
        advanced = maybe_advance_step(session)
        self.assertTrue(advanced)
        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 1)
        # forced_advances entry logged
        forced = session.engine_state.get('forced_advances', [])
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]['from_step_index'], 0)

    def test_below_turn_cap_no_force(self):
        session, qs = _make_session(n_questions=3, n_steps=2)
        step0 = session.lesson.steps.first()
        for _ in range(DEFAULT_STEP_TURN_CAP - 1):
            SessionTurn.objects.create(
                session=session, role='student',
                content='x', step=step0,
            )
        advanced = maybe_advance_step(session)
        self.assertFalse(advanced)


class PickCurrentQuestionByObjectiveTest(DjangoTestCase):
    """When question + step share enabling_objective, picker filters
    correctly. When the step's objective is empty (sparse legacy data),
    picker falls back to lesson-wide pool.
    """

    def test_legacy_data_no_objective_falls_back_to_lesson_pool(self):
        session, qs = _make_session(n_questions=2, with_objective=False)
        # Both step + questions have empty objective — picker falls back
        # to lesson-wide pool, returns first un-graded question.
        q = pick_current_question(session)
        self.assertIsNotNone(q)
        self.assertEqual(q.pk, qs[0].pk)

    def test_with_objective_filters_correctly(self):
        # Default with_objective=True: step.enabling_objective matches
        # questions, so picker filters and returns first.
        session, qs = _make_session(n_questions=3, with_objective=True)
        q = pick_current_question(session)
        self.assertEqual(q.pk, qs[0].pk)
