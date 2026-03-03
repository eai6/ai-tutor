"""Tests for R6: InterleavedPracticeService wired into practice/quiz steps."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR6InterleavedPractice(BaseTutoringTestCase):
    """Test that interleaved review questions are injected during practice/quiz steps."""

    def test_tutor_has_interleaved_practice_method(self):
        """ConversationalTutor should have _build_interleaved_practice_block."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)
        self.assertTrue(hasattr(tutor, '_build_interleaved_practice_block'))

    def test_interleaved_block_empty_outside_practice(self):
        """Block should be empty when not on a practice/quiz step."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        session = self._create_session(engine_state={'session_state': 'tutoring'})
        tutor = ConversationalTutor(session)
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

        # With current_topic_index pointing at a teach step, block should be empty
        block = tutor._build_interleaved_practice_block()
        self.assertEqual(block, "")

    @patch('apps.tutoring.personalization.InterleavedPracticeService')
    def test_interleaved_block_in_practice_step(self, mock_svc_cls):
        """Block should contain review questions during a practice step."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        # Build mock return data matching actual structure: item['type'] == 'review', item['step']
        mock_step = MagicMock()
        mock_step.question = 'What are the layers of the Earth?'
        mock_step.expected_answer = 'Crust, mantle, core'

        mock_skill = MagicMock()
        mock_skill.name = 'Earth Layers'

        mock_svc = MagicMock()
        mock_svc.get_interleaved_questions.return_value = [
            {
                'type': 'review',
                'step': mock_step,
                'skill': mock_skill,
            },
        ]
        mock_svc_cls.return_value = mock_svc

        # Point current_topic_index at a practice step (step2 is index 1)
        session = self._create_session(engine_state={
            'session_state': 'tutoring',
            'current_topic_index': 1,
        })
        tutor = ConversationalTutor(session)

        block = tutor._build_interleaved_practice_block()
        self.assertIn('INTERLEAVED PRACTICE', block)
        self.assertIn('layers of the Earth', block)

    @patch('apps.tutoring.personalization.InterleavedPracticeService')
    def test_interleaved_block_cached(self, mock_svc_cls):
        """Block should be cached after first call to avoid re-fetching."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        mock_step = MagicMock()
        mock_step.question = 'Q1'
        mock_step.expected_answer = 'A1'

        mock_svc = MagicMock()
        mock_svc.get_interleaved_questions.return_value = [
            {'type': 'review', 'step': mock_step, 'skill': None},
        ]
        mock_svc_cls.return_value = mock_svc

        # Point current_topic_index at a practice step (step2 is index 1)
        session = self._create_session(engine_state={
            'session_state': 'tutoring',
            'current_topic_index': 1,
        })
        tutor = ConversationalTutor(session)

        block1 = tutor._build_interleaved_practice_block()
        self.assertIn('INTERLEAVED PRACTICE', block1)

        block2 = tutor._build_interleaved_practice_block()

        # Should return same result both times
        self.assertEqual(block1, block2)
        # Service should only be called once (result cached)
        self.assertEqual(mock_svc.get_interleaved_questions.call_count, 1)

    def test_interleaved_block_graceful_on_error(self):
        """Block should return empty string if service fails."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        # Point current_topic_index at a practice step (step2 is index 1)
        session = self._create_session(engine_state={
            'session_state': 'tutoring',
            'current_topic_index': 1,
        })
        tutor = ConversationalTutor(session)

        with patch('apps.tutoring.personalization.InterleavedPracticeService') as mock_cls:
            mock_cls.side_effect = Exception("Service error")

            block = tutor._build_interleaved_practice_block()
            self.assertEqual(block, "")
