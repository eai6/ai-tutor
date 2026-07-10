"""Tool-protocol compliance regressions (Eval-3 bottleneck analysis).

The multi-turn sweep showed that lesson progression is gated on a
two-tool-per-turn protocol (pose_question registers the question,
record_answer submits the student's reply), and that non-Anthropic models
break it in three distinct ways:

  RC-3  pose_question was dispatched without a cap, and every call REPLACES
        the InFlightQuestion row. gemini-3.1-pro emitted up to 139 pose calls
        in a single turn: the student read question #1 while the grader held
        question #139, so correct answers were marked wrong (33% correct-grade
        rate against Anthropic's 87%).

  RC-2  The tool_choice override that guarantees a gradable slot was hard-gated
        to ``_family == 'gemini'``. Every Qwen model narrated its questions as
        prose instead, so no slot was ever created.

  RC-1  OllamaClient accepts tool_choice "for signature parity but NOT
        forwarded", so local models cannot be forced at all. They need a
        server-side repair call instead.

See offline_eval/EVAL3_MULTITURN_BOTTLENECK_ANALYSIS_12MODELS.docx.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase as DjangoTestCase

from apps.tutoring.models import InFlightQuestion
from apps.tutoring.simple_tutor import engine
from apps.tutoring.simple_tutor.tests.test_tools import _make_session


def _block(name, **inp):
    return SimpleNamespace(type='tool_use', id=f'tu_{name}_{id(inp)}',
                           name=name, input=inp)


def _response(*blocks, text=''):
    content = ([SimpleNamespace(type='text', text=text)] if text else []) + list(blocks)
    return SimpleNamespace(content=content)


class DuplicatePoseQuestionTests(SimpleTestCase):
    """RC-3 — only the FIRST pose_question of a turn may write the slot."""

    def _dispatch(self, response):
        with patch('apps.tutoring.simple_tutor.tools.handle_pose_question') as pose, \
             patch('apps.tutoring.simple_tutor.tools.handle_record_answer') as rec:
            pose.return_value = {'posed': True, 'question_type': 'mcq'}
            rec.return_value = {'recorded': True, 'verdict': 'correct'}
            text, results, called_rec = engine._dispatch_tools(
                session=SimpleNamespace(), response=response, figure_catalog=[],
            )
        return text, results, called_rec, pose, rec

    def test_single_pose_dispatches_normally(self):
        r = _response(_block('pose_question', question_text='What is 2+2?',
                             question_type='short', reference_answer='4', source='llm'))
        _, results, _, pose, _ = self._dispatch(r)
        self.assertEqual(pose.call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['result'].get('posed'))

    def test_duplicate_pose_calls_dispatch_only_once(self):
        """139 poses in one turn must write the slot exactly once."""
        r = _response(*[
            _block('pose_question', question_text=f'Q{i}', question_type='short',
                   reference_answer=str(i), source='llm')
            for i in range(5)
        ])
        _, results, _, pose, _ = self._dispatch(r)
        self.assertEqual(pose.call_count, 1, 'slot must be written exactly once')
        # The FIRST question is the one the student reads, so it wins.
        self.assertEqual(pose.call_args.kwargs['question_text'], 'Q0')

    def test_duplicate_pose_calls_still_return_a_tool_result_each(self):
        """Every tool_use block needs a paired tool_result or the Anthropic
        API rejects the Call-2 message. Skipped poses get a skip result."""
        blocks = [
            _block('pose_question', question_text=f'Q{i}', question_type='short',
                   reference_answer=str(i), source='llm')
            for i in range(3)
        ]
        r = _response(*blocks)
        _, results, _, _, _ = self._dispatch(r)
        self.assertEqual(len(results), 3)
        skipped = [x for x in results if x['result'].get('skipped')]
        self.assertEqual(len(skipped), 2)
        for s in skipped:
            self.assertFalse(s['result'].get('posed'))
            self.assertIn('duplicate', (s['result'].get('skip_reason') or '').lower())

        paired = engine._build_tool_result_content(blocks, results)
        self.assertEqual(len(paired), 3, 'tool_use/tool_result pairing must hold')

    def test_record_answer_after_duplicate_poses_reads_the_first_slot(self):
        r = _response(
            _block('record_answer', extracted_answer='4'),
            _block('pose_question', question_text='Q0', question_type='short',
                   reference_answer='4', source='llm'),
            _block('pose_question', question_text='Q1', question_type='short',
                   reference_answer='9', source='llm'),
        )
        _, _, called_rec, pose, rec = self._dispatch(r)
        self.assertEqual(pose.call_count, 1)
        self.assertEqual(pose.call_args.kwargs['question_text'], 'Q0')
        self.assertTrue(called_rec)


class ForcePoseGateTests(SimpleTestCase):
    """RC-2 — forcing must not be gated to a single vendor."""

    def test_production_never_forces(self):
        # No model profile → family is None → production path, unchanged.
        self.assertFalse(engine._should_force_pose(None, 'POSE', 'answer_or_other'))

    def test_anthropic_is_not_forced(self):
        """Anthropic complies natively and is the benchmark control; leave it."""
        self.assertFalse(engine._should_force_pose('anthropic', 'POSE', 'answer_or_other'))

    def test_gemini_still_forced(self):
        self.assertTrue(engine._should_force_pose('gemini', 'POSE', 'answer_or_other'))

    def test_qwen_now_forced(self):
        self.assertTrue(engine._should_force_pose('qwen', 'POSE', 'answer_or_other'))

    def test_other_open_families_forced(self):
        for fam in ('deepseek', 'glm', 'kimi', 'mistral', 'grok', 'llama'):
            self.assertTrue(engine._should_force_pose(fam, 'POSE', 'answer_or_other'), fam)

    def test_only_pose_mode_is_forced(self):
        for mode in ('GRADE', 'REMEDIATION'):
            self.assertFalse(engine._should_force_pose('qwen', mode, 'answer_or_other'))

    def test_conversational_intents_are_never_forced(self):
        for intent in ('clarification', 'pushback', 'off_topic'):
            self.assertFalse(engine._should_force_pose('qwen', 'POSE', intent))


class MissingForcedToolTests(SimpleTestCase):
    def test_pose_missing_when_forced_and_not_registered(self):
        self.assertEqual(engine._missing_forced_tool(True, False, []), 'pose_question')

    def test_pose_not_missing_once_registered(self):
        results = [{'tool': 'pose_question', 'result': {'posed': True}}]
        self.assertIsNone(engine._missing_forced_tool(True, False, results))

    def test_a_skipped_duplicate_pose_does_not_count_as_registered(self):
        results = [{'tool': 'pose_question', 'result': {'posed': False, 'skipped': True}}]
        self.assertEqual(engine._missing_forced_tool(True, False, results), 'pose_question')

    def test_record_missing_when_forced_and_not_called(self):
        self.assertEqual(engine._missing_forced_tool(False, True, []), 'record_answer')

    def test_record_not_missing_even_when_it_recorded_nothing(self):
        """An empty extracted_answer means the model judged 'not an answer'.
        It made the call — that is all forcing is trying to elicit."""
        results = [{'tool': 'record_answer',
                    'result': {'recorded': False, 'error': 'extracted_answer is empty'}}]
        self.assertIsNone(engine._missing_forced_tool(False, True, results))

    def test_nothing_missing_when_nothing_forced(self):
        self.assertIsNone(engine._missing_forced_tool(False, False, []))


class Call2FoldTests(SimpleTestCase):
    """The repair rides on Call 2 instead of costing a call of its own."""

    TOOLS = [{'name': 'pose_question'}, {'name': 'record_answer'}, {'name': 'request_figure'}]

    def _run(self, response, missing_tool, call2):
        tool_results = []
        with patch.object(engine, '_call_llm', return_value=call2) as call, \
             patch('apps.tutoring.simple_tutor.tools.handle_pose_question') as pose, \
             patch('apps.tutoring.simple_tutor.tools.handle_record_answer') as rec:
            pose.return_value = {'posed': True, 'question_type': 'short'}
            rec.return_value = {'recorded': True, 'verdict': 'correct'}
            text, used = engine._run_second_call(
                session=SimpleNamespace(), system_blocks=[], tools=self.TOOLS,
                messages=[{'role': 'user', 'content': 'hi'}], response=response,
                text_reply_1='prose reply', tool_results=tool_results,
                figure_catalog=[], missing_tool=missing_tool, user_input='360',
            )
        return text, used, call, pose, rec

    def test_no_second_call_when_nothing_fired_and_nothing_missing(self):
        """Production / Anthropic conversational turn — unchanged, 1 call."""
        text, used, call, _, _ = self._run(_response(text='just talking'), None, None)
        self.assertEqual(call.call_count, 0)
        self.assertFalse(used)
        self.assertEqual(text, 'prose reply')

    def test_repair_becomes_the_second_call_when_call1_fired_no_tool(self):
        """The expensive case in the sweep: 236 of qwen2.5:72b's repairs land
        here, and cost nothing extra because there was no Call 2 to begin with."""
        call2 = _response(_block('pose_question', question_text='Q?', question_type='short',
                                 reference_answer='1', source='llm'), text='Try this: Q?')
        text, used, call, pose, _ = self._run(_response(text='prose question?'),
                                              'pose_question', call2)
        self.assertEqual(call.call_count, 1, 'exactly one further LLM call — never two')
        self.assertTrue(used)
        self.assertEqual(pose.call_count, 1)
        self.assertEqual(text, 'Try this: Q?')
        # Only the missing tool is exposed, so any tool call is the right one.
        self.assertEqual([t['name'] for t in call.call_args.kwargs['tools']], ['pose_question'])
        self.assertEqual(call.call_args.kwargs['tool_choice'],
                         {'type': 'tool', 'name': 'pose_question'})

    def test_repair_rides_along_when_call1_fired_a_different_tool(self):
        """Previously 3 calls (Call 1 + repair + Call 2). Now 2."""
        call1 = _response(_block('record_answer', extracted_answer='360'))
        call2 = _response(_block('pose_question', question_text='Next?', question_type='short',
                                 reference_answer='2', source='llm'), text='Right. Next?')
        # Call 1's record_answer must be dispatched first so a tool_result exists.
        with patch('apps.tutoring.simple_tutor.tools.handle_record_answer') as rec:
            rec.return_value = {'recorded': True, 'verdict': 'correct'}
            _, tool_results, _ = engine._dispatch_tools(
                session=SimpleNamespace(), response=call1, figure_catalog=[])
        with patch.object(engine, '_call_llm', return_value=call2) as call, \
             patch('apps.tutoring.simple_tutor.tools.handle_pose_question') as pose:
            pose.return_value = {'posed': True, 'question_type': 'short'}
            messages = [{'role': 'user', 'content': 'hi'}]
            text, used = engine._run_second_call(
                session=SimpleNamespace(), system_blocks=[], tools=self.TOOLS,
                messages=messages, response=call1, text_reply_1='',
                tool_results=tool_results, figure_catalog=[],
                missing_tool='pose_question', user_input='360',
            )
        self.assertEqual(call.call_count, 1, 'the repair must not add a third call')
        self.assertEqual(pose.call_count, 1)
        self.assertEqual(text, 'Right. Next?')
        # The repair instruction rides in the SAME user message as the tool
        # results — Gemini rejects two consecutive user turns.
        roles = [m['role'] for m in messages]
        self.assertEqual(roles, ['user', 'assistant', 'user'])
        blocks = messages[-1]['content']
        self.assertEqual(blocks[0]['type'], 'tool_result')
        self.assertEqual(blocks[-1]['type'], 'text')
        self.assertIn('pose_question', blocks[-1]['text'])

    def test_plain_second_call_is_unforced(self):
        call1 = _response(_block('record_answer', extracted_answer='360'))
        with patch('apps.tutoring.simple_tutor.tools.handle_record_answer') as rec:
            rec.return_value = {'recorded': True, 'verdict': 'correct'}
            _, tool_results, _ = engine._dispatch_tools(
                session=SimpleNamespace(), response=call1, figure_catalog=[])
        with patch.object(engine, '_call_llm', return_value=_response(text='Nice.')) as call:
            engine._run_second_call(
                session=SimpleNamespace(), system_blocks=[], tools=self.TOOLS,
                messages=[{'role': 'user', 'content': 'hi'}], response=call1,
                text_reply_1='', tool_results=tool_results, figure_catalog=[],
                missing_tool=None, user_input='360',
            )
        self.assertIsNone(call.call_args.kwargs['tool_choice'])
        self.assertIs(call.call_args.kwargs['tools'], self.TOOLS)

    def test_dead_second_call_keeps_call1_text(self):
        text, used, _, _, _ = self._run(_response(text='x'), 'pose_question', None)
        self.assertEqual(text, 'prose reply')
        self.assertFalse(used)


class Call2PlanTests(SimpleTestCase):
    ALL = [{'name': n} for n in ('pose_question', 'record_answer', 'request_figure')]

    def test_unforced_call2_is_unchanged(self):
        tools, choice = engine._plan_call2(self.ALL, None)
        self.assertIs(tools, self.ALL)
        self.assertIsNone(choice)

    def test_missing_tool_is_named_and_isolated(self):
        for name in ('pose_question', 'record_answer'):
            tools, choice = engine._plan_call2(self.ALL, name)
            self.assertEqual([t['name'] for t in tools], [name])
            self.assertEqual(choice, {'type': 'tool', 'name': name})


class SlotIntegrityTests(DjangoTestCase):
    """The bug that actually cost the score, end to end against the real
    InFlightQuestion row and the real grader — no handler mocks.

    gemini-3.1-pro emitted 139 pose_question calls in one turn. The student
    reads the FIRST question; every later call replaced the row, so the grader
    scored their answer against the LAST one and returned 'incorrect'.
    """

    def _parallel_pose_response(self):
        return _response(
            _block('pose_question', question_text='How many degrees around a point?',
                   question_type='short_numeric', reference_answer='360', source='inline_authored'),
            _block('pose_question', question_text='What is the sum of angles in a triangle?',
                   question_type='short_numeric', reference_answer='180', source='inline_authored'),
            _block('pose_question', question_text='How many sides has a pentagon?',
                   question_type='short_numeric', reference_answer='5', source='inline_authored'),
            text='How many degrees around a point?',
        )

    def test_slot_holds_the_question_the_student_read(self):
        session, _ = _make_session()
        engine._dispatch_tools(session=session, response=self._parallel_pose_response(),
                               figure_catalog=[])
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.question_text, 'How many degrees around a point?')
        self.assertEqual(slot.reference_answer, '360')
        self.assertEqual(InFlightQuestion.objects.filter(session=session).count(), 1)

    def test_correct_answer_to_the_visible_question_grades_correct(self):
        """Before the cap this returned verdict='incorrect' — 360 was scored
        against the reference answer '5' of the last pose call."""
        from apps.tutoring.simple_tutor.tools import handle_record_answer
        session, _ = _make_session()
        engine._dispatch_tools(session=session, response=self._parallel_pose_response(),
                               figure_catalog=[])
        result = handle_record_answer(session, extracted_answer='360')
        self.assertTrue(result['recorded'])
        self.assertEqual(result['verdict'], 'correct')


class ForceGradeGateTests(SimpleTestCase):
    """RC-1 — GRADE turns must also guarantee a tool call.

    GRADE mode means only 'a question is in flight', NOT 'the student
    answered'. So we force *some* tool, never a named one, and the model
    reports a non-answer via an empty extracted_answer.
    """

    def test_production_never_forces(self):
        self.assertFalse(engine._should_force_grade(None, 'GRADE', 'answer_or_other'))

    def test_anthropic_is_not_forced(self):
        self.assertFalse(engine._should_force_grade('anthropic', 'GRADE', 'answer_or_other'))

    def test_non_anthropic_families_are_forced(self):
        for fam in ('qwen', 'gemini', 'deepseek', 'glm'):
            self.assertTrue(engine._should_force_grade(fam, 'GRADE', 'answer_or_other'), fam)

    def test_remediation_grade_is_forced(self):
        self.assertTrue(engine._should_force_grade('qwen', 'REMEDIATION+GRADE', 'answer_or_other'))

    def test_pose_and_remediation_only_are_not_grade_turns(self):
        for mode in ('POSE', 'REMEDIATION'):
            self.assertFalse(engine._should_force_grade('qwen', mode, 'answer_or_other'))

    def test_conversational_intents_are_never_forced(self):
        for intent in ('clarification', 'pushback', 'off_topic'):
            self.assertFalse(engine._should_force_grade('qwen', 'GRADE', intent))

    def test_forced_grade_never_names_a_single_tool(self):
        """A named force suppresses the other tools, killing the combined turn
        (grade + pose in one reply) that best predicts pass rate."""
        self.assertEqual(sorted(engine._GRADE_FORCED_TOOLS),
                         ['pose_question', 'record_answer'])
        self.assertNotIn('advance_step', engine._GRADE_FORCED_TOOLS,
                         'a forced ANY must not let the model skip a step')


class Call1PlanTests(SimpleTestCase):
    ALL = [{'name': n} for n in
           ('pose_question', 'record_answer', 'request_figure',
            'advance_step', 'redirect_off_topic')]

    def test_unforced_call_is_unchanged(self):
        """Production and Anthropic must see exactly the old call."""
        tools, choice = engine._plan_call1(self.ALL, False, False)
        self.assertIs(tools, self.ALL)
        self.assertIsNone(choice)

    def test_forced_pose_names_the_tool_and_keeps_the_full_list(self):
        tools, choice = engine._plan_call1(self.ALL, True, False)
        self.assertIs(tools, self.ALL)
        self.assertEqual(choice, {'type': 'tool', 'name': 'pose_question'})

    def test_forced_grade_uses_any_and_narrows_the_menu(self):
        tools, choice = engine._plan_call1(self.ALL, False, True)
        self.assertEqual(choice, {'type': 'any'},
                         'must not name a tool — that would forbid the combined turn')
        names = sorted(t['name'] for t in tools)
        self.assertEqual(names, ['pose_question', 'record_answer'])
        self.assertNotIn('advance_step', names,
                         'a forced ANY could otherwise skip a lesson step')

    def test_forced_grade_falls_back_when_the_menu_is_missing(self):
        odd = [{'name': 'request_figure'}]
        tools, choice = engine._plan_call1(odd, False, True)
        self.assertIs(tools, odd)
        self.assertEqual(choice, {'type': 'any'})


class DuplicateRecordAnswerTests(SimpleTestCase):
    """Forcing makes parallel duplicates likelier; each record_answer grades,
    bumps attempt_count and can clear the slot."""

    def test_duplicate_record_answer_grades_once(self):
        r = _response(
            _block('record_answer', extracted_answer='360'),
            _block('record_answer', extracted_answer='360'),
            _block('record_answer', extracted_answer='180'),
        )
        with patch('apps.tutoring.simple_tutor.tools.handle_record_answer') as rec:
            rec.return_value = {'recorded': True, 'verdict': 'correct'}
            _, results, called = engine._dispatch_tools(
                session=SimpleNamespace(), response=r, figure_catalog=[])
        self.assertEqual(rec.call_count, 1)
        self.assertTrue(called)
        self.assertEqual(len(results), 3)
        skipped = [x for x in results if x['result'].get('skipped')]
        self.assertEqual(len(skipped), 2)
        self.assertIn('duplicate', skipped[0]['result']['skip_reason'])

    def test_combined_turn_still_allows_one_of_each(self):
        r = _response(
            _block('record_answer', extracted_answer='360'),
            _block('pose_question', question_text='Next?', question_type='short',
                   reference_answer='1', source='llm'),
        )
        with patch('apps.tutoring.simple_tutor.tools.handle_record_answer') as rec, \
             patch('apps.tutoring.simple_tutor.tools.handle_pose_question') as pose:
            rec.return_value = {'recorded': True, 'verdict': 'correct'}
            pose.return_value = {'posed': True, 'question_type': 'short'}
            _, results, _ = engine._dispatch_tools(
                session=SimpleNamespace(), response=r, figure_catalog=[])
        self.assertEqual(rec.call_count, 1)
        self.assertEqual(pose.call_count, 1)
        self.assertFalse(any(x['result'].get('skipped') for x in results))


class EmptyAnswerEscapeIsSideEffectFree(DjangoTestCase):
    """The escape hatch is only safe because handle_record_answer returns
    before it touches the slot, the verdict, or attempt_count."""

    def test_empty_answer_leaves_the_slot_untouched(self):
        from apps.tutoring.simple_tutor.tools import handle_record_answer
        session, _ = _make_session()
        engine._dispatch_tools(
            session=session, figure_catalog=[],
            response=_response(_block(
                'pose_question', question_text='Degrees around a point?',
                question_type='short_numeric', reference_answer='360',
                source='inline_authored')),
        )
        before = InFlightQuestion.objects.get(session=session)
        result = handle_record_answer(session, extracted_answer='   ')
        self.assertFalse(result['recorded'])
        after = InFlightQuestion.objects.get(session=session)
        self.assertEqual(after.attempt_count, before.attempt_count)
        self.assertEqual(after.question_text, before.question_text)
        self.assertEqual(after.reference_answer, '360')


class ToolNameNormalisationTests(SimpleTestCase):
    """RC-7 — the text-recovery parser emitted ' record_answer' (leading
    space), 'requestFigure' and a literal 'tool_name' placeholder."""

    def test_whitespace_padded_name_is_normalised(self):
        r = _response(_block(' record_answer ', extracted_answer='4'))
        with patch('apps.tutoring.simple_tutor.tools.handle_record_answer') as rec:
            rec.return_value = {'recorded': True, 'verdict': 'correct'}
            _, results, called, = engine._dispatch_tools(
                session=SimpleNamespace(), response=r, figure_catalog=[])
        self.assertTrue(called)
        self.assertEqual(rec.call_count, 1)
        self.assertEqual(results[0]['tool'], 'record_answer')

    def test_camelcase_name_is_normalised(self):
        r = _response(_block('requestFigure', figure_id=1))
        with patch('apps.tutoring.simple_tutor.tools.handle_request_figure') as fig:
            fig.return_value = {'displayed': True}
            _, results, _ = engine._dispatch_tools(
                session=SimpleNamespace(), response=r, figure_catalog=[{'id': 1}])
        self.assertEqual(fig.call_count, 1)
        self.assertEqual(results[0]['tool'], 'request_figure')

    def test_placeholder_name_is_rejected_not_dispatched(self):
        r = _response(_block('tool_name', foo='bar'))
        _, results, _ = engine._dispatch_tools(
            session=SimpleNamespace(), response=r, figure_catalog=[])
        self.assertIn('error', results[0]['result'])
