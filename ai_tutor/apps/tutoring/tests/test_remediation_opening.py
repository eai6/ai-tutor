"""Tests for the remediation opening message.

Pilot directive (2026-05-12):
  "The remediation should start with a recap of the exit ticket.
   It should state how many questions the student got correct and
   got wrong and list the questions one after the other for us to
   work on."

The opening is now deterministic (no LLM call) so the recap shape
is predictable + auditable.
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
from ai_tutor.apps.tutoring.models import (
    ExitTicket,
    ExitTicketQuestion,
    TutorSession,
)


def _setup_lesson_with_exit_ticket(*, n_questions: int = 5):
    institution = Institution.objects.create(name="T", slug="t-rem")
    student = User.objects.create_user(username="stu-rem", password="x")
    course = Course.objects.create(
        institution=institution, title='Math', subject_type='math',
        is_published=True,
    )
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='L', objective='Solve', order_index=0,
        is_published=True,
    )
    LessonStep.objects.create(
        lesson=lesson, step_type='practice', phase='practice',
        order_index=0,
    )
    exit_ticket = ExitTicket.objects.create(
        lesson=lesson, passing_score=4, is_published=True,
    )
    questions = []
    for i in range(n_questions):
        q = ExitTicketQuestion.objects.create(
            exit_ticket=exit_ticket,
            question_type='mcq',
            question_text=f"Question {i+1}: what is {i}+{i}?",
            option_a=str(i*2), option_b="wrong1",
            option_c="wrong2", option_d="wrong3",
            correct_answer='A',
            order_index=i,
        )
        questions.append(q)
    session = TutorSession.objects.create(
        student=student, lesson=lesson, institution=institution,
    )
    return session, exit_ticket, questions


def _bind_tutor(session, *, walkthrough_queue):
    tutor = object.__new__(ConversationalTutor)
    tutor.session = session
    tutor.lesson = session.lesson
    tutor.student = session.student
    tutor.steps = []
    tutor.current_topic_index = 0
    tutor.session_state = SessionState.TUTORING
    tutor.conversation = []
    tutor.remediation_attempt = 1
    # Seed the walkthrough queue
    state = session.engine_state or {}
    state['remediation_walkthrough_queue'] = walkthrough_queue
    state['remediation_phase'] = 'walkthrough'
    session.engine_state = state
    session.save(update_fields=['engine_state'])
    return tutor


class RemediationOpeningRecapTest(TestCase):
    def test_opening_includes_score_and_failed_question_count(self):
        session, _et, questions = _setup_lesson_with_exit_ticket(n_questions=5)
        queue = [{'id': questions[0].id, 'eo': ''}, {'id': questions[2].id, 'eo': ''}]
        tutor = _bind_tutor(session, walkthrough_queue=queue)

        opening, metadata = tutor._generate_remediation_opening(
            score=3, total=5,
            failed_questions=[{'id': q.id} for q in [questions[0], questions[2]]],
        )

        self.assertIn("Exit ticket review", opening)
        self.assertIn("3 of 5", opening)
        self.assertIn("3 right", opening)
        self.assertIn("2 to revisit", opening)

    def test_opening_lists_failed_questions(self):
        session, _et, questions = _setup_lesson_with_exit_ticket(n_questions=4)
        # Student missed q1 and q3
        queue = [
            {'id': questions[0].id, 'eo': ''},
            {'id': questions[2].id, 'eo': ''},
        ]
        tutor = _bind_tutor(session, walkthrough_queue=queue)

        opening, _ = tutor._generate_remediation_opening(
            score=2, total=4, failed_questions=[],
        )

        # Numbered list of failed questions appears
        self.assertIn("Questions we'll work on (2):", opening)
        # Each failed question's stem appears in the list
        self.assertIn(questions[0].question_text[:50], opening)
        self.assertIn(questions[2].question_text[:50], opening)

    def test_opening_poses_first_failed_question_with_label(self):
        session, _et, questions = _setup_lesson_with_exit_ticket(n_questions=3)
        queue = [{'id': questions[1].id, 'eo': ''}]
        tutor = _bind_tutor(session, walkthrough_queue=queue)

        opening, metadata = tutor._generate_remediation_opening(
            score=2, total=3, failed_questions=[],
        )

        self.assertIn("Question 1 of 1:", opening)
        # The actual MCQ stem + options should render
        self.assertIn(questions[1].question_text, opening)
        self.assertIn("A)", opening)

        # Bank ref recorded so the student's reply gets graded
        self.assertEqual(
            metadata['bank_question_ref']['kind'], 'exit_ticket_question',
        )
        self.assertEqual(
            metadata['bank_question_ref']['id'], questions[1].id,
        )

    def test_opening_handles_empty_failed_queue_gracefully(self):
        session, _et, _q = _setup_lesson_with_exit_ticket(n_questions=3)
        tutor = _bind_tutor(session, walkthrough_queue=[])

        opening, metadata = tutor._generate_remediation_opening(
            score=3, total=3, failed_questions=[],
        )

        self.assertIn("Exit ticket review", opening)
        self.assertIn("3 out of 3", opening)
        self.assertEqual(metadata, {})

    def test_opening_truncates_long_question_lists(self):
        session, _et, questions = _setup_lesson_with_exit_ticket(n_questions=15)
        queue = [{'id': q.id, 'eo': ''} for q in questions[:12]]
        tutor = _bind_tutor(session, walkthrough_queue=queue)

        opening, _ = tutor._generate_remediation_opening(
            score=3, total=15, failed_questions=[],
        )

        # Caps at 10 list items + an ellipsis line
        self.assertIn("…and 2 more.", opening)

    def test_opening_is_deterministic_no_llm_call(self):
        """The opening should be plain Python — no LLM call required.
        We verify by NOT mocking _generate_response and confirming
        the call completes without trying to invoke the LLM."""
        session, _et, questions = _setup_lesson_with_exit_ticket(n_questions=3)
        queue = [{'id': questions[0].id, 'eo': ''}]
        tutor = _bind_tutor(session, walkthrough_queue=queue)
        # If the implementation tried to call self._generate_response,
        # this MagicMock would intercept and we'd see it called.
        tutor._generate_response = MagicMock(return_value="LLM PROSE")

        opening, _ = tutor._generate_remediation_opening(
            score=2, total=3, failed_questions=[],
        )

        # LLM stub must NOT have been called
        tutor._generate_response.assert_not_called()
        # And the opening should NOT contain the stub's marker
        self.assertNotIn("LLM PROSE", opening)
