"""M6 acceptance tests — session state utilities.

Tests against real DB fixtures because the functions query SessionTurn
+ LessonStep ORMs. Fixture shape mirrors apps/tutoring/tests/test_question_bank.py
per testing-patterns-expert guidance (order_index not order; Institution
needs name+slug).
"""
from unittest import TestCase

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.tutoring.models import TutorSession, SessionTurn
from apps.tutoring.simple_tutor.state import (
    DEFAULT_RECENT_WINDOW_TURNS,
    build_recent_window,
    build_step_summary,
    step_summary_log,
    _current_step,
    _step_label,
)

User = get_user_model()


_fixture_counter = {'n': 0}


def _make_session_with_lesson(n_steps: int = 3, current_step_index: int = 0):
    """Build a session with a lesson that has ``n_steps`` LessonSteps."""
    _fixture_counter['n'] += 1
    i = _fixture_counter['n']

    institution = Institution.objects.create(
        name=f'Test School {i}', slug=f'test-{i}',
    )
    user = User.objects.create_user(username=f'student-{i}', password='x')
    course = Course.objects.create(
        title=f'Test Course {i}',
        institution=institution,
        grade_level='S3',
        is_published=True,
    )
    unit = Unit.objects.create(course=course, title='Unit', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='Lesson', objective='x',
        order_index=0, is_published=True,
    )
    phases = ['engage', 'explore', 'explain', 'elaborate', 'evaluate']
    for idx in range(n_steps):
        LessonStep.objects.create(
            lesson=lesson,
            teacher_script=f'Step {idx + 1} script',
            question=f'Step {idx + 1} question?',
            expected_answer='42',
            phase=phases[idx % len(phases)],
            order_index=idx,
        )
    session = TutorSession.objects.create(
        institution=institution,
        student=user,
        lesson=lesson,
        engine='simple',
        current_step_index=current_step_index,
    )
    return session


def _add_turn(session, role, content, step=None, judge_outputs=None):
    """Helper: create a SessionTurn linked to a step."""
    return SessionTurn.objects.create(
        session=session,
        role=role,
        content=content,
        step=step,
        judge_outputs=judge_outputs or {},
    )


# ============================================================================
# build_recent_window
# ============================================================================


