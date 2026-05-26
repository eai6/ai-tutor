"""M8 + M12 acceptance tests — server-side tool handlers + flow primitives.

M12 pose_question architecture (2026-05-26):
  - record_answer takes only ``extracted_answer`` — the slot owns
    reference_answer / question_type / options
  - pose_question persists a single in-flight slot per session
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
    ExitTicket, ExitTicketQuestion, InFlightQuestion,
    SessionTurn, TutorSession,
)
from apps.tutoring.simple_tutor.tools import (
    DEFAULT_COMPETENCE_THRESHOLD,
    DEFAULT_POOL_SIZE,
    DEFAULT_STEP_TURN_CAP,
    EXCLUDED_QUESTION_TYPES,
    build_question_pool,
    handle_advance_step,
    handle_pose_question,
    handle_record_answer,
    handle_request_figure,
    handle_redirect_off_topic,
    maybe_advance_step,
    _current_step_correct_verdict_count,
)

User = get_user_model()


_counter = {'n': 0}


def _make_session(
    *,
    n_questions=3,
    n_steps=2,
    with_objective=True,
    step_question='',
    step_expected_answer='',
):
    """Build a session + lesson + exit ticket with N MCQ questions.

    Steps and questions share the same ``enabling_objective`` so the
    enabling_objective-based filter in pick_current_question routes
    them to the current step. Pass ``with_objective=False`` to leave
    fields blank (tests the legacy/sparse-data fallback).

    ``step_question`` + ``step_expected_answer`` are empty by default —
    the simple tutor's LessonStep-primary pickup is exercised by tests
    that explicitly set them (see ``PickCurrentQuestionLessonStepTest``).
    Leaving them blank routes pickup to the ExitTicketQuestion fallback,
    which is what these legacy tests assert against.
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
            question=step_question, expected_answer=step_expected_answer,
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


def _add_graded_turn(session, question, verdict='correct', source='exit_ticket'):
    """Helper: add a tutor turn with a recorded grader verdict on the
    session's CURRENT step (so _current_step_correct_verdict_count
    counts it). question is kept as the legacy ref but no longer drives
    pickup — see M11.3.
    """
    from apps.curriculum.models import LessonStep
    step = (
        LessonStep.objects
        .filter(lesson=session.lesson, order_index=session.current_step_index or 0)
        .first()
    )
    return SessionTurn.objects.create(
        session=session,
        role='tutor',
        content='turn',
        step=step,
        judge_outputs={
            'grader': {
                'verdict': verdict,
                'confidence': 1.0,
                'tier': 'mcq',
                'per_criterion_scores': {},
                'justification': 'test',
                'needs_followup': False,
                'question_type': 'mcq',
                'reference_answer': getattr(question, 'correct_answer', ''),
                'question_text': getattr(question, 'question_text', ''),
            },
        },
    )


# ============================================================================
# pick_current_question
# ============================================================================


