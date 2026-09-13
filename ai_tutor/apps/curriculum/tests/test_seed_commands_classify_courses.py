"""Every Course a seed command creates is classified.

Seeds are where a developer's idea of a normal row comes from, and both of
these were producing the exact shape memory/subject_grade_unification_plan.md
exists to remove: no subject_code (so is_math falls back to scanning the
title) and a grade the platform cannot match — 'Grade 3', or the ranges
'S1-S3' / 'S1-S5', which grade_levels splits on commas and so read back as one
meaningless token.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from ai_tutor.apps.accounts.models import PlatformConfig
from ai_tutor.apps.curriculum.models import Course


class SeededCoursesAreMatchableTest(TestCase):
    def _assert_all_courses_classified(self):
        valid_grades = {c[0].strip() for c in PlatformConfig.get_grade_choices()}
        courses = Course.objects.all()
        self.assertTrue(courses.exists(), 'seed created no courses')
        for course in courses:
            with self.subTest(course=course.title):
                self.assertTrue(
                    course.subject_code,
                    f'{course.title!r} has no subject_code, so material '
                    f'inheritance matches nothing and is_math reads the title',
                )
                self.assertTrue(course.grade_levels, f'{course.title!r} has no grade')
                off_list = [g for g in course.grade_levels if g not in valid_grades]
                self.assertFalse(
                    off_list,
                    f'{course.title!r} carries {off_list!r}, which is in no '
                    f'configured grade set and so matches no student',
                )

    def test_seed_sample_data(self):
        call_command('seed_sample_data', stdout=StringIO(), stderr=StringIO())
        self._assert_all_courses_classified()

    def test_seed_seychelles(self):
        call_command('seed_seychelles', stdout=StringIO(), stderr=StringIO())
        self._assert_all_courses_classified()
