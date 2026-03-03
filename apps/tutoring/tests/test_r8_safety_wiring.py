"""Tests for R8: Safety (rate limiting + content filtering) wired into chat endpoints."""

import json
from unittest.mock import patch, MagicMock
from django.test import RequestFactory
from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.safety import SafetyCheckResult, ContentFlag


class TestR8SafetyWiring(BaseTutoringTestCase):
    """Test that rate limiting and content safety are wired into chat endpoints."""

    def setUp(self):
        self.factory = RequestFactory()

    def _make_request(self, user, body=None):
        """Create a POST request with JSON body."""
        request = self.factory.post(
            '/api/chat/respond/',
            data=json.dumps(body or {}),
            content_type='application/json',
        )
        request.user = user
        return request

    @patch('apps.safety.RateLimiter.record_message')
    @patch('apps.safety.RateLimiter.check_rate_limit')
    def test_chat_start_session_rate_limited(self, mock_check, mock_record):
        """chat_start_session should return 429 when rate limited."""
        from apps.tutoring.views import chat_start_session

        mock_check.return_value = (False, "Too many requests")

        # Mark prereq as mastered so we don't get blocked by that
        self._create_progress(mastery_level='mastered')

        request = self.factory.post(f'/api/chat/start/{self.lesson.id}/')
        request.user = self.student_user

        response = chat_start_session(request, self.lesson.id)
        self.assertEqual(response.status_code, 429)
        data = json.loads(response.content)
        self.assertTrue(data.get('rate_limited'))

    @patch('apps.safety.RateLimiter.record_message')
    @patch('apps.safety.RateLimiter.check_rate_limit')
    def test_chat_respond_rate_limited(self, mock_check, mock_record):
        """chat_respond should return 429 when rate limited."""
        from apps.tutoring.views import chat_respond

        mock_check.return_value = (False, "Too many requests")

        session = self._create_session()
        request = self._make_request(self.student_user, {'message': 'hello'})

        response = chat_respond(request, session.id)
        self.assertEqual(response.status_code, 429)

    @patch('apps.safety.RateLimiter.record_message')
    @patch('apps.safety.RateLimiter.check_rate_limit')
    @patch('apps.safety.ContentSafetyFilter.check_content')
    @patch('apps.safety.ContentSafetyFilter.get_safe_response')
    def test_chat_respond_blocks_harmful_content(self, mock_safe_resp, mock_check_content, mock_rate_check, mock_record):
        """chat_respond should block harmful content."""
        from apps.tutoring.views import chat_respond

        mock_rate_check.return_value = (True, None)
        mock_check_content.return_value = SafetyCheckResult(
            is_safe=False,
            flags=[ContentFlag.HARMFUL],
            filtered_content='',
            warnings=['Harmful content detected'],
            blocked=True,
            block_reason='harmful_content',
        )
        mock_safe_resp.return_value = "I can't help with that."

        session = self._create_session()
        request = self._make_request(self.student_user, {'message': 'harmful text'})

        response = chat_respond(request, session.id)
        data = json.loads(response.content)
        self.assertEqual(data['phase'], 'safety')

    @patch('apps.safety.RateLimiter.record_message')
    @patch('apps.safety.RateLimiter.check_rate_limit')
    @patch('apps.safety.ContentSafetyFilter.check_content')
    @patch('apps.tutoring.conversational_tutor.ConversationalTutor.respond')
    def test_chat_respond_uses_filtered_content(self, mock_respond, mock_check_content, mock_rate_check, mock_record):
        """chat_respond should pass filtered (PII-scrubbed) content to tutor (JsonResponse)."""
        from apps.tutoring.views import chat_respond

        mock_rate_check.return_value = (True, None)
        mock_check_content.return_value = SafetyCheckResult(
            is_safe=True,
            flags=[ContentFlag.PERSONAL_INFO],
            filtered_content='my email is [REDACTED]',
            warnings=['PII detected'],
            blocked=False,
        )
        # respond() returns a result object; mock it with the expected attributes
        mock_result = MagicMock()
        mock_result.content = "Great question!"
        mock_result.phase = "instruction"
        mock_result.media = []
        mock_result.show_exit_ticket = False
        mock_result.exit_ticket_data = None
        mock_result.is_complete = False
        mock_respond.return_value = mock_result

        session = self._create_session()
        request = self._make_request(self.student_user, {'message': 'my email is test@example.com'})

        response = chat_respond(request, session.id)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['message'], "Great question!")

        # Tutor should receive filtered content, not original
        mock_respond.assert_called_once()
        call_args = mock_respond.call_args
        self.assertIn('[REDACTED]', call_args[0][0])
