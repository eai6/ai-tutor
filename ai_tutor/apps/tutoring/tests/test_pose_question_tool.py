"""Tests for the pose_question Anthropic tool flow.

Tool use replaces the old `|||QUESTION:N|||` text-tail signal so the
LLM is *structurally* unable to author a numerical question. These
tests lock in:
  - the tool definition has only slot + lead_in (no question_text)
  - the tool's slot range matches the bank id_map exactly
  - the message handler renders the right entry by slot
  - bank_question_ref is recorded so the next turn grades correctly
  - text-block prose with a numerical question gets stripped (defense)
  - non-math lessons skip the tool entirely
  - invalid / out-of-range slots fall back to slot 0
  - the bank scope is strict (no other-step leakage)

See memory/pose_question_tool_plan.md.
"""

from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import TestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.tutoring.models import (
    ExitTicket,
    ExitTicketQuestion,
    TutorSession,
)


def _fake_text_block(text: str):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _fake_tool_use_block(name: str, tool_input: dict):
    b = MagicMock()
    b.type = "tool_use"
    b.name = name
    b.input = tool_input
    return b


def _fake_message(content_blocks, stop_reason="tool_use"):
    """Mimic an anthropic.types.Message just enough for the handler."""
    m = MagicMock()
    m.content = content_blocks
    m.stop_reason = stop_reason
    return m


class PoseQuestionToolDefinitionTest(TestCase):
    """The tool definition must encode the bank scope into its schema —
    no question_text param, slot bounded by the actual id_map."""

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
            objective="Use the 360° rule",
            order_index=0, is_published=True,
        )
        cls.step = LessonStep.objects.create(
            lesson=cls.lesson, phase='practice', step_type='practice',
            order_index=0, concept_tag='angles_around_point',
            teacher_script="Three angles are 95°, 70°, x°. Find x.",
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
            concept_tag="angles_around_point",
            order_index=0, question_type='mcq',
        )

    def _make_tutor(self):
        from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor
        session = TutorSession.objects.create(
            institution=self.institution, student=self.student,
            lesson=self.lesson, status="active", engine_state={},
        )
        tutor = ConversationalTutor(session)
        tutor._llm_client = MagicMock()
        tutor._instructor_client = None
        return tutor

    def test_tool_schema_has_no_question_text_param(self):
        """The whole point of the tool: there's no way to pass arbitrary
        question text. The LLM can only commit to a slot index."""
        tutor = self._make_tutor()
        tutor._question_id_map = {0: self.step, 1: self.q1}
        tool = tutor._build_pose_question_tool()
        self.assertIsNotNone(tool)
        self.assertEqual(tool["name"], "pose_question")
        props = tool["input_schema"]["properties"]
        self.assertIn("slot", props)
        self.assertIn("lead_in", props)
        self.assertNotIn("question_text", props)
        self.assertNotIn("question", props)
        self.assertNotIn("text", props)

    def test_tool_slot_bounded_by_id_map(self):
        tutor = self._make_tutor()
        tutor._question_id_map = {0: self.step, 1: self.q1, 2: self.q1, 5: self.q1}
        tool = tutor._build_pose_question_tool()
        slot_schema = tool["input_schema"]["properties"]["slot"]
        self.assertEqual(slot_schema["minimum"], 0)
        self.assertEqual(slot_schema["maximum"], 5)

    def test_tool_returns_none_when_id_map_empty(self):
        """Non-math turns / empty bank → no tool, fall back to text path."""
        tutor = self._make_tutor()
        tutor._question_id_map = {}
        self.assertIsNone(tutor._build_pose_question_tool())


