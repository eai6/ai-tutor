"""Regression tests for the 2026-07-18 multi-turn eval bottleneck fixes.

Failure classes (see evals/reports/multi_turn_bottlenecks_2026-07-18.md):

- B1: prose-question / slot-question divergence — the LLM's visible text
  poses one question while the InFlightQuestion slot holds another; the
  student answers what they read and is graded against something else.
  Fix: the slot is the single source of truth for the visible question —
  strip a divergent trailing prose question and render from the slot.
- B2: re-asks of already-correct questions burn turns (tested in
  test_tools.py::RepeatPoseRejectionTest).
- B3: engine vocabulary ("in flight", "POSE/TEACH mode") leaks into
  student-facing text. Fix: deterministic scrub before persistence.
- B4: the empty-reply placeholder promises "Here's the next one:" with no
  question attached. Fix: slot-aware placeholder.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase as DjangoTestCase

from apps.tutoring.models import InFlightQuestion
from apps.tutoring.simple_tutor.engine import (
    _empty_reply_placeholder,
    _ensure_posed_question_in_text,
    _format_tool_result_for_call2,
    _render_slot_question,
    _scrub_engine_vocab,
)
from apps.tutoring.simple_tutor.tests.test_engine import _make_session


def _posed_tool_results():
    return [{'tool': 'pose_question', 'result': {'posed': True}}]


class RenderSlotQuestionTest(SimpleTestCase):
    """_render_slot_question — deterministic stem+options block."""

    def test_renders_stem_and_mcq_options(self):
        slot = SimpleNamespace(
            question_text='What is 2 + 2?',
            question_type='mcq',
            options=['3', '4', '5', '22'],
        )
        out = _render_slot_question(slot)
        self.assertIn('What is 2 + 2?', out)
        self.assertIn('A) 3', out)
        self.assertIn('B) 4', out)
        self.assertIn('D) 22', out)

    def test_numeric_question_renders_stem_only(self):
        slot = SimpleNamespace(
            question_text='What is 1 - 0.7?',
            question_type='short_numeric',
            options=[],
        )
        out = _render_slot_question(slot)
        self.assertEqual(out.strip(), 'What is 1 - 0.7?')


class EnsurePosedQuestionTest(DjangoTestCase):
    """B1 — the slot question must be the only actionable question in the
    visible reply when the LLM's prose diverges from the slot."""

    def _session_with_slot(self, *, stem, qtype='short_numeric', options=None):
        session, _questions = _make_session()
        InFlightQuestion.objects.create(
            session=session,
            question_text=stem,
            question_type=qtype,
            options=options or [],
            reference_answer='x',
            source='inline_authored',
        )
        return session

    def test_divergent_trailing_prose_question_replaced_by_slot(self):
        stem = ('A fisherman catches a tuna with probability 0.60. '
                'What is the probability he does NOT catch a tuna?')
        session = self._session_with_slot(stem=stem)
        reply = (
            "Exactly — 1 - 0.60 = 0.40. Great work.\n\n"
            "Now, here's the next one:\n\n"
            "A tourist has a probability of 0.5 of visiting Praslin Island "
            "this year. State the complement event and its probability."
        )
        out = _ensure_posed_question_in_text(
            reply, _posed_tool_results(), session,
        )
        self.assertIn(stem, out)
        self.assertNotIn('Praslin', out)
        self.assertIn('Great work', out)

    def test_matching_prose_left_untouched(self):
        stem = 'What is 1 - 0.7?'
        session = self._session_with_slot(stem=stem)
        reply = "Nice one. Next up:\n\nWhat is 1 - 0.7?"
        out = _ensure_posed_question_in_text(
            reply, _posed_tool_results(), session,
        )
        self.assertEqual(out, reply)

    def test_divergent_mcq_renders_slot_options(self):
        stem = 'Which angle equals a 60 degree corresponding angle?'
        session = self._session_with_slot(
            stem=stem, qtype='mcq', options=['30', '60', '90', '120'],
        )
        reply = (
            "Correct!\n\n"
            "Try this: two parallel lines are cut by a transversal. One "
            "alternate interior angle is 140 degrees. What is the other?\n\n"
            "A) 40\nB) 140\nC) 90\nD) 180"
        )
        out = _ensure_posed_question_in_text(
            reply, _posed_tool_results(), session,
        )
        self.assertIn(stem, out)
        self.assertIn('B) 60', out)
        self.assertNotIn('alternate interior', out)
        self.assertNotIn('B) 140', out)

    def test_no_pose_this_turn_leaves_reply_alone(self):
        session = self._session_with_slot(stem='What is 2 + 2?')
        reply = "Let's think about place value first. What does the 4 mean?"
        out = _ensure_posed_question_in_text(reply, [], session)
        self.assertEqual(out, reply)