class BuildQuestionPoolTest(DjangoTestCase):
    """The pool is context-only — no anchor, no skip-already-graded.
    The LLM picks what to pose and what reference answer to grade
    against, then passes both via record_answer.
    """

    def test_returns_pool_with_etq_questions(self):
        session, etqs = _make_session(n_questions=3)
        pool = build_question_pool(session)
        # Pool should include the ETQs (no LessonStep.question in this
        # default fixture).
        pks = {getattr(q, 'pk', None) for q in pool}
        for etq in etqs:
            self.assertIn(etq.pk, pks)

    def test_includes_lesson_step_question_as_first_entry(self):
        from apps.curriculum.models import LessonStep
        session, _ = _make_session(
            n_questions=2,
            step_question='What is 7 × 8?',
            step_expected_answer='56',
        )
        step = LessonStep.objects.get(lesson=session.lesson, order_index=0)
        pool = build_question_pool(session)
        self.assertGreater(len(pool), 0)
        # First entry: the LessonStep-backed StepQuestion adapter
        self.assertEqual(getattr(pool[0], 'source', None), 'lesson_step')
        self.assertEqual(pool[0].pk, step.pk)
        self.assertEqual(pool[0].question_text, 'What is 7 × 8?')

    def test_empty_when_lesson_has_no_questions(self):
        session, _ = _make_session(n_questions=0)
        # No LessonStep.question + no ETQs → empty pool.
        self.assertEqual(build_question_pool(session), [])

    def test_excludes_fill_in_blank_and_matching(self):
        from apps.tutoring.models import ExitTicketQuestion, ExitTicket
        from apps.curriculum.models import LessonStep
        session, _ = _make_session(n_questions=0)
        ticket = ExitTicket.objects.get(lesson=session.lesson)
        eo = LessonStep.objects.filter(lesson=session.lesson, order_index=0).first().enabling_objective
        fib = ExitTicketQuestion.objects.create(
            exit_ticket=ticket, question_type='fill_in_blank',
            question_text='_ is the capital.',
            option_a='', option_b='', option_c='', option_d='',
            correct_answer='', answer_data={'blanks': ['Paris']},
            order_index=0, enabling_objective=eo,
        )
        mat = ExitTicketQuestion.objects.create(
            exit_ticket=ticket, question_type='matching',
            question_text='Match pairs',
            option_a='', option_b='', option_c='', option_d='',
            correct_answer='', order_index=1, enabling_objective=eo,
        )
        mcq = ExitTicketQuestion.objects.create(
            exit_ticket=ticket, question_type='mcq',
            question_text='Which is correct?',
            option_a='alpha', option_b='beta',
            option_c='gamma', option_d='delta',
            correct_answer='B', order_index=2, enabling_objective=eo,
        )
        pool = build_question_pool(session)
        pks = {getattr(q, 'pk', None) for q in pool}
        self.assertIn(mcq.pk, pks)
        self.assertNotIn(fib.pk, pks)
        self.assertNotIn(mat.pk, pks)

    def test_pool_capped_at_default_size(self):
        session, _ = _make_session(n_questions=DEFAULT_POOL_SIZE + 5)
        pool = build_question_pool(session)
        self.assertLessEqual(len(pool), DEFAULT_POOL_SIZE)

    def test_skips_already_graded_by_question_text(self):
        """Questions whose question_text has already been graded in
        the session are dropped from the pool — keeping them around
        causes the LLM to reference settled questions in hints for
        new questions (caught 2026-05-26 in M11.3 E2E).

        The match is by lowercased question_text (the LLM-provided
        field on grader judge_outputs), not by pk, because the post-
        tear-down engine no longer stores question ids.
        """
        from apps.tutoring.models import SessionTurn
        session, etqs = _make_session(n_questions=2)
        # Simulate a prior graded turn referencing etqs[0]'s text.
        SessionTurn.objects.create(
            session=session, role='tutor', content='ack',
            judge_outputs={'grader': {
                'verdict': 'correct',
                'question_text': etqs[0].question_text,
            }},
        )
        pool = build_question_pool(session)
        pks = {getattr(q, 'pk', None) for q in pool}
        self.assertNotIn(etqs[0].pk, pks)
        self.assertIn(etqs[1].pk, pks)


# ============================================================================
# handle_record_answer
# ============================================================================


def _seed_in_flight(
    session,
    *,
    question_text='Which is greatest?',
    question_type='mcq',
    reference_answer='B',
    options=None,
    source='inline_authored',
    catalog_question_id=None,
    attempt_count=0,
):
    """Helper: write an in-flight slot for the session, mirroring what
    handle_pose_question would have created.
    """
    return InFlightQuestion.objects.create(
        session=session,
        question_text=question_text,
        question_type=question_type,
        options=options or [],
        reference_answer=reference_answer,
        source=source,
        catalog_question_id=catalog_question_id,
        attempt_count=attempt_count,
    )


