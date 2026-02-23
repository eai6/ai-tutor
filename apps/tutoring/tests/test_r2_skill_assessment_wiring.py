"""Tests for R2: SkillAssessmentService wired into both tutoring engines."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.tutoring.skills_models import Skill


class TestR2SkillAssessmentWiring(BaseTutoringTestCase):
    """Test that skill assessment recording is wired into both engines."""

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
        from apps.tutoring.conversational_tutor import ConversationalTutor, ConversationPhase

        session = self._create_session(engine_state={'phase': 'practice'})
        tutor = ConversationalTutor(session)
        tutor.phase = ConversationPhase.PRACTICE

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
        from apps.tutoring.conversational_tutor import ConversationalTutor, ConversationPhase

        session = self._create_session(engine_state={'phase': 'practice'})
        tutor = ConversationalTutor(session)
        tutor.phase = ConversationPhase.PRACTICE

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
        from apps.tutoring.conversational_tutor import ConversationalTutor, ConversationPhase

        # Remove skill-lesson associations
        self.skill1.lessons.remove(self.lesson)
        self.skill2.lessons.remove(self.lesson)

        try:
            session = self._create_session(engine_state={'phase': 'practice'})
            tutor = ConversationalTutor(session)

            # Should not raise
            tutor._analyze_student_response("some answer", "Great job!")
        finally:
            # Restore
            self.skill1.lessons.add(self.lesson)
            self.skill2.lessons.add(self.lesson)

    # =========================================================================
    # TutorEngine — _record_skill_practice
    # =========================================================================

    def test_engine_has_record_skill_practice(self):
        """TutorEngine should have _record_skill_practice method."""
        from apps.tutoring.engine import TutorEngine

        session = self._create_session()
        engine = TutorEngine(session)

        self.assertTrue(hasattr(engine, '_record_skill_practice'))
        self.assertTrue(callable(engine._record_skill_practice))

    @patch('apps.tutoring.personalization.SkillAssessmentService')
    def test_engine_record_skill_practice_calls_service(self, mock_svc_cls):
        """TutorEngine._record_skill_practice should lazy-load service and call record_practice."""
        from apps.tutoring.engine import TutorEngine

        session = self._create_session()
        engine = TutorEngine(session)

        mock_svc = MagicMock()
        mock_svc_cls.return_value = mock_svc

        engine._record_skill_practice(
            step=self.step2,
            was_correct=True,
            hints_used=0,
        )

        # Should have called record_practice on the service
        mock_svc.record_practice.assert_called_once()
        call_kwargs = mock_svc.record_practice.call_args[1]
        self.assertTrue(call_kwargs['was_correct'])
        self.assertEqual(call_kwargs['lesson_step'], self.step2)

    def test_engine_record_skill_practice_no_crash_without_skills(self):
        """_record_skill_practice should not crash when no skills exist."""
        from apps.tutoring.engine import TutorEngine

        # Remove skill-lesson associations
        self.skill1.lessons.remove(self.lesson)
        self.skill2.lessons.remove(self.lesson)

        try:
            session = self._create_session()
            engine = TutorEngine(session)

            # Should not raise
            engine._record_skill_practice(
                step=self.step2,
                was_correct=True,
                hints_used=0,
            )
        finally:
            # Restore
            self.skill1.lessons.add(self.lesson)
            self.skill2.lessons.add(self.lesson)

    def test_engine_initializes_skill_state(self):
        """TutorEngine should initialize _skill_assessment_service and _lesson_skills."""
        from apps.tutoring.engine import TutorEngine

        session = self._create_session()
        engine = TutorEngine(session)

        self.assertIsNone(engine._skill_assessment_service)
        self.assertIsNone(engine._lesson_skills)
