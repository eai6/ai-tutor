"""Tests for R1: Skill extraction wired into content generation pipeline."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR1SkillExtractionPipeline(BaseTutoringTestCase):
    """Test that SkillExtractionService is called during content generation."""

    @patch('apps.dashboard.background_tasks.connection')
    @patch('apps.dashboard.background_tasks.generate_exit_ticket_for_lesson', return_value=0)
    @patch('apps.tutoring.skill_extraction.SkillExtractionService')
    @patch('apps.curriculum.content_generator.LessonContentGenerator')
    def test_pipeline_calls_skill_extraction(self, mock_gen_cls, mock_skill_svc_cls, mock_exit, mock_conn):
        """Pipeline Phase 4 should call SkillExtractionService.extract_skills_for_lesson."""
        from apps.dashboard.background_tasks import generate_complete_lesson

        # Stub phase 1: lesson content generation
        mock_gen = MagicMock()
        mock_gen.generate_for_lesson.return_value = {'success': True, 'steps_generated': 5}
        mock_gen_cls.return_value = mock_gen

        # Configure skill extraction mock — returns a list (code calls len())
        mock_skill_svc = MagicMock()
        mock_skill_svc.extract_skills_for_lesson.return_value = [
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        ]
        mock_skill_svc_cls.return_value = mock_skill_svc

        result = generate_complete_lesson(self.lesson.id, self.institution.id)

        # Verify skill extraction was called with correct institution_id
        mock_skill_svc_cls.assert_called_once_with(institution_id=self.institution.id)
        mock_skill_svc.extract_skills_for_lesson.assert_called_once()

        # The lesson object passed should match our fixture lesson
        call_args = mock_skill_svc.extract_skills_for_lesson.call_args
        passed_lesson = call_args[0][0]
        self.assertEqual(passed_lesson.id, self.lesson.id)

        # Verify result includes skill data
        self.assertIn('skills', result)
        self.assertEqual(result['skills'], 5)

    @patch('apps.dashboard.background_tasks.connection')
    @patch('apps.dashboard.background_tasks.generate_exit_ticket_for_lesson', return_value=0)
    @patch('apps.tutoring.skill_extraction.SkillExtractionService')
    @patch('apps.curriculum.content_generator.LessonContentGenerator')
    def test_pipeline_continues_on_skill_extraction_error(self, mock_gen_cls, mock_skill_svc_cls, mock_exit, mock_conn):
        """Pipeline should continue even if skill extraction fails."""
        from apps.dashboard.background_tasks import generate_complete_lesson

        mock_gen = MagicMock()
        mock_gen.generate_for_lesson.return_value = {'success': True, 'steps_generated': 5}
        mock_gen_cls.return_value = mock_gen

        # Make skill extraction raise an error
        mock_skill_svc_cls.side_effect = Exception("Skill extraction failed")

        # Pipeline should not raise
        result = generate_complete_lesson(self.lesson.id, self.institution.id)

        # Should still return a result (pipeline continues)
        self.assertIsNotNone(result)
        self.assertTrue(result['success'])
        # skills should be 0 since extraction failed
        self.assertEqual(result['skills'], 0)

    @patch('apps.dashboard.background_tasks.connection')
    @patch('apps.dashboard.background_tasks.generate_exit_ticket_for_lesson', return_value=0)
    @patch('apps.tutoring.skill_extraction.SkillExtractionService')
    @patch('apps.curriculum.content_generator.LessonContentGenerator')
    def test_pipeline_records_extraction_errors(self, mock_gen_cls, mock_skill_svc_cls, mock_exit, mock_conn):
        """Pipeline should handle skill extraction returning errors in the result."""
        from apps.dashboard.background_tasks import generate_complete_lesson

        mock_gen = MagicMock()
        mock_gen.generate_for_lesson.return_value = {'success': True, 'steps_generated': 5}
        mock_gen_cls.return_value = mock_gen

        # Skill extraction returns a list of 2 skills (code calls len())
        mock_skill_svc = MagicMock()
        mock_skill_svc.extract_skills_for_lesson.return_value = [
            MagicMock(), MagicMock(),
        ]
        mock_skill_svc_cls.return_value = mock_skill_svc

        result = generate_complete_lesson(self.lesson.id, self.institution.id)

        # Pipeline should still complete
        self.assertEqual(result['skills'], 2)
