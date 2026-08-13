"""End-to-end Layer 4 integration tests.

A templated question landing in the exit-ticket persistence path:
  1. LLM emits a `template` field on the question dict
  2. content_generator.py renders it into prose fields
  3. ExitTicketQuestion is created with template_data set
  4. The grader can grade student answers against it

We don't run the full LLM call here — we directly inject what the
LLM would have emitted, then exercise the rest of the pipeline.
"""

from __future__ import annotations

from django.test import TestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.curriculum.parametric_renderer import (
    ANGLES_AROUND_A_POINT,
    TEMPLATE_LIBRARY,
    render_template,
)
from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketQuestion


class Layer4IntegrationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(
            name="L4 Test", slug="l4-test",
        )
        cls.course = Course.objects.create(
            institution=cls.institution,
            title="Mathematics S3",
            grade_level="S3",
            is_published=True,
        )
        cls.unit = Unit.objects.create(
            course=cls.course, title="Geometry", order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="Angles around a point",
            objective="Find missing angle",
            order_index=0, is_published=True,
        )
        cls.exit_ticket = ExitTicket.objects.create(
            lesson=cls.lesson, passing_score=8, time_limit_minutes=15,
        )

    def test_template_library_has_canonical_pattern(self):
        # Confirm the library exposes the L4.A pattern by name.
        self.assertIn("angles_around_point_3", TEMPLATE_LIBRARY)
        self.assertIs(
            TEMPLATE_LIBRARY["angles_around_point_3"],
            ANGLES_AROUND_A_POINT,
        )

    def test_render_then_persist_creates_question_with_template_data(self):
        rendered = render_template(ANGLES_AROUND_A_POINT, seed=42)
        self.assertIsNotNone(rendered)

        # Mimic the persistence loop in content_generator.py.
        q = ExitTicketQuestion.objects.create(
            exit_ticket=self.exit_ticket,
            question_type=rendered["question_type"],
            question_text=rendered["question_text"],
            explanation=rendered["explanation"],
            answer_data=rendered["answer_data"],
            template_data=rendered["template_data"],
            order_index=0,
        )

        q.refresh_from_db()
        # template_data round-trips.
        self.assertEqual(
            q.template_data["template_text"],
            ANGLES_AROUND_A_POINT.template_text,
        )
        self.assertEqual(
            q.template_data["answer_formula"], "360 - a - b",
        )
        # Prose fields filled in.
        self.assertIn("°", q.question_text)
        self.assertIn("°", q.correct_answer or rendered["correct_answer"])
        # Answer data carries the computed answer + parameters.
        self.assertIn("computed", q.answer_data)
        self.assertIn("model_answer", q.answer_data)
        self.assertIn("parameters", q.answer_data)
        a = q.answer_data["parameters"]["a"]
        b = q.answer_data["parameters"]["b"]
        self.assertEqual(q.answer_data["computed"], float(360 - a - b))

    def test_rendered_question_has_correct_arithmetic(self):
        # Run several seeds; every one should have a verifiable
        # answer matching 360 - a - b.
        for seed in range(20):
            r = render_template(ANGLES_AROUND_A_POINT, seed=seed)
            if r is None:
                continue  # constraints couldn't be satisfied
            params = r["answer_data"]["parameters"]
            expected = 360 - params["a"] - params["b"]
            self.assertEqual(r["answer_data"]["computed"], float(expected))
            # The displayed answer string also matches (with unit).
            self.assertEqual(r["correct_answer"], f"{expected}°")

    def test_four_angle_template_works(self):
        from ai_tutor.apps.curriculum.parametric_renderer import ANGLES_AROUND_A_POINT_4
        r = render_template(ANGLES_AROUND_A_POINT_4, seed=7)
        self.assertIsNotNone(r)
        params = r["answer_data"]["parameters"]
        expected = 360 - params["a"] - params["b"] - params["c"]
        self.assertEqual(r["answer_data"]["computed"], float(expected))

    def test_question_text_does_not_leak_unfilled_braces(self):
        r = render_template(ANGLES_AROUND_A_POINT, seed=1)
        self.assertNotIn("{a}", r["question_text"])
        self.assertNotIn("{b}", r["question_text"])
        self.assertNotIn("{answer}", r["explanation"])
