"""Tests for the lesson restart endpoint.

The student-facing "Restart lesson" button POSTs to
/tutor/api/chat/restart/<lesson_id>/. This must:
  - Archive any existing Active or Completed sessions for this
    student+lesson (status → ABANDONED, ended_at set).
  - PRESERVE student-level data: StudentLessonProgress mastery,
    ExitTicketAttempt history, StudentCompetencyRecord.
  - Leave the door open for chat_start_session to create a fresh
    session on the next call.
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import (
    ExitTicket,
    ExitTicketAttempt,
    ExitTicketQuestion,
    StudentLessonProgress,
    TutorSession,
)


class RestartLessonTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="R", slug="r")
        cls.student = User.objects.create_user(username="rstu", password="pw")
        Membership.objects.create(
            user=cls.student,
            institution=cls.institution,
            role="student",
        )
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math S2",
            grade_level="S2", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(course=cls.course, title="U", order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="x",
            order_index=0, is_published=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.student)

    def test_restart_archives_active_session(self):
        active = TutorSession.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=self.lesson,
            status=TutorSession.Status.ACTIVE,
            engine_state={'current_topic_index': 3},
        )
        url = reverse('tutoring:chat_restart_session', args=[self.lesson.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['restarted'])
        self.assertEqual(body['sessions_archived'], 1)
        active.refresh_from_db()
        self.assertEqual(active.status, TutorSession.Status.ABANDONED)
        self.assertIsNotNone(active.ended_at)

    def test_restart_archives_completed_session(self):
        # Student already passed once — restart still archives so they
        # can redo from scratch. (The chat_start_session "review" path
        # is bypassed because the completed session is now abandoned.)
        completed = TutorSession.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=self.lesson,
            status=TutorSession.Status.COMPLETED,
            ended_at=timezone.now(),
        )
        url = reverse('tutoring:chat_restart_session', args=[self.lesson.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        completed.refresh_from_db()
        self.assertEqual(completed.status, TutorSession.Status.ABANDONED)

    def test_restart_preserves_student_lesson_progress(self):
        # Student has mastery on this lesson — must survive the restart
        progress = StudentLessonProgress.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=self.lesson,
            mastery_level='mastered',
            best_score=0.9,
        )
        TutorSession.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=self.lesson,
            status=TutorSession.Status.ACTIVE,
        )
        self.client.post(reverse('tutoring:chat_restart_session', args=[self.lesson.id]))
        progress.refresh_from_db()
        self.assertEqual(progress.mastery_level, 'mastered')
        self.assertEqual(progress.best_score, 0.9)

    def test_restart_preserves_exit_ticket_attempts(self):
        ticket = ExitTicket.objects.create(
            lesson=self.lesson, passing_score=8, is_published=True,
        )
        ExitTicketQuestion.objects.create(
            exit_ticket=ticket,
            question_text="Q", option_a="A", option_b="B",
            option_c="C", option_d="D",
            correct_answer="A", explanation="",
            order_index=0,
        )
        sess = TutorSession.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=self.lesson,
            status=TutorSession.Status.ACTIVE,
        )
        attempt = ExitTicketAttempt.objects.create(
            exit_ticket=ticket,
            student=self.student,
            session=sess,
            score=7,
            passed=False,
            answers={"x": 1},
            completed_at=timezone.now(),
        )
        self.client.post(reverse('tutoring:chat_restart_session', args=[self.lesson.id]))
        # Attempt survives — FK is to ExitTicket (not session), and
        # we don't delete the session row.
        self.assertTrue(ExitTicketAttempt.objects.filter(id=attempt.id).exists())

    def test_restart_with_no_existing_session_is_noop_success(self):
        url = reverse('tutoring:chat_restart_session', args=[self.lesson.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['restarted'])
        self.assertEqual(body['sessions_archived'], 0)

    def test_restart_does_not_archive_other_students_sessions(self):
        other = User.objects.create_user(username="other", password="pw")
        Membership.objects.create(
            user=other,
            institution=self.institution,
            role="student",
        )
        their_session = TutorSession.objects.create(
            institution=self.institution,
            student=other,
            lesson=self.lesson,
            status=TutorSession.Status.ACTIVE,
        )
        my_session = TutorSession.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=self.lesson,
            status=TutorSession.Status.ACTIVE,
        )
        self.client.post(reverse('tutoring:chat_restart_session', args=[self.lesson.id]))
        their_session.refresh_from_db()
        my_session.refresh_from_db()
        self.assertEqual(their_session.status, TutorSession.Status.ACTIVE)
        self.assertEqual(my_session.status, TutorSession.Status.ABANDONED)

    def test_restart_does_not_archive_other_lessons(self):
        other_lesson = Lesson.objects.create(
            unit=self.unit, title="Other", objective="y",
            order_index=1, is_published=True,
        )
        unrelated = TutorSession.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=other_lesson,
            status=TutorSession.Status.ACTIVE,
        )
        self.client.post(reverse('tutoring:chat_restart_session', args=[self.lesson.id]))
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, TutorSession.Status.ACTIVE)

    def test_restart_requires_login(self):
        c = Client()
        url = reverse('tutoring:chat_restart_session', args=[self.lesson.id])
        response = c.post(url)
        # Redirect to login (302) — restart is login_required
        self.assertEqual(response.status_code, 302)
