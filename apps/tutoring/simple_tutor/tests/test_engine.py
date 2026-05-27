"""M9 acceptance tests — simple_tutor engine main loop.

Tests run end-to-end against the DB (real session, real grader, real
state mutations) but MOCK the Anthropic LLM call to avoid network +
API cost.

Per the design rules
(auto-memory/feedback_server_owns_question_state.md):

- Engine must NEVER raise. LLM exceptions → fallback reply, session
  preserved.
- Tool handlers don't block flow — every dispatch produces a result
  dict.
- Server-driven flow: pick_current_question BEFORE the call;
  auto_grade_if_missed + maybe_advance_step AFTER.
"""
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.tutoring.models import (
    ExitTicket, ExitTicketQuestion, SessionTurn, TutorSession,
)
from apps.tutoring.simple_tutor.engine import (
    respond,
    _build_figure_catalog,
    _figures_enabled,
    _retrieve_kb,
)

User = get_user_model()


_counter = {'n': 0}


def _make_session(*, n_questions=2, with_step_media=False, figures_enabled=True):
    """Build a session + lesson + 2 steps + N MCQ questions."""
    _counter['n'] += 1
    i = _counter['n']
    inst = Institution.objects.create(name=f'School {i}', slug=f'sch-{i}')
    user = User.objects.create_user(username=f'stu-{i}', password='x')
    course = Course.objects.create(
        title=f'Course {i}', institution=inst,
        grade_level='S3', is_published=True,
        tutoring_images_enabled=figures_enabled,
    )
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='L', objective='x',
        order_index=0, is_published=True,
    )
    objective_text = f'objective-{i}'
    for idx in range(2):
        step_media = None
        if with_step_media and idx == 0:
            step_media = {
                'images': [
                    {'url': '/m/a.png', 'alt': 'A figure', 'caption': 'cap A'},
                    {'url': '/m/b.png', 'alt': 'B figure', 'caption': 'cap B'},
                ],
            }
        # Leave step.question + step.expected_answer empty so
        # pick_current_question falls through to the MCQ
        # ExitTicketQuestion created below. LessonStep-primary pickup
        # is exercised separately in test_tools::PickCurrentQuestionLessonStepTest.
        LessonStep.objects.create(
            lesson=lesson, teacher_script='Teach this concept',
            question='', expected_answer='',
            phase='engage', order_index=idx,
            enabling_objective=objective_text,
            media=step_media,
        )
    ticket = ExitTicket.objects.create(lesson=lesson)
    questions = []
    for j in range(n_questions):
        q = ExitTicketQuestion.objects.create(
            exit_ticket=ticket,
            question_type='mcq',
            question_text=f'Q{j+1}',
            option_a='alpha', option_b='beta',
            option_c='gamma', option_d='delta',
            correct_answer='B',
            enabling_objective=objective_text,
            order_index=j,
        )
        questions.append(q)
    session = TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson,
        engine='simple',
    )
    return session, questions


def _llm_response(*, text='', tool_uses=None):
    """Mock an Anthropic Messages API response with text + tool_use blocks."""
    blocks = []
    if text:
        blocks.append(SimpleNamespace(type='text', text=text))
    for tu in (tool_uses or []):
        blocks.append(SimpleNamespace(
            type='tool_use', name=tu['name'], input=tu.get('input', {}),
        ))
    return SimpleNamespace(content=blocks)