class ScrubEngineVocabTest(SimpleTestCase):
    """B3 — internal engine vocabulary never reaches the student."""

    def test_removes_not_in_flight_sentence(self):
        text = (
            'You just answered "140" to a question that isn\'t currently '
            "in flight. Let's return to the lesson.\n\n"
            "Corresponding angles are equal when lines are parallel."
        )
        out = _scrub_engine_vocab(text)
        self.assertNotIn('in flight', out)
        self.assertIn('Corresponding angles are equal', out)
        self.assertIn("Let's return to the lesson.", out)

    def test_removes_pose_teach_mode_sentence(self):
        text = (
            "No in-flight question is active — we're in POSE/TEACH mode.\n\n"
            "Let's finish explaining corresponding angles."
        )
        out = _scrub_engine_vocab(text)
        self.assertNotIn('POSE', out)
        self.assertNotIn('in-flight', out)
        self.assertIn("Let's finish explaining corresponding angles.", out)

    def test_removes_parenthetical_slot_note(self):
        text = (
            "You're close — divide 3 by 8 first.\n\n"
            "(Keep the in-flight question live — this is still the same "
            "one: Convert 3/8 to a decimal.)\n\n"
            "Your turn — what's 0.375 rounded to two decimal places?"
        )
        out = _scrub_engine_vocab(text)
        self.assertNotIn('in-flight', out)
        self.assertIn('divide 3 by 8', out)
        self.assertIn('0.375 rounded', out)

    def test_clean_text_unchanged(self):
        text = (
            "Exactly — corresponding angles are equal, so the answer is 60.\n\n"
            "Now try this one:\n\nWhat is 1 - 0.7?"
        )
        self.assertEqual(_scrub_engine_vocab(text), text)

    def test_fully_leaked_text_scrubs_to_empty(self):
        self.assertEqual(
            _scrub_engine_vocab(
                "No in-flight question is active — we're in POSE/TEACH mode."
            ).strip(),
            '',
        )


class EmptyReplyPlaceholderTest(DjangoTestCase):
    """B4 — the placeholder must not promise a question it doesn't show."""

    def _correct_verdict_results(self):
        return [{
            'tool': 'record_answer',
            'result': {'recorded': True, 'verdict': 'correct'},
        }]

    def test_without_slot_does_not_promise_next_question(self):
        session, _ = _make_session()
        out = _empty_reply_placeholder(self._correct_verdict_results(), session)
        self.assertNotIn("Here's the next one", out)
        self.assertIn('right', out)

    def test_with_slot_includes_the_question(self):
        session, _ = _make_session()
        InFlightQuestion.objects.create(
            session=session,
            question_text='What is 3/12 as a decimal?',
            question_type='short_numeric',
            options=[],
            reference_answer='0.25',
            source='inline_authored',
        )
        out = _empty_reply_placeholder(self._correct_verdict_results(), session)
        self.assertIn('What is 3/12 as a decimal?', out)


class ToolResultPrivateNoteTest(SimpleTestCase):
    """B1b/B3 — Call-2 platform notes are marked private, and the
    empty-slot grade instructs the model to engage with the student's
    answer rather than dismiss it."""

    def test_no_inflight_result_instructs_to_engage_with_answer(self):
        out = _format_tool_result_for_call2(
            'record_answer',
            {'recorded': False, 'error': 'no in-flight question'},
        )
        self.assertIn('previous turn', out)
        low = out.lower()
        self.assertNotIn('treat their message as a clarification', low)

    def test_platform_notes_marked_private(self):
        for name, result in (
            ('pose_question', {'posed': True, 'question_type': 'mcq',
                               'source': 'catalog'}),
            ('record_answer', {'recorded': True, 'verdict': 'correct',
                               'question_text': 'Q', 'reference_answer': 'A',
                               'question_type': 'mcq',
                               'attempt_count_before': 0}),
            ('record_answer', {'recorded': False,
                               'error': 'no in-flight question'}),
        ):
            out = _format_tool_result_for_call2(name, result)
            self.assertIn('private', out.lower(),
                          f'{name} result missing private-note marker')