class BuildRecentWindowTest(DjangoTestCase):

    def test_empty_session_returns_empty(self):
        session = _make_session_with_lesson()
        self.assertEqual(build_recent_window(session), [])

    def test_returns_turns_in_chronological_order(self):
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        t1 = _add_turn(session, 'student', 'hi', step=step)
        t2 = _add_turn(session, 'tutor',   'hello', step=step)
        t3 = _add_turn(session, 'student', 'q1?', step=step)

        window = build_recent_window(session)
        self.assertEqual([t.pk for t in window], [t1.pk, t2.pk, t3.pk])

    def test_respects_max_turns_cap(self):
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        # Create 12 turns; cap is 5
        for i in range(12):
            _add_turn(session, 'student' if i % 2 == 0 else 'tutor',
                      f'msg {i}', step=step)
        # Use a high max_tutor_turns to test the overall cap, not the
        # tutor-turn cap.
        window = build_recent_window(session, max_turns=5, max_tutor_turns=10)
        self.assertEqual(len(window), 5)
        # Should be the last 5
        self.assertEqual(window[-1].content, 'msg 11')
        self.assertEqual(window[0].content, 'msg 7')

    def test_tutor_turn_cap_drops_older_questions(self):
        """max_tutor_turns=2 keeps only the most recent in-flight tutor
        turn plus at most one prior hint. Older tutor turns (from a
        different settled question) get dropped to prevent the LLM
        from referencing the wrong question in hints.
        Regression for 2026-05-26 staging E2E.
        """
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        # Conversation: tutor(Q1) → student → tutor(Q2-graded+Q3) → student
        # → tutor(Q4) — alternating. With max_tutor_turns=2, only the
        # two most recent tutor turns should remain.
        for i in range(8):
            role = 'tutor' if i % 2 == 0 else 'student'
            _add_turn(session, role, f'msg {i}', step=step)
        # Last turn is i=7 (student). Tutor turns at i = 0, 2, 4, 6.
        # With cap of 2, we keep tutors at i=6 and i=4, plus the
        # student turns from i=5..7.
        window = build_recent_window(
            session, max_turns=10, max_tutor_turns=2,
        )
        tutor_contents = [t.content for t in window if t.role == 'tutor']
        self.assertEqual(tutor_contents, ['msg 4', 'msg 6'])
        # Older tutor turns (msg 0, msg 2) are NOT in the window
        all_contents = [t.content for t in window]
        self.assertNotIn('msg 0', all_contents)
        self.assertNotIn('msg 2', all_contents)

    def test_respects_step_boundary(self):
        """Turns from prior steps must NOT appear in the window."""
        session = _make_session_with_lesson(n_steps=3, current_step_index=1)
        step1 = session.lesson.steps.get(order_index=0)
        step2 = session.lesson.steps.get(order_index=1)

        # Step 1 has 3 turns
        for content in ['s1-a', 's1-b', 's1-c']:
            _add_turn(session, 'student', content, step=step1)
        # Step 2 has 2 turns
        _add_turn(session, 'student', 's2-a', step=step2)
        _add_turn(session, 'tutor',   's2-b', step=step2)

        window = build_recent_window(session)
        # Only step 2's turns should be present
        contents = [t.content for t in window]
        self.assertEqual(contents, ['s2-a', 's2-b'])

    def test_zero_max_returns_empty(self):
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        _add_turn(session, 'student', 'hi', step=step)
        self.assertEqual(build_recent_window(session, max_turns=0), [])

    def test_no_current_step_falls_back_to_whole_session(self):
        # Session past the last step (exit ticket mode)
        session = _make_session_with_lesson(n_steps=2, current_step_index=99)
        step = session.lesson.steps.first()
        _add_turn(session, 'student', 'hi', step=step)
        _add_turn(session, 'tutor',   'hello', step=step)
        window = build_recent_window(session)
        # Fallback returns ALL session turns (capped by max_turns)
        self.assertEqual(len(window), 2)

    def test_default_window_size_is_8(self):
        self.assertEqual(DEFAULT_RECENT_WINDOW_TURNS, 8)


# ============================================================================
# build_step_summary
# ============================================================================


class BuildStepSummaryTest(DjangoTestCase):

    def test_no_turns_returns_no_responses(self):
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        summary = build_step_summary(session, step)
        self.assertIn('no student responses', summary)
        self.assertIn('Step 1', summary)
        self.assertIn('Engage', summary)

    def test_summary_includes_step_phase(self):
        session = _make_session_with_lesson()
        step = session.lesson.steps.get(order_index=1)  # 'explore'
        summary = build_step_summary(session, step)
        self.assertIn('Explore', summary)
        self.assertIn('Step 2', summary)

    def test_counts_student_attempts(self):
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        _add_turn(session, 'student', 'attempt 1', step=step)
        _add_turn(session, 'tutor',   't 1', step=step,
                  judge_outputs={'grader': {'verdict': 'incorrect'}})
        _add_turn(session, 'student', 'attempt 2', step=step)
        _add_turn(session, 'tutor',   't 2', step=step,
                  judge_outputs={'grader': {'verdict': 'correct'}})

        summary = build_step_summary(session, step)
        self.assertIn('2 student attempt(s)', summary)

    def test_lists_verdicts_in_order(self):
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        _add_turn(session, 'student', 'a', step=step)
        _add_turn(session, 'tutor', 'b', step=step,
                  judge_outputs={'grader': {'verdict': 'incorrect'}})
        _add_turn(session, 'student', 'c', step=step)
        _add_turn(session, 'tutor', 'd', step=step,
                  judge_outputs={'grader': {'verdict': 'partial'}})
        _add_turn(session, 'student', 'e', step=step)
        _add_turn(session, 'tutor', 'f', step=step,
                  judge_outputs={'grader': {'verdict': 'correct'}})

        summary = build_step_summary(session, step)
        self.assertIn('incorrect, partial, correct', summary)

    def test_deterministic(self):
        """Same inputs → identical string. No randomness, no LLM."""
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        _add_turn(session, 'student', 'a', step=step)
        _add_turn(session, 'tutor', 'b', step=step,
                  judge_outputs={'grader': {'verdict': 'correct'}})
        s1 = build_step_summary(session, step)
        s2 = build_step_summary(session, step)
        s3 = build_step_summary(session, step)
        self.assertEqual(s1, s2)
        self.assertEqual(s2, s3)

    def test_student_response_without_grade_distinguished(self):
        # Student responded but tutor turn had no grader entry → not
        # an attempt-with-verdict.
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        _add_turn(session, 'student', 'what does X mean?', step=step)
        _add_turn(session, 'tutor', 'X means...', step=step)
        summary = build_step_summary(session, step)
        self.assertIn('no graded answers', summary)
        self.assertIn('1 student response', summary)

    def test_ignores_other_judge_keys(self):
        # judge_outputs may contain other judges' verdicts (e.g. safety,
        # coherence) — only the 'grader' key counts here.
        session = _make_session_with_lesson()
        step = session.lesson.steps.first()
        _add_turn(session, 'student', 'a', step=step)
        _add_turn(session, 'tutor', 'b', step=step,
                  judge_outputs={
                      'safety': {'verdict': 'pass'},
                      'grader': {'verdict': 'correct'},
                      'coherence': {'verdict': 'ok'},
                  })
        summary = build_step_summary(session, step)
        # Only the grader entry contributes
        self.assertIn('correct', summary)
        self.assertNotIn('pass', summary)


