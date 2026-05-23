"""Tests for audit v3 R3: LLM verification of keyword coverage at the
remediation gate.

The per-turn `_keyword_concept_coverage_check` over-counts coverage
(mentioning the concept term ≠ teaching it). Commit `5d6cbd7` made
the `covered` flag load-bearing for the remediation gate. R3 wires an
LLM verifier in at the gate so that false-positive coverage cannot
promote a student back to the exit ticket without real re-teaching.
"""

from unittest.mock import MagicMock

from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.tutoring.conversational_tutor import ConceptCoverageResult


class TestR3RemediationCoverageGate(BaseTutoringTestCase):
    def _make_tutor_in_remediation(self, exchange_count=10, failed_concept_ids=(1, 2)):
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session(engine_state={
            'phase': 'remediation',
            'exchange_count': exchange_count,
        })
        tutor = ConversationalTutor(session)
        tutor.is_remediation = True
        tutor.exchange_count = exchange_count
        tutor._failed_eos = [{'name': 'EO1'}, {'name': 'EO2'}]
        tutor.failed_exit_questions = [
            {'id': i} for i in failed_concept_ids
        ]
        tutor.exit_ticket_concepts = [
            {
                'id': cid,
                'question': f'Question {cid}: explain the concept',
                'correct_text': f'Answer {cid}',
                'explanation': f'Explanation {cid}',
                'covered': True,  # keyword check marked it covered
            }
            for cid in failed_concept_ids
        ]
        return tutor

    def test_llm_can_unmark_false_positive_coverage(self):
        """When LLM says concept 1 was NOT meaningfully covered, the flag
        should flip back to False — keeping remediation going."""
        tutor = self._make_tutor_in_remediation()

        mock_client = MagicMock()
        # LLM confirms only concept #2 was meaningfully covered
        mock_client.chat.completions.create.return_value = ConceptCoverageResult(
            covered_indices=[2],
        )
        tutor._instructor_client = mock_client

        # Stub recent-conversation pull so the verifier has text to send
        tutor._recent_conversation_text = MagicMock(return_value=(
            "STUDENT: not sure\nTUTOR: let's keep going\n"
            "STUDENT: ok\nTUTOR: what did you learn?\n"
        ))

        tutor._verify_keyword_coverage_with_llm({1, 2})

        # Concept 1 unmarked (LLM disagreed); concept 2 stays covered
        self.assertFalse(tutor.exit_ticket_concepts[0]['covered'])
        self.assertTrue(tutor.exit_ticket_concepts[1]['covered'])

    def test_llm_confirms_all_keeps_covered(self):
        """When LLM confirms every keyword-marked concept, all stay covered."""
        tutor = self._make_tutor_in_remediation()

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = ConceptCoverageResult(
            covered_indices=[1, 2],
        )
        tutor._instructor_client = mock_client
        tutor._recent_conversation_text = MagicMock(return_value="some real teaching")

        tutor._verify_keyword_coverage_with_llm({1, 2})

        self.assertTrue(tutor.exit_ticket_concepts[0]['covered'])
        self.assertTrue(tutor.exit_ticket_concepts[1]['covered'])

    def test_llm_error_keeps_keyword_decision(self):
        """LLM transient failure must not block remediation — covered flags
        stay where the keyword check left them. Fail-soft is intentional."""
        tutor = self._make_tutor_in_remediation()

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("LLM offline")
        tutor._instructor_client = mock_client
        tutor._recent_conversation_text = MagicMock(return_value="some text")

        # Should not raise — both stay covered (per keyword check)
        tutor._verify_keyword_coverage_with_llm({1, 2})

        self.assertTrue(tutor.exit_ticket_concepts[0]['covered'])
        self.assertTrue(tutor.exit_ticket_concepts[1]['covered'])

    def test_no_instructor_client_is_noop(self):
        """When the instructor client isn't available, the verifier is a no-op
        — the keyword-marked covered flags remain. Tutor degrades gracefully."""
        tutor = self._make_tutor_in_remediation()
        tutor._instructor_client = None
        # Even with no LLM, calling the verifier should not raise.
        tutor._verify_keyword_coverage_with_llm({1, 2})
        self.assertTrue(tutor.exit_ticket_concepts[0]['covered'])
        self.assertTrue(tutor.exit_ticket_concepts[1]['covered'])

    def test_remediation_gate_invokes_llm_verification(self):
        """`_remediation_steps_complete` should call the LLM verifier
        before consulting `covered` for the gating decision."""
        tutor = self._make_tutor_in_remediation(exchange_count=15)

        called_with = {}

        def fake_verify(ids):
            called_with['ids'] = set(ids)
            # Pretend the LLM rejected concept 1's coverage
            tutor.exit_ticket_concepts[0]['covered'] = False

        tutor._verify_keyword_coverage_with_llm = fake_verify

        # Floor (max(6, 2*3) = 6) already exceeded at exchange_count=15;
        # without LLM verification, the gate would return True. With
        # verification unmarking concept 1, it must return False.
        result = tutor._remediation_steps_complete()
        self.assertFalse(result)
        self.assertEqual(called_with['ids'], {1, 2})

    def test_remediation_gate_passes_when_llm_confirms(self):
        """When LLM confirms all failed concepts are covered, gate fires."""
        tutor = self._make_tutor_in_remediation(exchange_count=15)
        # Stub verifier to be a no-op (LLM confirmed all)
        tutor._verify_keyword_coverage_with_llm = MagicMock(return_value=None)

        result = tutor._remediation_steps_complete()
        self.assertTrue(result)
