"""Tests for R9: Science-of-learning system prompt replacement."""

from unittest.mock import patch, MagicMock, PropertyMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR9SystemPrompt(BaseTutoringTestCase):
    """Test that the system prompt template contains all science-of-learning principles."""

    def test_prompt_template_exists(self):
        """TUTOR_SYSTEM_PROMPT_TEMPLATE should exist."""
        from apps.tutoring.conversational_tutor import TUTOR_SYSTEM_PROMPT_TEMPLATE
        self.assertIsNotNone(TUTOR_SYSTEM_PROMPT_TEMPLATE)
        self.assertGreater(len(TUTOR_SYSTEM_PROMPT_TEMPLATE), 500)

    def test_prompt_contains_placeholders(self):
        """Template should contain dynamic placeholders."""
        from apps.tutoring.conversational_tutor import TUTOR_SYSTEM_PROMPT_TEMPLATE

        self.assertIn('{institution_name}', TUTOR_SYSTEM_PROMPT_TEMPLATE)
        self.assertIn('{language}', TUTOR_SYSTEM_PROMPT_TEMPLATE)
        self.assertIn('{grade_level}', TUTOR_SYSTEM_PROMPT_TEMPLATE)

    def test_prompt_contains_science_of_learning_principles(self):
        """Template should reference key science-of-learning principles."""
        from apps.tutoring.conversational_tutor import TUTOR_SYSTEM_PROMPT_TEMPLATE
        prompt = TUTOR_SYSTEM_PROMPT_TEMPLATE.lower()

        principles = [
            'retrieval',
            'spaced',
            'interleav',
            'scaffold',
            'mastery',
            'feedback',
            'worked example',
        ]
        for principle in principles:
            self.assertIn(principle, prompt, f"Missing principle: {principle}")

    def test_build_system_prompt_fills_placeholders(self):
        """_build_system_prompt should fill all placeholders."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        prompt = tutor._build_system_prompt()

        # Should not contain unfilled placeholders
        self.assertNotIn('{institution_name}', prompt)
        self.assertNotIn('{language}', prompt)
        self.assertNotIn('{grade_level}', prompt)

        # Should contain the institution name
        self.assertIn(self.institution.name, prompt)

    def test_build_system_prompt_includes_grade_level(self):
        """_build_system_prompt should include a grade level (default or from profile)."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        prompt = tutor._build_system_prompt()
        # Default is "secondary school" when no StudentProfile exists
        self.assertIn('secondary school', prompt)

    def test_generate_response_uses_built_prompt(self):
        """_generate_response should use _build_system_prompt, not raw constant."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        # Directly set the internal _llm_client (bypassing the property)
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = 'test response'
        mock_llm.generate.return_value = mock_response
        tutor._llm_client = mock_llm

        with patch.object(tutor, '_build_system_prompt', return_value='CUSTOM PROMPT') as mock_build:
            tutor._generate_response("test input")

            mock_build.assert_called()
            # Verify the system_prompt kwarg passed to generate
            call_kwargs = mock_llm.generate.call_args[1]
            self.assertEqual(call_kwargs['system_prompt'], 'CUSTOM PROMPT')

    def test_prompt_contains_formatting_guidance(self):
        """Format rules should include bold key terms and list guidance."""
        from apps.tutoring.conversational_tutor import TUTOR_SYSTEM_PROMPT_TEMPLATE
        self.assertIn('**bold**', TUTOR_SYSTEM_PROMPT_TEMPLATE)
        self.assertIn('key terms', TUTOR_SYSTEM_PROMPT_TEMPLATE)

    def test_opening_prompt_states_learning_objective(self):
        """_generate_opening prompt should instruct stating the learning objective."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        # Mock _generate_response to capture the prompt
        captured_prompt = {}
        original_generate = tutor._generate_response

        def capture_prompt(prompt, **kwargs):
            captured_prompt['text'] = prompt
            return "Hello! Today we'll learn about plate boundaries."

        tutor._generate_response = capture_prompt
        tutor._generate_opening()

        self.assertIn('learning objective', captured_prompt['text'].lower())

    def test_opening_prompt_handles_prior_knowledge_conditionally(self):
        """_generate_opening should only recall prior knowledge when prior lessons exist."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)

        captured_prompt = {}

        def capture_prompt(prompt, **kwargs):
            captured_prompt['text'] = prompt
            return "Hello! Today we'll learn about plate boundaries."

        tutor._generate_response = capture_prompt
        tutor._generate_opening()

        # First lesson in unit: should NOT reference prior lessons
        prompt_lower = captured_prompt['text'].lower()
        self.assertIn('do not reference prior lessons', prompt_lower)
