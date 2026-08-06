"""The loop detector must work when the model never calls record_answer.

Regression test for session 6 (2026-08-05). The tutor confirmed three correct
answers in prose, never called ``record_answer``, and so:

  - no grader verdict was written on any turn
  - ``engine_state`` stayed {} and ``answered_correct`` was None
  - the competence trigger in maybe_advance_step counted 0 correct
  - the repetition trigger had nothing to compare against

Three "independent" safety nets, two of which shared one point of failure. The
lesson sat on step 1/5 while the model alternated between two questions; the
student typed "we already did this". Only the turn cap (8 student turns) could
have released it, and by then the student would have answered correctly
repeatedly and been credited for none of it.

These tests pin the property that matters: repetition is detectable from what
the SERVER posed, needing no verdict, no tool call, and no cooperation from
the model.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import TutorSession
from apps.tutoring.simple_tutor.tools import (
    _POSE_HISTORY_WINDOW, _note_pose_repetition,
)

MCQ_OPTIONS = [
    'The easting or horizontal distance',
    'The northing or vertical distance',
    'The scale of the map',
    'The longitude lines',
]


class PoseRepetitionWithoutVerdictsTest(TestCase):

    def setUp(self):
        inst = Institution.objects.create(name='T', slug='t')
        course = Course.objects.create(institution=inst, title='Geography')
        unit = Unit.objects.create(course=course, title='Maps')
        self.lesson = Lesson.objects.create(unit=unit, title='Grid References')
        student = User.objects.create(username='s')
        # engine_state deliberately EMPTY — the observed broken state.
        self.session = TutorSession.objects.create(
            student=student, lesson=self.lesson, institution=inst,
            engine_state={},
        )

    def pose(self, text, options=None, qtype='mcq'):
        return _note_pose_repetition(
            self.session, text, options=options, question_type=qtype)

    def test_exact_reask_forces_advance_with_no_verdicts(self):
        """The base case: same question twice, nothing ever graded."""
        self.assertFalse(self.pose('What does the horizontal axis represent?'))
        # Second pose is a repeat; third crosses the streak threshold.
        self.pose('What does the horizontal axis represent?')
        forced = self.pose('What does the horizontal axis represent?')
        self.assertTrue(forced)
        self.session.refresh_from_db()
        self.assertTrue(self.session.engine_state.get('_repeat_force_advance'))

    def test_alternating_questions_are_caught(self):
        """The ACTUAL observed failure — the model ping-ponged between two
        questions, so 'same as the previous pose' never matched."""
        self.assertFalse(self.pose('What does the horizontal axis represent?'))
        self.assertFalse(self.pose('What does the vertical axis represent?'))
        # Both are now in history, so the next two poses are repeats.
        self.pose('What does the horizontal axis represent?')
        forced = self.pose('What does the vertical axis represent?')
        self.assertTrue(
            forced,
            'alternating between two questions must be detected as a loop')

    def test_reworded_stem_with_identical_options_is_caught(self):
        """The option set is fingerprinted too. The observed loop reworded the
        stem while re-using the same four options, which a text-only
        fingerprint would miss."""
        self.pose('What does the horizontal axis represent?', MCQ_OPTIONS)
        self.pose('Now, what does the vertical axis represent?', MCQ_OPTIONS)
        forced = self.pose('Tell me what the horizontal axis shows.', MCQ_OPTIONS)
        self.assertTrue(forced)

    def test_distinct_questions_do_not_force_advance(self):
        """The detector must not fire on a normal lesson. A step that walks a
        student through several genuinely different questions is the common
        case, and force-advancing it would skip teaching."""
        for i in range(_POSE_HISTORY_WINDOW):
            forced = self.pose(f'Distinct question number {i} about grid refs?')
            self.assertFalse(forced, f'pose {i} wrongly flagged as a repeat')

    def test_streak_resets_on_a_new_question(self):
        """One accidental repeat mid-lesson should not arm the trigger for the
        rest of the step."""
        self.pose('Question A?')
        self.pose('Question A?')            # streak 1
        self.pose('A completely different question B?')   # resets
        self.session.refresh_from_db()
        self.assertEqual(self.session.engine_state.get('repeat_pose_streak'), 0)

    def test_history_is_bounded(self):
        """engine_state is persisted on the session row; an unbounded list
        would grow for the length of a lesson."""
        for i in range(50):
            self.pose(f'Question {i}?', MCQ_OPTIONS)
        self.session.refresh_from_db()
        history = self.session.engine_state.get('recent_poses') or []
        self.assertLessEqual(len(history), _POSE_HISTORY_WINDOW * 2)

    def test_still_uses_answered_correct_when_available(self):
        """The verdict-based signal is strictly better where it exists and must
        keep working — re-asking an already-correct question is a stronger
        signal than merely having asked it."""
        self.session.engine_state = {'answered_correct': ['what is 2 plus 2']}
        self.session.save(update_fields=['engine_state'])
        self.pose('What is 2 plus 2?')
        forced = self.pose('What is 2 plus 2?')
        self.assertTrue(forced)

    @patch('apps.tutoring.simple_tutor.tools._antirepeat_enabled', return_value=False)
    def test_kill_switch_disables_detection(self, _mock):
        for _ in range(5):
            self.assertFalse(self.pose('Same question every time?'))