class PoseNextAfterCorrectTest(DjangoTestCase):
    """B1 follow-up (2026-07-20 smoke run): on a correct-verdict GRADE turn
    the model writes the next question in prose without registering it, so
    the next student answer meets an empty slot and a fabricated re-grade.
    Call 2 must be required to pose when the verdict was correct and lesson
    content remains."""

    def _correct_results(self):
        return [{'tool': 'record_answer',
                 'result': {'recorded': True, 'verdict': 'correct'}}]

    def _steps(self, session):
        from apps.curriculum.models import LessonStep
        return list(LessonStep.objects.filter(
            lesson=session.lesson).order_by('order_index'))

    def test_wants_pose_for_nonexempt_family_mid_lesson(self):
        from apps.tutoring.simple_tutor.engine import (
            _should_pose_next_after_correct,
        )
        session, _ = _make_session()
        first_step = self._steps(session)[0]
        self.assertTrue(_should_pose_next_after_correct(
            'qwen', 'GRADE', self._correct_results(), first_step))

    def test_production_and_anthropic_untouched(self):
        from apps.tutoring.simple_tutor.engine import (
            _should_pose_next_after_correct,
        )
        session, _ = _make_session()
        first_step = self._steps(session)[0]
        for family in (None, '', 'anthropic'):
            self.assertFalse(_should_pose_next_after_correct(
                family, 'GRADE', self._correct_results(), first_step))

    def test_no_repair_when_pose_already_registered(self):
        from apps.tutoring.simple_tutor.engine import (
            _should_pose_next_after_correct,
        )
        session, _ = _make_session()
        first_step = self._steps(session)[0]
        results = self._correct_results() + [
            {'tool': 'pose_question', 'result': {'posed': True}}]
        self.assertFalse(_should_pose_next_after_correct(
            'qwen', 'GRADE', results, first_step))

    def test_no_repair_on_incorrect_verdict(self):
        from apps.tutoring.simple_tutor.engine import (
            _should_pose_next_after_correct,
        )
        session, _ = _make_session()
        first_step = self._steps(session)[0]
        results = [{'tool': 'record_answer',
                    'result': {'recorded': True, 'verdict': 'incorrect'}}]
        self.assertFalse(_should_pose_next_after_correct(
            'qwen', 'GRADE', results, first_step))

    def test_no_repair_on_last_step(self):
        from apps.tutoring.simple_tutor.engine import (
            _should_pose_next_after_correct,
        )
        session, _ = _make_session()
        last_step = self._steps(session)[-1]
        self.assertFalse(_should_pose_next_after_correct(
            'qwen', 'GRADE', self._correct_results(), last_step))

    def test_repair_instruction_and_call2_plan(self):
        from apps.tutoring.simple_tutor.engine import (
            _plan_call2, _repair_instruction,
        )
        text = _repair_instruction('pose_question_next', 'stu', 'reply')
        self.assertIn('pose_question', text)
        self.assertIn('next question', text.lower())
        tools = [{'name': 'pose_question'}, {'name': 'record_answer'}]
        call2_tools, choice = _plan_call2(tools, 'pose_question_next')
        self.assertEqual([t['name'] for t in call2_tools], ['pose_question'])
        self.assertEqual(choice, {'type': 'tool', 'name': 'pose_question'})


class ScrubOrphanPunctuationTest(SimpleTestCase):
    """Cycle-7 regression: kimi narrated a tool call as a bracketed JSON
    block; the scrub removed the vocab lines but left an orphan '[' as the
    entire bubble, which repeated and deadlocked the session. Punctuation-
    only residue must be dropped so the slot-aware placeholder takes over."""

    def test_bracketed_tool_json_scrubs_to_empty(self):
        text = '[\n{"name": "pose_question", "arguments": {"q": "x"}}\n]'
        self.assertEqual(_scrub_engine_vocab(text).strip(), '')

    def test_orphan_bracket_lines_dropped_from_mixed_reply(self):
        text = ('Nice work!\n[\n{"name": "record_answer", '
                '"arguments": {"extracted_answer": "5"}}\n]')
        out = _scrub_engine_vocab(text)
        self.assertEqual(out.strip(), 'Nice work!')


# ════════════════════════════════════════════════════════════════════════════
# Cycle-8 fixes — from the cycle-7 transcript review (2026-07-20)
# ════════════════════════════════════════════════════════════════════════════


