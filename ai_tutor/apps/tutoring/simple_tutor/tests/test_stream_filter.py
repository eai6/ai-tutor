"""Streaming safe-prefix tests (P1/P2 of memory/offline_streaming_plan.md).

The contract under test, in one line: **nothing reaches a student through the
stream that the batch filter pipeline would have removed.**

Streaming is an advisory preview — `respond()` still runs all eight
post-generation transforms on the complete text, and that is what persists.
So these tests are not about byte-equality with the batch result (which is
provably unachievable, see stream_filter's module docstring). They are about
the one-way guarantee: the preview may be SHORTER or LAGGING, never leakier.
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase as DjangoTestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.tutoring.models import InFlightQuestion, TutorSession
from ai_tutor.apps.tutoring.simple_tutor.stream_filter import (
    StreamGate, safe_cut_index, streaming_enabled,
)

User = get_user_model()


def _graded(verdict):
    """A tool_results list carrying a recorded grade."""
    return [{'tool': 'record_answer',
             'result': {'recorded': True, 'verdict': verdict}}]


class SafeCutIndexTest(SimpleTestCase):
    """safe_cut_index is pure: str in, int out. Every case here is a
    construct the batch filters treat as an indivisible unit, so cutting
    inside it would emit something the batch pass would have deleted."""

    def test_nothing_to_emit_before_a_boundary(self):
        self.assertEqual(safe_cut_index(''), 0)
        self.assertEqual(safe_cut_index('Nice work, that is'), 0)

    def test_cuts_after_a_completed_sentence(self):
        raw = 'Nice work. Now try this one. '
        self.assertEqual(raw[:safe_cut_index(raw)], 'Nice work. Now try this one.')

    def test_partial_sentence_is_withheld(self):
        """The head rule. _align_reply_polarity replaces the FIRST sentence
        when it contradicts the verdict; emitting half of it would let the
        student read 'That's right!' and watch it flip to 'Not quite.'"""
        raw = "That's right! You got"
        self.assertEqual(raw[:safe_cut_index(raw)], "That's right!")

    def test_holds_inside_an_unbalanced_paren(self):
        """_PAREN_RE spans sentence boundaries, so a cut at the inner '. '
        would emit half a parenthetical the batch pass drops whole."""
        raw = 'Good. (see step 2. then retry'
        self.assertEqual(raw[:safe_cut_index(raw)], 'Good.')

    def test_holds_inside_leaked_tool_json(self):
        """Ollama models leak tool calls as JSON text (_maybe_parse_text_tool_call).
        _is_tool_json_line drops the whole line; half of one must never ship."""
        raw = 'Sure. {"name": "pose_question", "arguments": {"stem": "What. '
        self.assertEqual(raw[:safe_cut_index(raw)], 'Sure.')

    def test_holds_inside_an_unclosed_tool_tag(self):
        """_XML_TOOL_TAG_RE needs both ends to strip the pair."""
        raw = 'Okay. <tool_call>pose_question. more'
        self.assertEqual(raw[:safe_cut_index(raw)], 'Okay.')

    def test_holds_inside_an_unclosed_code_fence(self):
        raw = 'Try this. ```\nx = 1. y = 2\n'
        self.assertEqual(raw[:safe_cut_index(raw)], 'Try this.')

    def test_reopened_construct_falls_back_to_the_earlier_cut(self):
        """A balanced paren followed by a newly-opened one must not let the
        cut advance past the open bracket."""
        raw = 'One. (a note.) Two. (unfinished. '
        self.assertEqual(raw[:safe_cut_index(raw)], 'One. (a note.) Two.')

    def test_is_monotonic_as_text_grows(self):
        """A snapshot must never shrink — transports render cumulative
        snapshots, so a shrinking cut would rewind the bubble mid-reply."""
        raw = 'One. Two (with a note. still open) three. Four. '
        seen = 0
        for i in range(1, len(raw) + 1):
            cut = safe_cut_index(raw[:i])
            self.assertGreaterEqual(cut, seen, f'cut went backwards at i={i}')
            seen = cut


