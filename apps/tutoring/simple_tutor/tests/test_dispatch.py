"""M10 acceptance tests — env-based engine selector + response adapter.

Per the design (auto-memory/feedback_server_owns_question_state.md):

  - The student NEVER picks the engine. Selection is deploy-config
    via the SIMPLE_TUTOR_ENGINE env var.
  - is_enabled() reads the env on every call (no process restart
    needed to flip it).
  - respond_for_view() projects the engine's dict result into the
    same JSON shape the legacy v1 chat view returns, so the chat UI
    works unchanged.
"""
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.tutoring.models import (
    ExitTicket, ExitTicketQuestion, TutorSession,
)
from apps.tutoring import simple_tutor
from apps.tutoring.simple_tutor.engine import respond_for_view

User = get_user_model()


_counter = {'n': 0}


def _make_session(*, n_questions=1):
    _counter['n'] += 1
    i = _counter['n']
    inst = Institution.objects.create(name=f'S {i}', slug=f's-{i}')
    user = User.objects.create_user(username=f'u-{i}', password='x')
    course = Course.objects.create(
        title=f'C {i}', institution=inst,
        grade_level='S3', is_published=True,
    )
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='L', objective='x',
        order_index=0, is_published=True,
    )
    for idx in range(2):
        # Empty step.question/expected_answer routes pick_current_question
        # to the ExitTicketQuestion fallback that test_dispatch needs.
        LessonStep.objects.create(
            lesson=lesson, teacher_script='t',
            question='', expected_answer='',
            phase='explore', order_index=idx,
            enabling_objective=f'obj-{i}',
        )
    ticket = ExitTicket.objects.create(lesson=lesson)
    for j in range(n_questions):
        ExitTicketQuestion.objects.create(
            exit_ticket=ticket, question_type='mcq',
            question_text=f'Q{j}',
            option_a='a', option_b='b', option_c='c', option_d='d',
            correct_answer='B',
            enabling_objective=f'obj-{i}',
            order_index=j,
        )
    return TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson, engine='simple',
    )


def _llm_response(text='Hello', tool_uses=None):
    blocks = [SimpleNamespace(type='text', text=text)]
    for tu in (tool_uses or []):
        blocks.append(SimpleNamespace(
            type='tool_use', name=tu['name'], input=tu.get('input', {}),
        ))
    return SimpleNamespace(content=blocks)


# ============================================================================
# is_enabled — env handling
# ============================================================================


class IsEnabledTest(DjangoTestCase):
    """The engine flag is read fresh from the env on every call —
    flippable at runtime without process restart.
    """

    def test_default_on(self):
        # Default env (no SIMPLE_TUTOR_ENGINE) → on: simple_tutor is the
        # default engine since da8b57f.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SIMPLE_TUTOR_ENGINE', None)
            self.assertTrue(simple_tutor.is_enabled())

    def test_on_truthy(self):
        for val in ('on', 'true', 'True', '1', 'yes', 'simple', 'enabled'):
            with patch.dict(os.environ, {'SIMPLE_TUTOR_ENGINE': val}):
                self.assertTrue(
                    simple_tutor.is_enabled(),
                    f"expected enabled for {val!r}",
                )

    def test_off_falsy(self):
        for val in ('off', 'false', '0', 'no', 'disabled', 'v1', 'legacy', 'old'):
            with patch.dict(os.environ, {'SIMPLE_TUTOR_ENGINE': val}):
                self.assertFalse(
                    simple_tutor.is_enabled(),
                    f"expected disabled for {val!r}",
                )

    def test_whitespace_stripped(self):
        with patch.dict(os.environ, {'SIMPLE_TUTOR_ENGINE': '  on  '}):
            self.assertTrue(simple_tutor.is_enabled())


# ============================================================================
# respond_for_view — adapter shape
# ============================================================================