class DispatchOrderTest(DjangoTestCase):
    """BN3 — record_answer must grade the question the student SAW.

    Cycle-7 gemini transcript: with no prior slot, the model posed the next
    question (fisherman) and recorded the student's answer to the previous
    visible question (rain) in one turn; pose-first dispatch made the grader
    read the fresh fisherman slot → correct answer marked wrong."""

    def _response(self, *, pose_stem, pose_ref, answer):
        from apps.tutoring.simple_tutor.tests.test_engine import _llm_response
        return _llm_response(text='ok', tool_uses=[
            {'name': 'pose_question', 'input': {
                'question_text': pose_stem, 'question_type': 'short_numeric',
                'reference_answer': pose_ref, 'source': 'inline_authored'}},
            {'name': 'record_answer', 'input': {'extracted_answer': answer}},
        ])

    def _dispatch(self, session, response):
        from apps.tutoring.simple_tutor.engine import _dispatch_tools
        return _dispatch_tools(
            session=session, response=response, figure_catalog=[])

    def test_prior_slot_graded_before_new_pose_replaces_it(self):
        session, _ = _make_session()
        InFlightQuestion.objects.create(
            session=session,
            question_text='P(rain)=0.7. P(not rain)?',
            question_type='short_numeric', options=[],
            reference_answer='0.3', source='inline_authored',
            attempt_count=1,
        )
        resp = self._response(
            pose_stem='P(tuna)=0.6. P(not tuna)?', pose_ref='0.4',
            answer='0.3')
        _, results, _ = self._dispatch(session, resp)
        rec = next(r['result'] for r in results if r['tool'] == 'record_answer')
        self.assertTrue(rec.get('recorded'))
        self.assertEqual(rec.get('verdict'), 'correct')
        self.assertIn('rain', rec.get('question_text', ''))
        slot = InFlightQuestion.objects.get(session=session)
        self.assertIn('tuna', slot.question_text)

    def test_late_registration_of_previous_turn_question_still_grades(self):
        from apps.tutoring.models import SessionTurn
        session, _ = _make_session()
        SessionTurn.objects.create(
            session=session, role=SessionTurn.Role.TUTOR,
            content='Nice. Now: P(rain)=0.7. P(not rain)?')
        resp = self._response(
            pose_stem='P(rain)=0.7. P(not rain)?', pose_ref='0.3',
            answer='0.3')
        _, results, _ = self._dispatch(session, resp)
        rec = next(r['result'] for r in results if r['tool'] == 'record_answer')
        self.assertTrue(rec.get('recorded'))
        self.assertEqual(rec.get('verdict'), 'correct')

    def test_fresh_question_never_grades_the_current_message(self):
        from apps.tutoring.models import SessionTurn
        session, _ = _make_session()
        SessionTurn.objects.create(
            session=session, role=SessionTurn.Role.TUTOR,
            content='Nice. Now: P(rain)=0.7. P(not rain)?')
        resp = self._response(
            pose_stem='P(tuna)=0.6. P(not tuna)?', pose_ref='0.4',
            answer='0.3')
        _, results, _ = self._dispatch(session, resp)
        rec = next(r['result'] for r in results if r['tool'] == 'record_answer')
        self.assertFalse(rec.get('recorded'))
        self.assertIn('in-flight', str(rec.get('error', '')))
        slot = InFlightQuestion.objects.get(session=session)
        self.assertIn('tuna', slot.question_text)


class EnsureOptionsRenderedTest(DjangoTestCase):
    """BN4 — an MCQ's options must be visible even when the stem is."""

    def test_options_appended_when_stem_present_but_options_missing(self):
        session, _ = _make_session()
        InFlightQuestion.objects.create(
            session=session,
            question_text='What is the bearing of Southwest?',
            question_type='mcq',
            options=['180°', '225°', '270°', '315°'],
            reference_answer='B', source='inline_authored',
        )
        reply = ("Let's check directions.\n\n"
                 "What is the bearing of Southwest?")
        out = _ensure_posed_question_in_text(
            reply, _posed_tool_results(), session)
        self.assertIn('B) 225°', out)


class RenderOptionPrefixTest(SimpleTestCase):
    """BN5 — options that already carry a letter prefix must not be
    double-lettered ("A) A) 11", seen throughout cycle-7 transcripts)."""

    def test_letter_prefixed_options_not_doubled(self):
        slot = SimpleNamespace(
            question_text='How many hearts?',
            question_type='mcq',
            options=['A) 11', 'B) 3.8', 'C) 31', 'D) 12'],
        )
        out = _render_slot_question(slot)
        self.assertIn('A) 11', out)
        self.assertNotIn('A) A)', out)


class PlaceholderVarietyTest(DjangoTestCase):
    """BN7 — the placeholder must not repeat the identical acknowledgement
    every turn (judges flag 'Got it — that's right. Here's the next one:'
    ×12 as robotic)."""

    def _results(self):
        return [{'tool': 'record_answer',
                 'result': {'recorded': True, 'verdict': 'correct'}}]

    def test_ack_varies_with_session_length(self):
        from apps.tutoring.models import SessionTurn
        session, _ = _make_session()
        outs = set()
        for i in range(4):
            outs.add(_empty_reply_placeholder(self._results(), session))
            SessionTurn.objects.create(
                session=session, role=SessionTurn.Role.STUDENT, content='x')
        self.assertGreater(len(outs), 1)