class StreamingEnabledTest(SimpleTestCase):
    def test_default_is_off(self):
        """CLAUDE.md forbids SSE in production (Azure Container Apps cannot
        do chunked streaming). Only the offline kiosk opts in."""
        with patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('TUTOR_STREAMING', None)
            self.assertFalse(streaming_enabled())

    def test_opt_in_values(self):
        import os
        for val, want in (('1', True), ('true', True), ('0', False), ('', False)):
            with patch.dict(os.environ, {'TUTOR_STREAMING': val}):
                self.assertIs(streaming_enabled(), want)


class OllamaStreamReassemblyTest(SimpleTestCase):
    """_stream_chat must rebuild EXACTLY the dict the buffered call returns,
    so _adapt_ollama_response and everything downstream stays one code path."""

    def _client(self):
        from ai_tutor.apps.llm.client import OllamaClient
        cfg = SimpleNamespace(
            model_name='qwen3-4b-jetson', api_base='http://localhost:11434',
            temperature=0.3, max_tokens=1024,
        )
        return OllamaClient.__new__(OllamaClient), cfg

    def _run(self, lines, on_delta=None):
        from ai_tutor.apps.llm.client import OllamaClient
        client = OllamaClient.__new__(OllamaClient)
        body = '\n'.join(json.dumps(o) for o in lines)

        class _Resp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def raise_for_status(self_inner): pass
            def iter_lines(self_inner, decode_unicode=False):
                return iter(body.split('\n'))

        deltas = []
        cb = on_delta if on_delta is not None else deltas.append
        with patch('requests.post', return_value=_Resp()):
            data = client._stream_chat('http://x/api/chat', {}, cb)
        return data, deltas

    def test_content_deltas_are_joined_and_forwarded(self):
        data, deltas = self._run([
            {'message': {'role': 'assistant', 'content': 'Nice '}},
            {'message': {'role': 'assistant', 'content': 'work.'}},
            {'done': True, 'prompt_eval_count': 120, 'eval_count': 9,
             'eval_duration': 500000000, 'message': {}},
        ])
        self.assertEqual(deltas, ['Nice ', 'work.'])
        self.assertEqual(data['message']['content'], 'Nice work.')
        # The final line's counters must survive — the timing log and
        # AdaptedUsage both read them.
        self.assertEqual(data['prompt_eval_count'], 120)
        self.assertEqual(data['eval_count'], 9)

    def test_thinking_is_accumulated_but_never_streamed(self):
        """Hybrid Qwen3.5 templates emit a reasoning channel. Forwarding it
        would stream an internal monologue to a student."""
        data, deltas = self._run([
            {'message': {'thinking': 'The student said 12, which is wrong'}},
            {'message': {'content': 'Not quite.'}},
            {'done': True, 'message': {}},
        ])
        self.assertEqual(deltas, ['Not quite.'])
        self.assertNotIn('wrong', ''.join(deltas))
        self.assertEqual(data['message']['thinking'],
                         'The student said 12, which is wrong')

    def test_tool_calls_are_collected_for_the_adapter(self):
        data, _ = self._run([
            {'message': {'content': ''}},
            {'message': {'tool_calls': [
                {'function': {'name': 'pose_question',
                              'arguments': {'stem': 'What is 3x4?'}}}]}},
            {'done': True, 'message': {}},
        ])
        self.assertEqual(len(data['message']['tool_calls']), 1)
        # And the adapter must walk it identically to the buffered shape.
        from ai_tutor.apps.llm.client import _adapt_ollama_response
        adapted = _adapt_ollama_response(data, model_name='m', tools=[])
        self.assertEqual(adapted.stop_reason, 'tool_use')
        self.assertEqual(adapted.content[0].name, 'pose_question')

    def test_malformed_line_is_skipped_not_fatal(self):
        from ai_tutor.apps.llm.client import OllamaClient
        client = OllamaClient.__new__(OllamaClient)
        body = '\n'.join([
            json.dumps({'message': {'content': 'A.'}}),
            'this is not json',
            json.dumps({'done': True, 'eval_count': 2, 'message': {}}),
        ])

        class _Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def raise_for_status(s): pass
            def iter_lines(s, decode_unicode=False): return iter(body.split('\n'))

        with patch('requests.post', return_value=_Resp()):
            data = client._stream_chat('http://x', {}, lambda d: None)
        self.assertEqual(data['message']['content'], 'A.')

    def test_on_delta_exception_does_not_abort_generation(self):
        """A transport-side failure (client disconnect) must not lose the
        turn — the buffered result is still correct and still persists."""
        def _boom(_):
            raise RuntimeError('client went away')
        data, _ = self._run([
            {'message': {'content': 'Hello.'}},
            {'done': True, 'eval_count': 3, 'message': {}},
        ], on_delta=_boom)
        self.assertEqual(data['message']['content'], 'Hello.')

    def test_error_line_raises_so_the_retry_path_sees_it(self):
        with self.assertRaises(RuntimeError):
            self._run([{'error': 'model not found'}])