class HandleRecordAnswerTest(DjangoTestCase):
    """M12: record_answer reads question / reference / type from the
    InFlightQuestion slot. The LLM only passes ``extracted_answer``.
    """

    def test_mcq_correct(self):
        session, _ = _make_session()
        _seed_in_flight(
            session, question_type='mcq', reference_answer='B',
            question_text='Which is greatest?',
            options=['10', '100', '1000', '0.1'],
        )
        r = handle_record_answer(session, extracted_answer='B')
        self.assertTrue(r['recorded'])
        self.assertEqual(r['verdict'], 'correct')
        self.assertEqual(r['tier'], 'mcq')
        self.assertEqual(r['question_type'], 'mcq')
        self.assertEqual(r['reference_answer'], 'B')
        # Slot cleared on correct verdict.
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())

    def test_mcq_incorrect_increments_attempt_count(self):
        session, _ = _make_session()
        _seed_in_flight(
            session, question_type='mcq', reference_answer='B',
            options=['x', 'y', 'z', 'w'],
        )
        r = handle_record_answer(session, extracted_answer='A')
        self.assertTrue(r['recorded'])
        self.assertEqual(r['verdict'], 'incorrect')
        # Slot persists; attempt_count incremented.
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.attempt_count, 1)
        # Snapshot field on the result for the LLM's hint-ladder logic.
        self.assertEqual(r['attempt_count_before'], 0)

    def test_short_numeric_correct(self):
        session, _ = _make_session()
        _seed_in_flight(
            session, question_type='short_numeric', reference_answer='42',
            question_text='6 × 7 = ?',
        )
        r = handle_record_answer(session, extracted_answer='42')
        self.assertTrue(r['recorded'])
        self.assertEqual(r['verdict'], 'correct')

    def test_short_numeric_with_unit_suffix(self):
        """Reference '150°' should grade '150' as correct via the math
        grader's unit-strip + tolerance.
        """
        session, _ = _make_session()
        _seed_in_flight(
            session, question_type='short_numeric', reference_answer='150°',
            question_text='What angle remains?',
        )
        r = handle_record_answer(session, extracted_answer='150')
        self.assertTrue(r['recorded'])
        self.assertEqual(r['verdict'], 'correct')

    def test_empty_extracted_returns_error(self):
        session, _ = _make_session()
        _seed_in_flight(session)
        r = handle_record_answer(session, extracted_answer='   ')
        self.assertFalse(r['recorded'])
        self.assertIn('empty', r['error'])

    def test_no_in_flight_returns_error(self):
        """When no slot exists, record_answer refuses — the LLM is
        meant to call pose_question first, or treat the student's
        message as conversation.
        """
        session, _ = _make_session()
        # No slot.
        r = handle_record_answer(session, extracted_answer='B')
        self.assertFalse(r['recorded'])
        self.assertIn('no in-flight', r['error'])


# ============================================================================
# handle_pose_question (M12)
# ============================================================================


class HandlePoseQuestionTest(DjangoTestCase):

    def test_creates_in_flight_slot(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session,
            question_text='What is 5 + 3?',
            question_type='short_numeric',
            reference_answer='8',
            source='inline_authored',
        )
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.question_text, 'What is 5 + 3?')
        self.assertEqual(slot.question_type, 'short_numeric')
        self.assertEqual(slot.reference_answer, '8')
        self.assertEqual(slot.attempt_count, 0)
        self.assertEqual(slot.source, 'inline_authored')

    def test_pose_mcq_persists_options(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session,
            question_text='Which is largest?',
            question_type='mcq',
            options=['10', '100', '1000', '0.1'],
            reference_answer='C',
            source='inline_authored',
        )
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.options, ['10', '100', '1000', '0.1'])
        self.assertEqual(slot.reference_answer, 'C')

    def test_pose_replaces_prior_slot_and_logs_orphan(self):
        """If a slot already exists when pose_question fires, it's
        replaced — and the orphaned prior question is logged for audit
        (the student answered nothing on it).
        """
        session, _ = _make_session()
        _seed_in_flight(
            session, question_text='OLD Q', reference_answer='A',
            attempt_count=2,
        )

        r = handle_pose_question(
            session,
            question_text='NEW Q',
            question_type='short_answer',
            reference_answer='photosynthesis',
            source='inline_authored',
        )
        self.assertTrue(r['posed'])
        # Only one slot per session (OneToOne).
        slots = InFlightQuestion.objects.filter(session=session)
        self.assertEqual(slots.count(), 1)
        self.assertEqual(slots.first().question_text, 'NEW Q')
        # Orphan logged into engine_state for audit.
        session.refresh_from_db()
        orphans = (session.engine_state or {}).get('orphan_questions') or []
        self.assertGreaterEqual(len(orphans), 1)
        last = orphans[-1]
        self.assertEqual(last['question_text'], 'OLD Q')
        self.assertEqual(last['attempt_count'], 2)

    def test_pose_falls_back_to_short_answer_on_bad_type(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session,
            question_text='Q',
            question_type='fill_in_blank',  # disallowed
            reference_answer='x',
            source='inline_authored',
        )
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        # Falls back to short_answer rather than crashing.
        self.assertEqual(slot.question_type, 'short_answer')

    def test_pose_falls_back_to_inline_authored_on_bad_source(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session,
            question_text='Q',
            question_type='short_answer',
            reference_answer='x',
            source='made_up_source',
        )
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.source, 'inline_authored')

    def test_pose_empty_question_text_refuses(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session,
            question_text='   ',
            question_type='mcq',
            reference_answer='B',
            source='inline_authored',
        )
        self.assertFalse(r['posed'])
        self.assertIn('error', r)
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())

    def test_pose_empty_reference_refuses(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session,
            question_text='Q',
            question_type='mcq',
            reference_answer='',
            source='inline_authored',
        )
        self.assertFalse(r['posed'])
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())


