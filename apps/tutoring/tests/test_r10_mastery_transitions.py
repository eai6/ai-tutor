"""Tests for R10: Mastery-based phase transitions."""

from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR10MasteryTransitions(BaseTutoringTestCase):
    """Test mastery-based phase transitions with fallbacks."""

    def _make_tutor(self, phase='instruction', exchange_count=0, **extra_state):
        from apps.tutoring.conversational_tutor import ConversationalTutor

        state = {
            'phase': phase,
            'phase_exchange_count': exchange_count,
            'exchange_count': exchange_count,
            **extra_state,
        }
        session = self._create_session(engine_state=state)
        return ConversationalTutor(session)

    def test_instruction_to_practice_mastery(self):
        """INSTRUCTION -> PRACTICE when 2+ comprehension checks correct."""
        from apps.tutoring.conversational_tutor import ConversationPhase

        tutor = self._make_tutor(
            phase='instruction',
            exchange_count=3,
            instruction_checks_correct=2,
        )
        tutor.phase_exchange_count = 3  # Below fallback of 8

        tutor._maybe_transition_phase()
        self.assertEqual(tutor.phase, ConversationPhase.PRACTICE)

    def test_instruction_to_practice_fallback(self):
        """INSTRUCTION -> PRACTICE at 8 exchanges even without mastery."""
        from apps.tutoring.conversational_tutor import ConversationPhase

        tutor = self._make_tutor(
            phase='instruction',
            exchange_count=8,
            instruction_checks_correct=0,
        )
        tutor.phase_exchange_count = 8

        tutor._maybe_transition_phase()
        self.assertEqual(tutor.phase, ConversationPhase.PRACTICE)

    def test_instruction_no_transition_below_threshold(self):
        """INSTRUCTION should not transition with 1 check and 3 exchanges."""
        from apps.tutoring.conversational_tutor import ConversationPhase

        tutor = self._make_tutor(
            phase='instruction',
            exchange_count=3,
            instruction_checks_correct=1,
        )
        tutor.phase_exchange_count = 3

        tutor._maybe_transition_phase()
        self.assertEqual(tutor.phase, ConversationPhase.INSTRUCTION)

    def test_practice_to_wrapup_mastery(self):
        """PRACTICE -> WRAPUP when >=70% accuracy on 3+ questions."""
        from apps.tutoring.conversational_tutor import ConversationPhase

        tutor = self._make_tutor(phase='practice', exchange_count=4)
        tutor.phase_exchange_count = 4
        tutor.practice_total = 4
        tutor.practice_correct = 3  # 75% accuracy

        tutor._maybe_transition_phase()
        self.assertEqual(tutor.phase, ConversationPhase.WRAPUP)

    def test_practice_no_transition_low_accuracy(self):
        """PRACTICE should not transition with low accuracy."""
        from apps.tutoring.conversational_tutor import ConversationPhase

        tutor = self._make_tutor(phase='practice', exchange_count=4)
        tutor.phase_exchange_count = 4
        tutor.practice_total = 4
        tutor.practice_correct = 1  # 25% accuracy

        tutor._maybe_transition_phase()
        self.assertEqual(tutor.phase, ConversationPhase.PRACTICE)

    def test_practice_to_wrapup_fallback(self):
        """PRACTICE -> WRAPUP at 7 exchanges even with low accuracy."""
        from apps.tutoring.conversational_tutor import ConversationPhase

        tutor = self._make_tutor(phase='practice', exchange_count=7)
        tutor.phase_exchange_count = 7
        tutor.practice_total = 7
        tutor.practice_correct = 1  # Low accuracy but hit fallback

        tutor._maybe_transition_phase()
        self.assertEqual(tutor.phase, ConversationPhase.WRAPUP)

    def test_practice_no_transition_insufficient_questions(self):
        """PRACTICE should not transition on mastery with <3 questions."""
        from apps.tutoring.conversational_tutor import ConversationPhase

        tutor = self._make_tutor(phase='practice', exchange_count=3)
        tutor.phase_exchange_count = 3
        tutor.practice_total = 2
        tutor.practice_correct = 2  # 100% but only 2 questions

        tutor._maybe_transition_phase()
        self.assertEqual(tutor.phase, ConversationPhase.PRACTICE)

    def test_instruction_checks_correct_persisted(self):
        """instruction_checks_correct should be saved and loaded from state."""
        from apps.tutoring.conversational_tutor import ConversationalTutor

        session = self._create_session(engine_state={
            'phase': 'instruction',
            'instruction_checks_correct': 3,
        })
        tutor = ConversationalTutor(session)
        self.assertEqual(tutor.instruction_checks_correct, 3)

    def test_instruction_checks_reset_on_transition(self):
        """instruction_checks_correct should reset when entering PRACTICE."""
        tutor = self._make_tutor(
            phase='instruction',
            exchange_count=3,
            instruction_checks_correct=2,
        )
        tutor.phase_exchange_count = 3

        tutor._maybe_transition_phase()
        self.assertEqual(tutor.instruction_checks_correct, 0)

    def test_analyze_response_increments_instruction_checks(self):
        """_analyze_student_response should increment instruction_checks_correct."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, ConversationPhase

        session = self._create_session(engine_state={
            'phase': 'instruction',
            'instruction_checks_correct': 0,
        })
        tutor = ConversationalTutor(session)
        tutor.phase = ConversationPhase.INSTRUCTION

        tutor._analyze_student_response(
            "The answer is divergent",
            "Excellent! That's exactly right.",
        )

        self.assertGreaterEqual(tutor.instruction_checks_correct, 1)
