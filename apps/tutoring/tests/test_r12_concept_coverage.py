"""Tests for R12: LLM-based concept coverage assessment."""

from unittest.mock import patch, MagicMock, PropertyMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.tutoring.conversational_tutor import ConceptCoverageResult


class TestR12ConceptCoverage(BaseTutoringTestCase):
    """Test LLM-based concept coverage with keyword fallback."""

    def _make_tutor(self, phase='instruction', exchange_count=0):
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session(engine_state={
            'phase': phase,
            'exchange_count': exchange_count,
            'phase_exchange_count': exchange_count,
        })
        tutor = ConversationalTutor(session)
        tutor.exchange_count = exchange_count
        return tutor

    def test_keyword_coverage_check_exists(self):
        """_keyword_concept_coverage_check method should exist."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)
        self.assertTrue(hasattr(tutor, '_keyword_concept_coverage_check'))

    def test_llm_coverage_check_exists(self):
        """_llm_concept_coverage_check method should exist."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)
        self.assertTrue(hasattr(tutor, '_llm_concept_coverage_check'))

    def test_keyword_coverage_marks_concept(self):
        """Keyword check should mark concepts as covered based on keyword overlap."""
        tutor = self._make_tutor()

        # Set up a concept with keywords that appear in the text
        tutor.exit_ticket_concepts = [
            {
                'id': 1,
                'question': 'What type of boundary forms mountains?',
                'correct_text': 'Convergent',
                'explanation': 'Convergent boundaries push plates together forming mountains.',
                'covered': False,
            }
        ]

        # Text with many matching keywords
        text = "convergent boundary plates together mountains forming"
        tutor._keyword_concept_coverage_check(text)

        self.assertTrue(tutor.exit_ticket_concepts[0]['covered'])

    def test_keyword_coverage_skips_already_covered(self):
        """Keyword check should skip concepts already marked as covered."""
        tutor = self._make_tutor()

        tutor.exit_ticket_concepts = [
            {
                'id': 1,
                'question': 'Q1',
                'correct_text': 'A1',
                'explanation': 'E1',
                'covered': True,
            }
        ]

        tutor._keyword_concept_coverage_check("unrelated text")
        self.assertTrue(tutor.exit_ticket_concepts[0]['covered'])

    def test_llm_coverage_marks_concepts(self):
        """LLM check should mark concepts based on instructor structured response."""
        tutor = self._make_tutor()

        tutor.exit_ticket_concepts = [
            {'id': 1, 'question': 'Q1 about mountains', 'covered': False},
            {'id': 2, 'question': 'Q2 about oceans', 'covered': False},
        ]

        # Mock instructor_client to return a ConceptCoverageResult indicating concept 1 covered
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = ConceptCoverageResult(covered_indices=[1])
        tutor._instructor_client = mock_client

        tutor._llm_concept_coverage_check("We discussed mountain formation extensively.")

        self.assertTrue(tutor.exit_ticket_concepts[0]['covered'])
        self.assertFalse(tutor.exit_ticket_concepts[1]['covered'])

    def test_llm_coverage_fallback_on_error(self):
        """LLM check should fall back to keyword matching on error."""
        tutor = self._make_tutor()

        tutor.exit_ticket_concepts = [
            {
                'id': 1,
                'question': 'What type of boundary forms mountains?',
                'correct_text': 'Convergent',
                'explanation': 'Convergent boundaries push plates together.',
                'covered': False,
            }
        ]

        # Mock instructor_client so that chat.completions.create raises an exception
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("LLM error")
        tutor._instructor_client = mock_client

        # Text with matching keywords - should still work via fallback
        tutor._llm_concept_coverage_check(
            "convergent boundary plates together mountains"
        )

        # Keyword fallback should have marked it
        self.assertTrue(tutor.exit_ticket_concepts[0]['covered'])

    def test_analyze_uses_llm_on_even_exchanges(self):
        """_analyze_student_response should use LLM check on even exchange counts."""
        tutor = self._make_tutor(exchange_count=2)

        with patch.object(tutor, '_llm_concept_coverage_check') as mock_llm:
            with patch.object(tutor, '_keyword_concept_coverage_check') as mock_kw:
                tutor._analyze_student_response("test", "Great job!")

                mock_llm.assert_called_once()
                mock_kw.assert_not_called()

    def test_analyze_uses_keyword_on_odd_exchanges(self):
        """_analyze_student_response should use keyword check on odd exchange counts."""
        tutor = self._make_tutor(exchange_count=1)

        with patch.object(tutor, '_llm_concept_coverage_check') as mock_llm:
            with patch.object(tutor, '_keyword_concept_coverage_check') as mock_kw:
                tutor._analyze_student_response("test", "response")

                mock_kw.assert_called_once()
                mock_llm.assert_not_called()