@patch('apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class HappyPathTest(DjangoTestCase):

    def test_text_only_reply(self, _mock_kb):
        session, qs = _make_session()
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='Hello, let me explain...'),
        ):
            result = respond(session, 'hi')
        self.assertEqual(result['content'], 'Hello, let me explain...')
        self.assertFalse(result['fallback'])
        # Persisted: 1 student turn + 1 tutor turn
        self.assertEqual(session.turns.count(), 2)

    def test_record_answer_grades_correctly(self, _mock_kb):
        """M12: the slot owns reference / question_type / text. The LLM
        only passes extracted_answer; the platform looks the rest up
        from InFlightQuestion.
        """
        from apps.tutoring.models import InFlightQuestion
        session, _qs = _make_session()
        # Pre-create the in-flight slot (as if pose_question fired on
        # an earlier turn).
        InFlightQuestion.objects.create(
            session=session,
            question_text='Which is greatest?',
            question_type='mcq',
            options=['a', 'b', 'c', 'd'],
            reference_answer='B',
            source='inline_authored',
        )
        # Mock both Call 1 (tool_use) and Call 2 (text reply).
        responses = [
            _llm_response(
                text="Let's see how you did.",
                tool_uses=[{'name': 'record_answer',
                            'input': {'extracted_answer': 'B'}}],
            ),
            _llm_response(text='Nice — correct.'),
        ]
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            side_effect=responses,
        ):
            result = respond(session, 'I think the answer is B')
        ranswer = next(
            t for t in result['tool_calls'] if t['tool'] == 'record_answer'
        )
        self.assertTrue(ranswer['result']['recorded'])
        self.assertEqual(ranswer['result']['verdict'], 'correct')
        # Tutor turn carries the verdict in judge_outputs['grader']
        tutor_turn = session.turns.filter(role='tutor').latest('created_at')
        self.assertEqual(
            tutor_turn.judge_outputs['grader']['verdict'], 'correct',
        )
        self.assertEqual(
            tutor_turn.judge_outputs['grader']['reference_answer'], 'B',
        )
        self.assertEqual(
            tutor_turn.judge_outputs['grader']['question_type'], 'mcq',
        )
        # Correct verdict clears the slot.
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())

    def test_step_advances_after_correct_verdict(self, _mock_kb):
        """End-to-end: correct verdict → maybe_advance_step bumps step
        (competence threshold = 1).
        """
        from apps.tutoring.models import InFlightQuestion
        session, _qs = _make_session()
        InFlightQuestion.objects.create(
            session=session,
            question_text='Q?',
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
            _llm_response(text='Moving on.'),
        ]
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            side_effect=responses,
        ):
            result = respond(session, 'B')
        session.refresh_from_db()
        self.assertEqual(session.current_step_index, 1)
        self.assertTrue(result['step_advanced'])


@patch('apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class TrustTheLLMTest(DjangoTestCase):
    """Regression for the 2026-05-26 auto-fallback removal: if the LLM
    doesn't call record_answer, the engine MUST NOT auto-grade. Trust
    the LLM's tool-call decision — no safety-net grading.
    """

    def test_no_grading_when_llm_skips_record_answer(self, _mock_kb):
        session, _qs = _make_session()
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='Interesting answer.'),
        ):
            result = respond(session, 'B')
        # No record_answer should appear — the LLM didn't call it,
        # so no verdict is recorded. Trust the LLM.
        self.assertEqual(
            [t for t in result['tool_calls'] if t['tool'] == 'record_answer'],
            [],
        )


@patch('apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class LLMFailureTest(DjangoTestCase):

    def test_llm_returns_none_uses_fallback(self, _mock_kb):
        session, qs = _make_session()
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=None,
        ):
            result = respond(session, 'hi')
        self.assertTrue(result['fallback'])
        self.assertIn('Sorry', result['content'])
        # Student turn STILL persisted (audit log)
        self.assertEqual(
            session.turns.filter(role='student').count(), 1,
        )


@patch('apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class FigureCatalogTest(DjangoTestCase):

    def test_request_figure_dispatched(self, _mock_kb):
        session, qs = _make_session(with_step_media=True)
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(
                text='Look at this.',
                tool_uses=[{'name': 'request_figure',
                            'input': {'figure_id': 1}}],
            ),
        ):
            result = respond(session, 'show me')
        fig = next(
            t for t in result['tool_calls'] if t['tool'] == 'request_figure'
        )
        self.assertTrue(fig['result']['displayed'])
        self.assertEqual(fig['result']['url'], '/m/a.png')

    def test_invalid_figure_id_returns_error_no_crash(self, _mock_kb):
        session, qs = _make_session(with_step_media=True)
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(
                tool_uses=[{'name': 'request_figure',
                            'input': {'figure_id': 999}}],
            ),
        ):
            result = respond(session, 'show me')
        fig = next(
            t for t in result['tool_calls'] if t['tool'] == 'request_figure'
        )
        self.assertFalse(fig['result']['displayed'])
        # Engine did NOT crash
        self.assertFalse(result['fallback'])