# ============================================================================
# step_summary_log
# ============================================================================


class StepSummaryLogTest(DjangoTestCase):

    def test_session_on_step_0_returns_empty(self):
        session = _make_session_with_lesson(current_step_index=0)
        self.assertEqual(step_summary_log(session), [])

    def test_returns_completed_steps_in_order(self):
        session = _make_session_with_lesson(n_steps=4, current_step_index=2)
        # Add some turns to steps 0 and 1
        step0 = session.lesson.steps.get(order_index=0)
        step1 = session.lesson.steps.get(order_index=1)
        _add_turn(session, 'student', 'a', step=step0)
        _add_turn(session, 'student', 'b', step=step1)

        log = step_summary_log(session)
        self.assertEqual(len(log), 2)   # steps 0 and 1
        self.assertIn('Step 1', log[0])
        self.assertIn('Step 2', log[1])

    def test_current_step_not_in_log(self):
        # Step 2 is in progress; it should NOT appear in the log
        session = _make_session_with_lesson(n_steps=4, current_step_index=1)
        log = step_summary_log(session)
        # Only step 0 is "completed"
        self.assertEqual(len(log), 1)
        self.assertIn('Step 1', log[0])

    def test_session_past_all_steps_returns_all(self):
        session = _make_session_with_lesson(n_steps=3, current_step_index=99)
        log = step_summary_log(session)
        self.assertEqual(len(log), 3)


# NOTE: set_current_question / clear_current_question tests were
# removed 2026-05-26 (M11.3). The deterministic question anchor was
# retired — see state.py for the rationale. The schema fields remain
# on TutorSession but the simple-tutor engine no longer reads/writes
# them.


# ============================================================================
# Internal helpers
# ============================================================================


class CurrentStepHelperTest(DjangoTestCase):

    def test_resolves_current_step(self):
        session = _make_session_with_lesson(n_steps=3, current_step_index=1)
        step = _current_step(session)
        self.assertIsNotNone(step)
        self.assertEqual(step.order_index, 1)

    def test_returns_none_when_past_last(self):
        session = _make_session_with_lesson(n_steps=2, current_step_index=99)
        self.assertIsNone(_current_step(session))

    def test_returns_none_when_no_steps(self):
        session = _make_session_with_lesson(n_steps=0, current_step_index=0)
        self.assertIsNone(_current_step(session))


class StepLabelTest(TestCase):
    """Doesn't need DB — _step_label is pure."""

    def test_lesson_step_object(self):
        from types import SimpleNamespace
        step = SimpleNamespace(order_index=2, phase='explore')
        self.assertEqual(_step_label(step), 'Step 3 (Explore)')

    def test_step_without_phase(self):
        from types import SimpleNamespace
        step = SimpleNamespace(order_index=0, phase='')
        self.assertEqual(_step_label(step), 'Step 1')

    def test_int_index(self):
        self.assertEqual(_step_label(2), 'Step 3')

    def test_none(self):
        self.assertEqual(_step_label(None), 'Step ?')
