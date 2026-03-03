"""Tests for R5: RemediationService wired into remediation flow."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR5RemediationWiring(BaseTutoringTestCase):
    """Test that RemediationService is called when remediation starts."""

    @patch('apps.tutoring.conversational_tutor.ConversationalTutor._generate_response')
    def test_start_remediation_calls_remediation_service(self, mock_gen):
        """_start_remediation should call RemediationService.get_remediation_plan."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        mock_gen.return_value = "Let's review the concepts you missed."

        session = self._create_session(engine_state={'session_state': 'exit_ticket'})
        tutor = ConversationalTutor(session)
        tutor.exit_ticket_concepts = [
            {'id': 1, 'question': 'Q1', 'covered': True},
            {'id': 2, 'question': 'Q2', 'covered': True},
        ]

        results = [{'id': 1, 'correct': True}, {'id': 2, 'correct': False}]
        failed = [{'id': 2, 'question': 'Q2'}]

        with patch('apps.tutoring.personalization.RemediationService') as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc.get_remediation_plan.return_value = {
                'needs_remediation': True,
                'weak_skills': [],
                'prerequisite_gaps': [],
                'review_steps': [],
                'message': '',
            }
            mock_svc_cls.return_value = mock_svc

            tutor._start_remediation(results, score=1, failed_questions=failed)

            mock_svc_cls.assert_called_once_with(self.student_user, self.lesson)
            mock_svc.get_remediation_plan.assert_called_once()

    @patch('apps.tutoring.conversational_tutor.ConversationalTutor._generate_response')
    def test_start_remediation_stores_plan(self, mock_gen):
        """_start_remediation should store the remediation plan on the instance."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        mock_gen.return_value = "Let's review."

        session = self._create_session(engine_state={'session_state': 'exit_ticket'})
        tutor = ConversationalTutor(session)
        tutor.exit_ticket_concepts = [{'id': 1, 'question': 'Q1', 'covered': True}]

        plan = {
            'needs_remediation': True,
            'weak_skills': [],
            'prerequisite_gaps': [],
            'review_steps': [],
            'message': '',
        }

        with patch('apps.tutoring.personalization.RemediationService') as mock_svc_cls:
            mock_svc_cls.return_value.get_remediation_plan.return_value = plan

            tutor._start_remediation(
                [{'id': 1, 'correct': False}],
                score=0,
                failed_questions=[{'id': 1, 'question': 'Q1'}],
            )

            self.assertEqual(tutor._remediation_plan, plan)

    @patch('apps.tutoring.conversational_tutor.ConversationalTutor._generate_response')
    def test_start_remediation_graceful_on_service_error(self, mock_gen):
        """_start_remediation should not crash if RemediationService fails."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        mock_gen.return_value = "Let's review."

        session = self._create_session(engine_state={'session_state': 'exit_ticket'})
        tutor = ConversationalTutor(session)
        tutor.exit_ticket_concepts = [{'id': 1, 'question': 'Q1', 'covered': True}]

        with patch('apps.tutoring.personalization.RemediationService') as mock_svc_cls:
            mock_svc_cls.side_effect = Exception("Service unavailable")

            # Should not raise
            result = tutor._start_remediation(
                [{'id': 1, 'correct': False}],
                score=0,
                failed_questions=[{'id': 1, 'question': 'Q1'}],
            )

            self.assertIsNotNone(result)
            self.assertIsNone(tutor._remediation_plan)

    def test_remediation_state_loads_from_engine_state(self):
        """Remediation state flags should load correctly from engine state."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        session = self._create_session(engine_state={
            'session_state': 'tutoring',
            'is_remediation': True,
            'remediation_attempt': 1,
            'failed_exit_questions': [{'id': 1, 'question': 'Q1'}],
        })
        tutor = ConversationalTutor(session)

        self.assertEqual(tutor.session_state, SessionState.TUTORING)
        self.assertTrue(tutor.is_remediation)
        self.assertEqual(tutor.remediation_attempt, 1)
        self.assertEqual(tutor.failed_exit_questions, [{'id': 1, 'question': 'Q1'}])