@patch('apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class FiguresDisabledTest(DjangoTestCase):

    def test_engine_threads_figures_disabled_flag(self, _mock_kb):
        session, qs = _make_session(
            with_step_media=True, figures_enabled=False,
        )
        # When figures disabled, request_figure is not in the tools list
        # passed to the LLM. But if the LLM somehow still calls it, the
        # handler refuses.
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(
                tool_uses=[{'name': 'request_figure',
                            'input': {'figure_id': 1}}],
            ),
        ):
            result = respond(session, 'show me')
        fig = next(
            t for t in result['tool_calls'] if t['tool'] == 'request_figure'
        )
        self.assertFalse(fig['result']['displayed'])
        self.assertIn('disabled', fig['result']['error'].lower())


@patch('apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class StatelessPromptTest(DjangoTestCase):
    """The system prompt MUST be rebuilt from scratch every turn —
    same template each time, only rolling history changes. Verifies
    the M9 engine doesn't accidentally cache a prompt.
    """

    def test_system_prompt_rebuilt_each_turn(self, _mock_kb):
        session, qs = _make_session()
        with patch(
            'apps.tutoring.simple_tutor.engine._call_llm',
            return_value=_llm_response(text='reply 1'),
        ) as mock_call:
            respond(session, 'first')
            respond(session, 'second')
        self.assertEqual(mock_call.call_count, 2)
        # Each call should have system + tools + messages
        for call in mock_call.call_args_list:
            kwargs = call.kwargs
            self.assertIn('system_blocks', kwargs)
            self.assertIn('tools', kwargs)
            self.assertIn('messages', kwargs)
            # First call's first message should be the student's input.
            msgs = kwargs['messages']
            self.assertGreaterEqual(len(msgs), 1)
            self.assertEqual(msgs[0]['role'], 'user')


# ============================================================================
# Helper unit tests
# ============================================================================


class FigureCatalogBuilderTest(DjangoTestCase):

    def test_builds_from_step_media(self):
        step = SimpleNamespace(
            media={'images': [
                {'url': '/x/1.png', 'alt': 'one', 'caption': 'first'},
                {'url': '/x/2.png', 'alt': 'two', 'caption': 'second'},
            ]},
        )
        catalog = _build_figure_catalog(step)
        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog[0]['id'], 1)
        self.assertEqual(catalog[0]['url'], '/x/1.png')
        self.assertEqual(catalog[0]['alt_text'], 'one')
        self.assertEqual(catalog[1]['id'], 2)

    def test_skips_entries_without_url(self):
        step = SimpleNamespace(
            media={'images': [
                {'alt': 'no url'},   # skipped
                {'url': '/x/1.png', 'alt': 'one'},
            ]},
        )
        catalog = _build_figure_catalog(step)
        # The skipped entry leaves no gap — ids increment by index, so
        # the surviving entry gets id=2 (its index+1 in source list).
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]['id'], 2)

    def test_no_media_returns_empty(self):
        step = SimpleNamespace(media=None)
        self.assertEqual(_build_figure_catalog(step), [])

    def test_no_step_returns_empty(self):
        self.assertEqual(_build_figure_catalog(None), [])


class FiguresEnabledTest(DjangoTestCase):

    def test_returns_course_flag(self):
        session, _ = _make_session(figures_enabled=False)
        self.assertFalse(_figures_enabled(session))

    def test_defaults_true_when_missing(self):
        # Bare session-shaped object without unit/course
        bare = SimpleNamespace(lesson=None)
        self.assertTrue(_figures_enabled(bare))