class PivotGuidanceTest(SimpleTestCase):
    """Cycle-8: after repeated failed/refused attempts on one question the
    in-flight block must direct a downshift — cycle-7/8 smoke runs ground
    30 turns on one multi-step question against a non-responder."""

    def _block(self, attempts):
        from apps.tutoring.simple_tutor.prompts import _render_in_flight_block
        slot = SimpleNamespace(
            question_text='Hard multi-step Q?', question_type='short_numeric',
            reference_answer='3.33', source='inline_authored',
            attempt_count=attempts, options=[], catalog_question_id=None,
        )
        return _render_in_flight_block(slot)

    def test_pivot_guidance_appears_after_repeated_attempts(self):
        block = self._block(3)
        self.assertIn('pivot', block.lower())
        self.assertIn('simpler', block.lower())

    def test_no_pivot_guidance_on_early_attempts(self):
        self.assertNotIn('pivot', self._block(1).lower())


class ScrubToolJsonTest(SimpleTestCase):
    """Cycle-8b: narrated tool-call JSON without vocab words (e.g.
    advance_step) escaped the scrub and left orphan '[' bubbles again."""

    def test_tool_json_lines_scrubbed(self):
        text = ('[\n{"name": "advance_step", "arguments": {}}\n]')
        self.assertEqual(_scrub_engine_vocab(text).strip(), '')

    def test_mixed_reply_keeps_prose(self):
        text = ('Nice work on that one!\n'
                '[\n{"name": "advance_step", "arguments": {}}\n]')
        self.assertEqual(_scrub_engine_vocab(text).strip(), 'Nice work on that one!')


class PlaceholderNoSlotVarietyTest(DjangoTestCase):
    """Cycle-8b: the no-verdict/no-slot placeholder repeated 'Let's keep
    going.' verbatim → sim deadlock. It must vary too."""

    def test_neutral_placeholder_varies(self):
        from apps.tutoring.models import SessionTurn
        session, _ = _make_session()
        outs = set()
        for _ in range(3):
            outs.add(_empty_reply_placeholder([], session))
            SessionTurn.objects.create(
                session=session, role=SessionTurn.Role.STUDENT, content='x')
        self.assertGreater(len(outs), 1)


class AutoPoseFallbackTest(DjangoTestCase):
    """Cycle-9: when a correct verdict leaves no slot and the model
    declined to pose (bare "That's it — well done." turns), the engine
    poses the next pool question deterministically instead of handing
    the student a dead turn."""

    def _correct_results(self):
        return [{'tool': 'record_answer',
                 'result': {'recorded': True, 'verdict': 'correct'}}]

    def test_poses_next_pool_question_and_appends_text(self):
        from apps.tutoring.simple_tutor.engine import _auto_pose_fallback
        from apps.tutoring.simple_tutor.tests.test_engine import _make_session
        from apps.curriculum.models import LessonStep
        session, questions = _make_session()
        step = LessonStep.objects.filter(lesson=session.lesson).first()
        out = _auto_pose_fallback(
            session=session, step=step, family='qwen',
            tool_results=self._correct_results(),
            text_reply="That's it — well done.")
        slot = InFlightQuestion.objects.filter(session=session).first()
        self.assertIsNotNone(slot)
        self.assertIn(slot.question_text, out)
        self.assertIn("That's it — well done.", out)

    def test_no_fallback_when_slot_already_exists(self):
        from apps.tutoring.simple_tutor.engine import _auto_pose_fallback
        from apps.tutoring.simple_tutor.tests.test_engine import _make_session
        from apps.curriculum.models import LessonStep
        session, _ = _make_session()
        step = LessonStep.objects.filter(lesson=session.lesson).first()
        InFlightQuestion.objects.create(
            session=session, question_text='Live Q?', question_type='mcq',
            options=['a', 'b', 'c', 'd'], reference_answer='A',
            source='inline_authored')
        out = _auto_pose_fallback(
            session=session, step=step, family='qwen',
            tool_results=self._correct_results(), text_reply='ok')
        self.assertEqual(out, 'ok')
        self.assertEqual(
            InFlightQuestion.objects.get(session=session).question_text,
            'Live Q?')

    def test_production_family_untouched(self):
        from apps.tutoring.simple_tutor.engine import _auto_pose_fallback
        from apps.tutoring.simple_tutor.tests.test_engine import _make_session
        from apps.curriculum.models import LessonStep
        session, _ = _make_session()
        step = LessonStep.objects.filter(lesson=session.lesson).first()
        out = _auto_pose_fallback(
            session=session, step=step, family=None,
            tool_results=self._correct_results(), text_reply='ok')
        self.assertEqual(out, 'ok')
        self.assertFalse(
            InFlightQuestion.objects.filter(session=session).exists())


