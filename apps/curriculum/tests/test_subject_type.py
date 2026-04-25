"""Tests for Course.subject_type and the updated is_math property.

See memory/math_tutor_fix_plan.md M8.
"""

from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course


class SubjectTypeTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='T', slug='t')

    def test_subject_type_overrides_title_keyword(self):
        """When subject_type is set, is_math derives from it (not the title)."""
        course = Course.objects.create(
            institution=self.institution,
            title='General Studies',  # title doesn't contain math keywords
            subject_type='math',
        )
        self.assertTrue(course.is_math)

    def test_subject_type_science_not_math(self):
        course = Course.objects.create(
            institution=self.institution,
            title='Math Lab',  # title says math
            subject_type='science',  # but it's actually science
        )
        # subject_type wins — title heuristic is only fallback
        self.assertFalse(course.is_math)

    def test_legacy_fallback_when_subject_type_empty(self):
        course = Course.objects.create(
            institution=self.institution,
            title='Grade 8 Mathematics',
            # subject_type intentionally not set
        )
        # Legacy MATH_KEYWORDS keyword match still kicks in
        self.assertTrue(course.is_math)

    def test_legacy_fallback_returns_false_for_non_math_title(self):
        course = Course.objects.create(
            institution=self.institution,
            title='World History',
        )
        self.assertFalse(course.is_math)

    def test_blank_subject_type_default(self):
        course = Course.objects.create(
            institution=self.institution,
            title='Course',
        )
        self.assertEqual(course.subject_type, '')
