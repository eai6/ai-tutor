"""Tests for the competency helpers + the lesson_competency endpoint.

See memory/lesson_competency_plan.md Phase C3.
"""

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Unit, Lesson
from apps.tutoring.competency import (
    compute_passing_threshold_pct,
    per_concept_breakdown,
    competency_snapshot,
)
from apps.tutoring.models import (
    ExitTicket,
    ExitTicketQuestion,
    ExitTicketAttempt,
    StudentLessonProgress,
)


class CompetencyHelpersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T", slug="t")
        cls.student = User.objects.create_user(username="s", password="pw")
        Membership.objects.create(
            user=cls.student, institution=cls.institution, role="student",
        )
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math 8", grade_level="8", is_published=True,
        )
        cls.unit = Unit.objects.create(course=cls.course, title="U", order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="o", order_index=0, is_published=True,
        )
        cls.exit_ticket = ExitTicket.objects.create(
            lesson=cls.lesson, passing_score=7,
        )
        for i in range(10):
            ExitTicketQuestion.objects.create(
                exit_ticket=cls.exit_ticket,
                question_text=f"Q{i}",
                option_a="A", option_b="B", option_c="C", option_d="D",
                correct_answer="A", explanation="",
                concept_tag="A" if i < 5 else "B",
                order_index=i,
            )

    def test_passing_threshold_pct(self):
        pct = compute_passing_threshold_pct(self.exit_ticket)
        self.assertEqual(pct, 0.7)

    def test_per_concept_breakdown_weakest_first(self):
        attempt = ExitTicketAttempt.objects.create(
            exit_ticket=self.exit_ticket,
            student=self.student,
            score=6,
            passed=False,
            completed_at=timezone.now(),
            answers=(
                [{"concept_tag": "A", "correct": True} for _ in range(4)]
                + [{"concept_tag": "A", "correct": False}]
                + [{"concept_tag": "B", "correct": True} for _ in range(2)]
                + [{"concept_tag": "B", "correct": False} for _ in range(3)]
            ),
        )
        rows = per_concept_breakdown(attempt)
        self.assertEqual(rows[0]["concept"], "B")   # 2/5 = 40% — weakest first
        self.assertEqual(rows[1]["concept"], "A")   # 4/5 = 80%
        self.assertAlmostEqual(rows[0]["pct"], 0.4)
        self.assertAlmostEqual(rows[1]["pct"], 0.8)

    def test_snapshot_no_attempts(self):
        snap = competency_snapshot(self.student, self.lesson)
        self.assertEqual(snap["mastery_level"], "not_started")
        self.assertIsNone(snap["best_score_pct"])
        self.assertEqual(snap["attempts_count"], 0)
        self.assertEqual(snap["per_concept"], [])
        self.assertEqual(snap["weak_concepts"], [])

    def test_snapshot_with_attempts(self):
        # Create progress + an attempt
        StudentLessonProgress.objects.create(
            student=self.student,
            lesson=self.lesson,
            institution=self.institution,
            mastery_level="mastered",
            best_score=0.8,
            attempts_count=1,
            last_attempt_at=timezone.now(),
        )
        ExitTicketAttempt.objects.create(
            exit_ticket=self.exit_ticket,
            student=self.student,
            score=8,
            passed=True,
            completed_at=timezone.now(),
            answers=(
                [{"concept_tag": "A", "correct": True} for _ in range(5)]
                + [{"concept_tag": "B", "correct": True} for _ in range(3)]
                + [{"concept_tag": "B", "correct": False} for _ in range(2)]
            ),
        )
        snap = competency_snapshot(self.student, self.lesson)
        self.assertEqual(snap["mastery_level"], "mastered")
        self.assertEqual(snap["best_score_pct"], 0.8)
        self.assertEqual(snap["attempts_count"], 1)
        self.assertIsNotNone(snap["best_attempt"])
        # Weak: concept B (3/5 = 60% < 70%). A is 5/5 = 100%.
        self.assertIn("B", snap["weak_concepts"])
        self.assertNotIn("A", snap["weak_concepts"])


class CompetencyEndpointTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name="T2", slug="t2")
        self.student = User.objects.create_user(username="s2", password="pw")
        Membership.objects.create(
            user=self.student, institution=self.institution, role="student",
        )
        self.course = Course.objects.create(
            institution=self.institution, title="Math 9", grade_level="9",
            is_published=True,
        )
        self.unit = Unit.objects.create(course=self.course, title="U2", order_index=0)
        self.lesson = Lesson.objects.create(
            unit=self.unit, title="L2", objective="o", order_index=0,
            is_published=True,
        )
        self.client = Client()
        self.client.force_login(self.student)

    def test_endpoint_returns_competency_snapshot(self):
        url = reverse("tutoring:lesson_competency", args=[self.lesson.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["lesson_id"], self.lesson.id)
        self.assertIn("mastery_level", body)
        self.assertIn("per_concept", body)

    def test_endpoint_rejects_cross_school(self):
        other = Institution.objects.create(name="Other", slug="other")
        other_course = Course.objects.create(
            institution=other, title="M", grade_level="8", is_published=True,
        )
        other_unit = Unit.objects.create(course=other_course, title="x", order_index=0)
        other_lesson = Lesson.objects.create(
            unit=other_unit, title="other", objective="o", order_index=0,
            is_published=True,
        )
        url = reverse("tutoring:lesson_competency", args=[other_lesson.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)  # institution scoping