def _make_session(idx):
    inst = Institution.objects.create(name=f'S{idx}', slug=f's-{idx}')
    user = User.objects.create_user(username=f'u-{idx}', password='x')
    course = Course.objects.create(
        title=f'C{idx}', institution=inst, grade_level='S3', is_published=True)
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='L', objective='x', order_index=0, is_published=True)
    LessonStep.objects.create(
        lesson=lesson, teacher_script='t', question='', expected_answer='',
        phase='explore', order_index=0, enabling_objective='obj')
    return TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson, engine='simple',
        engine_state={})


# StreamGateRevealTest was removed with the reveal gate it covered
# (2026-08-06). It asserted that the stream is withheld when the reference
# answer cannot be resolved on a wrong-answer turn — a gate that existed only
# to keep _filter_reveals from silently no-opping mid-stream. With the filter
# gone the gate protected nothing and only cost the student a stalled stream.
# Leak prevention is the prompt's job now.


class StreamGateRotationTest(DjangoTestCase):
    """_rotation_index INCREMENTS a persisted counter and saves the session
    on every call. Re-running the polarity filter per chunk would rotate the
    acknowledgement between snapshots — the student watches 'Exactly!' become
    'Nice work!' — and issue a DB write per chunk."""

    def setUp(self):
        self.session = _make_session(2)

    def test_ack_is_stable_across_snapshots(self):
        emitted = []
        gate = StreamGate(
            session=self.session, tool_results=_graded('correct'),
            family='qwen', emit=emitted.append,
        )
        # A negative opener on a CORRECT verdict — polarity alignment fires.
        for ch in 'Not quite. Actually that works out. One more? ':
            gate.feed(ch)
        self.assertTrue(emitted)
        first_words = {s.split('.')[0].split('!')[0] for s in emitted}
        self.assertEqual(
            len(first_words), 1,
            f'acknowledgement changed between snapshots: {first_words}')

    def test_rotation_index_is_consumed_exactly_once(self):
        gate = StreamGate(
            session=self.session, tool_results=_graded('correct'),
            family='qwen', emit=lambda s: None,
        )
        with patch('ai_tutor.apps.tutoring.simple_tutor.engine._rotation_index',
                   return_value=0) as m:
            for ch in 'Not quite. Actually that works. Next one. ':
                gate.feed(ch)
            self.assertEqual(m.call_count, 1, 'rotation counter advanced per chunk')

    def test_anthropic_family_skips_the_oss_nets(self):
        """respond() gates these filters on family; the gate must match, or
        production behaviour would differ between streamed and buffered."""
        emitted = []
        gate = StreamGate(
            session=self.session, tool_results=_graded('correct'),
            family='anthropic', emit=emitted.append,
        )
        for ch in 'Not quite. Actually correct. ':
            gate.feed(ch)
        self.assertTrue(emitted[-1].startswith('Not quite.'))


