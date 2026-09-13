"""Tests for backfill_course_subjects management command.

The command writes one field now: subject_code. subject_type follows from it
(a property, step 4 of memory/subject_grade_unification_plan.md), so the
assertions below check the derived value as well — filling the code is what
makes is_math stop falling through to the legacy MATH_KEYWORDS heuristic on a
course whose title doesn't contain a keyword.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course


class BackfillCourseSubjectsTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='T', slug='t')

    def _backfill(self):
        out = StringIO()
        call_command('backfill_course_subjects', apply=True, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_math_course_gets_both_code_and_type(self):
        course = Course.objects.create(
            institution=self.institution,
            title='S3 Algebra',
        )
        self._backfill()
        course.refresh_from_db()
        self.assertEqual(course.subject_code, 'mathematics')
        self.assertEqual(course.subject_type, 'math')
        self.assertTrue(course.is_math)

    def test_geography_course_gets_both_code_and_type(self):
        course = Course.objects.create(
            institution=self.institution,
            title='Geography of Seychelles',
        )
        self._backfill()
        course.refresh_from_db()
        self.assertEqual(course.subject_code, 'geography')
        self.assertEqual(course.subject_type, 'humanities')
        self.assertFalse(course.is_math)

    def test_science_courses_get_science_type(self):
        for title, code in [
            ('Physics for S4', 'physics'),
            ('Chemistry Basics', 'chemistry'),
            ('Biology I', 'biology'),
        ]:
            with self.subTest(title=title):
                c = Course.objects.create(institution=self.institution, title=title)
                self._backfill()
                c.refresh_from_db()
                self.assertEqual(c.subject_code, code)
                self.assertEqual(c.subject_type, 'science')

    def test_does_not_overwrite_an_existing_subject_code(self):
        """Without --overwrite, a subject a teacher chose stays put — even when
        the title keywords say something else."""
        course = Course.objects.create(
            institution=self.institution,
            title='Math 101',
            subject_code='french',  # deliberately wrong, simulating teacher input
        )
        self._backfill()
        course.refresh_from_db()
        self.assertEqual(course.subject_code, 'french')
        self.assertEqual(course.subject_type, 'language')
        self.assertFalse(course.is_math)

    def test_overwrite_flag_replaces_the_subject_code(self):
        course = Course.objects.create(
            institution=self.institution,
            title='Math 101',
            subject_code='french',
        )
        out = StringIO()
        call_command(
            'backfill_course_subjects', apply=True, overwrite=True,
            stdout=out, stderr=StringIO(),
        )
        course.refresh_from_db()
        self.assertEqual(course.subject_code, 'mathematics')
        # The type follows the code with no second write.
        self.assertEqual(course.subject_type, 'math')
