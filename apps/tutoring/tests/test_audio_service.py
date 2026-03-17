"""Tests for audio_service.py — Piper TTS + faster-whisper STT."""

import os
from unittest.mock import patch, MagicMock
from django.test import TestCase


class TestTranscribe(TestCase):
    """Unit tests for audio_service.transcribe()."""

    @patch("apps.tutoring.audio_service.DISABLE_STT", True)
    def test_transcribe_disabled_returns_none(self):
        from apps.tutoring.audio_service import transcribe
        result = transcribe(b"fake audio", "audio/webm")
        self.assertIsNone(result)

    @patch("apps.tutoring.audio_service.DISABLE_STT", False)
    @patch("apps.tutoring.audio_service._get_whisper_model")
    def test_transcribe_returns_text(self, mock_get_model):
        """transcribe() joins segment texts and returns a string."""
        seg1 = MagicMock()
        seg1.text = " Hello "
        seg2 = MagicMock()
        seg2.text = " world "
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([seg1, seg2], MagicMock())
        mock_get_model.return_value = mock_model

        from apps.tutoring.audio_service import transcribe
        result = transcribe(b"fake audio bytes", "audio/webm")
        self.assertEqual(result, "Hello world")

    @patch("apps.tutoring.audio_service.DISABLE_STT", False)
    @patch("apps.tutoring.audio_service._get_whisper_model")
    def test_transcribe_empty_segments_returns_none(self, mock_get_model):
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([], MagicMock())
        mock_get_model.return_value = mock_model

        from apps.tutoring.audio_service import transcribe
        result = transcribe(b"silence", "audio/webm")
        self.assertIsNone(result)

    @patch("apps.tutoring.audio_service.DISABLE_STT", False)
    @patch("apps.tutoring.audio_service._get_whisper_model")
    def test_transcribe_exception_returns_none(self, mock_get_model):
        mock_get_model.side_effect = RuntimeError("model load failed")

        from apps.tutoring.audio_service import transcribe
        result = transcribe(b"audio", "audio/webm")
        self.assertIsNone(result)

    @patch("apps.tutoring.audio_service.DISABLE_STT", False)
    @patch("apps.tutoring.audio_service._get_whisper_model")
    def test_transcribe_wav_suffix(self, mock_get_model):
        """content_type=audio/wav should use .wav suffix (temp file)."""
        seg = MagicMock()
        seg.text = "test"
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([seg], MagicMock())
        mock_get_model.return_value = mock_model

        from apps.tutoring.audio_service import transcribe
        result = transcribe(b"wav bytes", "audio/wav")
        self.assertEqual(result, "test")
        # Verify temp file path used .wav suffix
        call_path = mock_model.transcribe.call_args[0][0]
        self.assertTrue(call_path.endswith(".wav"))


class TestSynthesize(TestCase):
    """Unit tests for audio_service.synthesize().

    synthesize() returns (bytes | None, content_type_str).
    """

    @patch("apps.tutoring.audio_service.DISABLE_TTS", True)
    def test_synthesize_disabled_returns_none(self):
        from apps.tutoring.audio_service import synthesize
        audio_bytes, content_type = synthesize("Hello world")
        self.assertIsNone(audio_bytes)

    @patch("apps.tutoring.audio_service.DISABLE_TTS", False)
    def test_synthesize_empty_text_returns_none(self):
        from apps.tutoring.audio_service import synthesize
        audio_bytes, _ = synthesize("")
        self.assertIsNone(audio_bytes)
        audio_bytes2, _ = synthesize("   ")
        self.assertIsNone(audio_bytes2)

    @patch("apps.tutoring.audio_service.DISABLE_TTS", False)
    @patch("apps.tutoring.audio_service._get_piper_voice")
    def test_synthesize_returns_wav_bytes(self, mock_get_voice):
        """synthesize() should return bytes starting with RIFF WAV header."""
        mock_voice = MagicMock()

        # synthesize_wav(text, wav_file) writes PCM frames into the wave.Wave_write
        def fake_synthesize_wav(text, wav_file):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x01" * 1000)

        mock_voice.synthesize_wav.side_effect = fake_synthesize_wav
        mock_get_voice.return_value = mock_voice

        from apps.tutoring.audio_service import synthesize
        audio_bytes, content_type = synthesize("Hello world")

        self.assertIsNotNone(audio_bytes)
        self.assertIsInstance(audio_bytes, bytes)
        # WAV files start with RIFF header
        self.assertTrue(audio_bytes[:4] == b"RIFF")
        self.assertEqual(content_type, "audio/wav")

    @patch("apps.tutoring.audio_service.DISABLE_TTS", False)
    @patch("apps.tutoring.audio_service._get_piper_voice")
    def test_synthesize_exception_returns_none(self, mock_get_voice):
        mock_get_voice.side_effect = RuntimeError("piper load failed")

        from apps.tutoring.audio_service import synthesize
        audio_bytes, _ = synthesize("Hello")
        self.assertIsNone(audio_bytes)
