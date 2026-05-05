"""Tests for the ExitTicketQuestion.enabling_objective sub-objective
field added 2026-05-01.

Promoted from answer_data JSON to a first-class CharField so:
  - remediation can target the failing sub-skill (specific EO), not
    just the broad learning objective (concept_tag)
  - the dashboard can show both as separate pills
  - the bank-pull helper prefers sub-objective matches over broad ones
"""

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import (
    ExitTicket,
    ExitTicketQuestion,
)
from apps.tutoring.question_bank import pick_published_for_concept_tag


class EnablingObjectiveFieldTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="EO", slug="eo")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math S2",
            grade_level="S2", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(course=cls.course, title="U", order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="Profit & Loss",
            objective="Know and use the terms selling/cost/profit",
            order_index=0, is_published=True,
        )
        cls.ticket = ExitTicket.objects.create(
            lesson=cls.lesson, passing_score=8, is_published=True,
        )

    def test_enabling_objective_field_is_writable_and_queryable(self):
        q = ExitTicketQuestion.objects.create(
            exit_ticket=self.ticket,
            question_text="Q1",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="Know and use the terms",
            enabling_objective="Define cost price, selling price, profit",
            order_index=0,
        )
        q.refresh_from_db()
        self.assertEqual(
            q.enabling_objective,
            "Define cost price, selling price, profit",
        )
        # Queryable as a first-class field
        self.assertTrue(
            ExitTicketQuestion.objects
            .filter(enabling_objective="Define cost price, selling price, profit")
            .exists()
        )

    def test_enabling_objective_blank_default(self):
        q = ExitTicketQuestion.objects.create(
            exit_ticket=self.ticket,
            question_text="Q",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="x",
            order_index=1,
        )
        self.assertEqual(q.enabling_objective, "")

    def test_pick_published_prefers_enabling_objective_over_concept_tag(self):
        """When both fields could match, the helper must prefer
        enabling_objective (the narrow sub-skill) over concept_tag."""
        # Two questions: one matches by enabling_objective, the other
        # only by concept_tag. The helper should pick the EO match.
        eo_match = ExitTicketQuestion.objects.create(
            exit_ticket=self.ticket,
            question_text="Specific sub-skill question",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="Other broad",
            enabling_objective="Calculate profit from cost and selling price",
            order_index=10,
        )
        broad_match = ExitTicketQuestion.objects.create(
            exit_ticket=self.ticket,
            question_text="Broad question",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="Calculate profit from cost and selling price",
            enabling_objective="A different EO",
            order_index=11,
        )
        picks = pick_published_for_concept_tag(
            self.lesson,
            "Calculate profit from cost and selling price",
            max_candidates=1,
        )
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].id, eo_match.id)

    def test_pick_published_falls_back_to_concept_tag(self):
        """When no enabling_objective matches but concept_tag does,
        the helper falls back to the broad match."""
        broad_only = ExitTicketQuestion.objects.create(
            exit_ticket=self.ticket,
            question_text="Broad-only match",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="Profit and loss basics",
            enabling_objective="",  # blank
            order_index=20,
        )
        picks = pick_published_for_concept_tag(
            self.lesson,
            "Profit and loss basics",
            max_candidates=1,
        )
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].id, broad_only.id)

    def test_pick_published_returns_empty_when_no_field_matches(self):
        """STRICT: no cross-tag fallback (2026-05-04). The previous
        "any lesson question" fallback was leaking later-step questions
        onto earlier steps. When a tag has no match, we return [] so
        the caller can fall back to slot 0 (current step's
        teacher_script) instead of pulling unrelated bank questions.
        """
        ExitTicketQuestion.objects.create(
            exit_ticket=self.ticket,
            question_text="Generic",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A", explanation="",
            concept_tag="not the requested tag",
            enabling_objective="not the requested EO",
            order_index=30,
        )
        picks = pick_published_for_concept_tag(
            self.lesson, "no_such_tag_anywhere", max_candidates=1,
        )
        self.assertEqual(picks, [])
