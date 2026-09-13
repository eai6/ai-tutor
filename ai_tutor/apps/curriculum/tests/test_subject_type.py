"""Tests for Course.subject_type (derived) and the is_math property.

subject_type was a stored field with its own dropdown on the course page, set
independently of subject_code. It is now a property mapping from subject_code
— step 4 of memory/subject_grade_unification_plan.md.

The tests that used to live here asserted the opposite (that a stored
subject_type overrides subject_code, and answers on its own when there is no
code). They are gone rather than ported: they pinned the divergence this
change removes.
"""

from django.test import TestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course


class SubjectTypeIsDerivedTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='T', slug='t')

    def _course(self, **kwargs):
        kwargs.setdefault('title', 'Course')
        return Course.objects.create(institution=self.institution, **kwargs)

    def test_the_code_decides_the_type(self):
        self.assertEqual(
            self._course(subject_code='geography').subject_type, 'humanities')
        self.assertEqual(
            self._course(subject_code='mathematics', title='M').subject_type, 'math')
        self.assertEqual(
            self._course(subject_code='biology', title='B').subject_type, 'science')
        self.assertEqual(
            self._course(subject_code='french', title='F').subject_type, 'language')

    def test_no_code_means_no_type(self):
        """Blank in, blank out — so `if course.subject_type` stays falsy for an
        unclassified course exactly as it did when the column was stored."""
        self.assertEqual(self._course().subject_type, '')

    def test_it_is_a_plain_string_not_an_enum_member(self):
        """Callers put this in logs, prompts and JSON. A TextChoices member is
        a str subclass, but its repr is 'Course.SubjectType.MATH', which leaks
        into anything that reprs rather than formats."""
        value = self._course(subject_code='mathematics').subject_type
        self.assertIs(type(value), str)

    def test_every_subject_code_maps_to_a_type(self):
        """Totality is the whole argument for deriving it: if some code had no
        type, the column would still be carrying information."""
        for code, _label in Course.SubjectCode.choices:
            course = Course(title='x', subject_code=code)
            self.assertIn(
                course.subject_type,
                {c[0] for c in Course.SubjectType.choices},
                msg=f'{code!r} maps to {course.subject_type!r}, not a SubjectType',
            )

    def test_history_and_geography_share_a_type_but_not_a_code(self):
        """Why subject_type could never be the canonical field, and why the
        benchmark sampler's 'humanities' → 'geography' mapping was wrong."""
        history = self._course(subject_code='history', title='H')
        geography = self._course(subject_code='geography', title='G')
        self.assertEqual(history.subject_type, geography.subject_type)
        self.assertNotEqual(history.subject_code, geography.subject_code)


class IsMathReadsSubjectCodeFirstTest(TestCase):
    """subject_code is the canonical subject — the value the upload form
    collects and the key material sharing joins on. Reading it last meant a
    course explicitly marked `mathematics` could still have is_math decided by
    a keyword scan of its title.

    Step 1 of memory/subject_grade_unification_plan.md.
    """

    def setUp(self):
        self.institution = Institution.objects.create(name='T2', slug='t2')

    def test_a_maths_course_survives_a_rename(self):
        """The production case. "Layer S Demo — Math S3" carries
        subject_code='mathematics', so is_math used to be True only because the
        title contains "Math"."""
        course = Course.objects.create(
            institution=self.institution,
            title='Angles around a point',      # no math keyword
            subject_code='mathematics',
        )
        self.assertTrue(course.is_math)

    def test_a_geography_course_named_after_a_keyword_is_not_math(self):
        """"Statistics" is a MATH_KEYWORD, and a geography lesson can be
        called that."""
        course = Course.objects.create(
            institution=self.institution,
            title='Population Statistics',
            subject_code='geography',
        )
        self.assertFalse(course.is_math)

    def test_the_title_still_answers_when_there_is_no_code(self):
        """The MATH_KEYWORDS fallback stays until `backfill_course_subjects
        --apply` has run on prod. Until then, dropping it would silently switch
        the math tutoring rules off for any prod course with no subject_code —
        a worse failure than the heuristic."""
        course = Course.objects.create(
            institution=self.institution, title='Grade 8 Mathematics')
        self.assertTrue(course.is_math)

    def test_the_title_fallback_says_no_for_a_non_math_title(self):
        course = Course.objects.create(
            institution=self.institution, title='World History')
        self.assertFalse(course.is_math)

    def test_expanded_math_keyword_fallback(self):
        """Audit v3 R4: MATH_KEYWORDS covers fractions/decimals/etc. A course
        titled 'Fractions and Decimals' with nothing set returned is_math=False,
        silently bypassing the math protection layer in the tutor.
        """
        for title in (
            'Equivalent Fractions',
            'Decimals and Percentages',
            'Probability Basics',
            'Statistics for S3',
            'Trigonometry I',
            'Mental Arithmetic',
        ):
            course = Course.objects.create(
                institution=self.institution, title=title)
            self.assertTrue(
                course.is_math,
                msg=f'{title!r} should be detected as math via title heuristic',
            )