class QwenVariantCycle10Test(SimpleTestCase):
    """Cycle-10 qwen prompt iteration — pins the rules added for the
    cycle-9 qwen signature: hint micro-step loops (0.70+0.30 asked 8x,
    'yes' and '1' both rejected), precision pedantry (33.3% for 1/3
    rejected), re-asking answered questions, naming the correct option
    mid-hint, and authored questions with impossible numbers."""

    def _qwen_block(self):
        from apps.tutoring.simple_tutor.family_prompts import (
            build_family_block_0,
        )
        return build_family_block_0('qwen', 'BASE')

    def test_micro_step_rule_present(self):
        b = self._qwen_block()
        self.assertIn('micro-step', b)

    def test_precision_rule_present(self):
        self.assertIn('33.3', self._qwen_block())

    def test_answered_question_is_finished_rule_present(self):
        self.assertIn('answered question is finished', self._qwen_block().lower())

    def test_authored_number_sanity_rule_present(self):
        b = self._qwen_block().lower()
        self.assertIn('between 0 and 1', b)

    def test_no_worries_not_seeded_as_example_opener(self):
        # The non-answer worked example must not itself model the
        # templated opener the judge flags.
        b = self._qwen_block()
        self.assertNotIn('> No worries', b)


# ════════════════════════════════════════════════════════════════════════════
# OSS-sweep fixes — from the oss13_mt transcript analysis (2026-07-22)
# ════════════════════════════════════════════════════════════════════════════


class DedupeReplyTest(DjangoTestCase):
    """BN1 — a tutor reply must never persist byte-identical (normalised)
    to the previous tutor turn: qwen3:14b lost 6 sessions to verbatim
    repeats, which is the student-sim's deadlock trigger and terrible UX."""

    def _prev(self, session, content):
        from apps.tutoring.models import SessionTurn
        SessionTurn.objects.create(
            session=session, role=SessionTurn.Role.TUTOR, content=content)

    def test_identical_reply_is_varied(self):
        from apps.tutoring.simple_tutor.engine import _dedupe_reply
        session, _ = _make_session()
        self._prev(session, 'What is 100% minus 90%?')
        out = _dedupe_reply(session, 'What is 100% minus 90%?')
        self.assertIn('What is 100% minus 90%?', out)
        self.assertNotEqual(
            out.lower().strip(), 'what is 100% minus 90%?')

    def test_case_and_whitespace_variants_still_count_as_identical(self):
        from apps.tutoring.simple_tutor.engine import _dedupe_reply
        session, _ = _make_session()
        self._prev(session, 'What  is 100% minus 90%? ')
        out = _dedupe_reply(session, 'what is 100% minus 90%?')
        self.assertNotEqual(' '.join(out.lower().split()).rstrip('.!?'),
                            'what is 100% minus 90%?')

    def test_different_reply_untouched(self):
        from apps.tutoring.simple_tutor.engine import _dedupe_reply
        session, _ = _make_session()
        self._prev(session, 'What is 100% minus 90%?')
        out = _dedupe_reply(session, 'Exactly — 10%. Next question:')
        self.assertEqual(out, 'Exactly — 10%. Next question:')


class PolarityAlignTest(DjangoTestCase):
    """BN2 — the reply's opening must agree with the grader's verdict.
    OSS models opened with 'Not quite' on graded-correct answers and
    'Exactly!' on graded-wrong ones — the top rubric killer of the sweep."""

    def _results(self, verdict):
        return [{'tool': 'record_answer',
                 'result': {'recorded': True, 'verdict': verdict}}]

    def test_negative_opener_on_correct_verdict_replaced(self):
        from apps.tutoring.simple_tutor.engine import _align_reply_polarity
        session, _ = _make_session()
        out = _align_reply_polarity(
            session,
            "Not quite — let's look at that again. The complement rule says "
            "the two probabilities add to 1.",
            self._results('correct'))
        self.assertNotIn('Not quite', out)
        self.assertIn('complement rule', out)

    def test_positive_opener_on_incorrect_verdict_replaced(self):
        from apps.tutoring.simple_tutor.engine import _align_reply_polarity
        session, _ = _make_session()
        out = _align_reply_polarity(
            session,
            'Exactly! Corresponding angles are equal. Now try the next one.',
            self._results('incorrect'))
        self.assertNotIn('Exactly!', out)
        self.assertIn('Corresponding angles are equal', out)

    def test_consistent_reply_untouched(self):
        from apps.tutoring.simple_tutor.engine import _align_reply_polarity
        session, _ = _make_session()
        text = 'Exactly — 10%. Here is the next one:'
        self.assertEqual(
            _align_reply_polarity(session, text, self._results('correct')),
            text)

    def test_no_verdict_untouched(self):
        from apps.tutoring.simple_tutor.engine import _align_reply_polarity
        session, _ = _make_session()
        text = "Not quite what we covered — let's recap."
        self.assertEqual(_align_reply_polarity(session, text, []), text)


