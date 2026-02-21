"""Tests for R3+R4: Session personalization and retrieval warmup."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR3R4SessionPersonalization(BaseTutoringTestCase):
    """Test session personalization loads at start and retrieval block is formatted."""

    # =========================================================================
    # Method existence
    # =========================================================================

    def test_tutor_has_personalization_methods(self):
        """ConversationalTutor should have _load_personalization and _build_retrieval_block."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        self.assertTrue(hasattr(tutor, '_load_personalization'))
        self.assertTrue(hasattr(tutor, '_build_retrieval_block'))
        self.assertTrue(callable(tutor._load_personalization))
        self.assertTrue(callable(tutor._build_retrieval_block))

    # =========================================================================
    # _load_personalization
    # =========================================================================

    @patch('apps.tutoring.personalization.SessionPersonalizationService')
    def test_load_personalization_calls_service(self, mock_svc_cls):
        """_load_personalization should call SessionPersonalizationService."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        from apps.tutoring.personalization import SessionPersonalization

        mock_svc = MagicMock()
        mock_svc.get_session_personalization.return_value = SessionPersonalization(
            retrieval_questions=[],
            recommended_pace='normal',
            weak_skills=[],
            strong_skills=[],
        )
        mock_svc_cls.return_value = mock_svc

        session = self._create_session()
        tutor = ConversationalTutor(session)
        tutor._load_personalization()

        mock_svc_cls.assert_called_once_with(self.student_user, self.lesson)
        mock_svc.get_session_personalization.assert_called_once()

    @patch('apps.tutoring.personalization.SessionPersonalizationService')
    def test_load_personalization_stores_result(self, mock_svc_cls):
        """_load_personalization should set self._personalization."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        from apps.tutoring.personalization import SessionPersonalization

        personalization_data = SessionPersonalization(
            retrieval_questions=[],
            recommended_pace='slow',
            weak_skills=[self.prereq_skill],
            strong_skills=[self.skill1],
        )
        mock_svc = MagicMock()
        mock_svc.get_session_personalization.return_value = personalization_data
        mock_svc_cls.return_value = mock_svc

        session = self._create_session()
        tutor = ConversationalTutor(session)
        tutor._load_personalization()

        self.assertIs(tutor._personalization, personalization_data)

    @patch('apps.tutoring.personalization.SessionPersonalizationService')
    def test_load_personalization_graceful_on_error(self, mock_svc_cls):
        """_load_personalization should set _personalization to None on error."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        mock_svc_cls.side_effect = Exception("Service unavailable")

        session = self._create_session()
        tutor = ConversationalTutor(session)
        # Should not raise
        tutor._load_personalization()

        self.assertIsNone(tutor._personalization)

    # =========================================================================
    # _build_retrieval_block
    # =========================================================================

    def test_build_retrieval_block_empty_without_personalization(self):
        """_build_retrieval_block should return empty string without personalization data."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        # _personalization is None by default (not loaded)
        block = tutor._build_retrieval_block()
        self.assertEqual(block, "")

    def test_build_retrieval_block_empty_without_questions(self):
        """_build_retrieval_block should return empty string when no retrieval questions."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        from apps.tutoring.personalization import SessionPersonalization

        session = self._create_session()
        tutor = ConversationalTutor(session)
        tutor._personalization = SessionPersonalization(
            retrieval_questions=[],
            recommended_pace='normal',
        )

        block = tutor._build_retrieval_block()
        self.assertEqual(block, "")

    def test_build_retrieval_block_formats_questions(self):
        """_build_retrieval_block should format retrieval questions when available."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        from apps.tutoring.personalization import SessionPersonalization

        # Build a mock RetrievalQuestion matching the actual dataclass
        mock_rq = MagicMock()
        mock_rq.skill = self.prereq_skill
        mock_rq.question_text = "What are the layers of the Earth?"
        mock_rq.expected_answer = "crust, mantle, outer core, inner core"
        mock_rq.mastery_record = None  # no mastery record

        session = self._create_session()
        tutor = ConversationalTutor(session)
        tutor._personalization = SessionPersonalization(
            retrieval_questions=[mock_rq],
            recommended_pace='normal',
        )

        block = tutor._build_retrieval_block()
        self.assertIn("[WARMUP RETRIEVAL]", block)
        self.assertIn("What are the layers of the Earth?", block)
        self.assertIn("Identify Earth Layers", block)  # skill name
        self.assertIn("[/WARMUP RETRIEVAL]", block)

    def test_build_retrieval_block_includes_last_practiced(self):
        """_build_retrieval_block should show days since last practice when available."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        from apps.tutoring.personalization import SessionPersonalization
        from django.utils import timezone
        from datetime import timedelta

        mock_mastery = MagicMock()
        mock_mastery.last_practiced = timezone.now() - timedelta(days=5)

        mock_rq = MagicMock()
        mock_rq.skill = self.prereq_skill
        mock_rq.question_text = "Name the layers of the Earth."
        mock_rq.expected_answer = "crust, mantle, core"
        mock_rq.mastery_record = mock_mastery

        session = self._create_session()
        tutor = ConversationalTutor(session)
        tutor._personalization = SessionPersonalization(
            retrieval_questions=[mock_rq],
            recommended_pace='normal',
        )

        block = tutor._build_retrieval_block()
        self.assertIn("last reviewed:", block)
        self.assertIn("5 days ago", block)

    def test_build_retrieval_block_limits_to_two_questions(self):
        """_build_retrieval_block should include at most 2 retrieval questions."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        from apps.tutoring.personalization import SessionPersonalization

        questions = []
        for i in range(5):
            rq = MagicMock()
            rq.skill = self.prereq_skill
            rq.question_text = f"Question {i+1}?"
            rq.expected_answer = f"Answer {i+1}"
            rq.mastery_record = None
            questions.append(rq)

        session = self._create_session()
        tutor = ConversationalTutor(session)
        tutor._personalization = SessionPersonalization(
            retrieval_questions=questions,
            recommended_pace='normal',
        )

        block = tutor._build_retrieval_block()
        # Should only include Q1 and Q2
        self.assertIn("Q1:", block)
        self.assertIn("Q2:", block)
        self.assertNotIn("Q3:", block)

    # =========================================================================
    # start() integration
    # =========================================================================

    @patch('apps.tutoring.conversational_tutor.ConversationalTutor._generate_response')
    @patch('apps.tutoring.conversational_tutor.ConversationalTutor._load_personalization')
    def test_start_calls_load_personalization(self, mock_load, mock_gen):
        """start() should call _load_personalization before generating opening."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        mock_gen.return_value = "Hello! Welcome to today's lesson."

        session = self._create_session()
        tutor = ConversationalTutor(session)
        tutor.start()

        mock_load.assert_called_once()

    @patch('apps.tutoring.conversational_tutor.ConversationalTutor._generate_response')
    @patch('apps.tutoring.personalization.SessionPersonalizationService')
    def test_start_uses_retrieval_in_opening_prompt(self, mock_svc_cls, mock_gen):
        """start() should include retrieval block in the opening prompt."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        from apps.tutoring.personalization import SessionPersonalization

        mock_rq = MagicMock()
        mock_rq.skill = self.prereq_skill
        mock_rq.question_text = "What are tectonic plates?"
        mock_rq.expected_answer = "Large pieces of Earth's crust"
        mock_rq.mastery_record = None

        mock_svc = MagicMock()
        mock_svc.get_session_personalization.return_value = SessionPersonalization(
            retrieval_questions=[mock_rq],
            recommended_pace='normal',
        )
        mock_svc_cls.return_value = mock_svc

        mock_gen.return_value = "Hello! Let's start with a warmup question."

        session = self._create_session()
        tutor = ConversationalTutor(session)
        tutor.start()

        # Verify _generate_response was called with a prompt containing retrieval
        mock_gen.assert_called_once()
        prompt_arg = mock_gen.call_args[0][0]
        self.assertIn("WARMUP RETRIEVAL", prompt_arg)
        self.assertIn("What are tectonic plates?", prompt_arg)
