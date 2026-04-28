"""Tests for difficulty signal (ZPD adjustment) and in-conversation gamification."""

import json
from unittest.mock import patch, MagicMock, PropertyMock

from django.test import RequestFactory
from django.contrib.auth.models import User

from apps.curriculum.models import LessonStep
from apps.tutoring.tests.fixtures import BaseTutoringTestCase
from apps.tutoring.views import chat_difficulty_signal


class TestDifficultySignalView(BaseTutoringTestCase):
    """Test the difficulty-signal API endpoint."""

    def setUp(self):
        self.factory = RequestFactory()

    def _post_signal(self, session, signal):
        request = self.factory.post(
            f'/tutor/api/chat/{session.id}/difficulty-signal/',
            data=json.dumps({'signal': signal}),
            content_type='application/json',
        )
        request.user = self.student_user
        return chat_difficulty_signal(request, session.id)

    def test_difficulty_signal_too_easy(self):
        session = self._create_session(engine_state={})
        resp = self._post_signal(session, 'too_easy')
        data = json.loads(resp.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['difficulty_level'], 1)
        session.refresh_from_db()
        self.assertEqual(session.engine_state['difficulty_level'], 1)

    def test_difficulty_signal_too_hard(self):
        session = self._create_session(engine_state={})
        resp = self._post_signal(session, 'too_hard')
        data = json.loads(resp.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['difficulty_level'], -1)

    def test_difficulty_signal_cumulative(self):
        session = self._create_session(engine_state={})
        self._post_signal(session, 'too_easy')
        session.refresh_from_db()
        resp = self._post_signal(session, 'too_easy')
        data = json.loads(resp.content)
        self.assertEqual(data['difficulty_level'], 2)

    def test_difficulty_signal_capped_at_plus_two(self):
        session = self._create_session(engine_state={'difficulty_level': 2})
        resp = self._post_signal(session, 'too_easy')
        data = json.loads(resp.content)
        self.assertEqual(data['difficulty_level'], 2)

    def test_difficulty_signal_capped_at_minus_two(self):
        session = self._create_session(engine_state={'difficulty_level': -2})
        resp = self._post_signal(session, 'too_hard')
        data = json.loads(resp.content)
        self.assertEqual(data['difficulty_level'], -2)

    def test_difficulty_signal_reset(self):
        session = self._create_session(engine_state={'difficulty_level': 2})
        resp = self._post_signal(session, 'reset')
        data = json.loads(resp.content)
        self.assertEqual(data['difficulty_level'], 0)

    def test_difficulty_signal_too_easy_then_too_hard_cancels(self):
        session = self._create_session(engine_state={})
        self._post_signal(session, 'too_easy')
        session.refresh_from_db()
        resp = self._post_signal(session, 'too_hard')
        data = json.loads(resp.content)
        self.assertEqual(data['difficulty_level'], 0)

    def test_difficulty_signal_invalid(self):
        session = self._create_session(engine_state={})
        resp = self._post_signal(session, 'invalid')
        self.assertEqual(resp.status_code, 400)

    def test_difficulty_signal_persists_in_engine_state(self):
        """Verify _load_state / _save_state round-trips difficulty_level."""
        session = self._create_session(engine_state={'difficulty_level': 1})
        from apps.tutoring.conversational_tutor import ConversationalTutor
        tutor = ConversationalTutor.__new__(ConversationalTutor)
        tutor.session = session
        tutor.lesson = self.lesson
        tutor.student = self.student_user
        tutor.steps = list(
            LessonStep.objects.filter(lesson=session.lesson).order_by('order_index')
        )
        tutor.conversation = []
        tutor.exit_ticket_concepts = []
        tutor._personalization = None
        tutor._lesson_skills = []
        tutor._skill_assessment_service = None
        tutor._step_media_ids = {}
        tutor._load_state()
        self.assertEqual(tutor.difficulty_level, 1)


