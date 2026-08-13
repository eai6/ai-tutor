"""Tests for tutoring-pool exclusion in exit-ticket selection.

Pins: ``_load_exit_ticket_concepts`` excludes ``engine_state[
'question_pool_ids']`` (the per-session tutoring bank pool) from
the candidate set so the exit ticket has zero overlap with what
the tutor practised during the session.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor
from ai_tutor.apps.tutoring.models import (
    ExitTicket,
    ExitTicketQuestion,
    TutorSession,
)


def _build_lesson(*, n_questions: int = 25):
    """Construct a published lesson with N exit-ticket questions."""
    institution = Institution.objects.create(name="T", slug="t-ex-excl")
    student = User.objects.create_user(username="stu-excl", password="x")
    course = Course.objects.create(
        institution=institution, title="Math S3",
        grade_level="S3", is_published=True,
    )
    unit = Unit.objects.create(
        course=course, title="Geometry", order_index=0,
    )
    lesson = Lesson.objects.create(
        unit=unit, title="Angles", objective="Use 360°",
        order_index=0, is_published=True,
    )
    LessonStep.objects.create(
        lesson=lesson, phase='practice', step_type='practice',
        order_index=0,
        teacher_script="Find x given a=95, b=70.",
        expected_answer="195",
    )
    exit_ticket = ExitTicket.objects.create(
        lesson=lesson, passing_score=8, is_published=True,
    )
    questions = [
        ExitTicketQuestion.objects.create(
            exit_ticket=exit_ticket,
            question_text=f"Bank Q{i}",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="angles_around_point" if i < n_questions // 2
                        else "angles_on_line",
            order_index=i,
        )
        for i in range(n_questions)
    ]
    session = TutorSession.objects.create(
        student=student, lesson=lesson, institution=institution,
    )
    return session, questions


def _bind_tutor(session):
    """Construct a ConversationalTutor instance bypassing __init__.

    _load_exit_ticket_concepts only reads ``self.session``, ``self.lesson``
    and ``self.student`` — no need to spin up the full engine for this
    unit test.
    """
    tutor = object.__new__(ConversationalTutor)
    tutor.session = session
    tutor.lesson = session.lesson
    tutor.student = session.student
    return tutor


class ExitTicketTutoringExclusionTest(TestCase):
    def test_exit_ticket_excludes_tutoring_pool(self):
        session, questions = _build_lesson(n_questions=25)
        # Mark the first 10 question IDs as the per-session tutoring pool
        # (mirrors what sample_session_pool writes at session start).
        pool_ids = [q.id for q in questions[:10]]
        session.engine_state = {'question_pool_ids': pool_ids}
        session.save(update_fields=['engine_state'])

        tutor = _bind_tutor(session)
        concepts = tutor._load_exit_ticket_concepts()

        selected_ids = {c['id'] for c in concepts}
        self.assertEqual(len(selected_ids), 10)
        # Zero overlap with the tutoring pool — the requirement
        self.assertEqual(
            selected_ids & set(pool_ids), set(),
            f"Exit ticket should NOT include any tutoring pool IDs. "
            f"Overlap: {selected_ids & set(pool_ids)}",
        )

    def test_no_pool_means_no_exclusion(self):
        """When engine_state has no tutoring pool yet (e.g., a session
        that went straight to exit ticket without using the bank), the
        new constraint is a no-op."""
        session, questions = _build_lesson(n_questions=15)
        session.engine_state = {}
        session.save(update_fields=['engine_state'])

        tutor = _bind_tutor(session)
        concepts = tutor._load_exit_ticket_concepts()
        self.assertEqual(len(concepts), 10)

    def test_fallback_when_bank_too_small_for_strict_separation(self):
        """Bank of 12 - tutoring pool of 10 = 2 left after strict
        exclusion. Fallback should relax and ship 10 questions anyway
        rather than ship a 2-question exit ticket. Some overlap will
        occur but the warning log signals the under-sized bank."""
        session, questions = _build_lesson(n_questions=12)
        pool_ids = [q.id for q in questions[:10]]
        session.engine_state = {'question_pool_ids': pool_ids}
        session.save(update_fields=['engine_state'])

        tutor = _bind_tutor(session)
        concepts = tutor._load_exit_ticket_concepts()
        # Fallback engaged — 10 questions delivered.
        self.assertEqual(len(concepts), 10)

    def test_exclusion_holds_across_multiple_calls(self):
        """Selection is randomised per call (cached in engine_state by a
        separate code path in the engine), but the exclusion invariant
        must hold every time."""
        session, questions = _build_lesson(n_questions=25)
        pool_ids = [q.id for q in questions[:10]]
        session.engine_state = {'question_pool_ids': pool_ids}
        session.save(update_fields=['engine_state'])

        tutor = _bind_tutor(session)
        for _ in range(5):
            session.refresh_from_db()
            selected = {c['id'] for c in tutor._load_exit_ticket_concepts()}
            self.assertEqual(
                selected & set(pool_ids), set(),
                "Every call must exclude the tutoring pool, "
                "regardless of randomisation order.",
            )