class PoseQuestionMessageHandlerTest(TestCase):
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
            unit=cls.unit, title="L", objective="o",
            order_index=0, is_published=True,
        )
        cls.step = LessonStep.objects.create(
            lesson=cls.lesson, phase='practice', step_type='practice',
            order_index=0, concept_tag='t',
            teacher_script="Three angles are 95°, 70°, x°. Find x.",
            expected_answer="195°",
        )
        cls.ticket = ExitTicket.objects.create(
            lesson=cls.lesson, passing_score=8, is_published=True,
        )
        cls.q1 = ExitTicketQuestion.objects.create(
            exit_ticket=cls.ticket,
            question_text="Bank Q1: pose me?",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="t", order_index=0, question_type='mcq',
        )

    def _make_tutor(self):
        from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor
        session = TutorSession.objects.create(
            institution=self.institution, student=self.student,
            lesson=self.lesson, status="active", engine_state={},
        )
        tutor = ConversationalTutor(session)
        tutor._llm_client = MagicMock()
        tutor._instructor_client = None
        tutor._question_id_map = {0: self.step, 1: self.q1}
        return tutor

    def test_renders_slot_0_lesson_step(self):
        tutor = self._make_tutor()
        message = _fake_message([
            _fake_tool_use_block("pose_question", {
                "slot": 0,
                "lead_in": "Right — let's apply that. Try this:",
            }),
        ])
        meta = {}
        out = tutor._handle_pose_question_message(message, meta)
        # Lead-in + rendered teacher_script verbatim.
        self.assertIn("Right — let's apply that", out)
        self.assertIn("Three angles are 95°, 70°, x°. Find x.", out)
        # bank_question_ref recorded so next turn grades correctly.
        self.assertEqual(meta["bank_question_ref"]["kind"], "lesson_step")
        self.assertEqual(meta["bank_question_ref"]["id"], self.step.id)

    def test_renders_slot_1_exit_ticket_question_with_options(self):
        tutor = self._make_tutor()
        message = _fake_message([
            _fake_tool_use_block("pose_question", {"slot": 1}),
        ])
        meta = {}
        out = tutor._handle_pose_question_message(message, meta)
        self.assertIn("Bank Q1: pose me?", out)
        self.assertIn("A) A", out)
        self.assertIn("D) D", out)
        self.assertEqual(
            meta["bank_question_ref"]["kind"], "exit_ticket_question",
        )
        self.assertEqual(meta["bank_question_ref"]["id"], self.q1.id)

    def test_invalid_slot_falls_back_to_slot_0(self):
        tutor = self._make_tutor()
        message = _fake_message([
            _fake_tool_use_block("pose_question", {"slot": 999}),
        ])
        out = tutor._handle_pose_question_message(message, {})
        # Slot 0 (the LessonStep) gets posed instead.
        self.assertIn("Three angles are 95°, 70°, x°", out)

    def test_text_block_passes_through_verbatim(self):
        """Policy (2026-05-05): text blocks pass through as-is. We do
        NOT edit the tutor message in place — that produced robotic
        output ("Quick check:" with no question). When the LLM authors
        a numerical question in prose, the combined judge flags
        NO_AUTHORING and the V3 regen path takes one clean retry.
        Editing in place is gone for good."""
        tutor = self._make_tutor()
        message = _fake_message([
            _fake_text_block(
                "If three angles around a point are 100°, 120°, 80°, what's the fourth?"
            ),
            _fake_tool_use_block("pose_question", {"slot": 0}),
        ])
        out = tutor._handle_pose_question_message(message, {})
        # Text block content survives verbatim.
        self.assertIn("100°", out)
        self.assertIn("120°", out)
        # The bank question is NOT appended: the text block already
        # carries an authored question, and rendering a second one would
        # leave the student two questions with awaiting_answer pointing
        # at the wrong one. See AUTHORED_VS_BANK_CONFLICT in
        # conversational_tutor.py — the guard skips the bank render and
        # lets the chat-authored grader take the next reply. This test
        # asserted the pre-guard behaviour.
        self.assertNotIn("Three angles are 95°, 70°, x°", out)

    def test_bank_question_renders_when_the_text_poses_none(self):
        """The other half of the conflict guard: with no authored
        question in the text, the bank question is rendered as usual."""
        tutor = self._make_tutor()
        message = _fake_message([
            _fake_text_block("Angles around a point always add to the same total."),
            _fake_tool_use_block("pose_question", {"slot": 0}),
        ])
        out = tutor._handle_pose_question_message(message, {})
        self.assertIn("Angles around a point always add", out)
        self.assertIn("Three angles are 95°, 70°, x°", out)

    def test_text_only_response_passes_through(self):
        """No tool call → plain text. Acceptable on teach turns where
        the tutor doesn't pose a question this exchange."""
        tutor = self._make_tutor()
        message = _fake_message(
            [_fake_text_block("Let's review the rule first. Why do angles around a point sum to 360°?")],
            stop_reason="end_turn",
        )
        out = tutor._handle_pose_question_message(message, {})
        # Conceptual question without digits — keeps as-is.
        self.assertIn("Let's review the rule first", out)

    def test_text_block_with_digits_passes_through_unchanged(self):
        """Whatever the LLM types in a text block reaches the student
        verbatim. Editing in place is no longer used. If the LLM types
        a numerical question, the combined judge regenerates the turn
        (one clean retry) — we don't surgically edit."""
        tutor = self._make_tutor()
        message = _fake_message(
            [_fake_text_block(
                "Remember the rule: angles around a point sum to 360°. "
                "Does that ring a bell?"
            )],
            stop_reason="end_turn",
        )
        out = tutor._handle_pose_question_message(message, {})
        self.assertIn("ring a bell?", out)
        self.assertIn("360°", out)


