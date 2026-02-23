"""Tests for R1: Skill extraction wired into content generation pipeline."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR1SkillExtractionPipeline(BaseTutoringTestCase):
    """Test that SkillExtractionService is called during content generation."""

    @patch('apps.dashboard.background_tasks.generate_exit_tickets_for_lessons')
    @patch('apps.dashboard.background_tasks.generate_media_for_lessons')
    @patch('apps.tutoring.skill_extraction.SkillExtractionService')
    @patch('apps.curriculum.content_generator.LessonContentGenerator')
    def test_pipeline_calls_skill_extraction(self, mock_gen_cls, mock_skill_svc_cls, mock_media, mock_exit):
        """Pipeline Phase 4 should call SkillExtractionService.extract_skills_for_course."""
        from apps.dashboard.background_tasks import generate_all_content_async

        # Stub phases 1, 2, and 3
        mock_gen = MagicMock()
        mock_gen.generate_for_lesson.return_value = {'success': True, 'steps_generated': 5}
        mock_gen_cls.return_value = mock_gen

        mock_media.return_value = {'generated': 0, 'failed': 0, 'skipped': 0}
        mock_exit.return_value = {'generated': 0, 'failed': 0, 'skipped': 0}

        # Configure skill extraction mock
        mock_skill_svc = MagicMock()
        mock_skill_svc.extract_skills_for_course.return_value = {
            'skills_created': 5,
            'prerequisites_created': 3,
            'errors': [],
        }
        mock_skill_svc_cls.return_value = mock_skill_svc

        result = generate_all_content_async(self.course.id)

        # Verify skill extraction was called with correct institution_id
        mock_skill_svc_cls.assert_called_once_with(institution_id=self.institution.id)
        mock_skill_svc.extract_skills_for_course.assert_called_once()

        # The course object passed should match our fixture course
        call_args = mock_skill_svc.extract_skills_for_course.call_args
        passed_course = call_args[0][0]
        self.assertEqual(passed_course.id, self.course.id)

        # Verify result includes skill data
        self.assertIn('skills_extracted', result)
        self.assertEqual(result['skills_extracted'], 5)
        self.assertIn('prerequisites_created', result)
        self.assertEqual(result['prerequisites_created'], 3)

    @patch('apps.dashboard.background_tasks.generate_exit_tickets_for_lessons')
    @patch('apps.dashboard.background_tasks.generate_media_for_lessons')
    @patch('apps.tutoring.skill_extraction.SkillExtractionService')
    @patch('apps.curriculum.content_generator.LessonContentGenerator')
    def test_pipeline_continues_on_skill_extraction_error(self, mock_gen_cls, mock_skill_svc_cls, mock_media, mock_exit):
        """Pipeline should continue even if skill extraction fails."""
        from apps.dashboard.background_tasks import generate_all_content_async

        mock_gen = MagicMock()
        mock_gen.generate_for_lesson.return_value = {'success': True, 'steps_generated': 5}
        mock_gen_cls.return_value = mock_gen

        mock_media.return_value = {'generated': 0, 'failed': 0, 'skipped': 0}
        mock_exit.return_value = {'generated': 0, 'failed': 0, 'skipped': 0}

        # Make skill extraction raise an error
        mock_skill_svc_cls.side_effect = Exception("Skill extraction failed")

        # Pipeline should not raise
        result = generate_all_content_async(self.course.id)

        # Should still return a result (pipeline continues)
        self.assertIsNotNone(result)
        # skills_extracted should be 0 since extraction failed
        self.assertEqual(result['skills_extracted'], 0)

    @patch('apps.dashboard.background_tasks.generate_exit_tickets_for_lessons')
    @patch('apps.dashboard.background_tasks.generate_media_for_lessons')
    @patch('apps.tutoring.skill_extraction.SkillExtractionService')
    @patch('apps.curriculum.content_generator.LessonContentGenerator')
    def test_pipeline_records_extraction_errors(self, mock_gen_cls, mock_skill_svc_cls, mock_media, mock_exit):
        """Pipeline should handle skill extraction returning errors in the result."""
        from apps.dashboard.background_tasks import generate_all_content_async

        mock_gen = MagicMock()
        mock_gen.generate_for_lesson.return_value = {'success': True, 'steps_generated': 5}
        mock_gen_cls.return_value = mock_gen

        mock_media.return_value = {'generated': 0, 'failed': 0, 'skipped': 0}
        mock_exit.return_value = {'generated': 0, 'failed': 0, 'skipped': 0}

        mock_skill_svc = MagicMock()
        mock_skill_svc.extract_skills_for_course.return_value = {
            'skills_created': 2,
            'prerequisites_created': 1,
            'errors': ['Failed for lesson X', 'Failed for lesson Y'],
        }
        mock_skill_svc_cls.return_value = mock_skill_svc

        result = generate_all_content_async(self.course.id)

        # Pipeline should still complete
        self.assertEqual(result['skills_extracted'], 2)
        self.assertEqual(result['prerequisites_created'], 1)