class StreamGateRetryTest(DjangoTestCase):
    """_invoke_with_transient_retry replays the whole generation on a
    transient error, so the gate must drop the dead attempt's text."""

    def setUp(self):
        self.session = _make_session(3)

    def test_begin_attempt_discards_the_partial_attempt(self):
        emitted = []
        gate = StreamGate(
            session=self.session, tool_results=[], family='qwen',
            emit=emitted.append,
        )
        gate.feed('Half a repl')
        gate.begin_attempt()
        gate.feed('A clean second attempt. ')
        self.assertEqual(emitted, ['A clean second attempt.'])

    def test_gate_is_the_callable_and_exposes_the_reset_hook(self):
        """_call_llm discovers begin_attempt on the on_delta object itself."""
        gate = StreamGate(session=self.session, tool_results=[],
                          family='qwen', emit=lambda s: None)
        self.assertTrue(callable(gate))
        self.assertTrue(callable(getattr(gate, 'begin_attempt', None)))


def _llm_response(*, text='', tool_uses=None):
    blocks = []
    if text:
        blocks.append(SimpleNamespace(type='text', text=text))
    for tu in (tool_uses or []):
        blocks.append(SimpleNamespace(
            type='tool_use', name=tu['name'], input=tu.get('input', {})))
    return SimpleNamespace(content=blocks)


