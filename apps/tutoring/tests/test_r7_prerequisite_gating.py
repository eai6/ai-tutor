"""Tests for R7: Prerequisite gating on lesson start."""

from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.tutoring.models import StudentLessonProgress


class TestR7PrerequisiteGating(BaseTutoringTestCase):
    """Test that lessons with unmet prerequisites are blocked."""

    def test_check_prerequisites_met(self):
        """Should return (True, []) when prerequisites are mastered."""
        from apps.tutoring.views import check_lesson_prerequisites

        # Mark prereq lesson as mastered
        self._create_progress(mastery_level='mastered')

        met, unmet = check_lesson_prerequisites(self.student_user, self.lesson)
        self.assertTrue(met)
        self.assertEqual(len(unmet), 0)

    def test_check_prerequisites_not_met(self):
        """Should return (False, [...]) when prerequisites are not mastered."""
        from apps.tutoring.views import check_lesson_prerequisites

        met, unmet = check_lesson_prerequisites(self.student_user, self.lesson)
        self.assertFalse(met)
        self.assertEqual(len(unmet), 1)
        self.assertEqual(unmet[0]['lesson_id'], self.prereq_lesson.id)

    def test_check_prerequisites_in_progress_not_sufficient(self):
        """In-progress mastery should not satisfy prerequisites."""
        from apps.tutoring.views import check_lesson_prerequisites

        self._create_progress(mastery_level='in_progress')

        met, unmet = check_lesson_prerequisites(self.student_user, self.lesson)
        self.assertFalse(met)

    def test_check_prerequisites_no_prereqs(self):
        """Lesson with no prerequisites should always pass."""
        from apps.tutoring.views import check_lesson_prerequisites

        # prereq_lesson has no prerequisites itself
        met, unmet = check_lesson_prerequisites(self.student_user, self.prereq_lesson)
        self.assertTrue(met)
        self.assertEqual(len(unmet), 0)

    def test_check_prerequisites_fails_open(self):
        """Should return (True, []) if the check itself errors."""
        from apps.tutoring.views import check_lesson_prerequisites
        from unittest.mock import patch

        with patch('apps.tutoring.views.check_lesson_prerequisites.__module__'):
            # Force an import error by patching the model import
            with patch('apps.tutoring.skills_models.LessonPrerequisite.objects') as mock_qs:
                mock_qs.filter.side_effect = Exception("DB error")

                met, unmet = check_lesson_prerequisites(self.student_user, self.lesson)
                self.assertTrue(met)  # Fails open
                self.assertEqual(len(unmet), 0)
