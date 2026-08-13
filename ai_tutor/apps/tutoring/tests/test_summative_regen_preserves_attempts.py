"""Regression test: regenerating the course summative bank must NOT
cascade-delete ExitTicketAttempt rows.

History:
  - The lesson exit-ticket regen had this same bug; fixed in 25c62a2
    (Exit-ticket regen: keep attempts).
  - The summative regen path had the same bug — it deleted the
    ExitTicket row and cascade-wiped baseline/final/retake attempts.
  - Fixed by mirroring the in-place question replacement: drop only
    the OLD questions, keep the ExitTicket row.

If this test fails, baseline + final + retake attempts get wiped
when teachers click "Regenerate summative bank".
"""

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import (
    ExitTicket,
    ExitTicketAttempt,
    ExitTicketQuestion,
)


class SummativeRegenPreservesAttemptsTest(TransactionTestCase):
    """Verify generate_summative_for_course preserves ExitTicketAttempt
    rows on the existing summative ExitTicket — no cascade-wipe.

    TransactionTestCase (not TestCase) because the summative generator
    uses a ThreadPoolExecutor whose worker threads call
    django.db.connections.close_all(), which would clobber TestCase's
    wrapping transaction and hide our fixture data from the workers.
    """

    def setUp(self):
        self.institution = Institution.objects.create(name="SR", slug="sr")
        self.student = User.objects.create_user(username="srstu", password="pw")
        self.course = Course.objects.create(
            institution=self.institution, title="Math S3",
            grade_level="S3", is_published=True, subject_type='math',
        )
        self.unit = Unit.objects.create(
            course=self.course, title="Geo", order_index=0,
        )
        self.lesson = Lesson.objects.create(
            unit=self.unit, title="L1", objective="x",
            order_index=0, is_published=True,
        )
        # Lesson-level published exit ticket with a few questions —
        # so the summative sampler has something to draw from.
        lesson_ticket = ExitTicket.objects.create(
            lesson=self.lesson, passing_score=8,
            assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
            is_published=True,
        )
        for i in range(8):
            ExitTicketQuestion.objects.create(
                exit_ticket=lesson_ticket,
                question_text=f"Lesson Q{i}: 2+{i}=?",
                option_a=f"{2+i}", option_b="x", option_c="y", option_d="z",
                correct_answer="A", explanation="",
                concept_tag="addition", order_index=i,
            )
        # Pre-existing summative with 3 attempts on it (baseline + final
        # + retake). We assert ALL three survive a regen.
        self.old_summative = ExitTicket.objects.create(
            course=self.course,
            assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
            question_bank_size=3,
            questions_per_attempt=3,
            passing_score=2,
            time_limit_minutes=60,
            is_published=True,
        )
        for i in range(3):
            ExitTicketQuestion.objects.create(
                exit_ticket=self.old_summative,
                question_text=f"Old summative Q{i}",
                option_a="A", option_b="B", option_c="C", option_d="D",
                correct_answer="A", explanation="",
                concept_tag="addition", order_index=i,
            )
        self.attempt_baseline = ExitTicketAttempt.objects.create(
            exit_ticket=self.old_summative,
            student=self.student,
            score=2, passed=True,
            purpose=ExitTicketAttempt.Purpose.BASELINE,
            answers={"snapshot": "baseline"},
            completed_at=timezone.now(),
        )
        self.attempt_final = ExitTicketAttempt.objects.create(
            exit_ticket=self.old_summative,
            student=self.student,
            score=3, passed=True,
            purpose=ExitTicketAttempt.Purpose.FINAL,
            answers={"snapshot": "final"},
            completed_at=timezone.now(),
        )
        self.attempt_retake = ExitTicketAttempt.objects.create(
            exit_ticket=self.old_summative,
            student=self.student,
            score=2, passed=True,
            purpose=ExitTicketAttempt.Purpose.RETAKE,
            answers={"snapshot": "retake"},
            completed_at=timezone.now(),
        )

    def test_regen_summative_preserves_attempts(self):
        from ai_tutor.apps.tutoring.summative_generator import (
            generate_summative_for_course,
        )

        result = generate_summative_for_course(self.course, min_per_lesson=3)
        self.assertTrue(result.get('success'), result)

        # The ExitTicket row should be the SAME ROW (in-place replace).
        same_summative = ExitTicket.objects.filter(
            course=self.course,
            assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
        ).first()
        self.assertIsNotNone(same_summative)
        self.assertEqual(same_summative.id, self.old_summative.id)

        # All three attempts must still exist
        for label, attempt in (
            ("baseline", self.attempt_baseline),
            ("final", self.attempt_final),
            ("retake", self.attempt_retake),
        ):
            self.assertTrue(
                ExitTicketAttempt.objects.filter(id=attempt.id).exists(),
                f"{label} attempt was wiped by summative regen — "
                "in-place replacement broken",
            )

        # And they're still attached to the same ExitTicket
        for attempt in (self.attempt_baseline, self.attempt_final, self.attempt_retake):
            attempt.refresh_from_db()
            self.assertEqual(attempt.exit_ticket_id, self.old_summative.id)

    def test_regen_summative_replaces_question_bank(self):
        """Sanity-check that questions DO get replaced (the regen
        actually does its job — old questions gone, new ones in)."""
        from ai_tutor.apps.tutoring.summative_generator import (
            generate_summative_for_course,
        )
        old_question_ids = set(
            self.old_summative.questions.values_list('id', flat=True)
        )
        result = generate_summative_for_course(self.course, min_per_lesson=3)
        self.assertTrue(result.get('success'))
        new_question_ids = set(
            ExitTicketQuestion.objects
            .filter(exit_ticket=self.old_summative)
            .values_list('id', flat=True)
        )
        # No overlap — old questions deleted, new ones created
        self.assertEqual(old_question_ids & new_question_ids, set())
        self.assertGreater(len(new_question_ids), 0)

    def test_summative_carries_enabling_objective_forward(self):
        """Sub-objective on the source lesson question must end up
        on the summative copy — preserves sub-skill granularity for
        post-summative remediation, while concept_tag stays at the
        lesson-objective level for the competency matrix."""
        # Tag every source lesson question with a distinctive sub-EO
        # so we can verify it survives the sample → snapshot → create
        # round-trip into the summative bank.
        ExitTicketQuestion.objects.filter(
            exit_ticket__lesson=self.lesson,
            exit_ticket__assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
        ).update(
            enabling_objective="Calculate sum given two addends",
        )
        from ai_tutor.apps.tutoring.summative_generator import (
            generate_summative_for_course,
        )
        result = generate_summative_for_course(self.course, min_per_lesson=3)
        self.assertTrue(result.get('success'))
        # Every freshly-created summative question should carry the
        # source EO forward.
        new_qs = ExitTicketQuestion.objects.filter(exit_ticket=self.old_summative)
        self.assertGreater(new_qs.count(), 0)
        for q in new_qs:
            self.assertEqual(
                q.enabling_objective,
                "Calculate sum given two addends",
                "summative question dropped the source enabling_objective",
            )
