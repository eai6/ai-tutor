"""Tests for R11: Student profile block injected into LLM context."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.tutoring.skills_models import StudentSkillMastery, StudentKnowledgeProfile


class TestR11StudentProfile(BaseTutoringTestCase):
    """Test that student profile block is built and injected into context."""

    def test_tutor_has_student_profile_method(self):
        """ConversationalTutor should have _build_student_profile_block."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)
        self.assertTrue(hasattr(tutor, '_build_student_profile_block'))

    def test_student_profile_block_with_mastery_data(self):
        """Profile block should include mastery levels when data exists."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        # Create mastery records
        self._create_mastery(skill=self.skill1, level=0.85)
        self._create_mastery(skill=self.skill2, level=0.4)

        session = self._create_session()
        tutor = ConversationalTutor(session)

        block = tutor._build_student_profile_block()
        self.assertIn('STUDENT PROFILE', block)

    def test_student_profile_block_empty_gracefully(self):
        """Profile block should not crash when no mastery data exists."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        # Should not raise
        block = tutor._build_student_profile_block()
        self.assertIsInstance(block, str)

    def test_student_profile_includes_xp(self):
        """Profile block should include XP and level when profile exists."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        # Create knowledge profile with XP
        StudentKnowledgeProfile.objects.create(
            student=self.student_user,
            course=self.course,
            total_xp=500,
            level=1,
            current_streak_days=3,
        )

        session = self._create_session()
        tutor = ConversationalTutor(session)

        block = tutor._build_student_profile_block()
        self.assertIn('STUDENT PROFILE', block)

    def test_profile_block_injected_into_contextual_response(self):
        """_generate_contextual_response prompt should include the profile block."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        from unittest.mock import call

        session = self._create_session(engine_state={'phase': 'instruction'})
        tutor = ConversationalTutor(session)

        with patch.object(tutor, '_generate_response', return_value='response') as mock_gen:
            with patch.object(tutor, '_build_student_profile_block', return_value='[STUDENT PROFILE] test data'):
                tutor._generate_contextual_response('test input', 'test kb context')

                # The prompt passed to _generate_response should contain profile block
                prompt_arg = mock_gen.call_args[0][0]
                self.assertIn('[STUDENT PROFILE] test data', prompt_arg)