@patch('apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class RespondForViewTest(DjangoTestCase):
    """Adapter projects engine output into legacy v1 JSON shape."""

    def test_returns_v1_json_shape(self, _mock_kb):
        session = _make_session()
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='Hello, student.'),
        ):
            payload = respond_for_view(session, 'what is an angle?')
        # All v1 fields present (some default-None when not produced)
        expected_keys = {
            'message', 'phase', 'media', 'show_exit_ticket', 'exit_ticket',
            'is_complete', 'step_number', 'total_steps',
            'is_correct', 'streak_count', 'practice_score', 'milestone',
            'artifact_html', 'probe', 'pending_question', 'follow_up_message',
        }
        self.assertEqual(set(payload.keys()), expected_keys)
        self.assertEqual(payload['message'], 'Hello, student.')

    def test_step_number_and_total_reflect_session(self, _mock_kb):
        session = _make_session()
        # We have 2 LessonSteps in the fixture
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='ok'),
        ):
            payload = respond_for_view(session, 'what is happening?')
        self.assertEqual(payload['step_number'], 1)
        self.assertEqual(payload['total_steps'], 2)

    def test_phase_from_current_step(self, _mock_kb):
        session = _make_session()
        # fixture step.phase = 'explore'
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='ok'),
        ):
            payload = respond_for_view(session, 'what is happening?')
        self.assertEqual(payload['phase'], 'explore')

    def test_is_correct_when_record_answer_correct(self, _mock_kb):
        from apps.tutoring.models import InFlightQuestion
        session = _make_session()
        InFlightQuestion.objects.create(
            session=session,
            question_text='Which?',
            question_type='mcq',
            options=['a', 'b', 'c', 'd'],
            reference_answer='B',
            source='inline_authored',
        )
        responses = [
            _llm_response(
                text='Great.',
                tool_uses=[{'name': 'record_answer',
                            'input': {'extracted_answer': 'B'}}],
            ),
            _llm_response(text='Correct.'),
        ]
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            side_effect=responses,
        ):
            payload = respond_for_view(session, 'B')
        self.assertTrue(payload['is_correct'])

    def test_is_correct_false_when_wrong(self, _mock_kb):
        from apps.tutoring.models import InFlightQuestion
        session = _make_session()
        InFlightQuestion.objects.create(
            session=session,
            question_text='Which?',
            question_type='mcq',
            options=['a', 'b', 'c', 'd'],
            reference_answer='B',
            source='inline_authored',
        )
        responses = [
            _llm_response(
                text='Try again.',
                tool_uses=[{'name': 'record_answer',
                            'input': {'extracted_answer': 'A'}}],
            ),
            _llm_response(text='Not quite.'),
        ]
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            side_effect=responses,
        ):
            payload = respond_for_view(session, 'A')
        self.assertIs(payload['is_correct'], False)

    def test_is_correct_none_when_no_answer(self, _mock_kb):
        session = _make_session()
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='Let me explain.'),
        ):
            payload = respond_for_view(session, 'what is an angle?')
        self.assertIsNone(payload['is_correct'])

    def test_complete_after_advancing_past_last_step(self, _mock_kb):
        """When all lesson steps are done, the engine transitions to the
        exit ticket instead of completing immediately (M12.8 wiring).
        """
        session = _make_session(n_questions=1)
        # Force-advance past last step
        session.current_step_index = 99
        session.save(update_fields=['current_step_index'])
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='done'),
        ):
            payload = respond_for_view(session, 'what is an angle?')
        # The fixture session has an attached ExitTicket → bridge to it.
        self.assertTrue(payload['show_exit_ticket'])
        self.assertIsNotNone(payload['exit_ticket'])
        # is_complete stays False until the exit ticket is scored.
        self.assertFalse(payload['is_complete'])
        # Payload shape matches the legacy engine's contract.
        self.assertIn('questions', payload['exit_ticket'])
        self.assertIn('total', payload['exit_ticket'])
        self.assertIn('passing_score', payload['exit_ticket'])
        # Default: MCQ-only, capped at 10. The fixture in
        # apps/tutoring/simple_tutor/tests/test_tools.py creates 3 MCQ
        # questions per ExitTicket, so total should be min(3, 10) = 3.
        self.assertLessEqual(payload['exit_ticket']['total'], 10)
        for q in payload['exit_ticket']['questions']:
            self.assertEqual(q['question_type'], 'mcq')


