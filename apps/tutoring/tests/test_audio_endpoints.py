"""Tests for audio endpoints (transcribe_audio, speak_text) and concise prompt updates."""

import json
from io import BytesIO
from unittest.mock import patch, MagicMock

from django.test import RequestFactory
from django.core.files.uploadedfile import SimpleUploadedFile
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestTranscribeAudioEndpoint(BaseTutoringTestCase):
    """Tests for POST /tutor/api/chat/<session_id>/transcribe/."""

    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, session_id, files=None, user=None):
        from apps.tutoring.views import transcribe_audio
        request = self.factory.post(
            f"/tutor/api/chat/{session_id}/transcribe/",
            data=files or {},
        )
        request.user = user or self.student_user
        return transcribe_audio(request, session_id)

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    @patch("apps.tutoring.audio_service.transcribe", return_value="Hello teacher")
    def test_transcribe_success(self, mock_transcribe, mock_rate, mock_record):
        session = self._create_session()
        audio = SimpleUploadedFile("recording.webm", b"fake audio", content_type="audio/webm")
        request = self.factory.post(
            f"/tutor/api/chat/{session.id}/transcribe/",
            {"audio": audio},
        )
        request.user = self.student_user

        from apps.tutoring.views import transcribe_audio
        response = transcribe_audio(request, session.id)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["text"], "Hello teacher")

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    def test_transcribe_no_audio_file(self, mock_rate, mock_record):
        session = self._create_session()
        response = self._post(session.id)
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("No audio", data["error"])

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    @patch("apps.tutoring.audio_service.transcribe", return_value=None)
    def test_transcribe_fails_returns_422(self, mock_t, mock_rate, mock_record):
        session = self._create_session()
        audio = SimpleUploadedFile("recording.webm", b"bad audio", content_type="audio/webm")
        request = self.factory.post(
            f"/tutor/api/chat/{session.id}/transcribe/",
            {"audio": audio},
        )
        request.user = self.student_user

        from apps.tutoring.views import transcribe_audio
        response = transcribe_audio(request, session.id)
        self.assertEqual(response.status_code, 422)

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(False, "Too many requests"))
    def test_transcribe_rate_limited(self, mock_rate, mock_record):
        session = self._create_session()
        audio = SimpleUploadedFile("recording.webm", b"audio", content_type="audio/webm")
        request = self.factory.post(
            f"/tutor/api/chat/{session.id}/transcribe/",
            {"audio": audio},
        )
        request.user = self.student_user

        from apps.tutoring.views import transcribe_audio
        response = transcribe_audio(request, session.id)
        self.assertEqual(response.status_code, 429)

    def test_transcribe_wrong_user_404(self):
        """Session owned by student_user should 404 for staff_user."""
        session = self._create_session()
        audio = SimpleUploadedFile("recording.webm", b"audio", content_type="audio/webm")
        request = self.factory.post(
            f"/tutor/api/chat/{session.id}/transcribe/",
            {"audio": audio},
        )
        request.user = self.staff_user

        from apps.tutoring.views import transcribe_audio
        with self.assertRaises(Exception):
            # get_object_or_404 raises Http404
            transcribe_audio(request, session.id)

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    def test_transcribe_file_too_large(self, mock_rate, mock_record):
        session = self._create_session()
        # 11 MB file
        big_audio = SimpleUploadedFile("big.webm", b"x" * (11 * 1024 * 1024), content_type="audio/webm")
        request = self.factory.post(
            f"/tutor/api/chat/{session.id}/transcribe/",
            {"audio": big_audio},
        )
        request.user = self.student_user

        from apps.tutoring.views import transcribe_audio
        response = transcribe_audio(request, session.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("too large", json.loads(response.content)["error"])


class TestSpeakTextEndpoint(BaseTutoringTestCase):
    """Tests for POST /tutor/api/speak/."""

    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, body, user=None):
        from apps.tutoring.views import speak_text
        request = self.factory.post(
            "/tutor/api/speak/",
            data=json.dumps(body),
            content_type="application/json",
        )
        request.user = user or self.student_user
        return speak_text(request)

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    @patch("apps.tutoring.audio_service.synthesize_with_timestamps", return_value=None)
    @patch("apps.tutoring.audio_service.synthesize")
    def test_speak_success(self, mock_synth, mock_ts, mock_rate, mock_record):
        # Fake WAV bytes — synthesize returns (bytes, content_type)
        mock_synth.return_value = (b"RIFF" + b"\x00" * 100, "audio/wav")
        response = self._post({"text": "Hello student"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/wav")
        self.assertTrue(response.content.startswith(b"RIFF"))

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    def test_speak_empty_text(self, mock_rate, mock_record):
        response = self._post({"text": ""})
        self.assertEqual(response.status_code, 400)

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    def test_speak_text_too_long(self, mock_rate, mock_record):
        response = self._post({"text": "a" * 2001})
        self.assertEqual(response.status_code, 400)
        self.assertIn("too long", json.loads(response.content)["error"])

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    @patch("apps.tutoring.audio_service.synthesize_with_timestamps", return_value=None)
    @patch("apps.tutoring.audio_service.synthesize", return_value=(None, "audio/wav"))
    def test_speak_tts_unavailable(self, mock_synth, mock_ts, mock_rate, mock_record):
        response = self._post({"text": "Hello"})
        self.assertEqual(response.status_code, 503)

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(False, "Rate limited"))
    def test_speak_rate_limited(self, mock_rate, mock_record):
        response = self._post({"text": "Hello"})
        self.assertEqual(response.status_code, 429)

    @patch("apps.safety.RateLimiter.record_message")
    @patch("apps.safety.RateLimiter.check_rate_limit", return_value=(True, None))
    def test_speak_invalid_json(self, mock_rate, mock_record):
        from apps.tutoring.views import speak_text
        request = self.factory.post(
            "/tutor/api/speak/",
            data="not json",
            content_type="application/json",
        )
        request.user = self.student_user
        response = speak_text(request)
        self.assertEqual(response.status_code, 400)


class TestAudioURLPatterns(BaseTutoringTestCase):
    """Verify URL patterns resolve correctly."""

    def test_transcribe_url_resolves(self):
        from django.urls import reverse
        url = reverse("tutoring:transcribe_audio", args=[123])
        self.assertEqual(url, "/tutor/api/chat/123/transcribe/")

    def test_speak_url_resolves(self):
        from django.urls import reverse
        url = reverse("tutoring:speak_text")
        self.assertEqual(url, "/tutor/api/speak/")


class TestConcisePrompts(BaseTutoringTestCase):
    """Verify tutor prompts were tightened to shorter sentence limits."""

    def test_system_prompt_has_concise_format_rules(self):
        """The format_rules should specify 1-2 sentences + ~60 words max."""
        session = self._create_session()
        from apps.tutoring.conversational_tutor import ConversationalTutor
        tutor = ConversationalTutor(session)
        prompt = tutor._build_system_prompt()
        self.assertIn("1-2 sentences", prompt)
        self.assertIn("60 words", prompt)

    def test_active_learning_principle_tightened(self):
        session = self._create_session()
        from apps.tutoring.conversational_tutor import ConversationalTutor
        tutor = ConversationalTutor(session)
        prompt = tutor._build_system_prompt()
        # Should NOT contain old "3 sentences" in active_learning principle
        self.assertNotIn("more than 3 sentences", prompt)
        self.assertIn("more than 1-2 sentences", prompt)

    def test_cognitive_load_principle_tightened(self):
        session = self._create_session()
        from apps.tutoring.conversational_tutor import ConversationalTutor
        tutor = ConversationalTutor(session)
        prompt = tutor._build_system_prompt()
        self.assertNotIn("2-3 sentences max", prompt)
        self.assertIn("One to two sentences maximum per idea", prompt)
