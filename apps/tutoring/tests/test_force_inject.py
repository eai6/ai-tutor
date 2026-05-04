"""Tests for the force-inject structural enforcement (last-resort guard
that fires when the LLM has authored a numerical question despite the
system prompt + the V3 regen path).

Guarantees:
  - The LLM-authored question stem is dropped at the first '?' (snapped
    to the previous sentence boundary so the cut reads cleanly).
  - A verified bank entry replaces it verbatim.
  - turn_metadata gets a bank_question_ref so the next student reply
    is graded deterministically against the bank — closing the loop.
  - When the id_map is empty (no bank), the helper returns None and
    the caller leaves the response untouched (degrade gracefully).
"""

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.tutoring.models import (
    ExitTicket,
    ExitTicketQuestion,
    TutorSession,
)


class ForceInjectTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T", slug="t")
        cls.student = User.objects.create_user(username="stu", password="pw")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math S3",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(
            course=cls.course, title="Geometry", order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="Angles around a point",
            objective="Use the 360° rule", order_index=0,
            is_published=True,
        )
        cls.step = LessonStep.objects.create(
            lesson=cls.lesson, phase='practice', step_type='practice',
            order_index=0,
            teacher_script="Three angles around a point are 95°, 70°, "
                           "and x°. Find x.",
            expected_answer="195°",
        )
        cls.ticket = ExitTicket.objects.create(
            lesson=cls.lesson, passing_score=8, is_published=True,
        )
        cls.q1 = ExitTicketQuestion.objects.create(
            exit_ticket=cls.ticket,
            question_text="Bank Q1: angles around a point sum to ___?",
            option_a="180°", option_b="270°", option_c="360°", option_d="540°",
            correct_answer="C", explanation="",
            concept_tag="angles_around_point", order_index=0,
            question_type='mcq',
        )

    def _make_tutor(self):
        from apps.tutoring.conversational_tutor import ConversationalTutor
        session = TutorSession.objects.create(
            institution=self.institution, student=self.student,
            lesson=self.lesson, status="active", engine_state={},
        )
        tutor = ConversationalTutor(session)
        return tutor

    def test_force_inject_replaces_llm_authored_question_with_bank_entry(self):
        tutor = self._make_tutor()
        # Simulate the bank id_map populated by _build_question_bank_block
        tutor._question_id_map = {0: self.step, 1: self.q1}

        llm_response = (
            "Great work spotting the rule! Now let's apply it. "
            "If three angles around a point are 80°, 120°, and 100°, "
            "what is the missing angle?"
        )
        meta = {}
        out = tutor._force_inject_bank_question(llm_response, meta)

        self.assertIsNotNone(out)
        # The LLM's invented numbers (80°, 120°, 100°) MUST be gone.
        for invented in ("80°", "120°", "100°"):
            self.assertNotIn(invented, out)
        # The bank entry (verbatim) MUST be present.
        self.assertIn("Three angles around a point are 95°, 70°", out)
        # Defensive transition phrasing so the cut reads cleanly.
        self.assertIn("verified question from the bank", out)
        # bank_question_ref recorded so next turn grades deterministically.
        self.assertIn('bank_question_ref', meta)
        self.assertEqual(meta['bank_question_ref']['kind'], 'lesson_step')
        self.assertEqual(meta['bank_question_ref']['id'], self.step.id)

    def test_force_inject_falls_back_to_first_non_zero_slot_when_step_missing(self):
        tutor = self._make_tutor()
        # Slot 0 missing (e.g. step bank disabled) — should pick slot 1.
        tutor._question_id_map = {1: self.q1}

        out = tutor._force_inject_bank_question(
            "Try this — what is 50 + 60?", {},
        )
        self.assertIsNotNone(out)
        self.assertIn("Bank Q1", out)
        # MCQ options rendered verbatim.
        self.assertIn("A) 180°", out)
        self.assertIn("C) 360°", out)

    def test_force_inject_returns_none_when_bank_empty(self):
        tutor = self._make_tutor()
        tutor._question_id_map = {}
        out = tutor._force_inject_bank_question(
            "What is 2 + 2?", {},
        )
        self.assertIsNone(out)

    def test_force_inject_keeps_teaching_prose_before_question(self):
        """The helper truncates at the first '?' so the LLM's teaching
        prose ahead of the unsafe question is preserved — only the bad
        question itself gets dropped."""
        tutor = self._make_tutor()
        tutor._question_id_map = {0: self.step}

        llm_response = (
            "Right — the rule is angles around a point sum to 360°. "
            "Now: if three angles are 80°, 120°, 100°, what's the fourth?"
        )
        out = tutor._force_inject_bank_question(llm_response, {})
        # Teaching sentence preserved.
        self.assertIn("angles around a point sum to 360°", out)
        # The LLM's invented numerical question stripped.
        self.assertNotIn("what's the fourth?", out)


class FinalReminderBlockTest(TestCase):
    """The <final_reminder> block must be appended to the system prompt
    on every math turn, in the highest-salience (last) position. Without
    this, the rules above dilute as the conversation grows."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T2", slug="t2")
        cls.student = User.objects.create_user(username="stu2", password="pw")
        cls.math_course = Course.objects.create(
            institution=cls.institution, title="Math S3",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.math_unit = Unit.objects.create(
            course=cls.math_course, title="U", order_index=0,
        )
        cls.math_lesson = Lesson.objects.create(
            unit=cls.math_unit, title="L", objective="o",
            order_index=0, is_published=True,
        )
        LessonStep.objects.create(
            lesson=cls.math_lesson, phase='practice', step_type='practice',
            order_index=0, teacher_script="q", expected_answer="a",
        )
        cls.nonmath_course = Course.objects.create(
            institution=cls.institution, title="History S3",
            grade_level="S3", is_published=True, subject_type='humanities',
        )
        cls.nonmath_unit = Unit.objects.create(
            course=cls.nonmath_course, title="U", order_index=0,
        )
        cls.nonmath_lesson = Lesson.objects.create(
            unit=cls.nonmath_unit, title="L", objective="o",
            order_index=0, is_published=True,
        )

    def _build_prompt(self, lesson):
        from apps.tutoring.conversational_tutor import ConversationalTutor
        session = TutorSession.objects.create(
            institution=self.institution, student=self.student,
            lesson=lesson, status="active", engine_state={},
        )
        tutor = ConversationalTutor(session)
        return tutor._build_system_prompt()

    def test_final_reminder_appended_for_math_lesson(self):
        prompt = self._build_prompt(self.math_lesson)
        self.assertIn("<final_reminder>", prompt)
        self.assertIn("|||QUESTION:N|||", prompt)
        self.assertIn("|||QUESTION_EO:N|||", prompt)
        # Must be the LAST block — highest salience.
        self.assertTrue(
            prompt.rstrip().endswith("</final_reminder>"),
            "final_reminder must be the very last block in the prompt",
        )

    def test_final_reminder_absent_for_non_math_lesson(self):
        prompt = self._build_prompt(self.nonmath_lesson)
        self.assertNotIn("<final_reminder>", prompt)