class ExitTicketEnvVarTest(DjangoTestCase):
    """The MCQ filter + size cap are env-tunable so we can adjust
    during the pilot without a code deploy.
    """

    def test_cap_override_env_var(self):
        from apps.tutoring.simple_tutor import engine as _e
        from unittest.mock import patch
        with patch.dict('os.environ', {'EXIT_TICKET_MAX_QUESTIONS': '3'}):
            self.assertEqual(_e._exit_ticket_cap(), 3)
        with patch.dict('os.environ', {'EXIT_TICKET_MAX_QUESTIONS': '0'}):
            # Invalid (≤0) falls back to default.
            self.assertEqual(_e._exit_ticket_cap(), 10)
        with patch.dict('os.environ', {'EXIT_TICKET_MAX_QUESTIONS': 'not-a-number'}):
            self.assertEqual(_e._exit_ticket_cap(), 10)
        # Unset → default
        with patch.dict('os.environ', {}, clear=False):
            import os as _os
            _os.environ.pop('EXIT_TICKET_MAX_QUESTIONS', None)
            self.assertEqual(_e._exit_ticket_cap(), 10)

    def test_types_override_env_var(self):
        from apps.tutoring.simple_tutor import engine as _e
        from unittest.mock import patch
        with patch.dict('os.environ', {'EXIT_TICKET_TYPES': 'mcq,short_answer'}):
            self.assertEqual(
                _e._exit_ticket_allowed_types(),
                ('mcq', 'short_answer'),
            )
        with patch.dict('os.environ', {'EXIT_TICKET_TYPES': '  '}):
            # Empty → default (mcq-only).
            self.assertEqual(_e._exit_ticket_allowed_types(), ('mcq',))


class StartForViewResumeTest(DjangoTestCase):
    """When the student returns to a session with an InFlightQuestion
    already posed, ``start_for_view`` must re-anchor deterministically
    (no LLM call, no new pose) so the original question isn't orphaned.
    """

    def test_resume_with_in_flight_skips_llm(self):
        from apps.tutoring.models import InFlightQuestion, SessionTurn
        from apps.tutoring.simple_tutor.engine import start_for_view

        session = _make_session(n_questions=1)
        # Simulate a prior turn (so this looks like a resume, not a
        # fresh start) and an in-flight question awaiting an answer.
        SessionTurn.objects.create(
            session=session, role=SessionTurn.Role.TUTOR,
            content='Earlier tutor turn',
        )
        InFlightQuestion.objects.create(
            session=session,
            question_text='Which is the largest-scale map?',
            question_type='mcq',
            options=['1:5,000', '1:50,000', '1:500,000', '1:5,000,000'],
            reference_answer='A',
            source='inline_authored',
        )

        # If the LLM is called, the patch raises — the resume branch
        # MUST short-circuit before any LLM call.
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            side_effect=AssertionError('LLM should not be called on resume'),
        ):
            payload = start_for_view(session)

        # The message re-displays the stem + lettered options.
        self.assertIn('Welcome back', payload['message'])
        self.assertIn('Which is the largest-scale map?', payload['message'])
        self.assertIn('A) 1:5,000', payload['message'])
        self.assertIn('D) 1:5,000,000', payload['message'])

        # Slot is preserved so the next student answer routes to GRADE.
        self.assertTrue(
            InFlightQuestion.objects.filter(session=session).exists()
        )

        # A new tutor turn was persisted with the resume message.
        latest = SessionTurn.objects.filter(
            session=session, role=SessionTurn.Role.TUTOR,
        ).latest('created_at')
        self.assertIn('Welcome back', latest.content)

    def test_resume_without_in_flight_falls_through_to_start(self):
        """Resume with no in-flight slot just runs the normal start path
        (the LLM decides whether to teach or pose).
        """
        from apps.tutoring.models import SessionTurn
        from apps.tutoring.simple_tutor.engine import start_for_view

        session = _make_session(n_questions=1)
        SessionTurn.objects.create(
            session=session, role=SessionTurn.Role.TUTOR,
            content='Earlier turn',
        )

        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='resumed via LLM'),
        ):
            payload = start_for_view(session)

        self.assertEqual(payload['message'], 'resumed via LLM')

    def test_fresh_start_falls_through_to_start(self):
        """No prior turns + no slot → normal warmup path."""
        from apps.tutoring.simple_tutor.engine import start_for_view

        session = _make_session(n_questions=1)

        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='fresh start warmup'),
        ):
            payload = start_for_view(session)

        self.assertEqual(payload['message'], 'fresh start warmup')
