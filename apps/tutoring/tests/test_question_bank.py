"""Tests for the no-authoring question-bank helpers (P1).

See memory/tutor_no_authoring_plan.md.
"""

from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.tutoring.models import ExitTicket, ExitTicketQuestion
from apps.tutoring.question_bank import (
    CANDIDATES_PER_STEP,
    POOL_SIZE_PER_LESSON,
    SENTINEL_NO_QUESTION,
    parse_question_signal,
    pick_candidates_for_step,
    render_bank_block,
    render_question_to_prose,
    sample_session_pool,
)


class QuestionBankHelpersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T", slug="t")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math S3",
            grade_level="S3", is_published=True,
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
            lesson=cls.lesson,
            phase='practice',
            step_type='practice',
            order_index=0,
            teacher_script="Three angles around a point are 95°, 70°, and x°. Find x.",
            expected_answer="195°",
        )
        cls.published_ticket = ExitTicket.objects.create(
            lesson=cls.lesson, passing_score=8, is_published=True,
        )
        # 20 published questions across two concept_tags
        for i in range(20):
            ExitTicketQuestion.objects.create(
                exit_ticket=cls.published_ticket,
                question_text=f"Bank Q{i}: find x given a={i}, b={i+1}",
                option_a="A", option_b="B", option_c="C", option_d="D",
                correct_answer="A", explanation="",
                concept_tag="angles_around_point" if i < 10 else "angles_on_line",
                order_index=i,
            )
        # A second lesson with an UNPUBLISHED ticket — pool for that
        # lesson must be empty (the bank is gated on is_published).
        cls.unpublished_lesson = Lesson.objects.create(
            unit=cls.unit, title="Pending review",
            objective="x", order_index=1, is_published=False,
        )
        cls.unpublished_ticket = ExitTicket.objects.create(
            lesson=cls.unpublished_lesson, passing_score=8, is_published=False,
        )
        ExitTicketQuestion.objects.create(
            exit_ticket=cls.unpublished_ticket,
            question_text="Unpublished — should never surface",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="angles_around_point",
            order_index=0,
        )

    def test_pool_excludes_unpublished_ticket_questions(self):
        # Pool for the published lesson — only published bank questions
        pool = sample_session_pool(self.lesson, seed=1, pool_size=20)
        for q in pool:
            self.assertTrue(q.exit_ticket.is_published)
            self.assertNotIn("Unpublished", q.question_text)
        # Pool for the lesson whose ticket is_published=False — empty
        empty = sample_session_pool(self.unpublished_lesson, seed=1)
        self.assertEqual(empty, [])

    def test_pool_is_deterministic_per_seed(self):
        a = sample_session_pool(self.lesson, seed=42)
        b = sample_session_pool(self.lesson, seed=42)
        self.assertEqual([q.id for q in a], [q.id for q in b])

    def test_pool_varies_across_seeds(self):
        a = sample_session_pool(self.lesson, seed=1)
        b = sample_session_pool(self.lesson, seed=2)
        # With 20 published questions and pool_size=12, two different
        # seeds should produce different orderings/membership.
        self.assertNotEqual([q.id for q in a], [q.id for q in b])

    def test_pool_size_caps_at_request(self):
        pool = sample_session_pool(self.lesson, seed=1, pool_size=5)
        self.assertEqual(len(pool), 5)

    def test_pool_returns_all_when_bank_smaller_than_request(self):
        pool = sample_session_pool(self.lesson, seed=1, pool_size=999)
        # 20 published items in setUpTestData — pool returns all of them
        self.assertEqual(len(pool), 20)

    def test_pool_empty_when_no_published_bank(self):
        empty_lesson = Lesson.objects.create(
            unit=self.unit, title="Empty", objective="x",
            order_index=99, is_published=True,
        )
        pool = sample_session_pool(empty_lesson, seed=1)
        self.assertEqual(pool, [])

    def test_pick_candidates_filters_by_concept_tag(self):
        pool = sample_session_pool(self.lesson, seed=1, pool_size=20)
        cands = pick_candidates_for_step(pool, "angles_around_point")
        # All returned candidates must carry the requested tag
        for q in cands:
            self.assertEqual(q.concept_tag, "angles_around_point")

    def test_pick_candidates_falls_back_when_no_tag_match(self):
        pool = sample_session_pool(self.lesson, seed=1, pool_size=20)
        cands = pick_candidates_for_step(pool, "no_such_tag_in_bank")
        # Fallback returns same-lesson questions even though tag missed
        self.assertGreater(len(cands), 0)
        self.assertLessEqual(len(cands), CANDIDATES_PER_STEP)

    def test_pick_candidates_caps_at_max(self):
        pool = sample_session_pool(self.lesson, seed=1, pool_size=20)
        cands = pick_candidates_for_step(pool, "angles_around_point", max_candidates=2)
        self.assertEqual(len(cands), 2)

    def test_render_bank_block_includes_step_at_slot_0(self):
        pool = sample_session_pool(self.lesson, seed=1, pool_size=20)
        cands = pick_candidates_for_step(pool, "angles_around_point", max_candidates=3)
        block, id_map = render_bank_block(self.step, cands)
        self.assertIn("<question_bank>", block)
        self.assertIn("[0]", block)
        # Slot 0 must be the LessonStep itself
        self.assertIs(id_map[SENTINEL_NO_QUESTION], self.step)
        # Step's teacher_script must appear verbatim (truncated to 300)
        self.assertIn("Three angles around a point", block)

    def test_render_bank_block_numbers_candidates_from_1(self):
        pool = sample_session_pool(self.lesson, seed=1, pool_size=20)
        cands = pick_candidates_for_step(pool, "angles_around_point", max_candidates=3)
        block, id_map = render_bank_block(self.step, cands)
        for i, q in enumerate(cands, start=1):
            self.assertIn(f"[{i}]", block)
            self.assertIs(id_map[i], q)

    def test_render_bank_block_includes_no_authoring_rule(self):
        block, _ = render_bank_block(self.step, [])
        self.assertIn("MUST come from this bank", block)
        self.assertIn("|||QUESTION:N|||", block)

    def test_parse_signal_extracts_id_and_strips(self):
        text = "Let's try this one. |||QUESTION:3|||"
        clean, n = parse_question_signal(text)
        self.assertEqual(n, 3)
        self.assertNotIn("|||QUESTION", clean)

    def test_parse_signal_returns_none_when_absent(self):
        clean, n = parse_question_signal("No signal here.")
        self.assertIsNone(n)
        self.assertEqual(clean, "No signal here.")

    def test_parse_signal_handles_zero_sentinel(self):
        clean, n = parse_question_signal("Pose the step. |||QUESTION:0|||")
        self.assertEqual(n, SENTINEL_NO_QUESTION)

    def test_parse_signal_handles_whitespace_around_id(self):
        clean, n = parse_question_signal("ok |||QUESTION : 7 |||")
        self.assertEqual(n, 7)

    def test_render_to_prose_step_returns_teacher_script(self):
        prose = render_question_to_prose(self.step)
        self.assertEqual(prose, self.step.teacher_script)

    def test_render_to_prose_mcq_includes_lettered_options(self):
        q = ExitTicketQuestion.objects.first()
        prose = render_question_to_prose(q)
        # Stem + each option appears
        self.assertIn(q.question_text, prose)
        self.assertIn("A) A", prose)
        self.assertIn("B) B", prose)
        self.assertIn("C) C", prose)
        self.assertIn("D) D", prose)

    def test_render_to_prose_none_returns_empty(self):
        self.assertEqual(render_question_to_prose(None), '')