class RevealFilterTest(DjangoTestCase):
    """BN3 — while a question is open after a wrong answer, the reply must
    not state the reference: qwen3:14b printed '(Answer: A)' verbatim."""

    def _session_with_slot(self, *, ref, qtype='mcq'):
        session, _ = _make_session()
        InFlightQuestion.objects.create(
            session=session, question_text='Which option?', question_type=qtype,
            options=['w', 'x', 'y', 'z'] if qtype == 'mcq' else [],
            reference_answer=ref, source='inline_authored', attempt_count=1)
        return session

    def _incorrect(self):
        return [{'tool': 'record_answer',
                 'result': {'recorded': True, 'verdict': 'incorrect'}}]

    def test_literal_answer_marker_stripped(self):
        from apps.tutoring.simple_tutor.engine import _filter_reveals
        session = self._session_with_slot(ref='A')
        out = _filter_reveals(
            session, "You're close — think about digit order. (Answer: A)",
            self._incorrect())
        self.assertNotIn('(Answer: A)', out)
        self.assertIn('digit order', out)

    def test_option_reveal_sentence_dropped(self):
        from apps.tutoring.simple_tutor.engine import _filter_reveals
        session = self._session_with_slot(ref='C')
        out = _filter_reveals(
            session,
            'Not quite. Option C is correct because rows come first. '
            'Look at the options again — which one matches?',
            self._incorrect())
        self.assertNotIn('Option C is correct', out)
        self.assertIn('which one matches', out)

    def test_numeric_reference_reveal_dropped(self):
        from apps.tutoring.simple_tutor.engine import _filter_reveals
        session = self._session_with_slot(ref='0.3', qtype='short_numeric')
        out = _filter_reveals(
            session,
            'Think about the total. So the answer is 0.3. '
            'What do you get when you subtract?',
            self._incorrect())
        self.assertNotIn('0.3', out)
        self.assertIn('subtract', out)

    def test_correct_verdict_untouched(self):
        from apps.tutoring.simple_tutor.engine import _filter_reveals
        session = self._session_with_slot(ref='A')
        results = [{'tool': 'record_answer',
                    'result': {'recorded': True, 'verdict': 'correct'}}]
        text = 'Exactly — A is right because rows come first.'
        self.assertEqual(_filter_reveals(session, text, results), text)


class AutoGradeFallbackTest(DjangoTestCase):
    """BN4 — when the student clearly answered, a slot exists, and the
    model declined record_answer through both calls (Ollama cannot honour
    forced tool_choice; qwen3.5:4b lost 12% of its answer turns this way),
    the engine grades the raw student message server-side. Strictly gated:
    intent must be 'answer' (not answer_or_other) — the ungated version of
    this fallback was removed in 2026-05 for over-firing, before the
    intent classifier existed."""

    def _slot(self, session):
        return InFlightQuestion.objects.create(
            session=session, question_text='P(not rain) if P(rain)=0.7?',
            question_type='short_numeric', options=[],
            reference_answer='0.3', source='inline_authored')

    def test_grades_raw_answer_when_model_declined(self):
        from apps.tutoring.simple_tutor.engine import _auto_grade_fallback
        session, _ = _make_session()
        self._slot(session)
        results = []
        _auto_grade_fallback(
            session=session, family='qwen', student_intent='answer',
            user_input='0.3', tool_results=results)
        rec = next(r for r in results if r['tool'] == 'auto_grade_fallback')
        self.assertTrue(rec['result'].get('recorded'))
        self.assertEqual(rec['result'].get('verdict'), 'correct')
        self.assertFalse(
            InFlightQuestion.objects.filter(session=session).exists())

    def test_skipped_when_record_already_fired(self):
        from apps.tutoring.simple_tutor.engine import _auto_grade_fallback
        session, _ = _make_session()
        self._slot(session)
        results = [{'tool': 'record_answer',
                    'result': {'recorded': True, 'verdict': 'incorrect'}}]
        _auto_grade_fallback(
            session=session, family='qwen', student_intent='answer',
            user_input='0.3', tool_results=results)
        self.assertFalse(
            any(r['tool'] == 'auto_grade_fallback' for r in results))

    def test_skipped_for_ambiguous_intent_and_production(self):
        from apps.tutoring.simple_tutor.engine import _auto_grade_fallback
        session, _ = _make_session()
        self._slot(session)
        for family, intent in ((None, 'answer'), ('qwen', 'answer_or_other'),
                               ('anthropic', 'answer')):
            results = []
            _auto_grade_fallback(
                session=session, family=family, student_intent=intent,
                user_input='0.3', tool_results=results)
            self.assertEqual(results, [], f'fired for {family}/{intent}')


