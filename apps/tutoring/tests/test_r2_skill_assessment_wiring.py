"""Tests for R2: SkillAssessmentService wired into ConversationalTutor."""

from unittest.mock import MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.tutoring.skills_models import Skill


class TestR2SkillAssessmentWiring(BaseTutoringTestCase):
    """Test that skill assessment recording is wired into ConversationalTutor."""

    # =========================================================================
    # ConversationalTutor — property existence and loading
    # =========================================================================

    def test_conversational_tutor_has_skill_properties(self):
        """ConversationalTutor should have lesson_skills and skill_assessment_service."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        # These should be lazy-loaded properties
        self.assertTrue(hasattr(tutor, 'lesson_skills'))
        self.assertTrue(hasattr(tutor, 'skill_assessment_service'))

    def test_conversational_tutor_lesson_skills_loads(self):
        """lesson_skills property should return skills for the lesson."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        skills = tutor.lesson_skills
        self.assertIn(self.skill1, skills)
        self.assertIn(self.skill2, skills)

    def test_conversational_tutor_lesson_skills_empty_when_none(self):
        """lesson_skills should return empty list when no skills exist for lesson."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        # Remove skill-lesson associations
        self.skill1.lessons.remove(self.lesson)
        self.skill2.lessons.remove(self.lesson)

        try:
            session = self._create_session()
            tutor = ConversationalTutor(session)

            skills = tutor.lesson_skills
            self.assertEqual(skills, [])
        finally:
            # Restore
            self.skill1.lessons.add(self.lesson)
            self.skill2.lessons.add(self.lesson)

    # =========================================================================
    # ConversationalTutor — _analyze_student_response records skills
    # =========================================================================

    def test_analyze_student_response_records_skill_on_success(self):
        """_analyze_student_response should call skill_assessment_service.record_practice."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        session = self._create_session(engine_state={
            'session_state': 'tutoring',
            'current_topic_index': 1,
        })
        tutor = ConversationalTutor(session)
        tutor.session_state = SessionState.TUTORING

        mock_svc = MagicMock()
        tutor._skill_assessment_service = mock_svc
        tutor._lesson_skills = [self.skill1]

        # Simulate a successful student response (tutor praises in response)
        tutor._analyze_student_response(
            "divergent boundary",
            "Excellent! That's exactly right."
        )

        # skill_assessment_service.record_practice should have been called
        mock_svc.record_practice.assert_called()
        call_kwargs = mock_svc.record_practice.call_args[1]
        self.assertTrue(call_kwargs.get('was_correct'))

    def test_analyze_student_response_records_incorrect(self):
        """_analyze_student_response should record was_correct=False on incorrect answers."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        session = self._create_session(engine_state={
            'session_state': 'tutoring',
            'current_topic_index': 1,
        })
        tutor = ConversationalTutor(session)
        tutor.session_state = SessionState.TUTORING

        mock_svc = MagicMock()
        tutor._skill_assessment_service = mock_svc
        tutor._lesson_skills = [self.skill1]

        # Simulate an incorrect student response (no praise keywords)
        tutor._analyze_student_response(
            "I think it is a volcano",
            "Not quite. Let's think about this differently."
        )

        mock_svc.record_practice.assert_called()
        call_kwargs = mock_svc.record_practice.call_args[1]
        self.assertFalse(call_kwargs.get('was_correct'))

    def test_conversational_tutor_graceful_without_skills(self):
        """ConversationalTutor should work even when no skills exist for lesson."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        # Remove skill-lesson associations
        self.skill1.lessons.remove(self.lesson)
        self.skill2.lessons.remove(self.lesson)

        try:
            session = self._create_session(engine_state={
                'session_state': 'tutoring',
                'current_topic_index': 1,
            })
            tutor = ConversationalTutor(session)

            # Should not raise
            tutor._analyze_student_response("some answer", "Great job!")
        finally:
            # Restore
            self.skill1.lessons.add(self.lesson)
            self.skill2.lessons.add(self.lesson)
