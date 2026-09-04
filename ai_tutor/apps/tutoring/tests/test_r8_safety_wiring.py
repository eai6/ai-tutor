"""Tests for R8: Safety (rate limiting + content filtering) wired into chat endpoints."""

import json
from unittest.mock import patch, MagicMock
from django.test import RequestFactory
from ai_tutor.apps.tutoring.tests.fixtures import BaseTutoringTestCase
from ai_tutor.apps.safety import SafetyCheckResult, ContentFlag


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

    @patch('ai_tutor.apps.safety.RateLimiter.record_message')
    @patch('ai_tutor.apps.safety.RateLimiter.check_rate_limit')
    def test_chat_start_session_rate_limited(self, mock_check, mock_record):
        """chat_start_session should return 429 when rate limited."""
        from ai_tutor.apps.tutoring.views import chat_start_session

        mock_check.return_value = (False, "Too many requests")

        # Mark prereq as mastered so we don't get blocked by that
        self._create_progress(mastery_level='mastered')

        request = self.factory.post(f'/api/chat/start/{self.lesson.id}/')
        request.user = self.student_user

        response = chat_start_session(request, self.lesson.id)
        self.assertEqual(response.status_code, 429)
        data = json.loads(response.content)
        self.assertTrue(data.get('rate_limited'))

    @patch('ai_tutor.apps.safety.RateLimiter.record_message')
    @patch('ai_tutor.apps.safety.RateLimiter.check_rate_limit')
    def test_chat_respond_rate_limited(self, mock_check, mock_record):
        """chat_respond should return 429 when rate limited."""
        from ai_tutor.apps.tutoring.views import chat_respond

        mock_check.return_value = (False, "Too many requests")

        session = self._create_session()
        request = self._make_request(self.student_user, {'message': 'hello'})

        response = chat_respond(request, session.id)
        self.assertEqual(response.status_code, 429)

    @patch('ai_tutor.apps.safety.RateLimiter.record_message')
    @patch('ai_tutor.apps.safety.RateLimiter.check_rate_limit')
    @patch('ai_tutor.apps.tutoring.judges.safety.run_safety_judge')
    @patch('ai_tutor.apps.safety.ContentSafetyFilter.get_safe_response')
    def test_chat_respond_blocks_harmful_content(self, mock_safe_resp, mock_safety_judge, mock_rate_check, mock_record):
        """chat_respond should block harmful content (LLM safety judge path)."""
        from ai_tutor.apps.tutoring.views import chat_respond
        from ai_tutor.apps.tutoring.judges.safety import SafetyResult

        mock_rate_check.return_value = (True, None)
        # Simulate the LLM safety judge flagging harmful content.
        mock_safety_judge.return_value = SafetyResult(
            severity="critical",
            categories=["harmful"],
            reasoning="explicit harmful content",
        )
        mock_safe_resp.return_value = "I can't help with that."

        session = self._create_session()
        request = self._make_request(self.student_user, {'message': 'harmful text'})

        response = chat_respond(request, session.id)
        data = json.loads(response.content)
        self.assertEqual(data['phase'], 'safety')

    @patch('ai_tutor.apps.safety.RateLimiter.record_message')
    @patch('ai_tutor.apps.safety.RateLimiter.check_rate_limit')
    @patch('ai_tutor.apps.safety.ContentSafetyFilter.check_content')
    # simple_tutor.engine.respond_for_view, not
    # ConversationalTutor.respond: the SIMPLE_TUTOR_ENGINE dispatch was
    # removed on 2026-06-01 and the view calls the engine directly. With
    # the old patch in place the real engine ran, failed for want of an
    # LLM, and the assertion compared the tutor's "I had trouble
    # responding" fallback against the mock's text — so this test proved
    # nothing about PII scrubbing, which is the thing it exists for.
    @patch('ai_tutor.apps.tutoring.simple_tutor.engine.respond_for_view')
    def test_chat_respond_uses_filtered_content(self, mock_respond, mock_check_content, mock_rate_check, mock_record):
        """chat_respond should pass filtered (PII-scrubbed) content to tutor (JsonResponse)."""
        from ai_tutor.apps.tutoring.views import chat_respond

        mock_rate_check.return_value = (True, None)
        mock_check_content.return_value = SafetyCheckResult(
            is_safe=True,
            flags=[ContentFlag.PERSONAL_INFO],
            filtered_content='my email is [REDACTED]',
            warnings=['PII detected'],
            blocked=False,
        )
        # respond_for_view returns the view payload directly — a plain
        # dict that JsonResponse serialises, so there is nothing to pin
        # field by field the way the old result object needed.
        mock_respond.return_value = {
            'message': "Great question!",
            'phase': "instruction",
            'media': [],
            'is_complete': False,
            'step_number': 1,
            'total_steps': 5,
        }

        session = self._create_session()
        request = self._make_request(self.student_user, {'message': 'my email is test@example.com'})

        response = chat_respond(request, session.id)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['message'], "Great question!")

        # Tutor should receive filtered content, not original. The
        # engine takes (session, message), so the scrubbed text is the
        # second positional argument.
        mock_respond.assert_called_once()
        self.assertIn('[REDACTED]', mock_respond.call_args[0][1])