# ============================================================================
# pose → record cycle (M12 integration)
# ============================================================================


class PoseThenRecordCycleTest(DjangoTestCase):

    def test_full_cycle_correct(self):
        session, _ = _make_session()

        # Cycle 1: tutor poses a question.
        p = handle_pose_question(
            session,
            question_text='What is 7 × 6?',
            question_type='short_numeric',
            reference_answer='42',
            source='inline_authored',
        )
        self.assertTrue(p['posed'])

        # Cycle 2: student answers correctly. Slot clears.
        g = handle_record_answer(session, extracted_answer='42')
        self.assertTrue(g['recorded'])
        self.assertEqual(g['verdict'], 'correct')
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())

    def test_full_cycle_wrong_then_correct(self):
        session, _ = _make_session()

        handle_pose_question(
            session,
            question_text='What is 7 × 6?',
            question_type='short_numeric',
            reference_answer='42',
            source='inline_authored',
        )

        # First attempt — wrong.
        g1 = handle_record_answer(session, extracted_answer='40')
        self.assertEqual(g1['verdict'], 'incorrect')
        self.assertEqual(g1['attempt_count_before'], 0)
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.attempt_count, 1)

        # Second attempt — wrong again, attempt_count climbs.
        g2 = handle_record_answer(session, extracted_answer='41')
        self.assertEqual(g2['verdict'], 'incorrect')
        self.assertEqual(g2['attempt_count_before'], 1)
        slot.refresh_from_db()
        self.assertEqual(slot.attempt_count, 2)

        # Third attempt — correct. Slot clears.
        g3 = handle_record_answer(session, extracted_answer='42')
        self.assertEqual(g3['verdict'], 'correct')
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())


# ============================================================================
# handle_request_figure
# ============================================================================


