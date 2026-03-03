"""Tests for R10: SessionState management."""

from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestR10SessionState(BaseTutoringTestCase):
    """Test SessionState loading and transitions."""

    def _make_tutor(self, session_state='tutoring', exchange_count=0, **extra_state):
        from apps.tutoring.conversational_tutor import ConversationalTutor

        state = {
            'session_state': session_state,
            'exchange_count': exchange_count,
            **extra_state,
        }
        session = self._create_session(engine_state=state)
        return ConversationalTutor(session)

    def test_default_session_state_is_tutoring(self):
        """Default session state should be TUTORING."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor()
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

    def test_session_state_tutoring_loads(self):
        """SessionState.TUTORING should load correctly from engine state."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor(session_state='tutoring')
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

    def test_session_state_exit_ticket_loads(self):
        """SessionState.EXIT_TICKET should load correctly from engine state."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor(session_state='exit_ticket')
        self.assertEqual(tutor.session_state, SessionState.EXIT_TICKET)

    def test_session_state_completed_loads(self):
        """SessionState.COMPLETED should load correctly from engine state."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor(session_state='completed')
        self.assertEqual(tutor.session_state, SessionState.COMPLETED)

    def test_old_instruction_phase_maps_to_tutoring(self):
        """Old 'instruction' phase value should map to SessionState.TUTORING."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor(session_state='instruction')
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

    def test_old_practice_phase_maps_to_tutoring(self):
        """Old 'practice' phase value should map to SessionState.TUTORING."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor(session_state='practice')
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

    def test_old_warmup_phase_maps_to_tutoring(self):
        """Old 'warmup' phase value should map to SessionState.TUTORING."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor(session_state='warmup')
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

    def test_old_wrapup_phase_maps_to_tutoring(self):
        """Old 'wrapup' phase value should map to SessionState.TUTORING."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor(session_state='wrapup')
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

    def test_old_phase_key_backward_compat(self):
        """Engine state with old 'phase' key should still load correctly."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        session = self._create_session(engine_state={
            'phase': 'instruction',
        })
        tutor = ConversationalTutor(session)
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

    def test_invalid_state_defaults_to_tutoring(self):
        """Invalid state string should default to SessionState.TUTORING."""
        from apps.tutoring.conversational_tutor import SessionState

        tutor = self._make_tutor(session_state='nonexistent_state')
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

    def test_session_state_persisted(self):
        """session_state should be saved and loaded from engine state."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        session = self._create_session(engine_state={
            'session_state': 'exit_ticket',
        })
        tutor = ConversationalTutor(session)
        self.assertEqual(tutor.session_state, SessionState.EXIT_TICKET)

    def test_analyze_response_updates_last_answer_correct(self):
        """_analyze_student_response should update last_answer_correct."""
        from apps.tutoring.conversational_tutor import ConversationalTutor, SessionState

        session = self._create_session(engine_state={
            'session_state': 'tutoring',
            'current_topic_index': 1,
        })
        tutor = ConversationalTutor(session)
        self.assertEqual(tutor.session_state, SessionState.TUTORING)

        tutor._analyze_student_response(
            "The answer is divergent",
            "Excellent! That's exactly right.",
        )

        self.assertTrue(hasattr(tutor, 'last_answer_correct'))
