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

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.tutoring.models import (
    ExitTicket, ExitTicketAttempt, ExitTicketQuestion, InFlightQuestion,
    SessionTurn, TutorSession,
)
from ai_tutor.apps.tutoring.simple_tutor.tools import (
    DEFAULT_COMPETENCE_THRESHOLD,
    DEFAULT_POOL_SIZE,
    DEFAULT_STEP_TURN_CAP,
    build_question_pool,
    handle_pose_question,
    handle_pose_question_by_index,
    handle_record_answer,
    handle_request_figure,
    maybe_advance_step,
    maybe_complete_remediation,
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
    from ai_tutor.apps.curriculum.models import LessonStep
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
        """Under broader TUTORING_QUESTION_TYPES, the StepQuestion (which
        produces short_numeric/short_answer) is allowed and lands as the
        first pool entry. The 2026-05-28 staging fix narrowed the default
        to MCQ-only, so this test explicitly opts back into the broader
        allowlist to validate the legacy behaviour is still reachable.
        """
        import os
        from unittest import mock
        from ai_tutor.apps.curriculum.models import LessonStep
        with mock.patch.dict(
            os.environ, {'TUTORING_QUESTION_TYPES': 'mcq,short_numeric,short_answer'},
        ):
            session, _ = _make_session(
                n_questions=2,
                step_question='What is 7 × 8?',
                step_expected_answer='56',
            )
            step = LessonStep.objects.get(lesson=session.lesson, order_index=0)
            pool = build_question_pool(session)
            self.assertGreater(len(pool), 0)
            self.assertEqual(getattr(pool[0], 'source', None), 'lesson_step')
            self.assertEqual(pool[0].pk, step.pk)
            self.assertEqual(pool[0].question_text, 'What is 7 × 8?')

    def test_mcq_only_default_skips_lesson_step_question(self):
        """Under the 2026-05-28 MCQ-only default, the StepQuestion path
        (short_numeric / short_answer) is filtered out. The pool falls
        back to MCQ ExitTicketQuestions.
        """
        session, etqs = _make_session(
            n_questions=2,
            step_question='What is 7 × 8?',
            step_expected_answer='56',
        )
        pool = build_question_pool(session)
        # No lesson_step entry should appear when default = mcq-only.
        sources = [getattr(q, 'source', None) for q in pool]
        self.assertNotIn('lesson_step', sources)
        # Pool still has MCQ ETQs from the fixture.
        types = {getattr(q, 'question_type', '') for q in pool}
        self.assertLessEqual(types, {'mcq'}, msg=f"unexpected types: {types}")

    def test_empty_when_lesson_has_no_questions(self):
        session, _ = _make_session(n_questions=0)
        # No LessonStep.question + no ETQs → empty pool.
        self.assertEqual(build_question_pool(session), [])

    def test_excludes_fill_in_blank_and_matching(self):
        from ai_tutor.apps.tutoring.models import ExitTicketQuestion, ExitTicket
        from ai_tutor.apps.curriculum.models import LessonStep
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
        from ai_tutor.apps.tutoring.models import SessionTurn
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


# handle_redirect_off_topic and its tests were removed 2026-08-05 — the handler
# wrote an off_topic_count that nothing read, and the model called it once in
# 1,443 production turns. memory/tool_surface_reduction_plan.md.


# ============================================================================
# maybe_advance_step
# ============================================================================


# HandleAdvanceStepTest was removed with the tool it covered (2026-08-05).
# Advancement is now MaybeAdvanceStepTest's subject alone, and the remediation
# signal advance_step used to carry is covered by
# MaybeCompleteRemediationTest at the bottom of this file.


class MaybeAdvanceStepTest(DjangoTestCase):
    """Server-driven auto-advance — the ONLY advancement path since
    advance_step was removed. Fires when:
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
        from ai_tutor.apps.curriculum.models import LessonStep
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


class AntiDesyncGuardTest(DjangoTestCase):
    """Cycle-3 guard: posing a new question while an UNANSWERED one is in flight
    (attempt_count==0) is blocked (swapping the question out from under the
    student); a pivot after a wrong attempt (attempt_count>=1) is allowed."""

    def test_blocks_pose_when_student_answered(self):
        # Student ATTEMPTED an answer this turn → block posing over it (grade first).
        session, _ = _make_session()
        session.engine_state = {'_student_intent': 'answer'}
        _seed_in_flight(session, question_text='Q1?', question_type='short_numeric',
                        reference_answer='8', attempt_count=0)
        r = handle_pose_question(session, question_text='Q2?',
                                 question_type='short_numeric', reference_answer='9',
                                 source='inline_authored')
        self.assertFalse(r['posed'])
        self.assertTrue(r.get('premature'))
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.question_text, 'Q1?')      # prior kept, not swapped
        self.assertEqual(slot.reference_answer, '8')

    def test_allows_pivot_when_student_declined(self):
        # Student DECLINED ("idk" → non_engagement) → allow the tutor to pivot,
        # else it gets trapped re-posing into a dead slot (the deadlock this fixes).
        session, _ = _make_session()
        session.engine_state = {'_student_intent': 'non_engagement'}
        _seed_in_flight(session, question_text='Q1?', question_type='short_numeric',
                        reference_answer='8', attempt_count=0)
        r = handle_pose_question(session, question_text='Q2 easier?',
                                 question_type='short_numeric', reference_answer='9',
                                 source='inline_authored')
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.question_text, 'Q2 easier?')

    def test_allows_pivot_after_wrong_attempt(self):
        session, _ = _make_session()
        _seed_in_flight(session, question_text='Q1?', question_type='short_numeric',
                        reference_answer='8', attempt_count=1)
        r = handle_pose_question(session, question_text='Q2 easier?',
                                 question_type='short_numeric', reference_answer='9',
                                 source='inline_authored')
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.question_text, 'Q2 easier?')   # pivot replaced it

    def test_toggle_off_allows_replacement(self):
        import os
        session, _ = _make_session()
        _seed_in_flight(session, question_text='Q1?', question_type='short_numeric',
                        reference_answer='8', attempt_count=0)
        os.environ['SIMPLE_TUTOR_ANTIDESYNC'] = '0'
        try:
            r = handle_pose_question(session, question_text='Q2?',
                                     question_type='short_numeric', reference_answer='9',
                                     source='inline_authored')
        finally:
            os.environ.pop('SIMPLE_TUTOR_ANTIDESYNC', None)
        self.assertTrue(r['posed'])


class RepeatPoseRejectionTest(DjangoTestCase):
    """B2 (2026-07-18 bottleneck fixes) — posing a question the student
    already answered correctly is rejected once with corrective feedback;
    an immediate re-pose of the same stem is accepted so a turn is never
    left without a gradable slot (the existing force-advance backstop
    then walks the lesson forward)."""

    def _mark_answered_correct(self, session, stem):
        from ai_tutor.apps.tutoring.simple_tutor.tools import _norm_q
        es = session.engine_state or {}
        es['answered_correct'] = [_norm_q(stem)]
        session.engine_state = es
        session.save(update_fields=['engine_state'])

    def test_first_reask_rejected_with_guidance(self):
        session, _ = _make_session()
        stem = 'A student has a 70% probability of passing. P(not passing)?'
        self._mark_answered_correct(session, stem)
        r = handle_pose_question(
            session, question_text=stem, question_type='short_numeric',
            reference_answer='0.3', source='inline_authored',
        )
        self.assertFalse(r['posed'])
        self.assertIn('already answered', r['error'])
        self.assertFalse(
            InFlightQuestion.objects.filter(session=session).exists())

    def test_second_reask_of_same_stem_accepted(self):
        session, _ = _make_session()
        stem = 'A student has a 70% probability of passing. P(not passing)?'
        self._mark_answered_correct(session, stem)
        r1 = handle_pose_question(
            session, question_text=stem, question_type='short_numeric',
            reference_answer='0.3', source='inline_authored',
        )
        self.assertFalse(r1['posed'])
        r2 = handle_pose_question(
            session, question_text=stem, question_type='short_numeric',
            reference_answer='0.3', source='inline_authored',
        )
        self.assertTrue(r2['posed'])
        self.assertTrue(
            InFlightQuestion.objects.filter(session=session).exists())

    def test_fresh_question_not_rejected(self):
        session, _ = _make_session()
        self._mark_answered_correct(session, 'Old question already right?')
        r = handle_pose_question(
            session, question_text='A brand new question?',
            question_type='short_numeric',
            reference_answer='5', source='inline_authored',
        )
        self.assertTrue(r['posed'])


class PoseValidationCycle8Test(DjangoTestCase):
    """Cycle-8: pose-time validation catches slots that can never grade
    correctly (qwen deadlock: short_numeric slot with reference 'D' marked
    the correct answer '4' wrong forever)."""

    def test_short_numeric_with_letter_reference_rejected(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='Expected number of 6s in 24 rolls?',
            question_type='short_numeric', reference_answer='D',
            source='inline_authored')
        self.assertFalse(r['posed'])
        self.assertIn('numeric', r['error'])

    def test_short_numeric_accepts_numbers_fractions_percent(self):
        for ref in ('0.3', '3/4', '25%', '225°', ' 12 '):
            session, _ = _make_session()
            r = handle_pose_question(
                session, question_text='Q?', question_type='short_numeric',
                reference_answer=ref, source='inline_authored')
            self.assertTrue(r['posed'], f'rejected valid numeric ref {ref!r}')

    def test_mcq_option_letter_prefixes_stripped_at_pose(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='How many hearts?', question_type='mcq',
            options=['A) 11', 'B. 3.8', 'c: 31', '12'],
            reference_answer='A', source='inline_authored')
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.options, ['11', '3.8', '31', '12'])

    def test_mcq_without_options_rejected(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='Expected number of 6s in 24 rolls?',
            question_type='mcq', options=[],
            reference_answer='A', source='catalog')
        self.assertFalse(r['posed'])
        self.assertIn('options', r['error'])

    def test_mcq_with_four_options_still_posed(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='Q?', question_type='mcq',
            options=['4', '6', '8', '10'],
            reference_answer='A', source='inline_authored')
        self.assertTrue(r['posed'])

    # ── cycle-8b: salvage malformed poses instead of rejecting ──────────
    # Kimi cannot self-correct from a rejection (its Call-2 text scrubs to
    # empty), so a rejected pose became a slotless "Let's keep going."
    # deadlock loop — 12 deadlocks in the first cycle-8 kimi leg.

    def test_optionless_mcq_with_numeric_ref_salvaged_as_short_numeric(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='Expected number of 6s in 24 rolls?',
            question_type='mcq', options=[],
            reference_answer='4', source='catalog')
        self.assertTrue(r['posed'])
        self.assertEqual(r['question_type'], 'short_numeric')
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.question_type, 'short_numeric')
        self.assertEqual(slot.reference_answer, '4')

    def test_optionless_mcq_with_text_ref_salvaged_as_short_answer(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='Which direction is 225 degrees?',
            question_type='mcq', options=[],
            reference_answer='southwest', source='inline_authored')
        self.assertTrue(r['posed'])
        self.assertEqual(r['question_type'], 'short_answer')

    def test_optionless_mcq_with_letter_ref_still_rejected(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='Q?', question_type='mcq', options=[],
            reference_answer='B', source='catalog')
        self.assertFalse(r['posed'])

    def test_short_numeric_with_letter_ref_and_options_salvaged_as_mcq(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='Expected number of 6s?',
            question_type='short_numeric', options=['2', '3', '4', '6'],
            reference_answer='C', source='catalog')
        self.assertTrue(r['posed'])
        self.assertEqual(r['question_type'], 'mcq')

    def test_optionless_mcq_with_letter_ref_salvaged_from_catalog(self):
        # The catalog question exists with options; the model posed its stem
        # verbatim but dropped the options. Adopt them.
        session, questions = _make_session()
        q = questions[0]
        r = handle_pose_question(
            session, question_text=q.question_text,
            question_type='mcq', options=[],
            reference_answer='B', source='catalog')
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.question_type, 'mcq')
        self.assertEqual(slot.options, ['alpha', 'beta', 'gamma', 'delta'])


class CatalogLetterCoherenceTest(DjangoTestCase):
    """Cycle-9: the prompt's MCQ letter-rotation rule made models re-letter
    catalog questions whose option order is fixed — the slot's reference
    letter then disagreed with the displayed options, and correct answers
    were graded wrong for entire sessions (Beau Vallon: correct B rejected
    six times). For a catalog-stem match, the catalog's correct TEXT is
    the authority; the reference letter is derived from where that text
    sits in the options actually shown."""

    def test_same_order_wrong_letter_overridden(self):
        session, questions = _make_session()
        q = questions[0]  # options alpha/beta/gamma/delta, correct B
        r = handle_pose_question(
            session, question_text=q.question_text, question_type='mcq',
            options=['alpha', 'beta', 'gamma', 'delta'],
            reference_answer='C', source='catalog')
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, 'B')

    def test_rotated_options_letter_follows_correct_text(self):
        session, questions = _make_session()
        q = questions[0]
        r = handle_pose_question(
            session, question_text=q.question_text, question_type='mcq',
            options=['beta', 'alpha', 'delta', 'gamma'],
            reference_answer='C', source='catalog')
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, 'A')  # 'beta' sits at A

    def test_authored_question_keeps_model_letter(self):
        session, _ = _make_session()
        r = handle_pose_question(
            session, question_text='A question the catalog has never seen?',
            question_type='mcq',
            options=['x', 'y', 'z', 'w'],
            reference_answer='C', source='inline_authored')
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, 'C')

    def test_salvaged_options_adopt_catalog_letter(self):
        session, questions = _make_session()
        q = questions[0]
        r = handle_pose_question(
            session, question_text=q.question_text, question_type='mcq',
            options=[], reference_answer='D', source='catalog')
        self.assertTrue(r['posed'])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.options, ['alpha', 'beta', 'gamma', 'delta'])
        self.assertEqual(slot.reference_answer, 'B')


class OptionSetRepeatTest(DjangoTestCase):
    """gemma_probe5_v2: 12b re-asked an already-correct MCQ three times
    with reworded stems — the verbatim _norm_q guard never fired. An MCQ
    with the SAME option set is the same question regardless of stem
    wording; a variant with different options (changed numbers) is not."""

    def _grade_correct(self, session, *, stem, options, ref):
        handle_pose_question(
            session, question_text=stem, question_type='mcq',
            options=options, reference_answer=ref, source='inline_authored')
        handle_record_answer(session, extracted_answer=ref)

    def test_reworded_stem_same_options_rejected(self):
        session, _ = _make_session()
        opts = ['breaking rock without chemical change', 'dissolving by acid',
                'plant root growth', 'oxidation of minerals']
        self._grade_correct(
            session, stem='What is physical weathering?', options=opts, ref='A')
        r = handle_pose_question(
            session, question_text='Which option best defines physical '
            'weathering of rocks?', question_type='mcq',
            options=list(opts), reference_answer='A',
            source='inline_authored')
        self.assertFalse(r['posed'])
        self.assertIn('already answered', r['error'])

    def test_different_options_still_allowed(self):
        session, _ = _make_session()
        self._grade_correct(
            session, stem='Angles on a line: x + 120 = 180. x?',
            options=['60', '90', '120', '180'], ref='A')
        r = handle_pose_question(
            session, question_text='Angles on a line: x + 140 = 180. x?',
            question_type='mcq', options=['40', '90', '140', '180'],
            reference_answer='A', source='inline_authored')
        self.assertTrue(r['posed'])


# ============================================================================
# maybe_complete_remediation — server-owned exit-ticket retake signal
# ============================================================================
#
# Replaces the advance_step remediation branch removed 2026-08-05. The old path
# depended on the LLM calling a tool it used once in 1,443 production turns, so
# the retake was effectively unreachable. These tests cover the full
# fail -> remediate -> retake cycle, which the compliance harness never reaches.


def _failed_attempt(session, objective, *, asked=2, correct=0, passed=False,
                    failed_ids=None):
    """A completed, failed ExitTicketAttempt whose eo_competency marks
    ``objective`` as incomplete. completed_at is backdated so turns created
    afterwards in the test count as remediation.

    ``failed_ids`` names the questions that were wrong. Remediation completion
    counts QUESTIONS recovered, not objectives (2026-08-06: "if I missed 10
    questions the denominator should be 10"), so a fixture without them has
    nothing to recover and the flag can never flip."""
    from datetime import timedelta
    from django.utils import timezone
    ticket = ExitTicket.objects.get(lesson=session.lesson)
    attempt = ExitTicketAttempt.objects.create(
        exit_ticket=ticket, student=session.student, session=session,
        score=correct, passed=passed,
        answers={
            'per_question': [{'enabling_objective': objective, 'correct': False}],
            'eo_competency': {objective: {
                'asked': asked, 'correct': correct,
                'failed_question_ids': list(failed_ids or []),
            }},
        },
        completed_at=timezone.now() - timedelta(hours=1),
    )
    return attempt


class MaybeCompleteRemediationTest(DjangoTestCase):

    def test_flag_set_once_every_missed_question_is_recovered(self):
        session, questions = _make_session()
        objective = session.lesson.steps.first().enabling_objective
        _failed_attempt(session, objective, failed_ids=[questions[0].pk])

        # Not recovered yet — no correct verdict since the attempt.
        self.assertFalse(maybe_complete_remediation(session))
        session.refresh_from_db()
        self.assertNotIn('remediation_complete', session.engine_state or {})

        # Student answers the missed objective correctly during remediation.
        _add_graded_turn(session, questions[0], verdict='correct')

        self.assertTrue(maybe_complete_remediation(session))
        session.refresh_from_db()
        self.assertTrue(session.engine_state.get('remediation_complete'))

    def test_recovering_one_of_two_missed_questions_is_not_enough(self):
        """The counter the student reads is this same set. Completing at 1/2
        would re-open the quiz while the chip still said there was work left."""
        session, questions = _make_session()
        objective = session.lesson.steps.first().enabling_objective
        _failed_attempt(session, objective,
                        failed_ids=[questions[0].pk, questions[1].pk])
        _add_graded_turn(session, questions[0], verdict='correct')

        self.assertFalse(maybe_complete_remediation(session))
        from ai_tutor.apps.tutoring.simple_tutor.tools import remediation_progress
        self.assertEqual(remediation_progress(session),
                         {'recovered': 1, 'total': 2})

        _add_graded_turn(session, questions[1], verdict='correct')
        self.assertTrue(maybe_complete_remediation(session))

    def test_incorrect_verdict_does_not_complete_remediation(self):
        session, questions = _make_session()
        objective = session.lesson.steps.first().enabling_objective
        _failed_attempt(session, objective)
        _add_graded_turn(session, questions[0], verdict='incorrect')

        self.assertFalse(maybe_complete_remediation(session))
        session.refresh_from_db()
        self.assertNotIn('remediation_complete', session.engine_state or {})

    def test_turns_before_the_attempt_do_not_count(self):
        """A correct answer from BEFORE the exit ticket is not remediation —
        the student already got it wrong on the ticket itself.

        created_at is auto_now_add, so the pre-attempt turn has to be
        backdated explicitly; creating it earlier in the test body is not
        enough because _failed_attempt backdates the attempt an hour.
        """
        from datetime import timedelta
        from django.utils import timezone
        session, questions = _make_session()
        objective = session.lesson.steps.first().enabling_objective
        turn = _add_graded_turn(session, questions[0], verdict='correct')
        attempt = _failed_attempt(session, objective)
        SessionTurn.objects.filter(pk=turn.pk).update(
            created_at=attempt.completed_at - timedelta(minutes=5),
        )

        self.assertFalse(maybe_complete_remediation(session))

    def test_passed_attempt_never_triggers(self):
        session, questions = _make_session()
        objective = session.lesson.steps.first().enabling_objective
        _failed_attempt(session, objective, correct=2, passed=True)
        _add_graded_turn(session, questions[0], verdict='correct')

        self.assertFalse(maybe_complete_remediation(session))

    def test_no_attempt_is_a_noop(self):
        session, _ = _make_session()
        self.assertFalse(maybe_complete_remediation(session))

    def test_does_not_refire_while_flag_is_still_set(self):
        """The consumer clears the flag on the transition. Until it does, this
        must not re-set it — otherwise the modal loops."""
        session, questions = _make_session()
        objective = session.lesson.steps.first().enabling_objective
        _failed_attempt(session, objective, failed_ids=[questions[0].pk])
        _add_graded_turn(session, questions[0], verdict='correct')

        self.assertTrue(maybe_complete_remediation(session))
        self.assertFalse(maybe_complete_remediation(session))

    def test_never_raises_on_malformed_answers(self):
        session, questions = _make_session()
        objective = session.lesson.steps.first().enabling_objective
        attempt = _failed_attempt(session, objective)
        attempt.answers = {'eo_competency': 'not-a-dict'}
        attempt.save(update_fields=['answers'])

        self.assertFalse(maybe_complete_remediation(session))


# ============================================================================
# handle_pose_question_by_index — the tutor selects, never invents
# ============================================================================
#
# The model passes an index; the SERVER reads stem/options/answer off the bank
# row. This is what makes stem corruption impossible rather than merely
# filtered: on a 4B tutor, re-authoring mangled numeric notation
# (1:200,000 -> -200,000, 1/6 -> -1/6, 0.5 -> 5) while the reference answer kept
# its original value, so correct answers were marked wrong.
# See memory/catalog_only_questions_plan.md.


class PoseQuestionByIndexTest(DjangoTestCase):

    def test_server_renders_the_question_from_the_bank(self):
        session, questions = _make_session(n_questions=3)
        pool = list(questions)
        r = maybe = handle_pose_question_by_index(
            session, question_index=2, question_pool=pool)
        self.assertTrue(r['posed'])

        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.question_text, pool[1].question_text)
        self.assertEqual(slot.reference_answer, pool[1].correct_answer)
        # Always catalog — there is no other provenance now.
        self.assertEqual(slot.source, 'catalog')

    def test_model_supplied_text_cannot_reach_the_slot(self):
        """The whole point: there is no parameter through which a corrupted
        stem could travel, so a mangled question cannot be shown to a student.
        """
        from ai_tutor.apps.tutoring.simple_tutor.prompts import TOOL_SCHEMAS
        pose = next(t for t in TOOL_SCHEMAS if t['name'] == 'pose_question')
        self.assertEqual(set(pose['input_schema']['properties']),
                         {'question_index'})

    def test_out_of_range_index_is_rejected_not_guessed(self):
        session, questions = _make_session(n_questions=3)
        for bad in (0, -1, 4, 99):
            r = handle_pose_question_by_index(
                session, question_index=bad, question_pool=list(questions))
            self.assertFalse(r['posed'], f'index {bad} should be rejected')
            self.assertIn('out of range', r['error'])
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())

    def test_non_integer_index_is_rejected(self):
        session, questions = _make_session()
        for bad in ('two', None, {}):
            r = handle_pose_question_by_index(
                session, question_index=bad, question_pool=list(questions))
            self.assertFalse(r['posed'])

    def test_empty_pool_poses_nothing(self):
        """2% of steps have no catalog questions. Nothing to ask is a defined
        outcome, not a fallback into inline authoring."""
        session, _ = _make_session()
        r = handle_pose_question_by_index(
            session, question_index=1, question_pool=[])
        self.assertFalse(r['posed'])
        self.assertIn('empty', r['error'])
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())

    def test_mcq_options_come_from_the_bank_row(self):
        session, questions = _make_session(n_questions=1)
        handle_pose_question_by_index(
            session, question_index=1, question_pool=list(questions))
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.options, ['alpha', 'beta', 'gamma', 'delta'])
        self.assertEqual(slot.reference_answer, 'B')

    def test_short_numeric_reference_comes_from_answer_data(self):
        """Only MCQ keeps its answer in `correct_answer`. Reading that field
        for a short_numeric question yields an empty reference, which
        handle_pose_question rejects — the eval caught this as 8/8 poses
        rejected on math lesson 1144, the session asking nothing for 24 turns.
        """
        from types import SimpleNamespace
        session, _ = _make_session()
        pool = [SimpleNamespace(
            question_text='P(rain)=0.7. What is P(not rain)?',
            question_type='short_numeric', correct_answer='',
            answer_data={'computed': 0.3, 'unit': ''},
            option_a='', option_b='', option_c='', option_d='', pk=0,
        )]
        r = handle_pose_question_by_index(
            session, question_index=1, question_pool=pool)
        self.assertTrue(r['posed'], r.get('error'))
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, '0.3')

    def test_numeric_reference_prefers_computed_over_model_answer(self):
        """model_answer carries the unit ('165 degrees'); the numeric grader
        needs the bare value. Preferring model_answer here produced 4 rejected
        poses in the eval with "short_numeric requires a numeric
        reference_answer".
        """
        from types import SimpleNamespace
        session, _ = _make_session()
        pool = [SimpleNamespace(
            question_text='Angles around a point?', question_type='short_numeric',
            correct_answer='', answer_data={'model_answer': '165 deg', 'computed': 165.0},
            option_a='', option_b='', option_c='', option_d='', pk=0,
        )]
        r = handle_pose_question_by_index(session, question_index=1, question_pool=pool)
        self.assertTrue(r['posed'], r.get('error'))
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, '165.0')

    def test_model_answer_wins_over_computed(self):
        from types import SimpleNamespace
        session, _ = _make_session()
        pool = [SimpleNamespace(
            question_text='Explain why.', question_type='short_answer',
            correct_answer='',
            answer_data={'model_answer': 'because the scale is larger',
                         'computed': 99},
            option_a='', option_b='', option_c='', option_d='', pk=0,
        )]
        handle_pose_question_by_index(session, question_index=1, question_pool=pool)
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.reference_answer, 'because the scale is larger')

    def test_entry_with_no_reference_is_skipped_not_posed(self):
        from types import SimpleNamespace
        session, _ = _make_session()
        pool = [SimpleNamespace(
            question_text='Unanswerable', question_type='short_numeric',
            correct_answer='', answer_data={},
            option_a='', option_b='', option_c='', option_d='', pk=0,
        )]
        r = handle_pose_question_by_index(session, question_index=1, question_pool=pool)
        self.assertFalse(r['posed'])
        self.assertIn('no reference answer', r['error'])
