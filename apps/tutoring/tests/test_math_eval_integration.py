"""End-to-end integration tests for the math-tutor false-positive fix.

Asserts the full respond() flow:
  - Layer 1 (deterministic numeric check) runs before the LLM
  - Layer 2 signal injection tells the LLM the truth
  - Layer 3 praise filter strips praise when the LLM still says it
  - Layer 4 metadata persistence records the verdict on SessionTurn.metadata

See memory/math_tutor_fix_plan.md Phase M7.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.llm.client import LLMResponse
from apps.tutoring.models import (
    TutorSession,
    SessionTurn,
    ExitTicket,
    ExitTicketQuestion,
)


def _fake_llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        tokens_in=10,
        tokens_out=20,
        model="test-model",
        stop_reason="end_turn",
    )


class MathTutoringIntegrationTest(TestCase):
    """Math lesson fixtures + tutor.respond() assertions."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(
            name="Test School",
            slug="test-school",
        )
        cls.student = User.objects.create_user(
            username="student1",
            password="testpass123",
        )
        Membership.objects.create(
            user=cls.student,
            institution=cls.institution,
            role="student",
        )

        # Course title contains "math" so Course.is_math == True.
        cls.course = Course.objects.create(
            institution=cls.institution,
            title="Grade 8 Math",
            grade_level="Grade 8",
            is_published=True,
        )
        cls.unit = Unit.objects.create(
            course=cls.course,
            title="Fractions",
            order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit,
            title="Multiply fractions by whole numbers",
            objective="Convert 21/4 to a mixed number",
            order_index=0,
            is_published=True,
        )
        cls.step_mixed_number = LessonStep.objects.create(
            lesson=cls.lesson,
            order_index=0,
            step_type="practice",
            teacher_script=(
                "We multiplied 7 by 6/8 and got 42/8 which simplifies to 21/4."
            ),
            question="Convert 21/4 to a mixed number.",
            answer_type="free_text",
            expected_answer="5 1/4",
        )
        # Second step so the session doesn't auto-complete after one answer.
        cls.step_next = LessonStep.objects.create(
            lesson=cls.lesson,
            order_index=1,
            step_type="practice",
            teacher_script="Now let's try another.",
            question="Convert 17/3 to a mixed number.",
            answer_type="free_text",
            expected_answer="5 2/3",
        )
        # Minimal exit ticket so the lesson is complete (not required for
        # the respond() path but avoids KeyError in _handle_exit_ticket).
        cls.exit_ticket = ExitTicket.objects.create(lesson=cls.lesson)
        ExitTicketQuestion.objects.create(
            exit_ticket=cls.exit_ticket,
            question_text="placeholder",
            option_a="A",
            option_b="B",
            option_c="C",
            option_d="D",
            correct_answer="A",
            explanation="",
            order_index=0,
        )

    def _make_tutor(self, mock_response_content: str):
        """Build a ConversationalTutor with mocked LLM returning the given
        response on every generate() call."""
        # Import inside to avoid Django app-load ordering issues at import time.
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = TutorSession.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=self.lesson,
            status="active",
            engine_state={},
        )

        fake_llm = MagicMock()
        fake_llm.generate.return_value = _fake_llm_response(mock_response_content)

        with patch.object(ConversationalTutor, "llm_client", new=fake_llm), \
             patch.object(ConversationalTutor, "instructor_client", new=None):
            tutor = ConversationalTutor(session)

        # After construction, force the lazy-property values for the life
        # of the test (the patch above only applies during __init__).
        tutor._llm_client = fake_llm
        tutor._instructor_client = None
        return tutor, session, fake_llm

    # ------------------------------------------------------------------
    # Layer 1 — deterministic check
    # ------------------------------------------------------------------

    def test_wrong_math_answer_marked_incorrect_without_llm_praise_leak(self):
        """The production bug in miniature: student says '3 3/4' when the
        expected answer is '5 1/4', LLM defies the signal and praises —
        the fix must catch it."""
        tutor, session, _ = self._make_tutor(
            "Brilliant, Vaani! You've got it — 21/4 = 5 1/4 kg. "
            "You correctly divided 21 by 4 to get 5 whole groups with 1 left over."
        )
        msg = tutor.respond("3 3/4")

        # Layer 3: praise stripped from content
        content_lower = msg.content.lower()
        self.assertNotIn("brilliant", content_lower)
        self.assertNotIn("you've got it", content_lower)
        self.assertNotIn("you got it", content_lower)

        # last_answer_correct is the transient engine state used downstream
        self.assertFalse(tutor.last_answer_correct)

    def test_correct_math_answer_is_accepted(self):
        """Correct bare answer: the verdict is correct, but praise still
        gets stripped (M9 bare-answer gate + Socratic validator V1)."""
        tutor, session, _ = self._make_tutor(
            "That's exactly right — 21/4 = 5 1/4. Great work!"
        )
        msg = tutor.respond("5 1/4")

        # Verdict is correct: state advances and the analyzer marks it.
        self.assertTrue(tutor.last_answer_correct)
        # But praise words must be stripped because the answer was bare.
        self.assertNotIn("exactly", msg.content.lower())
        self.assertNotIn("brilliant", msg.content.lower())

    def test_equivalent_fraction_form_accepted(self):
        """Student gives the improper fraction form (21/4) instead of
        the mixed-number form (5 1/4). Numerically identical → correct."""
        tutor, _, _ = self._make_tutor("Yes, 21/4 is the same as 5 1/4.")
        tutor.respond("21/4")
        self.assertTrue(tutor.last_answer_correct)

    # ------------------------------------------------------------------
    # Layer 4 — metadata persistence
    # ------------------------------------------------------------------

    def test_session_turn_metadata_populated_on_wrong_answer(self):
        tutor, session, _ = self._make_tutor(
            "Brilliant, you've got it! 21/4 = 5 1/4."
        )
        tutor.respond("3 3/4")

        tutor_turn = (
            SessionTurn.objects.filter(session=session, role="tutor")
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(tutor_turn)
        md = tutor_turn.metadata or {}
        self.assertEqual(md.get("is_correct"), False)
        self.assertEqual(md.get("eval_layer"), "deterministic_numeric")
        self.assertAlmostEqual(md.get("student_answer_parsed"), 3.75)
        self.assertAlmostEqual(md.get("expected_answer_parsed"), 5.25)
        self.assertTrue(md.get("praise_stripped"))

    def test_session_turn_metadata_populated_on_correct_answer(self):
        """Correct numeric answer → deterministic_numeric layer,
        is_correct=True, parsed values captured."""
        tutor, session, _ = self._make_tutor(
            "Exactly — 21/4 equals 5 1/4. Now let's try another."
        )
        tutor.respond("5 1/4")

        tutor_turn = (
            SessionTurn.objects.filter(session=session, role="tutor")
            .order_by("-created_at")
            .first()
        )
        md = tutor_turn.metadata or {}
        self.assertEqual(md.get("is_correct"), True)
        self.assertEqual(md.get("eval_layer"), "deterministic_numeric")
        self.assertAlmostEqual(md.get("student_answer_parsed"), 5.25)
        self.assertAlmostEqual(md.get("expected_answer_parsed"), 5.25)
        # This is a bare answer (no working shown), so M9 flags it and
        # strips praise. Metadata records both.
        self.assertTrue(md.get("bare_answer"))

    # ------------------------------------------------------------------
    # Layer 2 — signal injection visible in the prompt
    # ------------------------------------------------------------------

    def test_system_prompt_includes_evaluation_signal_on_math_turn(self):
        """The LLM must see the evaluation_signal block. Capture the
        system prompt passed to llm_client.generate()."""
        tutor, session, fake_llm = self._make_tutor(
            "Let's look at your working."
        )
        tutor.respond("3 3/4")

        # The generate() mock recorded call_args
        call_kwargs = fake_llm.generate.call_args.kwargs
        sys_prompt = call_kwargs.get("system_prompt", "")
        self.assertIn("<evaluation_signal>", sys_prompt)
        self.assertIn("INCORRECT", sys_prompt)
        self.assertIn("3.75", sys_prompt)
        self.assertIn("5.25", sys_prompt)

    def test_no_signal_injected_on_non_math_lesson(self):
        """Non-math lessons do not get the deterministic check or signal."""
        # Repurpose: change course title so is_math becomes False.
        self.course.title = "Grade 8 Science"
        self.course.save(update_fields=["title"])
        tutor, session, fake_llm = self._make_tutor(
            "Good thinking. Let's look at that together."
        )
        tutor.respond("something non-numeric")

        call_kwargs = fake_llm.generate.call_args.kwargs
        sys_prompt = call_kwargs.get("system_prompt", "")
        self.assertNotIn("<evaluation_signal>", sys_prompt)

        # Restore for other tests.
        self.course.title = "Grade 8 Math"
        self.course.save(update_fields=["title"])

    def test_no_signal_when_expected_answer_is_free_text(self):
        """If expected_answer isn't numeric, layer 1 returns None → fall
        through to normal LLM evaluator, no signal injected."""
        # Repurpose the step with a free-text expected answer.
        self.step_mixed_number.expected_answer = "any answer showing understanding"
        self.step_mixed_number.save(update_fields=["expected_answer"])

        tutor, session, fake_llm = self._make_tutor("Thanks, let's continue.")
        tutor.respond("3 3/4")

        call_kwargs = fake_llm.generate.call_args.kwargs
        sys_prompt = call_kwargs.get("system_prompt", "")
        self.assertNotIn("<evaluation_signal>", sys_prompt)

        # Restore for other tests.
        self.step_mixed_number.expected_answer = "5 1/4"
        self.step_mixed_number.save(update_fields=["expected_answer"])

    # ------------------------------------------------------------------
    # Layer 4 — bare-answer gate (M9)
    # ------------------------------------------------------------------

    def test_bare_correct_answer_still_strips_praise(self):
        """Even when the answer is numerically correct, a bare response
        (no working shown) should have praise stripped — per math_teaching
        Rule 1, 'working before evaluation'."""
        tutor, session, fake_llm = self._make_tutor(
            "Exactly right! 21/4 equals 5 1/4. Let's move on."
        )
        tutor.respond("5 1/4")

        tutor_turn = (
            SessionTurn.objects.filter(session=session, role="tutor")
            .order_by("-created_at")
            .first()
        )
        md = tutor_turn.metadata or {}
        self.assertEqual(md.get("is_correct"), True)
        self.assertTrue(md.get("bare_answer"))
        self.assertTrue(md.get("praise_stripped"))
        # Content should no longer contain praise
        self.assertNotIn("exactly", tutor_turn.content.lower())

    def test_bare_answer_signal_instructs_to_ask_for_working(self):
        """Signal block must tell the LLM to ask for working when bare."""
        tutor, session, fake_llm = self._make_tutor("Let's see your steps.")
        tutor.respond("5 1/4")

        sys_prompt = fake_llm.generate.call_args.kwargs.get("system_prompt", "")
        self.assertIn("<evaluation_signal>", sys_prompt)
        self.assertIn("Bare answer (no working shown): True", sys_prompt)
        # Guidance text should reference asking for working
        self.assertTrue(
            "walk you through" in sys_prompt
            or "walk them through" in sys_prompt
            or "each step" in sys_prompt,
            f"bare-answer guidance missing from system prompt: {sys_prompt[-600:]}"
        )

    def test_non_bare_answer_with_working_not_flagged(self):
        """An explanatory answer with working words should NOT be flagged
        as bare, even if it still parses as a number."""
        tutor, session, fake_llm = self._make_tutor("Let's check together.")
        tutor.respond("I divided 21 by 4 to get 5 remainder 1, so 5 1/4")

        tutor_turn = (
            SessionTurn.objects.filter(session=session, role="tutor")
            .order_by("-created_at")
            .first()
        )
        md = tutor_turn.metadata or {}
        # Non-bare because the input contains working markers ("divided")
        self.assertFalse(md.get("bare_answer", False))

    def test_repeated_bare_answers_set_flag_after_threshold(self):
        """3rd bare answer on the same step sets bare_answer_flagged=True
        for teacher visibility. Use wrong-bare answers so the step does
        not advance between them."""
        tutor, session, fake_llm = self._make_tutor("Let's see your steps.")
        tutor.respond("3")       # bare + wrong
        tutor.respond("4")       # bare + wrong
        tutor.respond("3 1/2")   # bare + wrong

        tutor_turn = (
            SessionTurn.objects.filter(session=session, role="tutor")
            .order_by("-created_at")
            .first()
        )
        md = tutor_turn.metadata or {}
        self.assertTrue(md.get("bare_answer_flagged"))
        self.assertGreaterEqual(md.get("bare_answer_count_for_step", 0), 3)
