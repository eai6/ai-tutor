"""Layer 3 wiring tests — _save_steps_to_db status + audit behaviour.

Focused integration tests:
  - Math lesson + arith_unresolved=True → content_status flips to
    READY_WITH_WARNINGS, retry_count persisted to audit.
  - Math lesson + arith_unresolved=False → content_status unchanged,
    retry_count=0 persisted.
  - Non-math lesson → no Layer 3 metadata written, content_status
    unchanged.

A full _generate_steps end-to-end retry test would require mocking
the instructor client; that's covered indirectly by the unit tests
on build_arithmetic_constraint_block + the retry path's idempotence.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.content_generator import LessonContentGenerator
from apps.curriculum.models import Course, Lesson, Unit


class Layer3SaveStepsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(
            name="L3 Test", slug="l3-test",
        )
        cls.math_course = Course.objects.create(
            institution=cls.institution,
            title="Mathematics S3",  # is_math=True via title heuristic
            grade_level="S3",
            is_published=True,
        )
        cls.geo_course = Course.objects.create(
            institution=cls.institution,
            title="Geography S3",
            grade_level="S3",
            is_published=True,
        )
        cls.math_unit = Unit.objects.create(
            course=cls.math_course, title="Geometry", order_index=0,
        )
        cls.geo_unit = Unit.objects.create(
            course=cls.geo_course, title="Maps", order_index=0,
        )

    def _make_lesson(self, course, unit, title="Sample"):
        return Lesson.objects.create(
            unit=unit,
            title=title,
            objective="Sample objective",
            order_index=0,
            content_status=Lesson.ContentStatus.GENERATING,
        )

    def _minimal_step(self, order=0, **overrides):
        base = {
            "order_index": order,
            "phase": "explore",
            "step_type": "teach",
            "concept_tag": "test",
            "enabling_objective": "Sample EO",
            "teacher_script": "Some teaching content.",
            "question": "",
            "answer_type": "none",
            "expected_answer": "",
            "hints": [],
            "media": None,
            "educational_content": None,
            "curriculum_context": None,
            "priority": 1,
        }
        base.update(overrides)
        return base

    def _gen(self):
        # `_save_steps_to_db` doesn't touch the LLM client — but
        # LessonContentGenerator.__init__ does (instantiates an
        # instructor client). Bypass __init__ via __new__ and set
        # only the institution_id we actually use.
        gen = LessonContentGenerator.__new__(LessonContentGenerator)
        gen.institution_id = self.institution.id
        return gen

    def test_unresolved_arithmetic_flips_status_to_warnings(self):
        lesson = self._make_lesson(self.math_course, self.math_unit)
        gen = self._gen()
        gen._save_steps_to_db(
            lesson,
            [self._minimal_step()],
            arith_retry_count=1,
            arith_unresolved=True,
        )
        lesson.refresh_from_db()
        self.assertEqual(
            lesson.content_status,
            Lesson.ContentStatus.READY_WITH_WARNINGS,
        )
        audit = (lesson.metadata or {}).get("verification_audit") or {}
        self.assertEqual(audit.get("retry_count"), 1)
        self.assertTrue(audit.get("math_check_run"))

    def test_resolved_arithmetic_keeps_status(self):
        lesson = self._make_lesson(self.math_course, self.math_unit)
        gen = self._gen()
        gen._save_steps_to_db(
            lesson,
            [self._minimal_step()],
            arith_retry_count=1,
            arith_unresolved=False,
        )
        lesson.refresh_from_db()
        # content_status unchanged from GENERATING (the caller bumps
        # it to READY elsewhere; we don't override on the happy path).
        self.assertNotEqual(
            lesson.content_status,
            Lesson.ContentStatus.READY_WITH_WARNINGS,
        )
        audit = (lesson.metadata or {}).get("verification_audit") or {}
        self.assertEqual(audit.get("retry_count"), 1)

    def test_no_retry_no_unresolved_records_zero(self):
        lesson = self._make_lesson(self.math_course, self.math_unit)
        gen = self._gen()
        gen._save_steps_to_db(
            lesson,
            [self._minimal_step()],
            # defaults: retry_count=0, unresolved=False
        )
        lesson.refresh_from_db()
        self.assertNotEqual(
            lesson.content_status,
            Lesson.ContentStatus.READY_WITH_WARNINGS,
        )
        audit = (lesson.metadata or {}).get("verification_audit") or {}
        self.assertEqual(audit.get("retry_count"), 0)

    def test_non_math_lesson_skips_layer3_metadata(self):
        lesson = self._make_lesson(self.geo_course, self.geo_unit)
        gen = self._gen()
        gen._save_steps_to_db(
            lesson,
            [self._minimal_step()],
            arith_retry_count=1,
            arith_unresolved=True,
        )
        lesson.refresh_from_db()
        # Geography lesson — Layer 3 is gated by is_math, no
        # status flip even when unresolved=True.
        self.assertNotEqual(
            lesson.content_status,
            Lesson.ContentStatus.READY_WITH_WARNINGS,
        )
        audit = (lesson.metadata or {}).get("verification_audit") or {}
        # Non-math lesson should not have retry_count written.
        self.assertNotIn("retry_count", audit)
