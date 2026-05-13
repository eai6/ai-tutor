"""Tests for the walkthrough → fresh-exit-ticket handoff.

Pilot directive (2026-05-12):
  "remove the requiz from the remediation. It is actually too long,
   so let us just focus on what the students got wrong and go back
   to the exit ticket after the questions have been walked through.
   new questions are sampled for the exit ticket so the students
   are evaluated there."

After this change, remediation has ONE phase (walkthrough). When all
failed exit-ticket questions have been walked through, the engine
flips back to EXIT_TICKET state with a FRESH question sample.
"""
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.tutoring.conversational_tutor import (
    ConversationalTutor,
    SessionState,
)
from apps.tutoring.models import (
    ExitTicket,
    ExitTicketQuestion,
    TutorSession,
)


def _setup():
    institution = Institution.objects.create(name="T", slug="t-phase")
    student = User.objects.create_user(username="stu-phase", password="x")
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
    for i in range(4):
        q = ExitTicketQuestion.objects.create(
            exit_ticket=exit_ticket,
            question_type='mcq',
            question_text=f"Q{i+1}: {i}+{i}?",
            option_a=str(i*2), option_b="w1",
            option_c="w2", option_d="w3",
            correct_answer='A', order_index=i,
        )
        questions.append(q)
    session = TutorSession.objects.create(
        student=student, lesson=lesson, institution=institution,
    )
    return session, exit_ticket, questions


def _bind(session, *, is_remediation=True):
    tutor = object.__new__(ConversationalTutor)
    tutor.session = session
    tutor.lesson = session.lesson
    tutor.student = session.student
    tutor.steps = []
    tutor.current_topic_index = 0
    tutor.session_state = SessionState.TUTORING
    tutor.conversation = []
    tutor.exit_ticket_concepts = []
    tutor.remediation_attempt = 1
    tutor.is_remediation = is_remediation
    return tutor


class FinishRemediationHandsOffToExitTicketTest(TestCase):
    """_finish_remediation flips state → EXIT_TICKET so the caller
    fires _handle_exit_ticket() on this same turn. No requiz phase."""

    def test_state_transitions_to_exit_ticket(self):
        session, _et, _questions = _setup()
        tutor = _bind(session, is_remediation=True)
        state = {
            'remediation_phase': 'walkthrough',
            'remediation_walkthrough_queue': [{'id': 1}],
            'remediation_walkthrough_index': 1,
            'selected_exit_ticket_ids': [1, 2, 3],
        }

        out = tutor._finish_remediation(state, clean_response="ok")

        self.assertEqual(tutor.session_state, SessionState.EXIT_TICKET)
        self.assertFalse(tutor.is_remediation)
        # Closing message tells the student a fresh exit ticket is coming
        self.assertIn("Review complete", out)
        self.assertIn("Fresh exit ticket", out)
        self.assertIn("NEW", out)

    def test_clears_remediation_engine_state_keys(self):
        session, _et, _q = _setup()
        tutor = _bind(session, is_remediation=True)
        state = {
            'remediation_phase': 'walkthrough',
            'remediation_walkthrough_queue': [{'id': 1}],
            'remediation_walkthrough_index': 2,
            'walkthrough_attempts_on_current': 3,
            'selected_exit_ticket_ids': [10, 11],
            'covered_concept_ids': [10],
        }

        tutor._finish_remediation(state, clean_response="x")

        mutated = tutor.session.engine_state
        # Remediation keys cleared
        self.assertNotIn('remediation_walkthrough_queue', mutated)
        self.assertNotIn('remediation_walkthrough_index', mutated)
        self.assertNotIn('walkthrough_attempts_on_current', mutated)
        # Exit-ticket replay state cleared so the next attempt resamples
        self.assertNotIn('selected_exit_ticket_ids', mutated)
        self.assertEqual(mutated.get('covered_concept_ids'), [])
        # Phase advanced
        self.assertEqual(mutated.get('remediation_phase'), 'done')

    def test_no_requiz_phase_set(self):
        """The walkthrough-complete branch must not leave the engine
        in 'requiz' phase — that branch was removed 2026-05-12."""
        session, _et, _q = _setup()
        tutor = _bind(session, is_remediation=True)
        state = {
            'remediation_phase': 'walkthrough',
            'remediation_walkthrough_queue': [{'id': 1}],
            'remediation_walkthrough_index': 0,
        }
        tutor._finish_remediation(state, clean_response="x")

        self.assertEqual(
            tutor.session.engine_state['remediation_phase'], 'done',
        )
        # No requiz keys persisted
        self.assertNotIn(
            'remediation_requiz_queue', tutor.session.engine_state,
        )


class WalkthroughEndCallsFinishRemediationTest(TestCase):
    """When the walkthrough index walks past the queue end, the engine
    routes to _finish_remediation (NOT _begin_requiz)."""

    def test_walkthrough_exhausted_triggers_finish(self):
        session, _et, questions = _setup()
        tutor = _bind(session, is_remediation=True)
        # Single-question walkthrough queue, currently on the last one.
        state = {
            'remediation_phase': 'walkthrough',
            'remediation_walkthrough_queue': [
                {'id': questions[0].id, 'eo': 'EO A'},
            ],
            'remediation_walkthrough_index': 0,
            'walkthrough_attempts_on_current': 0,
        }
        session.engine_state = state
        session.save(update_fields=['engine_state'])

        # Simulate a correct bank answer that triggers the advance.
        tutor._pending_bank_grade = MagicMock(is_correct=True)

        out = tutor._maybe_advance_walkthrough(
            clean_response="great job",
            turn_metadata={},
        )

        # Walkthrough done → handed off to fresh exit ticket
        self.assertEqual(tutor.session_state, SessionState.EXIT_TICKET)
        self.assertFalse(tutor.is_remediation)
        self.assertIn("Fresh exit ticket", out)
