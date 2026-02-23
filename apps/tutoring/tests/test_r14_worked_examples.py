"""Tests for R14: Worked example surfacing in INSTRUCTION phase."""

from unittest.mock import patch, MagicMock
from apps.curriculum.models import LessonStep
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR14WorkedExamples(BaseTutoringTestCase):
    """Test that worked examples are surfaced during instruction."""

    def test_tutor_has_worked_example_method(self):
        """ConversationalTutor should have _build_worked_example_block."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session()
        tutor = ConversationalTutor(session)
        self.assertTrue(hasattr(tutor, '_build_worked_example_block'))

    def test_worked_example_block_during_instruction(self):
        """Block should contain worked example during INSTRUCTION phase."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        # Create a worked_example step near the current topic
        we_step = LessonStep.objects.create(
            lesson=self.lesson,
            order_index=2,
            step_type='worked_example',
            teacher_script='Classify this plate boundary.',
            question='',
            answer_type='none',
            educational_content={
                'worked_example': {
                    'problem': 'Classify: Two plates moving apart',
                    'steps': [
                        {'step': 1, 'action': 'Identify motion direction', 'explanation': 'Plates separate'},
                        {'step': 2, 'action': 'Match to boundary type', 'explanation': 'This is divergent'},
                    ],
                    'final_answer': 'Divergent boundary',
                }
            },
        )

        session = self._create_session(engine_state={'phase': 'instruction'})
        tutor = ConversationalTutor(session)

        block = tutor._build_worked_example_block()
        self.assertIn('WORKED EXAMPLE', block)
        self.assertIn('Classify', block)

        # Clean up
        we_step.delete()

    def test_worked_example_block_empty_outside_instruction(self):
        """Block should be empty when not in INSTRUCTION phase."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session(engine_state={'phase': 'practice'})
        tutor = ConversationalTutor(session)

        block = tutor._build_worked_example_block()
        self.assertEqual(block, "")

    def test_worked_example_block_empty_without_examples(self):
        """Block should be empty when no worked examples exist."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session(engine_state={'phase': 'instruction'})
        tutor = ConversationalTutor(session)

        block = tutor._build_worked_example_block()
        self.assertEqual(block, "")

    def test_worked_example_injected_into_context(self):
        """_generate_contextual_response should include worked example block."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session(engine_state={'phase': 'instruction'})
        tutor = ConversationalTutor(session)

        with patch.object(tutor, '_generate_response', return_value='response') as mock_gen:
            with patch.object(tutor, '_build_worked_example_block', return_value='[WORKED EXAMPLE] test'):
                tutor._generate_contextual_response('input', 'kb context')

                prompt_arg = mock_gen.call_args[0][0]
                self.assertIn('[WORKED EXAMPLE] test', prompt_arg)