@patch('ai_tutor.apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class StreamedVsBufferedParityTest(DjangoTestCase):
    """The central claim of the design: turning streaming ON must not change
    a single byte of what gets persisted. The stream is a preview of the same
    turn, not a different turn.

    Runs under TUTOR_MODEL_OVERRIDE so `_family` resolves to 'qwen' — the
    kiosk's actual configuration. Without it `_family` is None (the Anthropic
    production path), the OSS nets are correctly skipped, and a parity
    assertion would pass trivially because no filter ran on either side.
    """

    KIOSK_MODEL = 'local_ollama/qwen3-4b-jetson'
    LEAKY = ("Exactly right! The answer is A, since newtons measure force. "
             "Ready for the next one?")

    def _run(self, session, *, stream):
        """Drive one graded-incorrect turn, optionally streaming Call 2."""
        import os
        from ai_tutor.apps.tutoring.simple_tutor import engine as _eng

        responses = [
            _llm_response(text='Let me check that.',
                          tool_uses=[{'name': 'record_answer',
                                      'input': {'extracted_answer': 'B'}}]),
            _llm_response(text=self.LEAKY),
        ]
        snapshots = []

        def _fake_call(*, system_blocks, tools, messages, tool_choice=None,
                       on_delta=None):
            resp = responses.pop(0)
            if on_delta is not None and not responses:
                # Call 2: hand the text over a character at a time, the way
                # a real decode arrives.
                for ch in self.LEAKY:
                    on_delta(ch)
            return resp

        # Pinned to two-call: this fixture puts the student-visible text in
        # Call 2, which is exactly the path under test. Left on 'auto' it
        # would resolve to one-call for a qwen family and Call 2 would be
        # skipped, so the fixture's reply would never be generated.
        with patch.dict(os.environ,
                        {'TUTOR_MODEL_OVERRIDE': self.KIOSK_MODEL,
                         'TUTOR_CALL_MODE': 'two'}):
            with patch.object(_eng, '_call_llm', side_effect=_fake_call):
                with patch.object(_eng, '_load_current_step',
                                  return_value=None):
                    out = _eng.respond(
                        session, 'is it B?',
                        on_delta=(snapshots.append if stream else None),
                    )
        return out, snapshots

    def _slot(self, session):
        InFlightQuestion.objects.create(
            session=session, question_text='Unit of force?',
            question_type='mcq', options=['N', 'J', 'W', 'Pa'],
            reference_answer='A', source='inline_authored')

    def test_persisted_text_is_identical_with_and_without_streaming(self, _kb):
        s1 = _make_session(10)
        self._slot(s1)
        buffered, _ = self._run(s1, stream=False)

        s2 = _make_session(11)
        self._slot(s2)
        streamed, _snapshots = self._run(s2, stream=True)

        self.assertEqual(buffered['content'], streamed['content'])

    def test_an_unsafe_reply_streams_only_its_corrected_form(self, _kb):
        """LEAKY is unsafe end to end — a false affirmation on a wrong answer,
        followed by the reference itself. What streams must be the CORRECTED
        text: the opener replaced with a verdict-consistent acknowledgement
        and the reveal sentence gone. The student never sees the model's
        original claim, not even briefly.

        This is also the regression test for the Call-1 handover. When the
        gate's buffer was not reset between Call 1's prose and Call 2's
        stream, the opener was no longer at position 0, `_align_reply_polarity`
        took its mid-reply branch instead of its opener branch, and the
        streamed text differed from the persisted text."""
        session = _make_session(13)
        self._slot(session)
        _out, snapshots = self._run(session, stream=True)
        self.assertTrue(snapshots)
        streamed = snapshots[-1]
        self.assertNotIn('Exactly right', streamed)
        self.assertNotIn('answer is A', streamed)
        # Positively assert the correction, not just the absence of the leak.
        self.assertIn('Not quite', streamed)

    def test_a_safe_reply_does_stream(self, _kb):
        """The counterpart: with nothing to redact, snapshots flow. Without
        this the suite could pass while streaming was silently dead."""
        session = _make_session(14)
        self._slot(session)
        with patch.object(type(self), 'LEAKY',
                          "Not quite — have another look. "
                          "What happens over a full turn? "):
            _out, snapshots = self._run(session, stream=True)
        self.assertTrue(snapshots, 'a benign reply produced no snapshots')
        self.assertIn('Not quite', snapshots[-1])

    def test_the_stream_never_showed_what_the_batch_pass_redacted(self, _kb):
        session = _make_session(12)
        self._slot(session)
        out, snapshots = self._run(session, stream=True)
        # The model claimed "Exactly right!" on a WRONG answer and stated the
        # reference. Neither may survive — in the final text or in any frame
        # the student saw on the way there.
        for snap in snapshots + [out['content']]:
            self.assertNotIn('The answer is A', snap)
            self.assertNotIn('Exactly right', snap)


@patch('ai_tutor.apps.tutoring.simple_tutor.engine._retrieve_kb', return_value=[])
class Call1FlushGuardTest(DjangoTestCase):
    """Call 1's prose is flushed once the verdict is known, because on local
    models it is usually the ONLY student-visible text (Call 2 is often a
    silent tool repair). But flushing it while a grade is still PENDING
    streams an unfiltered opinion about an answer nobody has graded.

    Observed on the Jetson 2026-07-29: student answered correctly, Call 1
    wrote "Yes — 360° is the total..." and skipped record_answer, Call 2's
    repair graded it incorrect, and the batch pass rewrote the opener. The
    student read "Yes" and watched it become "Not this time."
    """

    KIOSK_MODEL = 'local_ollama/qwen3-4b-jetson'

    def _run(self, session, *, call1_text, call1_grades):
        import os
        from ai_tutor.apps.tutoring.simple_tutor import engine as _eng

        tool_uses = ([{'name': 'record_answer', 'input': {'extracted_answer': 'B'}}]
                     if call1_grades else [])
        responses = [
            _llm_response(text=call1_text, tool_uses=tool_uses),
            _llm_response(text=''),      # Call 2: silent repair, no prose
        ]
        snapshots = []

        def _fake_call(*, system_blocks, tools, messages, tool_choice=None,
                       on_delta=None):
            return responses.pop(0) if responses else _llm_response(text='')

        with patch.dict(os.environ, {'TUTOR_MODEL_OVERRIDE': self.KIOSK_MODEL}):
            with patch.object(_eng, '_call_llm', side_effect=_fake_call):
                with patch.object(_eng, '_load_current_step', return_value=None):
                    _eng.respond(session, 'is it 360?',
                                 on_delta=snapshots.append)
        return snapshots

    def _slot(self, session):
        InFlightQuestion.objects.create(
            session=session, question_text='Unit of force?',
            question_type='mcq', options=['N', 'J', 'W', 'Pa'],
            reference_answer='A', source='inline_authored')

    def test_pending_grade_withholds_call1_prose(self, _kb):
        """A slot is open and Call 1 did NOT grade — the verdict arrives later,
        so nothing may stream yet."""
        session = _make_session(20)
        self._slot(session)
        snapshots = self._run(
            session, call1_text='Yes — 360 is the total. Well done. ',
            call1_grades=False)
        self.assertEqual(
            snapshots, [],
            'streamed an affirmation while the grade was still pending')

    def test_known_verdict_allows_the_flush(self, _kb):
        """Call 1 graded AND wrote prose — every filter has its inputs, so
        the text may stream immediately."""
        session = _make_session(21)
        self._slot(session)
        snapshots = self._run(
            session, call1_text='Not quite — have another look. Try again. ',
            call1_grades=True)
        self.assertTrue(snapshots, 'a fully-graded Call 1 should stream')

    def test_no_slot_means_no_grade_is_expected(self, _kb):
        """A teaching/posing turn with nothing in flight cannot produce a
        verdict, so both verdict-dependent filters are no-ops and Call 1's
        prose is safe to show at once."""
        session = _make_session(22)
        snapshots = self._run(
            session,
            call1_text='Angles around a point add to 360. What is x? ',
            call1_grades=False)
        self.assertTrue(snapshots, 'a no-grade turn should stream Call 1')


class StreamGateScrubTest(DjangoTestCase):
    def setUp(self):
        self.session = _make_session(4)

    def test_leaked_tool_json_line_never_reaches_the_student(self):
        emitted = []
        gate = StreamGate(session=self.session, tool_results=[],
                          family='qwen', emit=emitted.append)
        raw = ('Here we go.\n'
               '{"name": "pose_question", "arguments": {"stem": "What is 3x4?"}}\n'
               'What is 3 times 4? ')
        for ch in raw:
            gate.feed(ch)
        for snap in emitted:
            self.assertNotIn('pose_question', snap)
            self.assertNotIn('"arguments"', snap)
        self.assertIn('What is 3 times 4?', emitted[-1])


class CallModeTest(SimpleTestCase):
    """One-call vs two-call. Two-call is the original design: Call 1 picks
    tools, the platform grades, Call 2 writes the reply KNOWING the verdict.
    One-call accepts Call 1's prose and skips Call 2 — halving a turn on a
    box where each call is 8-10s, at the cost of a reply written before the
    grade exists."""

    def test_auto_keeps_anthropic_on_two_calls(self):
        import os
        from ai_tutor.apps.tutoring.simple_tutor.engine import _call_mode
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TUTOR_CALL_MODE', None)
            self.assertEqual(_call_mode('anthropic'), 'two')
            self.assertEqual(_call_mode(None), 'two',
                             'production (family None) must not change')

    def test_auto_puts_local_families_on_one_call(self):
        import os
        from ai_tutor.apps.tutoring.simple_tutor.engine import _call_mode
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TUTOR_CALL_MODE', None)
            self.assertEqual(_call_mode('qwen'), 'one')

    def test_explicit_override_wins_both_ways(self):
        import os
        from ai_tutor.apps.tutoring.simple_tutor.engine import _call_mode
        with patch.dict(os.environ, {'TUTOR_CALL_MODE': 'two'}):
            self.assertEqual(_call_mode('qwen'), 'two')
        with patch.dict(os.environ, {'TUTOR_CALL_MODE': 'one'}):
            self.assertEqual(_call_mode('anthropic'), 'one')

    def test_unknown_value_falls_back_to_auto(self):
        import os
        from ai_tutor.apps.tutoring.simple_tutor.engine import _call_mode
        with patch.dict(os.environ, {'TUTOR_CALL_MODE': 'banana'}):
            self.assertEqual(_call_mode('anthropic'), 'two')
            self.assertEqual(_call_mode('qwen'), 'one')
