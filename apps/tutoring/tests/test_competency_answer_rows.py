"""Tests for answer_rows() — the ExitTicketAttempt.answers format normalizer.

Two answers formats exist in production:
  - legacy list (retired conversational_tutor): [{concept_tag, correct, ...}]
  - dict (simple_tutor since 2026-05-26 / diagnostic pretest):
    {'per_question': [...], ...}

The dict format crashed per_concept_breakdown with
"AttributeError: 'str' object has no attribute 'get'" (iterating a dict
yields its string keys), which 500'd the dashboard student detail page.
See memory/exit_ticket_answers_format_fix.md.
"""

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Unit, Lesson
from apps.tutoring.competency import (
    answer_rows,
    competency_snapshot,
    per_concept_breakdown,
)
from apps.tutoring.models import (
    ExitTicket,
    ExitTicketAttempt,
    StudentLessonProgress,
)

LEGACY_LIST = [
    {"concept_tag": "fractions", "correct": True, "selected": "A"},
    {"concept_tag": "fractions", "correct": False, "selected": "B"},
    {"concept_tag": "decimals", "correct": True, "selected": "C"},
]

SIMPLE_TUTOR_DICT = {
    "per_question": [
        {"concept_tag": "fractions", "enabling_objective": "EO1",
         "correct": True, "selected": "A", "question_type": "mcq"},
        {"concept_tag": "fractions", "enabling_objective": "EO1",
         "correct": False, "selected": "C", "question_type": "mcq"},
        {"concept_tag": "decimals", "enabling_objective": "EO2",
         "correct": True, "selected": "B", "question_type": "mcq"},
    ],
    "eo_competency": {
        "EO1": {"asked": 2, "correct": 1, "failed_question_ids": [7],
                "is_mastered": False},
    },
}

DIAGNOSTIC_DICT = {
    "selected_question_ids": [1, 2],
    "per_question": [
        {"question_id": 1, "concept_tag": "fractions",
         "student_answer": "A", "correct": True},
        {"question_id": 2, "concept_tag": "", "student_answer": "D",
         "correct": False},
    ],
    "achieved_eos": ["fractions"],
    "failed_eos": [],
    "total": 2,
    "passing_score": 2,
}


class AnswerRowsTests(SimpleTestCase):
    """Pure-helper tests on unsaved attempts — no DB."""

    def _attempt(self, answers):
        return ExitTicketAttempt(answers=answers)

    def test_legacy_list_passes_through(self):
        self.assertEqual(answer_rows(self._attempt(LEGACY_LIST)), LEGACY_LIST)

    def test_legacy_list_drops_non_dict_entries(self):
        dirty = LEGACY_LIST + ["garbage", 42, None]
        self.assertEqual(answer_rows(self._attempt(dirty)), LEGACY_LIST)

    def test_simple_tutor_dict_returns_per_question(self):
        rows = answer_rows(self._attempt(SIMPLE_TUTOR_DICT))
        self.assertEqual(rows, SIMPLE_TUTOR_DICT["per_question"])

    def test_diagnostic_dict_returns_per_question(self):
        rows = answer_rows(self._attempt(DIAGNOSTIC_DICT))
        self.assertEqual(rows, DIAGNOSTIC_DICT["per_question"])

    def test_degenerate_inputs_return_empty(self):
        for answers in ({}, None, "garbage", 7,
                        {"per_question": "oops"},
                        {"per_question": None},
                        {"eo_competency": {}}):
            with self.subTest(answers=answers):
                self.assertEqual(answer_rows(self._attempt(answers)), [])

    def test_none_attempt_returns_empty(self):
        self.assertEqual(answer_rows(None), [])

    def test_per_concept_breakdown_on_dict_format(self):
        """The exact prod-crash scenario: dict-format answers."""
        rows = per_concept_breakdown(self._attempt(SIMPLE_TUTOR_DICT))
        by_concept = {r["concept"]: r for r in rows}
        self.assertEqual(by_concept["fractions"]["correct"], 1)
        self.assertEqual(by_concept["fractions"]["total"], 2)
        self.assertEqual(by_concept["decimals"]["pct"], 1.0)
        # Weakest first
        self.assertEqual(rows[0]["concept"], "fractions")

    def test_per_concept_breakdown_on_legacy_list(self):
        rows = per_concept_breakdown(self._attempt(LEGACY_LIST))
        by_concept = {r["concept"]: r for r in rows}
        self.assertEqual(by_concept["fractions"]["total"], 2)
        self.assertEqual(by_concept["decimals"]["total"], 1)


class CompetencySnapshotTotalTests(TestCase):
    """Snapshot total must count questions, not dict keys."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="Test", slug="test")
        cls.student = User.objects.create_user(username="stu", password="pw")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Maths", grade_level="Grade 8",
            is_published=True,
        )
        cls.unit = Unit.objects.create(course=cls.course, title="U1", order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L1", objective="obj", order_index=0,
            is_published=True,
        )
        cls.exit_ticket = ExitTicket.objects.create(lesson=cls.lesson)

    def test_total_counts_questions_for_dict_format(self):
        ExitTicketAttempt.objects.create(
            exit_ticket=self.exit_ticket,
            student=self.student,
            score=2,
            passed=False,
            answers=SIMPLE_TUTOR_DICT,
            completed_at=timezone.now(),
        )
        StudentLessonProgress.objects.create(
            student=self.student, lesson=self.lesson,
            institution=self.institution, mastery_level="in_progress",
            best_score=0.67,
        )
        snapshot = competency_snapshot(self.student, self.lesson)
        self.assertEqual(snapshot["latest_attempt"]["total"], 3)
        self.assertEqual(
            {r["concept"] for r in snapshot["per_concept"]},
            {"fractions", "decimals"},
        )
        self.assertEqual(snapshot["weak_concepts"], ["fractions"])
