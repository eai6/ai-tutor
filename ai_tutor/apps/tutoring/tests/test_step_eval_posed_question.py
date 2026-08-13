"""Regression test for posed_question anchoring in step_eval context.

Production session 255 (2026-05-12) — on a worked_example step the
tutor asked "How many groups of 25 can you make from 200?", student
said "8", but step_eval mis-graded because posed_question was
populated from step.teacher_script (the static walkthrough
narrative) instead of the actual question the student answered.

Fix: invert priority — prior_q first, step.question second,
teacher_script last.
"""
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import TestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.tutoring.conversational_tutor import (
    ConversationalTutor,
    SessionState,
)
from ai_tutor.apps.tutoring.models import TutorSession


def _build_session(*, step_type='worked_example',
                   teacher_script='', step_question='',
                   expected_answer='8'):
    institution = Institution.objects.create(name="T", slug="t-pq")
    student = User.objects.create_user(username="stu-pq", password="x")
    course = Course.objects.create(
        institution=institution, title='Math', subject_type='math',
        is_published=True,
    )
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='L', objective='Solve', order_index=0,
        is_published=True,
    )
    step = LessonStep.objects.create(
        lesson=lesson, step_type=step_type, phase=step_type,
        order_index=0,
        teacher_script=teacher_script,
        question=step_question,
        expected_answer=expected_answer,
        answer_type='short_numeric',
    )
    session = TutorSession.objects.create(
        student=student, lesson=lesson, institution=institution,
    )
    return session, step


def _bind_tutor(session, *, conversation):
    tutor = object.__new__(ConversationalTutor)
    tutor.session = session
    tutor.lesson = session.lesson
    tutor.student = session.student
    tutor.steps = list(
        LessonStep.objects.filter(lesson=session.lesson)
        .order_by('order_index')
    )
    tutor.current_topic_index = 0
    tutor.step_exchange_count = 2
    tutor.exchange_count = 4
    tutor.session_state = SessionState.TUTORING
    tutor.conversation = list(conversation)
    tutor._pending_bank_grade = None
    tutor._pending_bank_question = None
    return tutor


class PosedQuestionAnchorsOnPriorTurnTest(TestCase):
    """The student's reply is to the IMMEDIATELY PRIOR tutor turn's
    question. The step's teacher_script is often a static narrative.
    Anchor on prior_q first."""

    def test_worked_example_uses_prior_tutor_question(self):
        # Worked-example step with a long narrative teacher_script.
        # Tutor's prior turn poses a specific sub-question. The
        # student's reply is to that sub-question.
        session, _step = _build_session(
            step_type='worked_example',
            teacher_script=(
                "Step-by-step worked example of solving 25x = 200. "
                "Shows: Step 1: Write the equation '25x = 200'; "
                "Step 2: Label the operation binding x..."
            ),
        )
        tutor = _bind_tutor(
            session,
            conversation=[
                {'role': 'user', 'content': 'okay'},
                {'role': 'assistant', 'content': (
                    "Not quite right on the division. Let's break "
                    "down 200 ÷ 25 step by step. How many groups of "
                    "25 can you make from 200?"
                )},
                {'role': 'user', 'content': '8'},
            ],
        )
        ctx = tutor._build_step_eval_context(
            student_input="8",
            tutor_response="Solve 5y = 70. First, divide ...",  # NEW response, irrelevant
        )
        self.assertIsNotNone(ctx)
        # posed_question must reflect the ACTUAL question the student
        # answered, not the static teacher_script narrative.
        self.assertEqual(
            ctx['posed_question'],
            "How many groups of 25 can you make from 200?",
        )

    def test_practice_step_with_prior_question_uses_prior_question(self):
        session, _step = _build_session(
            step_type='practice',
            teacher_script="Try this practice problem:",
            step_question="What is x?",
        )
        tutor = _bind_tutor(
            session,
            conversation=[
                {'role': 'user', 'content': 'ready'},
                {'role': 'assistant', 'content': "Solve 5y = 70. What is y?"},
                {'role': 'user', 'content': '14'},
            ],
        )
        ctx = tutor._build_step_eval_context(
            student_input="14",
            tutor_response="Now let's try x + 5 = 12.",
        )
        # Even on practice, the immediately-asked question wins.
        self.assertEqual(ctx['posed_question'], "What is y?")

    def test_falls_back_to_step_question_when_no_prior_question(self):
        # First turn of the step — no prior tutor question exists.
        session, _step = _build_session(
            step_type='practice',
            teacher_script="",
            step_question="What is x?",
        )
        tutor = _bind_tutor(
            session,
            conversation=[
                {'role': 'user', 'content': '8'},  # student answer with no prior assistant
            ],
        )
        ctx = tutor._build_step_eval_context(
            student_input="8",
            tutor_response="...",
        )
        self.assertEqual(ctx['posed_question'], "What is x?")

    def test_falls_back_to_teacher_script_only_as_last_resort(self):
        # No prior question + no step.question — only teacher_script
        # remains.
        session, _step = _build_session(
            step_type='practice',
            teacher_script="Compute the value.",
            step_question="",
        )
        tutor = _bind_tutor(
            session,
            conversation=[{'role': 'user', 'content': '5'}],
        )
        ctx = tutor._build_step_eval_context(
            student_input="5",
            tutor_response="...",
        )
        self.assertEqual(ctx['posed_question'], "Compute the value.")