class HandleRequestFigureTest(DjangoTestCase):

    CATALOG = [
        {'id': 1, 'description': 'Map of Seychelles',
         'url': '/media/maps/seychelles.png', 'alt_text': 'map'},
        {'id': 2, 'description': 'Hydrological cycle',
         'url': '/media/diagrams/hydro.png', 'alt_text': 'cycle'},
    ]

    def test_valid_id_returns_url(self):
        session, _ = _make_session()
        r = handle_request_figure(
            session, figure_id=1, figure_catalog=self.CATALOG,
        )
        self.assertTrue(r['displayed'])
        self.assertEqual(r['url'], '/media/maps/seychelles.png')
        self.assertEqual(r['alt_text'], 'map')

    def test_invalid_id_returns_error_dict(self):
        session, _ = _make_session()
        r = handle_request_figure(
            session, figure_id=999, figure_catalog=self.CATALOG,
        )
        self.assertFalse(r['displayed'])
        self.assertIn('not in catalog', r['error'])

    def test_invalid_id_does_not_raise(self):
        # The whole point — handler must NEVER raise on bad input
        session, _ = _make_session()
        try:
            handle_request_figure(
                session, figure_id=-1, figure_catalog=self.CATALOG,
            )
        except Exception as exc:
            self.fail(f"handle_request_figure raised: {exc!r}")

    def test_empty_catalog_returns_error(self):
        session, _ = _make_session()
        r = handle_request_figure(
            session, figure_id=1, figure_catalog=[],
        )
        self.assertFalse(r['displayed'])

    def test_no_catalog_passed_returns_error(self):
        # Defensive — engine should always pass a catalog, but if it
        # doesn't, handler returns a graceful error rather than crashing
        session, _ = _make_session()
        r = handle_request_figure(session, figure_id=1)
        self.assertFalse(r['displayed'])

    def test_figures_disabled_on_course_refuses(self):
        """When Course.tutoring_images_enabled=False, handler returns
        error dict regardless of valid figure_id. Prevents the LLM
        from leaking figure references when the course turned them off.
        """
        session, _ = _make_session()
        course = session.lesson.unit.course
        course.tutoring_images_enabled = False
        course.save(update_fields=['tutoring_images_enabled'])
        r = handle_request_figure(
            session, figure_id=1, figure_catalog=self.CATALOG,
        )
        self.assertFalse(r['displayed'])
        self.assertIn('disabled', r['error'].lower())


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
    """Server-driven auto-advance (safety net). LLM's advance_step is
    the primary signal; this function only fires when:
      1. Student has ≥ competence_threshold CORRECT verdicts on step's
         objective (default 1 — "demonstrated enough").
      2. Soft turn cap exceeded on the current step.
    Per user direction: don't require ALL questions answered, just
    demonstrated competence.
    """

    def test_does_not_advance_when_no_verdicts(self):
        session, qs = _make_session(n_questions=3, n_steps=2)
        # No verdicts yet
        advanced = maybe_advance_step(session)
        self.assertFalse(advanced)
        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 0)

    def test_advances_after_one_correct_verdict(self):
        """Per user direction: 1 correct verdict is enough to advance.
        Doesn't require answering all questions for the objective.
        """
        session, qs = _make_session(n_questions=3, n_steps=2)
        _add_graded_turn(session, qs[0], verdict='correct')
        # qs[1], qs[2] still un-graded
        advanced = maybe_advance_step(session)
        self.assertTrue(advanced)
        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 1)

    def test_partial_verdict_does_not_count(self):
        """Only CORRECT verdicts count toward competence."""
        session, qs = _make_session(n_questions=3, n_steps=2)
        _add_graded_turn(session, qs[0], verdict='partial')
        _add_graded_turn(session, qs[1], verdict='incorrect')
        advanced = maybe_advance_step(session)
        self.assertFalse(advanced)

    def test_higher_threshold(self):
        # Caller can demand more correct verdicts per step
        session, qs = _make_session(n_questions=3, n_steps=2)
        _add_graded_turn(session, qs[0], verdict='correct')
        advanced = maybe_advance_step(session, competence_threshold=2)
        self.assertFalse(advanced)
        _add_graded_turn(session, qs[1], verdict='correct')
        advanced = maybe_advance_step(session, competence_threshold=2)
        self.assertTrue(advanced)

    def test_advance_bumps_step_index(self):
        """After a CORRECT verdict on the current step, the session
        moves to the next step. Post-tear-down the engine no longer
        touches current_question_id — that field is unused.
        """
        session, qs = _make_session(n_questions=1)
        _add_graded_turn(session, qs[0], verdict='correct')
        maybe_advance_step(session)
        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 1)

    def test_idempotent_when_no_more_to_advance(self):
        """Past the last step → no change on subsequent calls."""
        session, qs = _make_session(n_questions=1, n_steps=1)
        _add_graded_turn(session, qs[0], verdict='correct')
        maybe_advance_step(session)   # advances to step_index=1
        advanced_again = maybe_advance_step(session)
        self.assertFalse(advanced_again)

    def test_turn_cap_forces_advance_without_competence(self):
        """Even with no correct verdicts, server force-advances after
        the soft turn cap. forced=True logged in engine_state.
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


class CurrentStepCorrectVerdictCountTest(DjangoTestCase):
    """Helper underlying the competence trigger. Post-tear-down: uses
    SessionTurn.step FK + grader.verdict directly, no question_id
    matching.
    """

    def test_counts_only_correct_verdicts_on_current_step(self):
        session, qs = _make_session(n_questions=3, n_steps=2)
        _add_graded_turn(session, qs[0], verdict='correct')
        _add_graded_turn(session, qs[1], verdict='partial')
        _add_graded_turn(session, qs[2], verdict='incorrect')
        # 1 CORRECT verdict on the current step (step_index=0).
        self.assertEqual(_current_step_correct_verdict_count(session), 1)

    def test_zero_when_no_verdicts(self):
        session, _ = _make_session()
        self.assertEqual(_current_step_correct_verdict_count(session), 0)

    def test_verdicts_on_other_steps_dont_count(self):
        """Only verdicts on SessionTurn.step == current_step count."""
        from apps.curriculum.models import LessonStep
        session, qs = _make_session(n_questions=2, n_steps=2)
        # Add a correct verdict on step 1 (NOT the current step 0)
        step1 = LessonStep.objects.get(lesson=session.lesson, order_index=1)
        SessionTurn.objects.create(
            session=session, role='tutor', content='turn',
            step=step1,
            judge_outputs={'grader': {'verdict': 'correct'}},
        )
        # Engine is on step 0 — no correct verdicts on step 0 yet.
        self.assertEqual(_current_step_correct_verdict_count(session), 0)