class RetryLadderTest(SimpleTestCase):
    """BN6 — the [2, 5, 12]s ladder was not enough for the Anthropic
    overload window that cost 10 scenarios; both ladders now reach 30s+."""

    def test_client_ladder_extended(self):
        from apps.llm.client import _TRANSIENT_BACKOFF
        self.assertGreaterEqual(max(_TRANSIENT_BACKOFF), 30)

    def test_engine_ladder_extended(self):
        from apps.tutoring.simple_tutor.engine import _TRANSIENT_BACKOFF
        self.assertGreaterEqual(max(_TRANSIENT_BACKOFF), 30)


class GemmaFamilyProfileTest(SimpleTestCase):
    """Option-3 Gemma enablement: the tool-enabled repackaging tags must
    resolve to a 'gemma' family profile — with family=None the entire
    eval guard stack (forcing, salvage, nets) is silently gated off."""

    def test_gemma3_tools_tag_resolves(self):
        from apps.llm.model_profiles import get_model_profile
        p = get_model_profile('local_ollama/okamototk/gemma3-tools:12b')
        self.assertIsNotNone(p)
        self.assertEqual(p.family, 'gemma')

    def test_plain_gemma_tag_resolves(self):
        from apps.llm.model_profiles import get_model_profile
        p = get_model_profile('local_ollama/gemma3:4b')
        self.assertIsNotNone(p)
        self.assertEqual(p.family, 'gemma')


class ScrubXmlToolCallTest(SimpleTestCase):
    """gemma_probe5 smoke: the okamototk template's XML tool convention
    leaked '</tool_call>' fragments and fenced blocks into student-visible
    text, auto-failing no_tool_syntax_in_any_turn in 4 of 5 gemma3:12b
    sessions. The scrub only knew the JSON shape."""

    def test_fenced_xml_tool_block_removed(self):
        text = ("Let's check the answer.\n"
                "```xml\n"
                "<tool_call>\n"
                'pose_question(question_text="What is 2 + 2?")\n'
                "</tool_call>\n"
                "```\n"
                "What is 2 + 2?")
        out = _scrub_engine_vocab(text)
        self.assertNotIn('tool_call', out)
        self.assertNotIn('```', out)
        self.assertIn("Let's check the answer.", out)
        self.assertIn('What is 2 + 2?', out)

    def test_stray_tag_fragment_stripped_inline(self):
        out = _scrub_engine_vocab("Good try!</tool_call> Let's move on.")
        self.assertNotIn('tool_call', out)
        self.assertIn('Good try!', out)
        self.assertIn("Let's move on.", out)

    def test_unclosed_tag_fragment_stripped(self):
        out = _scrub_engine_vocab("Nice work.</tool_call")
        self.assertNotIn('tool_call', out)
        self.assertIn('Nice work.', out)


class MidReplyPolarityTest(DjangoTestCase):
    """gemma_probe5_v2: verdict contradictions escape the opener-level
    aligner by sitting mid-reply — 27b said "That's right – 50 is
    correct!" on an INCORRECT verdict (twice), and 12b told a correctly-
    answering student they "selected A instead of B". Sentence-level pass."""

    def _results(self, verdict):
        return [{'tool': 'record_answer',
                 'result': {'recorded': True, 'verdict': verdict}}]

    def test_mid_reply_affirmation_dropped_on_incorrect(self):
        from apps.tutoring.simple_tutor.engine import _align_reply_polarity
        session, _ = _make_session()
        text = ("Let's look at this together. That’s right – 50 is "
                "correct! Now think about what probability times total "
                "gives you.")
        out = _align_reply_polarity(session, text, self._results('incorrect'))
        self.assertNotIn('50 is correct', out)
        self.assertIn('probability times total', out)

    def test_mid_reply_denial_dropped_on_correct(self):
        from apps.tutoring.simple_tutor.engine import _align_reply_polarity
        session, _ = _make_session()
        text = ("You got the calculation spot-on: 2 * 500 = 1000. "
                "Looking at your working, you selected A instead of B as "
                "the final answer. Keep going.")
        out = _align_reply_polarity(session, text, self._results('correct'))
        self.assertNotIn('instead of B', out)
        self.assertIn('Keep going', out)

    def test_consistent_mid_reply_untouched(self):
        from apps.tutoring.simple_tutor.engine import _align_reply_polarity
        session, _ = _make_session()
        text = ('Not quite yet. The correct approach starts from the total. '
                'What do the two probabilities add up to?')
        self.assertEqual(
            _align_reply_polarity(session, text, self._results('incorrect')),
            text)
