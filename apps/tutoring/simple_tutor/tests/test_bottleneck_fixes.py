"""Regression tests for the 2026-07-18 multi-turn eval bottleneck fixes.

Failure classes (see offline_eval/multi_turn_results/multi_turn_bottlenecks_2026-07-18.md):

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
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase as DjangoTestCase

from apps.tutoring.models import InFlightQuestion
from apps.tutoring.simple_tutor.engine import (
    _empty_reply_placeholder,
    _ensure_posed_question_in_text,
    _format_tool_result_for_call2,
    _is_transient_error,
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
        """pose_question takes an index now, so the stem/ref the test wants
        posed are supplied via the pool on self._pool (see _dispatch)."""
        from types import SimpleNamespace
        from apps.tutoring.simple_tutor.tests.test_engine import _llm_response
        self._pool = [SimpleNamespace(
            question_text=pose_stem, question_type='short_numeric',
            correct_answer=pose_ref, option_a='', option_b='', option_c='',
            option_d='', answer_data={}, pk=0,
        )]
        return _llm_response(text='ok', tool_uses=[
            {'name': 'pose_question', 'input': {'question_index': 1}},
            {'name': 'record_answer', 'input': {'extracted_answer': answer}},
        ])

    def _dispatch(self, session, response):
        from apps.tutoring.simple_tutor.engine import _dispatch_tools
        return _dispatch_tools(
            session=session, response=response, figure_catalog=[],
            question_pool=getattr(self, '_pool', None))

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

    def test_pivot_guidance_appears_after_three_attempts(self):
        """Three hints, then pivot. This block renders at the point of
        decision, so its threshold must equal the Block-0 ladder's top rung and
        tools.PIVOT_AFTER_ATTEMPTS — higher and it stays silent on the turn the
        ladder calls for a pivot, lower and it asks for one the server will not
        perform."""
        block = self._block(3)
        self.assertIn('pivot', block.lower())
        self.assertIn('difficulty', block.lower())

    def test_no_pivot_guidance_while_hints_are_still_owed(self):
        for attempts in (0, 1, 2):
            with self.subTest(attempts=attempts):
                self.assertNotIn('pivot', self._block(attempts).lower())


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

    def test_optionless_mcq_rejection_also_triggers_fallback(self):
        """A pose rejected for MISSING OPTIONS strands the turn identically.

        Small models routinely write "A) … B) …" into the prose while calling
        pose_question without `options`; handle_pose_question refuses it
        (tools.py:504) because a letter reference with no option list cannot be
        graded. Before this, the fallback only rescued `repeat_of_correct`
        rejections, so the turn ended with no InFlightQuestion — the student
        answered "B" against nothing, the letter never graded, and the question
        was re-asked forever. Reproduced on qwen3-4b, geography lesson 1463,
        2026-07-27: only full option text worked, never a letter.
        """
        from apps.tutoring.simple_tutor.engine import _auto_pose_fallback
        from apps.tutoring.simple_tutor.tests.test_engine import _make_session
        from apps.curriculum.models import LessonStep
        session, _ = _make_session()
        step = LessonStep.objects.filter(lesson=session.lesson).first()
        rejected = [{'tool': 'pose_question',
                     'result': {'posed': False,
                                'error': 'mcq requires its options',
                                'question_type': 'mcq'}}]
        out = _auto_pose_fallback(
            session=session, step=step, family='qwen',
            tool_results=rejected, text_reply='Which map is best?')
        slot = InFlightQuestion.objects.filter(session=session).first()
        self.assertIsNotNone(slot, "rejected pose must be rescued by the pool")
        self.assertIn(slot.question_text, out)

    def test_fallback_strips_the_models_diverging_prose_question(self):
        """Only ONE question may survive, and it must be the graded one.

        The catalog question goes into the slot and is what grading uses, so
        leaving the model's own prose question in the reply asks the student two
        questions and grades a different one — the desync
        _strip_trailing_prose_question exists to repair.
        """
        from apps.tutoring.simple_tutor.engine import _auto_pose_fallback
        from apps.tutoring.simple_tutor.tests.test_engine import _make_session
        from apps.curriculum.models import LessonStep
        session, _ = _make_session()
        step = LessonStep.objects.filter(lesson=session.lesson).first()
        prose_q = 'Which of the following best defines a large scale map?'
        out = _auto_pose_fallback(
            session=session, step=step, family='qwen',
            tool_results=self._correct_results(),
            text_reply=f"Exactly — well reasoned.\n\n{prose_q}")
        slot = InFlightQuestion.objects.filter(session=session).first()
        self.assertIsNotNone(slot)
        self.assertNotIn(prose_q, out)
        self.assertIn(slot.question_text, out)
        self.assertIn('Exactly — well reasoned.', out)


class QwenVariantCycle10Test(SimpleTestCase):
    """Cycle-10 qwen prompt iteration — pins the rules added for the
    cycle-9 qwen signature: precision pedantry (33.3% for 1/3 rejected),
    re-asking answered questions, and naming the correct option mid-hint.

    Restructured 2026-08-06 (memory/offline_prompt_conflict_audit.md). These
    assert the RULE survives, not the sentence that carried it — pinning
    wording makes a prompt un-editable, which is how the template accumulated
    the contradictions the audit found. Two former members of this class are
    gone on purpose and are documented below.
    """

    def _qwen_block(self):
        from apps.tutoring.simple_tutor.family_prompts import (
            build_family_block_0,
        )
        return build_family_block_0('qwen', 'BASE')

    def test_precision_rule_present(self):
        b = self._qwen_block().lower()
        self.assertIn('rounding', b)

    def test_answered_question_is_finished_rule_present(self):
        b = self._qwen_block().lower()
        self.assertIn('answered correctly is finished', b)

    def test_no_worries_not_seeded_as_example_opener(self):
        # The non-answer example must not itself model the templated opener
        # the judge flags.
        b = self._qwen_block()
        self.assertNotIn('> No worries', b)

    # ── Deliberately deleted ────────────────────────────────────────────────
    #
    # test_micro_step_rule_present pinned "a hint carries at most ONE
    # micro-step ... once the student answers it". A micro-step is a question
    # asked mid-hint, and offline the student has four buttons and no text box
    # — there is nothing to answer it with. The rule was written when typing
    # was the only answer surface (audit C6).
    #
    # test_authored_number_sanity_rule_present pinned "a probability lies
    # between 0 and 1", from "check your numbers before posing an AUTHORED
    # question". pose_question takes question_index and nothing else; the tutor
    # cannot author a question, so the rule described a capability that does
    # not exist and implied one that must not (audit C3).

    def test_the_tutor_is_not_told_it_can_author_questions(self):
        """The inverse of the deleted authoring test, and the one worth having.

        Catalog-only (f59bdb7) removed question authoring from the tool; every
        sentence that still described it was telling a 4B it may write its own
        question, which is exactly the behaviour we keep seeing in transcripts.
        """
        b = self._qwen_block().lower()
        for phrase in ('authored question', 'distractors plausible',
                       'roll a fair 1-in-4', 'with four options'):
            self.assertNotIn(phrase, b, f'authoring guidance is back: {phrase!r}')
        # Not a bare 'four options' — the no-reveal rule says "the student is
        # reading those four options on screen", which is the opposite of
        # authoring. Match the authoring phrasing, not the noun.

    def test_no_hand_off_phrase_is_taught_verbatim(self):
        """Measured 2026-08-06: the model reaches for whatever hand-off string
        the prompt spells out, including on wrong answers where no question
        follows — 3/6 turns. Removing the literal string took it to 0/8. The
        rule that FORBIDS the phrases still names them, so match on the
        instructional shape rather than the words themselves.
        """
        b = self._qwen_block()
        self.assertNotIn('Introduce the question ("', b)
        self.assertNotIn('. Here\'s the next one:\n', b)


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


class QwenLocalTagProfileTest(SimpleTestCase):
    """The Qwen3.5 local tags must hit an EXACT MODEL_PROFILES entry, not the
    generic r"qwen3" family pattern. The family pattern is a cloud profile
    (max_tokens=16000, no num_ctx), which makes client.py derive
    num_ctx = max(8192, 16000+8192) = 24192 — the window that OOMs an 8 GB
    Jetson. The entire Eval-3 sweep measured qwen3.5:4b through that
    fallthrough, and it is the leading explanation for its 21/50 on mt50
    against 178/200 on the single-turn board."""

    LOCAL_QWEN35_TAGS = (
        'local_ollama/qwen3.5:4b',
        'local_ollama/qwen3.5:2b',
        'local_ollama/qwen3.5:0.8b',
        'local_ollama/qwen3.5:9b',
    )

    # Tags that actually tutor on the Jetson. These carry the runaway guard;
    # 0.8b is an intent classifier and 9b cannot run on this box, so neither
    # is held to it.
    JETSON_TUTORING_TAGS = (
        'local_ollama/qwen3.5:4b',
        'local_ollama/qwen3.5:2b',
        'local_ollama/qwen3.5-4b-jetson',
        'local_ollama/qwen3.5-2b-jetson',
        'local_ollama/qwen3-4b-jetson',
    )

    def test_tags_pin_a_jetson_safe_context(self):
        from apps.llm.model_profiles import get_model_profile
        for spec in self.LOCAL_QWEN35_TAGS:
            with self.subTest(spec=spec):
                p = get_model_profile(spec)
                self.assertIsNotNone(p)
                self.assertEqual(p.family, 'qwen')
                self.assertEqual(p.num_ctx, 16384)
                # Deliberately a bound, not an exact value. This assertion
                # used to pin 3072 and silently rotted the moment qwen3.5:4b
                # moved to 1024 — it was red on main before 2026-07-29. What
                # the test is actually for is that the tag does NOT fall
                # through to the generic r"qwen3" CLOUD pattern, which would
                # give max_tokens=_MT_INSTRUCT (16000) and num_ctx=None.
                self.assertLessEqual(p.max_tokens, 3072)

    def test_jetson_tutoring_tags_carry_the_runaway_guard(self):
        """max_tokens is an outer bound, not the length mechanism — measured
        replies are 27-193 tokens and <reply_length> in prompts.py does the
        real work. What this bounds is the bad case: a repetition loop at
        ~16 tok/s costs a student 64 s at 1024 against 192 s at 3072."""
        from apps.llm.model_profiles import get_model_profile
        for spec in self.JETSON_TUTORING_TAGS:
            with self.subTest(spec=spec):
                self.assertEqual(get_model_profile(spec).max_tokens, 1024)

    def test_tags_suppress_thinking(self):
        """Qwen3.5 templates are hybrid and gate on `Think`, so Ollama's
        top-level think flag genuinely suppresses reasoning. It must be sent:
        _adapt_ollama_response never calls _recover_reasoning_tool_call, so a
        tool call emitted into the reasoning channel has no salvage path."""
        from apps.llm.model_profiles import get_model_profile
        for spec in self.LOCAL_QWEN35_TAGS:
            with self.subTest(spec=spec):
                self.assertIs(get_model_profile(spec).sampling_dict()['think'], False)

    def test_jetson_qwen3_entry_still_omits_think(self):
        """qwen3:4b is Thinking-2507 — its template opens <think>
        unconditionally and think=False disables only Ollama's PARSER, so the
        monologue lands in content and truncates the answer. The flag must NOT
        leak onto this entry."""
        from apps.llm.model_profiles import get_model_profile
        p = get_model_profile('local_ollama/qwen3-4b-jetson')
        self.assertEqual(p.num_ctx, 16384)
        self.assertIsNone(p.ollama_think)
        self.assertNotIn('think', p.sampling_dict())

    def test_cloud_qwen_fallthrough_unchanged(self):
        """The committed Colab/cloud eval numbers run at num_ctx=24192 via the
        family pattern. Adding exact local keys must not disturb it."""
        from apps.llm.model_profiles import get_model_profile
        p = get_model_profile('vertex_model_garden/qwen/qwen3-235b-a22b-instruct-2507-maas')
        self.assertEqual(p.max_tokens, 16000)
        self.assertIsNone(p.num_ctx)

    def test_production_resolution_untouched(self):
        from apps.llm.model_profiles import get_model_profile
        self.assertIsNone(get_model_profile(None))
        self.assertIsNone(get_model_profile(''))
        p = get_model_profile('claude-opus-4-7')
        self.assertIsNotNone(p)
        self.assertEqual(p.family, 'anthropic')
        self.assertIsNone(p.ollama_think)


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


class RetryRenderVariationTest(DjangoTestCase):
    """gemma v3: legal hint-ladder re-renders of the SAME question read as
    'recycled the compass question three times' to the judge because each
    re-render is verbatim. From the second attempt on, the rendered
    question carries a rotated retry framing so consecutive re-renders
    differ."""

    def _slot(self, session, attempts):
        return InFlightQuestion.objects.create(
            session=session, question_text='Which compass point is 225°?',
            question_type='mcq', options=['N', 'SW', 'SE', 'W'],
            reference_answer='B', source='inline_authored',
            attempt_count=attempts)

    def test_first_ask_renders_plain(self):
        session, _ = _make_session()
        slot = self._slot(session, 0)
        out = _render_slot_question(slot)
        self.assertTrue(out.startswith('Which compass point'))

    def test_retry_render_varies(self):
        session, _ = _make_session()
        slot = self._slot(session, 2)
        out = _render_slot_question(slot)
        self.assertIn('Which compass point is 225°?', out)
        self.assertFalse(out.startswith('Which compass point'))


class AutoPoseAfterRejectTest(DjangoTestCase):
    """gemma v3 completeness: a repeat-rejected pose on a turn with NO
    correct verdict and no surviving slot must still end with a question
    — extend the auto-pose net beyond correct-verdict turns."""

    def test_fallback_fires_on_rejected_pose_without_verdict(self):
        from apps.tutoring.simple_tutor.engine import _auto_pose_fallback
        from apps.curriculum.models import LessonStep
        session, _ = _make_session()
        step = LessonStep.objects.filter(lesson=session.lesson).first()
        results = [{'tool': 'pose_question',
                    'result': {'posed': False, 'repeat_of_correct': True,
                               'error': 'repeat_question: ...'}}]
        out = _auto_pose_fallback(
            session=session, step=step, family='gemma',
            tool_results=results, text_reply='Let me think of another one.')
        slot = InFlightQuestion.objects.filter(session=session).first()
        self.assertIsNotNone(slot)
        self.assertIn(slot.question_text, out)


class RotationParityTest(DjangoTestCase):
    """GB2 (gemma20_mt): rotation indexes used the SessionTurn count, which
    advances by 2 per exchange — a 4-entry tuple cycled only half its
    variants, and 'One more time, from a different angle' repeated
    verbatim, tripping the phrase-window assert. Rotations now use a
    dedicated persisted counter."""

    def test_consecutive_dedupes_cycle_all_variants(self):
        from apps.tutoring.models import SessionTurn
        from apps.tutoring.simple_tutor.engine import _dedupe_reply
        session, _ = _make_session()
        SessionTurn.objects.create(
            session=session, role=SessionTurn.Role.TUTOR, content='Same Q?')
        firsts = set()
        for _ in range(4):
            out = _dedupe_reply(session, 'Same Q?')
            firsts.add(out.split('\n')[0])
        self.assertEqual(len(firsts), 4, firsts)


class HardPivotTest(DjangoTestCase):
    """GB1 (gemma20_mt): the attempt>=3 pivot GUIDANCE was ignored by
    gemma — a mis-authored slot (ref='100' vs answers in probability
    form) collected 5 straight incorrect verdicts and burned sessions to
    max_turns. At attempt >= 4 the engine force-replaces the stuck slot
    with the next pool question."""

    def _stuck_slot(self, session, attempts):
        return InFlightQuestion.objects.create(
            session=session, question_text='Sum of probabilities in percent?',
            question_type='short_numeric', options=[],
            reference_answer='100', source='inline_authored',
            attempt_count=attempts)

    def test_slot_replaced_at_four_attempts(self):
        from apps.tutoring.simple_tutor.engine import _force_pivot_stuck_slot
        from apps.curriculum.models import LessonStep
        session, _ = _make_session()
        step = LessonStep.objects.filter(lesson=session.lesson).first()
        self._stuck_slot(session, 4)
        out = _force_pivot_stuck_slot(
            session=session, step=step, family='gemma',
            tool_results=[], text_reply='Not quite — think percent.')
        slot = InFlightQuestion.objects.get(session=session)
        self.assertNotIn('percent', slot.question_text)
        self.assertIn(slot.question_text, out)

    def test_untouched_below_threshold_and_in_production(self):
        from apps.tutoring.simple_tutor.engine import _force_pivot_stuck_slot
        from apps.curriculum.models import LessonStep
        session, _ = _make_session()
        step = LessonStep.objects.filter(lesson=session.lesson).first()
        self._stuck_slot(session, 2)
        out = _force_pivot_stuck_slot(
            session=session, step=step, family='gemma',
            tool_results=[], text_reply='hint')
        self.assertEqual(out, 'hint')
        self.assertIn('percent',
                      InFlightQuestion.objects.get(session=session).question_text)
        InFlightQuestion.objects.filter(session=session).update(attempt_count=5)
        out = _force_pivot_stuck_slot(
            session=session, step=step, family=None,
            tool_results=[], text_reply='hint')
        self.assertEqual(out, 'hint')


class ScrubToolCallMetaTest(SimpleTestCase):
    """GB3 (gemma20_mt): 12b apologised at length 'about tool calls and
    development' in student-visible text — the vocab filter knew the tool
    NAMES but not the generic phrase."""

    def test_tool_call_meta_sentence_dropped(self):
        out = _scrub_engine_vocab(
            'I apologize for the tool call confusion earlier. '
            "Let's continue with the lesson.")
        self.assertNotIn('tool call', out)
        self.assertIn("Let's continue with the lesson.", out)


class TransientErrorClassificationTest(SimpleTestCase):
    """Local Ollama 500s must be retried like any other 5xx.

    ``requests.HTTPError`` puts the status on ``exc.response.status_code``,
    not ``exc.status_code`` — the attribute the classifier originally walked.
    So every local 500 was classified permanent and skipped the backoff, and
    because ``_invoke_with_transient_retry`` is fail-soft (returns None rather
    than raising) the turn silently degraded to the placeholder reply with
    nothing in the logs marking it as a retryable failure. Measured on the
    Jetson 2026-07-27: six in one session.

    Asserted through the real exception type rather than a stub, because the
    bug was entirely in which attribute the real type exposes.
    """

    def _http_error(self, status):
        import requests
        resp = requests.Response()
        resp.status_code = status
        return requests.HTTPError(
            f"{status} Server Error: Internal Server Error for url: "
            "http://localhost:11434/api/chat",
            response=resp,
        )

    def test_ollama_500_is_transient(self):
        self.assertTrue(_is_transient_error(self._http_error(500)))

    def test_other_5xx_are_transient(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(_is_transient_error(self._http_error(status)))

    def test_client_errors_stay_permanent(self):
        """400/401/404 must NOT retry — burning backoff on a schema or auth
        error just delays the fallback path."""
        for status in (400, 401, 404):
            with self.subTest(status=status):
                self.assertFalse(_is_transient_error(self._http_error(status)))

    def test_bare_message_500_is_transient(self):
        """Adapters that re-raise as a plain Exception lose the response
        object; the message substring is the remaining signal."""
        self.assertFalse(_is_transient_error(Exception('400 Bad Request')))
        self.assertTrue(_is_transient_error(
            Exception('500 Server Error: Internal Server Error for url: x')))


class RetryBudgetTest(SimpleTestCase):
    """Retrying a 5xx is right; retrying it six times is right only for a
    provider that has a queue to drain.

    The cloud ladder ([2,5,12,30,60]) applied to a local Ollama 500 turned one
    bad turn into ~11 minutes on the Jetson (2026-07-29): each attempt is a
    full generation, ~92-103 s measured, and the failure was deterministic in
    the request, so all six attempts failed identically before the placeholder
    reply was served anyway.

    Numbers here are asserted as CALL COUNTS, not durations — time.sleep is
    patched out, so a regression shows up as attempts rather than as a slow
    test.
    """

    # Verbatim from Ollama 0.30.7 on the Jetson, 2026-07-29. The important
    # part is that the status is 500 (so _is_transient_error says "retry")
    # while the body says the generation was truncated (so it can't succeed).
    TRUNCATED_TOOL_CALL_BODY = (
        '{"error":"llama-server returned invalid tool call arguments for '
        '\\"pose_question\\": unexpected end of JSON input"}'
    )

    def _http_error(self, status, body=''):
        import requests
        resp = requests.Response()
        resp.status_code = status
        resp._content = body.encode()
        return requests.HTTPError(
            f"{status} Server Error: Internal Server Error for url: "
            "http://localhost:11434/api/chat",
            response=resp,
        )

    def _count_attempts(self, exc, **kwargs):
        from apps.tutoring.simple_tutor import engine
        calls = []

        def fn():
            calls.append(1)
            raise exc

        with patch.object(engine.time, 'sleep'):
            out = engine._invoke_with_transient_retry(fn, label='t', **kwargs)
        self.assertIsNone(out, 'must stay fail-soft and return None')
        return len(calls)

    def test_local_provider_gets_one_retry(self):
        """A generic local 500 — a model reload or an allocation blip — is
        worth one cheap retry and no more."""
        self.assertEqual(
            self._count_attempts(self._http_error(500),
                                 provider='local_ollama'),
            2,
        )

    def test_cloud_provider_keeps_the_full_ladder(self):
        """Anthropic overload windows outlast a short ladder — that is why the
        cloud ladder was extended in the first place. Do not shorten it."""
        from apps.tutoring.simple_tutor.engine import _TRANSIENT_BACKOFF
        self.assertEqual(
            self._count_attempts(self._http_error(503), provider='anthropic'),
            len(_TRANSIENT_BACKOFF) + 1,
        )

    def test_unknown_provider_defaults_to_the_cloud_ladder(self):
        """Omitting the provider must not silently shorten retries for a
        remote model. Local is the special case and has to be named."""
        from apps.tutoring.simple_tutor.engine import _TRANSIENT_BACKOFF
        self.assertEqual(
            self._count_attempts(self._http_error(503)),
            len(_TRANSIENT_BACKOFF) + 1,
        )

    def test_truncated_tool_call_is_not_retried_at_all(self):
        """The 500 that started this: the server rejected its own truncated
        output. Resending the identical request reproduces it, so the only
        thing a retry buys is another ~92 s of decode."""
        self.assertEqual(
            self._count_attempts(
                self._http_error(500, self.TRUNCATED_TOOL_CALL_BODY),
                provider='local_ollama'),
            1,
        )

    def test_truncated_tool_call_is_not_retried_on_cloud_either(self):
        """The claim is about the failure, not the host — a server that
        rejects its own malformed generation cannot be retried into success
        wherever it runs."""
        self.assertEqual(
            self._count_attempts(
                self._http_error(500, self.TRUNCATED_TOOL_CALL_BODY),
                provider='anthropic'),
            1,
        )

    def test_error_detail_surfaces_the_response_body(self):
        """`str(HTTPError)` is only the status line. Six Ollama 500s in one
        session logged nothing about why (2026-07-27) because the provider's
        explanation lives in exc.response.text."""
        from apps.tutoring.simple_tutor.engine import _error_detail
        detail = _error_detail(
            self._http_error(500, self.TRUNCATED_TOOL_CALL_BODY))
        self.assertIn('500 Server Error', detail)
        self.assertIn('unexpected end of JSON input', detail)

    def test_error_detail_survives_a_body_that_cannot_be_read(self):
        """Logging must never be the thing that raises."""
        from apps.tutoring.simple_tutor.engine import _error_detail

        class Exploding:
            @property
            def text(self):
                raise RuntimeError('boom')

        exc = Exception('plain failure')
        exc.response = Exploding()
        self.assertIn('plain failure', _error_detail(exc))

    def test_retry_disable_switch_still_wins(self):
        from apps.tutoring.simple_tutor import engine
        with patch.dict(os.environ, {'SIMPLE_TUTOR_TRANSIENT_RETRY': '0'}):
            self.assertEqual(
                self._count_attempts(self._http_error(503),
                                     provider='anthropic'),
                1,
            )
        # Sanity: the switch, not the ladder, is what produced that 1.
        self.assertGreater(len(engine._TRANSIENT_BACKOFF), 0)


class NarratedQuestionWithNoPoseTest(DjangoTestCase):
    """A question written into the reply but never posed must not reach the
    student.

    Device session 20, lesson 1427. The slot held "In the four-figure grid
    reference 3947, which digits represent the easting value?" while the reply
    narrated a DIFFERENT MCQ:

        "...In the reference 3947, the easting is 39. Here's the next one:
         Which of the following four-figure grid references has an easting
         of 52?  A) 5234  B) 3452  C) 2552  D) 5125"

    pose_question was never called, so the slot never moved. The student
    answered the question in front of them — "5234" — and it was graded
    against 3947 and marked wrong. Two turns later auto_pose_fallback finally
    posed it.

    _ensure_posed_question_in_text existed to prevent exactly this but returned
    early when no pose fired, i.e. in the only case that mattered.
    """

    def _slot(self, session):
        InFlightQuestion.objects.filter(session=session).delete()
        return InFlightQuestion.objects.create(
            session=session,
            question_text='In the four-figure grid reference 3947, which digits '
                          'represent the easting value?',
            question_type='mcq', options=['47', '39', '3 and 9', '4 and 7'],
            reference_answer='B', source='catalog', attempt_count=1,
        )

    def test_narrated_question_is_replaced_by_the_live_slot(self):
        from apps.tutoring.simple_tutor.engine import _ensure_posed_question_in_text
        session, _ = _make_session()
        self._slot(session)
        text = (
            'You mentioned "easting" — that\'s the first two digits.\n'
            "Here's the next one: Which of the following four-figure grid "
            'references has an easting of 52?\n'
            'A) 5234\nB) 3452\nC) 2552\nD) 5125'
        )
        out = _ensure_posed_question_in_text(text, [], session)
        self.assertNotIn('5234', out, 'the unposed question must not be shown')
        self.assertIn('3947', out, "the live slot's question must be")

    def test_a_hint_with_no_options_is_left_alone(self):
        """Hints do not carry an A/B/C/D block. They must pass through
        untouched, or every hint turn would re-render the whole question."""
        from apps.tutoring.simple_tutor.engine import _ensure_posed_question_in_text
        session, _ = _make_session()
        self._slot(session)
        text = ('Not quite — the easting runs left to right, so it is the '
                'first pair. Which two digits are those?')
        self.assertEqual(_ensure_posed_question_in_text(text, [], session), text)

    def test_reply_restating_the_live_question_is_left_alone(self):
        """Re-showing the question the platform IS grading is fine."""
        from apps.tutoring.simple_tutor.engine import _ensure_posed_question_in_text
        session, _ = _make_session()
        self._slot(session)
        text = ('Have another go. In the four-figure grid reference 3947, which '
                'digits represent the easting value?\nA) 47\nB) 39\nC) 3 and 9\nD) 4 and 7')
        self.assertEqual(_ensure_posed_question_in_text(text, [], session), text)

    def test_no_slot_means_no_rewrite(self):
        from apps.tutoring.simple_tutor.engine import _ensure_posed_question_in_text
        session, _ = _make_session()
        InFlightQuestion.objects.filter(session=session).delete()
        text = 'Some prose with options\nA) x\nB) y\nC) z\nD) w'
        self.assertEqual(_ensure_posed_question_in_text(text, [], session), text)


class ToolNameNormalisationOrdersDispatchTest(DjangoTestCase):
    """A padded or camelCase tool name must not reorder dispatch.

    _dispatch_tools normalises names AFTER sorting, so _pose_before_record was
    comparing raw ones. A model emitting ' record_answer' made has_record False,
    the function fell through to `return not has_record` = True, and pose was
    dispatched BEFORE the grade. handle_pose_question then rejected it as
    premature_pose, so the tutor lost its pose while its reply still advertised
    the next question.

    Device session 21, lesson 1434: the mixed-use question was announced in
    three consecutive replies and never registered; the student answered it
    twice and was graded against the previous question both times.
    """

    def _blocks(self, record_name):
        return [SimpleNamespace(name='pose_question', input={'question_index': 1}),
                SimpleNamespace(name=record_name, input={'extracted_answer': 'b'})]

    def _session_with_live_slot(self):
        session, _ = _make_session()
        InFlightQuestion.objects.create(
            session=session, question_text='the live question',
            question_type='mcq', options=['a', 'b', 'c', 'd'],
            reference_answer='A', source='catalog', attempt_count=0,
        )
        return session

    def test_canonical_name_grades_first(self):
        from apps.tutoring.simple_tutor.engine import _pose_before_record
        s = self._session_with_live_slot()
        self.assertFalse(_pose_before_record(s, self._blocks('record_answer')))

    def test_padded_name_still_grades_first(self):
        from apps.tutoring.simple_tutor.engine import _pose_before_record
        s = self._session_with_live_slot()
        self.assertFalse(_pose_before_record(s, self._blocks(' record_answer ')))

    def test_camelcase_name_still_grades_first(self):
        from apps.tutoring.simple_tutor.engine import _pose_before_record
        s = self._session_with_live_slot()
        self.assertFalse(_pose_before_record(s, self._blocks('recordAnswer')))

    def test_padded_name_dispatches_the_grade_first_end_to_end(self):
        """_pose_before_record and _priority BOTH had to normalise. Fixing
        only the first leaves a padded name at priority 2 (last), so pose
        still runs first and is still rejected as premature_pose."""
        from apps.tutoring.simple_tutor import engine
        from apps.tutoring.simple_tutor.tests.test_engine import _llm_response
        session = self._session_with_live_slot()
        r = _llm_response(text='ok', tool_uses=[
            {'name': ' record_answer ', 'input': {'extracted_answer': 'b'}},
            {'name': 'pose_question', 'input': {'question_index': 1}},
        ])
        _, results, _ = engine._dispatch_tools(
            session=session, response=r, figure_catalog=[], question_pool=None)
        order = [x['tool'] for x in results]
        self.assertEqual(order[0], 'record_answer',
                         f'the grade must dispatch first, got {order}')


# RevealFilterTest, McqOptionTextLeakTest and VerbatimOptionRunLeakTest
# were removed with _filter_reveals (2026-08-06). Leak prevention is now
# the prompt's job — post-processing the tutor's reply was dropped as
# unreliable. See the note where _filter_reveals used to live in engine.py.


class QwenInstructModeTest(SimpleTestCase):
    """Every Qwen arm on the mt100 board must run instruct, not thinking."""

    ARMS = (
        'local_ollama/qwen3.5-2b-jetson',
        'local_ollama/qwen3-4b-jetson',
        'local_ollama/qwen3-8b-jetson',
        'local_ollama/qwen3.6-27b-instruct',
        'local_ollama/qwen3-30b-a3b-jetson',
    )
    # Tags whose BASE checkpoint is instruct — no runtime flag needed, and
    # setting one would be wrong (it only toggles Ollama's parser).
    BASE_INSTRUCT = {
        'local_ollama/qwen3-4b-jetson',
        'local_ollama/qwen3-30b-a3b-jetson',
    }

    # Expected num_ctx per arm, straight from the MODEL_PROFILES entries. NOT
    # a single value: qwen3.6-27b-instruct is laptop-class and pins 32768,
    # every jetson tag pins 16384. The point of asserting this field isn't
    # that it's uniform — it's that the generic r"qwen3" FAMILY_PATTERNS
    # fallback NEVER sets num_ctx (leaves it None, i.e. Ollama's 4096
    # default), so pinning the real value per arm catches an exact-key
    # deletion that the mode/ollama_think assertions alone would miss.
    NUM_CTX = {
        'local_ollama/qwen3.5-2b-jetson': 16384,
        'local_ollama/qwen3-4b-jetson': 16384,
        'local_ollama/qwen3-8b-jetson': 16384,
        'local_ollama/qwen3.6-27b-instruct': 32768,
        'local_ollama/qwen3-30b-a3b-jetson': 16384,
    }
    # num_gpu only where the entry actually sets it (qwen3.6-27b-instruct
    # leaves it on Ollama's autofit — laptop-class, not the Jetson memory
    # pressure the other four tags are pinned against).
    NUM_GPU = {
        'local_ollama/qwen3.5-2b-jetson': 99,
        'local_ollama/qwen3-4b-jetson': 99,
        'local_ollama/qwen3-8b-jetson': 99,
        'local_ollama/qwen3-30b-a3b-jetson': 99,
    }

    def test_arms_have_exact_dict_entries_not_just_a_resolved_profile(self):
        """get_model_profile() falls through to the generic r"qwen3" regex
        pattern when an exact key is missing from MODEL_PROFILES, and for a
        tail like "qwen3-30b-a3b-jetson" that fallback happens to return
        mode="instruct" with ollama_think=None — exactly the shape a
        BASE_INSTRUCT arm is supposed to have. So the two tests below would
        stay green even if the real entry were deleted, while num_ctx
        silently reverted to None (Ollama's 4096 default) and the
        runner-eviction/thrash bug this task exists to prevent came back
        with no test noticing. Assert against MODEL_PROFILES directly (not
        the resolver), and pin num_ctx/num_gpu — fields the fallback never
        sets — so a deleted entry fails loudly here instead of silently
        passing the mode/ollama_think checks via the fallback."""
        from apps.llm.model_profiles import MODEL_PROFILES
        for spec in self.ARMS:
            self.assertIn(spec, MODEL_PROFILES, f'{spec}: no exact profile entry (would fall through to the qwen3 regex)')
            p = MODEL_PROFILES[spec]
            self.assertEqual(p.num_ctx, self.NUM_CTX[spec], f'{spec}: num_ctx')
            expected_gpu = self.NUM_GPU.get(spec)
            if expected_gpu is not None:
                self.assertEqual(p.num_gpu, expected_gpu, f'{spec}: num_gpu')

    def test_every_arm_has_a_profile_in_instruct_mode(self):
        from apps.llm.model_profiles import get_model_profile
        for spec in self.ARMS:
            p = get_model_profile(spec)
            self.assertIsNotNone(p, f'{spec}: no profile')
            self.assertEqual(p.mode, 'instruct', spec)

    def test_hybrid_arms_suppress_thinking_and_base_instruct_arms_do_not(self):
        from apps.llm.model_profiles import get_model_profile
        for spec in self.ARMS:
            p = get_model_profile(spec)
            if spec in self.BASE_INSTRUCT:
                self.assertIsNone(p.ollama_think, f'{spec}: flag not needed')
            else:
                self.assertIs(p.ollama_think, False, f'{spec}: hybrid needs False')

    def test_bare_qwen3_8b_profile_also_suppresses_thinking(self):
        # The pre-existing entry claimed instruct without the flag.
        from apps.llm.model_profiles import MODEL_PROFILES
        self.assertIs(MODEL_PROFILES['local_ollama/qwen3:8b'].ollama_think, False)
