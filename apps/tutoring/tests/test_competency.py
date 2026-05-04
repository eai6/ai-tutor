"""Tests for the exit-ticket-driven competency fix.

Covers:
  - passing_score no longer hardcoded to 8
  - best_score populated as 0.0-1.0 fraction (not legacy percent)
  - attempts_count + last_attempt_at updated on every submission
  - mastery_level monotonic (never demoted)
  - per-concept competency derived from answers

See memory/lesson_competency_plan.md.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.llm.client import LLMResponse
from apps.tutoring.models import (
    TutorSession,
    ExitTicket,
    ExitTicketQuestion,
    ExitTicketAttempt,
    StudentLessonProgress,
)


def _fake_llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, tokens_in=1, tokens_out=1,
        model="test", stop_reason="end_turn",
    )


class CompetencyTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="Test", slug="test")
        cls.student = User.objects.create_user(username="stu", password="pw")
        Membership.objects.create(
            user=cls.student, institution=cls.institution, role="student",
        )
        cls.course = Course.objects.create(
            institution=cls.institution,
            title="Grade 8 Science",
            grade_level="Grade 8",
            is_published=True,
        )
        cls.unit = Unit.objects.create(course=cls.course, title="U1", order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L1", objective="obj", order_index=0,
            is_published=True,
        )
        cls.step = LessonStep.objects.create(
            lesson=cls.lesson, order_index=0, step_type="practice",
            teacher_script="s", question="q", answer_type="free_text",
            expected_answer="a",
        )
        cls.exit_ticket = ExitTicket.objects.create(
            lesson=cls.lesson, passing_score=7,  # threshold = 7, not default 8
        )
        for i in range(10):
            ExitTicketQuestion.objects.create(
                exit_ticket=cls.exit_ticket,
                question_text=f"Q{i}",
                option_a="A", option_b="B", option_c="C", option_d="D",
                correct_answer="A",
                explanation="",
                concept_tag="concept-A" if i < 5 else "concept-B",
                order_index=i,
            )

    def _make_tutor(self):
        from apps.tutoring.conversational_tutor import ConversationalTutor
        session = TutorSession.objects.create(
            institution=self.institution, student=self.student, lesson=self.lesson,
            status="active", engine_state={},
        )
        tutor = ConversationalTutor(session)
        fake_llm = MagicMock()
        fake_llm.generate.return_value = _fake_llm_response("OK")
        tutor._llm_client = fake_llm
        tutor._instructor_client = None
        return tutor, session

    def test_passing_score_no_longer_hardcoded_to_8(self):
        """ExitTicket.passing_score=7 should pass at correct=7 (old code
        required >= 8)."""
        tutor, session = self._make_tutor()
        # Build fake results: 7 correct, 3 wrong
        results = [{"is_correct": True, "concept_tag": "c", "selected": "A",
                    "question_type": "mcq", "index": i, "question": "q",
                    "correct_answer": "A", "explanation": ""} for i in range(7)]
        results += [{"is_correct": False, "concept_tag": "c", "selected": "B",
                     "question_type": "mcq", "index": i, "question": "q",
                     "correct_answer": "A", "explanation": ""}
                    for i in range(7, 10)]
        # Simulate submission logic
        answers = [
            {"answer": "A" if r["is_correct"] else "B"} for r in results
        ]
        with patch.object(tutor, "_grade_exit_question") as mg:
            mg.side_effect = [r["is_correct"] for r in results]
            tutor._load_exit_ticket_concepts()
            tutor._submit_exit_ticket_inner(answers)

        progress = StudentLessonProgress.objects.get(
            student=self.student, lesson=self.lesson,
        )
        self.assertEqual(progress.mastery_level, "mastered")
        self.assertAlmostEqual(progress.best_score, 0.7, places=2)

    def test_best_score_stored_as_fraction_not_percent(self):
        tutor, session = self._make_tutor()
        tutor._load_exit_ticket_concepts()
        answers = [{"answer": "A"} for _ in range(10)]
        with patch.object(tutor, "_grade_exit_question", return_value=True):
            tutor._submit_exit_ticket_inner(answers)

        progress = StudentLessonProgress.objects.get(
            student=self.student, lesson=self.lesson,
        )
        self.assertEqual(progress.best_score, 1.0)  # perfect score

    def test_mastery_demotes_on_failed_retake(self):
        """A failed retake demotes mastery_level + overwrites best_score
        with the latest (lower) score. Per the 2026-05 reset, the
        catalog should reflect where the student is RIGHT NOW, not
        their historical peak. Permanent transcript
        (StudentCompetencyRecord) keeps the high-water mark."""
        tutor, session = self._make_tutor()
        tutor._load_exit_ticket_concepts()
        # First: pass with perfect score
        with patch.object(tutor, "_grade_exit_question", return_value=True):
            tutor._submit_exit_ticket_inner([{"answer": "A"} for _ in range(10)])

        progress = StudentLessonProgress.objects.get(
            student=self.student, lesson=self.lesson,
        )
        self.assertEqual(progress.mastery_level, "mastered")
        self.assertEqual(progress.best_score, 1.0)

        # Second: fail badly. Mastery demotes, best_score = latest score.
        tutor2, session2 = self._make_tutor()
        tutor2._load_exit_ticket_concepts()
        with patch.object(tutor2, "_grade_exit_question", return_value=False):
            tutor2._submit_exit_ticket_inner([{"answer": "B"} for _ in range(10)])

        progress.refresh_from_db()
        self.assertEqual(progress.mastery_level, "in_progress")
        self.assertAlmostEqual(progress.best_score, 0.0, places=3)
        self.assertEqual(progress.attempts_count, 2)

    def test_attempts_count_increments_on_each_submission(self):
        tutor, session = self._make_tutor()
        tutor._load_exit_ticket_concepts()
        with patch.object(tutor, "_grade_exit_question", return_value=False):
            tutor._submit_exit_ticket_inner([{"answer": "B"} for _ in range(10)])

        progress = StudentLessonProgress.objects.get(
            student=self.student, lesson=self.lesson,
        )
        self.assertEqual(progress.attempts_count, 1)
        self.assertIsNotNone(progress.last_attempt_at)
        # Failed attempt → not mastered, but progressed
        self.assertEqual(progress.mastery_level, "in_progress")
        self.assertAlmostEqual(progress.best_score, 0.0, places=3)

    def test_score_reflects_latest_attempt(self):
        """``best_score`` is named for legacy reasons but now stores
        the LATEST attempt's score — not the historical best. The
        catalog needs to surface where students currently are, not
        their peak. Verify with three attempts that go up then down."""
        tutor, session = self._make_tutor()
        tutor._load_exit_ticket_concepts()
        # First attempt: 4/10
        side = [True] * 4 + [False] * 6
        with patch.object(tutor, "_grade_exit_question", side_effect=side):
            tutor._submit_exit_ticket_inner([{"answer": "A"} for _ in range(10)])
        progress = StudentLessonProgress.objects.get(
            student=self.student, lesson=self.lesson,
        )
        self.assertAlmostEqual(progress.best_score, 0.4, places=3)

        # Second session, better: 8/10 (passes threshold 7)
        tutor2, session2 = self._make_tutor()
        tutor2._load_exit_ticket_concepts()
        side = [True] * 8 + [False] * 2
        with patch.object(tutor2, "_grade_exit_question", side_effect=side):
            tutor2._submit_exit_ticket_inner([{"answer": "A"} for _ in range(10)])
        progress.refresh_from_db()
        self.assertAlmostEqual(progress.best_score, 0.8, places=3)
        self.assertEqual(progress.mastery_level, "mastered")

        # Third: 6/10 (worse). Score follows latest, mastery demotes.
        tutor3, session3 = self._make_tutor()
        tutor3._load_exit_ticket_concepts()
        side = [True] * 6 + [False] * 4
        with patch.object(tutor3, "_grade_exit_question", side_effect=side):
            tutor3._submit_exit_ticket_inner([{"answer": "A"} for _ in range(10)])
        progress.refresh_from_db()
        self.assertAlmostEqual(progress.best_score, 0.6, places=3)
        self.assertEqual(progress.mastery_level, "in_progress")
        self.assertEqual(progress.attempts_count, 3)
