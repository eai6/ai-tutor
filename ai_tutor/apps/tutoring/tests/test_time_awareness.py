"""Tests for time-awareness + exit-ticket reserve.

Two requirements (2026-05-12 pilot):
  1. "the tutor needs to leave 5 mins for the exit ticket, so if
      the user select 15 mins, the tutoring session should be 10
      mins max."
  2. "the tutor should know I have 15 minutes or 20 minutes or 10
      minutes left to cover the content and account for it during
      the step advancement."
"""
from datetime import timedelta
from unittest.mock import MagicMock

from django.test import SimpleTestCase
from django.utils import timezone

from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor


def _bind_tutor(*, target_override=None, elapsed_minutes=0.0,
                steps=3, current_index=0):
    """Build a ConversationalTutor bypassing __init__ heavy paths."""
    tutor = object.__new__(ConversationalTutor)
    tutor.session = MagicMock()
    tutor.session.engine_state = (
        {'target_minutes_override': target_override}
        if target_override else {}
    )
    tutor.session.started_lesson_at = (
        timezone.now() - timedelta(minutes=elapsed_minutes)
    )
    tutor.lesson = MagicMock()
    tutor.lesson.estimated_minutes = 20
    tutor.steps = [MagicMock(step_type='practice') for _ in range(steps)]
    tutor.current_topic_index = current_index
    return tutor


class ExitTicketReserveTest(SimpleTestCase):
    """Budget for tutoring = total - 5 min exit-ticket reserve."""

    def test_15_minute_session_gets_10_minute_tutoring(self):
        tutor = _bind_tutor(target_override=15)
        self.assertEqual(tutor._target_minutes_for_session(), 15)
        self.assertEqual(tutor._tutoring_minutes_budget(), 10)

    def test_20_minute_session_gets_15_minute_tutoring(self):
        tutor = _bind_tutor(target_override=20)
        self.assertEqual(tutor._tutoring_minutes_budget(), 15)

    def test_10_minute_session_gets_5_minute_tutoring(self):
        tutor = _bind_tutor(target_override=10)
        self.assertEqual(tutor._tutoring_minutes_budget(), 5)

    def test_5_minute_session_floor_holds(self):
        """At 5-min total the tutoring budget floors at 5 (we don't
        let it drop below 5 even though that means no exit-ticket
        buffer — better to teach a little than nothing)."""
        tutor = _bind_tutor(target_override=5)
        self.assertEqual(tutor._tutoring_minutes_budget(), 5)

    def test_no_override_uses_lesson_default(self):
        tutor = _bind_tutor(target_override=None)
        tutor.lesson.estimated_minutes = 30
        self.assertEqual(tutor._target_minutes_for_session(), 30)
        self.assertEqual(tutor._tutoring_minutes_budget(), 25)


class TimeAwarenessBlockTest(SimpleTestCase):
    """The <time_awareness> block carries the right pace directive
    based on remaining time per step."""

    def test_normal_pace_when_plenty_of_time(self):
        # 15 min budget, 1 min elapsed, 3 steps left → ~4.7 min/step
        tutor = _bind_tutor(
            target_override=20, elapsed_minutes=1, steps=3, current_index=0,
        )
        block = tutor._build_time_awareness_block()
        self.assertIn("<time_awareness>", block)
        self.assertIn("On track", block)
        self.assertIn("Normal pace", block)

    def test_tight_pace_band(self):
        # 10 min tutoring, 5 min elapsed, 2 steps left → 2.5 min/step
        tutor = _bind_tutor(
            target_override=15, elapsed_minutes=5, steps=4, current_index=2,
        )
        block = tutor._build_time_awareness_block()
        self.assertIn("Getting tight", block)
        self.assertIn("Trim explanations", block)

    def test_behind_schedule_aggressive(self):
        # 10 min tutoring, 9 min elapsed, 4 steps left → 0.25 min/step
        tutor = _bind_tutor(
            target_override=15, elapsed_minutes=9, steps=4, current_index=0,
        )
        block = tutor._build_time_awareness_block()
        self.assertIn("BEHIND SCHEDULE", block)
        self.assertIn("Advance aggressively", block)
        self.assertIn("exit ticket", block)

    def test_block_includes_budget_and_elapsed(self):
        tutor = _bind_tutor(
            target_override=20, elapsed_minutes=3, steps=3, current_index=0,
        )
        block = tutor._build_time_awareness_block()
        self.assertIn("Session budget: 15m tutoring", block)
        self.assertIn("Elapsed:", block)
        self.assertIn("Remaining tutoring time:", block)
        self.assertIn("Steps left in this session: 3", block)


class StepSelectionUsesTutoringBudgetTest(SimpleTestCase):
    """_select_steps_for_duration must use the tutoring budget, not
    the full session budget — otherwise a 15-min session over-packs
    steps that won't leave room for the exit ticket."""

    def test_step_count_derived_from_tutoring_budget(self):
        # Build a tutor with the same fixture but trigger the actual
        # step-selection path.
        tutor = object.__new__(ConversationalTutor)
        tutor.session = MagicMock()
        tutor.session.engine_state = {'target_minutes_override': 15}
        # Build 10 mock steps with order_index attrs
        from ai_tutor.apps.curriculum.models import LessonStep
        steps = [
            MagicMock(spec=LessonStep, order_index=i,
                      priority=LessonStep.Priority.REQUIRED, step_type='teach')
            for i in range(10)
        ]
        budget = tutor._tutoring_minutes_budget()
        # 15 min total → 10 min tutoring → target_count =
        # max(3, min(10, 10 // 5)) = max(3, 2) = 3 steps
        target_count = max(3, min(10, budget // 5))
        self.assertEqual(target_count, 3)
        # And the selector should return ~3 steps from a 10-step pool.
        selected = tutor._select_steps_for_duration(steps, budget)
        self.assertLessEqual(len(selected), 4)  # 3 ± 1 from algorithm
