"""Tests for R13: Gamification XP awarding."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.tutoring.skills_models import StudentKnowledgeProfile


class TestR13Gamification(BaseTutoringTestCase):
    """Test that XP is awarded during skill practice."""

    def _make_profile(self, xp=0, level=1, streak=0):
        """Create a StudentKnowledgeProfile."""
        return StudentKnowledgeProfile.objects.create(
            student=self.student_user,
            course=self.course,
            total_xp=xp,
            level=level,
            current_streak_days=streak,
        )

    def test_correct_answer_awards_xp(self):
        """Correct answer should award 10 XP base."""
        from apps.tutoring.personalization import SkillAssessmentService

        session = self._create_session()
        svc = SkillAssessmentService(self.student_user, session)

        profile = self._make_profile(xp=0)

        svc._award_practice_xp(self.course, was_correct=True, hints_used=1)

        profile.refresh_from_db()
        # Correct = 10, has hints so no bonus, no streak = 10
        self.assertEqual(profile.total_xp, 10)

    def test_no_hints_bonus(self):
        """Correct answer with no hints should get +5 bonus."""
        from apps.tutoring.personalization import SkillAssessmentService

        session = self._create_session()
        svc = SkillAssessmentService(self.student_user, session)

        profile = self._make_profile(xp=0)

        svc._award_practice_xp(self.course, was_correct=True, hints_used=0)

        profile.refresh_from_db()
        # Correct = 10, no hints bonus = +5, no streak = 15
        self.assertEqual(profile.total_xp, 15)

    def test_streak_bonus(self):
        """Streak should add bonus XP (capped at 10)."""
        from apps.tutoring.personalization import SkillAssessmentService

        session = self._create_session()
        svc = SkillAssessmentService(self.student_user, session)

        profile = self._make_profile(xp=0, streak=5)

        svc._award_practice_xp(self.course, was_correct=True, hints_used=0)

        profile.refresh_from_db()
        # Correct = 10, no hints = +5, streak 5 = +5, total = 20
        self.assertEqual(profile.total_xp, 20)

    def test_streak_bonus_capped(self):
        """Streak bonus should be capped at 10."""
        from apps.tutoring.personalization import SkillAssessmentService

        session = self._create_session()
        svc = SkillAssessmentService(self.student_user, session)

        profile = self._make_profile(xp=0, streak=25)

        svc._award_practice_xp(self.course, was_correct=True, hints_used=0)

        profile.refresh_from_db()
        # Correct = 10, no hints = +5, streak capped at 10 = +10, total = 25
        self.assertEqual(profile.total_xp, 25)

    def test_incorrect_answer_effort_reward(self):
        """Incorrect answer should award 2 XP effort reward."""
        from apps.tutoring.personalization import SkillAssessmentService

        session = self._create_session()
        svc = SkillAssessmentService(self.student_user, session)

        profile = self._make_profile(xp=0)

        svc._award_practice_xp(self.course, was_correct=False, hints_used=0)

        profile.refresh_from_db()
        self.assertEqual(profile.total_xp, 2)

    def test_xp_creates_profile_if_missing(self):
        """Should create profile if it doesn't exist."""
        from apps.tutoring.personalization import SkillAssessmentService

        session = self._create_session()
        svc = SkillAssessmentService(self.student_user, session)

        self.assertFalse(StudentKnowledgeProfile.objects.filter(
            student=self.student_user, course=self.course
        ).exists())

        svc._award_practice_xp(self.course, was_correct=True, hints_used=0)

        profile = StudentKnowledgeProfile.objects.get(
            student=self.student_user, course=self.course
        )
        self.assertEqual(profile.total_xp, 15)

    def test_xp_award_graceful_on_error(self):
        """Should not raise if XP awarding fails."""
        from apps.tutoring.personalization import SkillAssessmentService

        session = self._create_session()
        svc = SkillAssessmentService(self.student_user, session)

        with patch.object(StudentKnowledgeProfile.objects, 'get_or_create', side_effect=Exception("DB error")):
            # Should not raise
            svc._award_practice_xp(self.course, was_correct=True, hints_used=0)