class JudgeClientRoutingTest(TestCase):
    """The combined judge runs on a faster Purpose.JUDGE model (Sonnet
    by default) — keeps tutor on Opus while cutting per-turn latency."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T", slug="t")
        cls.student = User.objects.create_user(username="stu", password="pw")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(
            course=cls.course, title="U", order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="o",
            order_index=0, is_published=True,
        )
        LessonStep.objects.create(
            lesson=cls.lesson, phase='practice', step_type='practice',
            order_index=0, teacher_script="q", expected_answer="a",
        )

    def test_judge_client_resolves_to_purpose_judge_when_configured(self):
        from ai_tutor.apps.llm.models import ModelConfig
        from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor
        # Ensure a Purpose.JUDGE config exists (the migration seeds one
        # but the test DB may need re-seeding).
        ModelConfig.objects.get_or_create(
            institution=self.institution,
            purpose='judge',
            defaults=dict(
                name='Test judge',
                provider='anthropic',
                model_name='claude-sonnet-4-20250514',
                api_key_env_var='ANTHROPIC_API_KEY',
                api_key_encrypted='',
                is_active=True,
            ),
        )
        session = TutorSession.objects.create(
            institution=self.institution, student=self.student,
            lesson=self.lesson, status="active", engine_state={},
        )
        tutor = ConversationalTutor(session)
        tutor._llm_client = MagicMock()  # tutor stays whatever
        # Resolve judge_client — should hit Purpose.JUDGE config (Sonnet).
        client = tutor.judge_client
        self.assertIsNotNone(client)
        self.assertEqual(client.config.purpose, 'judge')
        self.assertEqual(client.config.model_name, 'claude-sonnet-4-20250514')

    def test_judge_client_falls_back_to_llm_client_when_unconfigured(self):
        from ai_tutor.apps.llm.models import ModelConfig
        from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor
        ModelConfig.objects.filter(purpose='judge', is_active=True).update(is_active=False)
        try:
            session = TutorSession.objects.create(
                institution=self.institution, student=self.student,
                lesson=self.lesson, status="active", engine_state={},
            )
            tutor = ConversationalTutor(session)
            sentinel = MagicMock(name="tutor_llm_client")
            tutor._llm_client = sentinel
            self.assertIs(tutor.judge_client, sentinel)
        finally:
            ModelConfig.objects.filter(purpose='judge').update(is_active=True)


class BankScopeStrictTest(TestCase):
    """Verify the bank no longer leaks across lesson steps."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T", slug="t")
        cls.student = User.objects.create_user(username="stu", password="pw")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math S3",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(
            course=cls.course, title="U", order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="o",
            order_index=0, is_published=True,
        )
        # Three steps with distinct concept_tags. Step-1 should NOT see
        # Step-2 or Step-3 questions in its bank.
        cls.step1 = LessonStep.objects.create(
            lesson=cls.lesson, phase='practice', step_type='practice',
            order_index=0, concept_tag='step_one_tag',
            teacher_script="Step 1 question?",
            expected_answer="1",
        )
        cls.step2 = LessonStep.objects.create(
            lesson=cls.lesson, phase='practice', step_type='practice',
            order_index=1, concept_tag='step_two_tag',
            teacher_script="Step 2 question?",
            expected_answer="2",
        )
        cls.step3 = LessonStep.objects.create(
            lesson=cls.lesson, phase='practice', step_type='practice',
            order_index=2, concept_tag='step_three_tag',
            teacher_script="Step 3 question?",
            expected_answer="3",
        )

    def _make_tutor_at_step(self, step_index):
        from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor
        session = TutorSession.objects.create(
            institution=self.institution, student=self.student,
            lesson=self.lesson, status="active",
            engine_state={'current_topic_index': step_index},
        )
        tutor = ConversationalTutor(session)
        tutor._llm_client = MagicMock()
        tutor._instructor_client = None
        return tutor

    def test_step_1_bank_does_not_include_step_2_or_3_questions(self):
        """The other-step augmentation that surfaced step-4 questions on
        step-1 was REMOVED. The id_map for step 1 should map only to the
        current step (slot 0) and same-tag exit ticket questions."""
        tutor = self._make_tutor_at_step(0)
        block = tutor._build_question_bank_block()
        # Slot 0 maps to step 1.
        self.assertEqual(tutor._question_id_map[0], self.step1)
        # No slot maps to step 2 or step 3.
        for slot, entry in tutor._question_id_map.items():
            if slot == 0:
                continue
            # slots 1+ should be ExitTicketQuestion, never a LessonStep
            # with a different order_index.
            self.assertFalse(
                hasattr(entry, 'order_index') and getattr(entry, 'step_type', None),
                f"slot {slot} unexpectedly maps to a LessonStep",
            )
        # The block text should NOT mention "Additional lesson-step
        # questions" — that was the leak.
        self.assertNotIn("Additional lesson-step questions", block)