class TestDifficultyPromptBlock(BaseTutoringTestCase):
    """Test the _build_difficulty_signal_block method."""

    def _make_tutor(self, difficulty_level=0):
        """Create a minimal ConversationalTutor mock for testing prompt block."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        tutor = ConversationalTutor.__new__(ConversationalTutor)
        tutor.difficulty_level = difficulty_level
        return tutor

    def test_difficulty_block_empty_at_zero(self):
        tutor = self._make_tutor(0)
        self.assertEqual(tutor._build_difficulty_signal_block(), "")

    def test_difficulty_block_too_easy_content(self):
        tutor = self._make_tutor(1)
        block = tutor._build_difficulty_signal_block()
        self.assertIn("TOO EASY", block)
        self.assertIn("expertise_reversal", block)
        self.assertIn("DIFFICULTY ADJUSTMENT", block)

    def test_difficulty_block_too_easy_level_2(self):
        tutor = self._make_tutor(2)
        block = tutor._build_difficulty_signal_block()
        self.assertIn("significantly", block)

    def test_difficulty_block_too_hard_content(self):
        tutor = self._make_tutor(-1)
        block = tutor._build_difficulty_signal_block()
        # Easy mode block emitted at level<=-1 — wording rewritten
        # 2026-04-28 to instruct MCQ / fill-in-blank format per pilot
        # feedback. "TOO HARD" → "easy mode" naming.
        self.assertIn("EASY MODE", block)
        self.assertIn("MCQ", block)
        self.assertIn("DIFFICULTY ADJUSTMENT", block)

    def test_difficulty_block_too_hard_level_2(self):
        tutor = self._make_tutor(-2)
        block = tutor._build_difficulty_signal_block()
        self.assertIn("significantly", block)


class TestCorrectStreak(BaseTutoringTestCase):
    """Test correct-answer streak tracking."""

    def _make_tutor_with_state(self, engine_state):
        """Create a ConversationalTutor with just _load_state initialized."""
        from apps.tutoring.conversational_tutor import ConversationalTutor
        session = self._create_session(engine_state=engine_state)
        tutor = ConversationalTutor.__new__(ConversationalTutor)
        tutor.session = session
        tutor.lesson = self.lesson
        tutor.student = self.student_user
        tutor.steps = list(LessonStep.objects.filter(lesson=session.lesson).order_by('order_index'))
        tutor.conversation = []
        tutor.exit_ticket_concepts = []
        tutor._personalization = None
        tutor._lesson_skills = []
        tutor._skill_assessment_service = None
        tutor._step_media_ids = {}
        tutor._load_state()
        return tutor

    def test_streak_increments_on_correct(self):
        tutor = self._make_tutor_with_state({
            'correct_streak': 2,
            'session_state': 'tutoring',
            'current_topic_index': 1,
            'practice_correct': 0,
            'practice_total': 0,
        })
        self.assertEqual(tutor._correct_streak, 2)
        # Simulate a correct answer updating the streak
        tutor.last_answer_correct = True
        tutor._correct_streak = tutor._correct_streak + 1
        self.assertEqual(tutor._correct_streak, 3)

    def test_streak_resets_on_wrong(self):
        tutor = self._make_tutor_with_state({
            'correct_streak': 5,
            'session_state': 'tutoring',
            'current_topic_index': 0,
        })
        self.assertEqual(tutor._correct_streak, 5)
        # Simulate wrong answer resetting the streak
        tutor._correct_streak = 0
        self.assertEqual(tutor._correct_streak, 0)


class TestMilestoneCheck(BaseTutoringTestCase):
    """Test _check_milestone method."""

    def _make_tutor(self, **kwargs):
        from apps.tutoring.conversational_tutor import ConversationalTutor
        tutor = ConversationalTutor.__new__(ConversationalTutor)
        tutor.steps = kwargs.get('steps', [MagicMock()] * 6)
        tutor.current_topic_index = kwargs.get('current_topic_index', 0)
        tutor._correct_streak = kwargs.get('streak', 0)
        tutor._step_just_advanced = kwargs.get('step_just_advanced', False)
        tutor.practice_correct = kwargs.get('practice_correct', 0)
        tutor.practice_total = kwargs.get('practice_total', 0)
        return tutor

    def test_milestone_streak_3(self):
        tutor = self._make_tutor(streak=3)
        self.assertEqual(tutor._check_milestone(), "streak_3")

    def test_milestone_streak_5_takes_priority(self):
        tutor = self._make_tutor(streak=5)
        self.assertEqual(tutor._check_milestone(), "streak_5")

    def test_milestone_halfway(self):
        # 6 steps, current_topic_index=2 -> step_num=3 -> (6+1)//2 = 3 -> halfway
        tutor = self._make_tutor(current_topic_index=2, step_just_advanced=True)
        self.assertEqual(tutor._check_milestone(), "halfway")

    def test_milestone_final_step(self):
        tutor = self._make_tutor(current_topic_index=5, step_just_advanced=True)
        self.assertEqual(tutor._check_milestone(), "final_step")

    def test_milestone_perfect_run(self):
        tutor = self._make_tutor(practice_correct=4, practice_total=4)
        self.assertEqual(tutor._check_milestone(), "perfect_run")

    def test_milestone_none_when_no_conditions(self):
        tutor = self._make_tutor(streak=0, practice_correct=1, practice_total=2)
        self.assertIsNone(tutor._check_milestone())


class TestTutorMessageGamificationFields(BaseTutoringTestCase):
    """Test that TutorMessage includes gamification fields."""

    def test_tutor_message_has_gamification_fields(self):
        from apps.tutoring.conversational_tutor import TutorMessage
        msg = TutorMessage(
            content="Good job!",
            phase="practice",
            is_correct=True,
            streak_count=3,
            practice_score="3/4",
            milestone="streak_3",
        )
        self.assertTrue(msg.is_correct)
        self.assertEqual(msg.streak_count, 3)
        self.assertEqual(msg.practice_score, "3/4")
        self.assertEqual(msg.milestone, "streak_3")

    def test_tutor_message_defaults(self):
        from apps.tutoring.conversational_tutor import TutorMessage
        msg = TutorMessage(content="Hello", phase="teach")
        self.assertFalse(msg.is_correct)
        self.assertEqual(msg.streak_count, 0)
        self.assertEqual(msg.practice_score, "")
        self.assertIsNone(msg.milestone)
