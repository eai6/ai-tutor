"""A correct answer gets a reason, not just a tick — and a wrong one gets neither.

Observed 2026-08-23 across 68 graded sessions: every correct answer drew a bare
"Got it — that's right. Here's the next one:" and nothing more. Two causes, both
in the prompt rather than the model:

  * The record_answer feedback said "If correct, briefly acknowledge" — the
    tutor was doing exactly what it was told.
  * The bank's authored explanation (12,375 of 12,375 questions have one) was
    rendered nowhere the model could see it.

The gating matters as much as the delivery: on a WRONG answer the explanation
states the answer, so sending it would turn the hint ladder into a reveal.
"""
from django.test import TestCase

from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketQuestion
from ai_tutor.apps.tutoring.simple_tutor.engine import (
    _format_tool_result_for_call2 as fmt,
)
from ai_tutor.apps.tutoring.simple_tutor.prompts import _render_question_pool

EXPLANATION = 'Bearings are measured clockwise from north by convention.'


class ExplanationReachesTheTutorTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        inst = Institution.objects.create(name='X', slug='x')
        course = Course.objects.create(title='C', institution=inst,
                                       grade_level='S3', is_published=True)
        unit = Unit.objects.create(course=course, title='U', order_index=0)
        lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                       order_index=0, is_published=True)
        ticket = ExitTicket.objects.create(lesson=lesson)
        cls.q = ExitTicketQuestion.objects.create(
            exit_ticket=ticket, question_type='mcq',
            question_text='A bearing is measured clockwise from which direction?',
            option_a='East', option_b='South', option_c='North', option_d='West',
            correct_answer='C', explanation=EXPLANATION,
        )

    def _feedback(self, verdict):
        return fmt('record_answer', {
            'recorded': True, 'verdict': verdict,
            'reference_answer': 'C', 'question_text': self.q.question_text,
            'question_type': 'mcq', 'attempt_count_before': 0,
            'catalog_question_id': self.q.pk,
        })

    # --- delivery ---------------------------------------------------------
    def test_correct_verdict_carries_the_explanation(self):
        self.assertIn(EXPLANATION, self._feedback('correct'))

    def test_the_tutor_is_told_to_give_a_reason(self):
        """Asserts the REQUIREMENT, not the wording — an earlier version of
        this test pinned the literal word "WHY" and broke when the instruction
        was rephrased, which is a test measuring the wrong thing.

        "briefly acknowledge" is the original wording that produced a bare
        tick; if it returns, so does the behaviour."""
        out = self._feedback('correct')
        self.assertRegex(out, r'REASON|why it is right',
                         'the instruction must demand a reason, not a tick')
        self.assertNotIn('briefly acknowledge', out)

    # --- gating -----------------------------------------------------------
    def test_wrong_answer_does_NOT_carry_the_explanation(self):
        """It states the answer. Sending it on a wrong attempt turns the hint
        ladder into a reveal — the single worst thing this change could do."""
        self.assertNotIn(EXPLANATION, self._feedback('incorrect'))

    def test_a_question_with_no_explanation_degrades_quietly(self):
        self.q.explanation = ''
        self.q.save(update_fields=['explanation'])
        out = self._feedback('correct')
        self.assertNotIn('<explanation></explanation>', out)

    def test_a_missing_catalog_id_does_not_raise(self):
        """StepQuestion-sourced slots have no catalog row behind them."""
        out = fmt('record_answer', {
            'recorded': True, 'verdict': 'correct', 'reference_answer': 'C',
            'question_text': 'q', 'question_type': 'mcq',
            'attempt_count_before': 0, 'catalog_question_id': None,
        })
        self.assertIn('VERDICT: CORRECT', out)

    # --- the pool ---------------------------------------------------------
    def test_question_pool_renders_the_explanation(self):
        self.assertIn(EXPLANATION, _render_question_pool([self.q]))

    def test_pool_renders_it_for_non_mcq_too(self):
        """It used to sit inside the mcq branch, which silently dropped it for
        every numeric and short-answer question — and also stole the else that
        renders reference_answer."""
        self.q.question_type = 'short_numeric'
        self.q.answer_data = {'model_answer': '42'}
        self.q.save(update_fields=['question_type', 'answer_data'])
        out = _render_question_pool([self.q])
        self.assertIn(EXPLANATION, out)
        self.assertIn('<reference_answer>', out,
                      'the mcq/else split was broken — numeric lost its answer')


class OpeningLeadInTest(TestCase):
    """qwen3-4b copied the example lead-in verbatim in 34 of 34 sessions and
    never greeted the student once; qwen3.8-27b ignored it and greeted 91% of
    the time. A concrete example next to the generation point beats an abstract
    instruction in the system prompt, and small models copy it literally."""

    def test_pose_feedback_gives_no_verbatim_lead_in_to_copy(self):
        out = fmt('pose_question', {
            'posed': True, 'question_type': 'mcq', 'source': 'catalog',
        })
        self.assertNotIn("'Try this:'", out)

    def test_it_still_demands_the_stem_in_the_visible_reply(self):
        """The requirement that made the example necessary must survive."""
        out = fmt('pose_question', {
            'posed': True, 'question_type': 'mcq', 'source': 'catalog',
        })
        self.assertIn('stem', out.lower())


class CatalogQuestionIdIsAPrimaryKeyTest(TestCase):
    """The field is read as a pk in three places and was written as a pool
    index. Everything downstream was silently resolving the wrong question:
    the catalog cross-check compared against a stranger's correct_answer, the
    pivot ladder read a stranger's difficulty, and the explanation lookup
    fetched a stranger and returned nothing — which is why "explain why the
    answer is right" shipped and did nothing at all.
    """

    @classmethod
    def setUpTestData(cls):
        inst = Institution.objects.create(name='PK', slug='pk')
        course = Course.objects.create(title='C', institution=inst,
                                       grade_level='S3', is_published=True)
        unit = Unit.objects.create(course=course, title='U', order_index=0)
        lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                       order_index=0, is_published=True)
        ticket = ExitTicket.objects.create(lesson=lesson)
        # Several rows, so a pool INDEX (1, 2, 3...) and a PK cannot coincide.
        cls.qs = [
            ExitTicketQuestion.objects.create(
                exit_ticket=ticket, question_type='mcq',
                question_text=f'Q{i}?', option_a='a', option_b='b',
                option_c='c', option_d='d', correct_answer='B',
                explanation=f'because reason {i}')
            for i in range(4)
        ]

    def test_posing_pool_entry_2_stores_that_question_pk(self):
        from ai_tutor.apps.tutoring.models import TutorSession
        from ai_tutor.apps.tutoring.simple_tutor.tools import (
            handle_pose_question_by_index,
        )
        from django.contrib.auth import get_user_model

        lesson = self.qs[0].exit_ticket.lesson
        inst = lesson.unit.course.institution
        user = get_user_model().objects.create_user('pk-stu', password='x')
        session = TutorSession.objects.create(
            institution=inst, student=user, lesson=lesson, engine='simple')

        # REVERSED pool, so position and pk cannot coincide: pks are 1..4 in
        # creation order, so pool position 1 is now the question with the
        # HIGHEST pk. Ordering them the same way is how the first version of
        # this test passed against the bug.
        pool = list(reversed(self.qs))
        target = pool[1]                       # pool position 2
        handle_pose_question_by_index(session, question_index=2, question_pool=pool)

        from ai_tutor.apps.tutoring.models import InFlightQuestion
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(
            slot.catalog_question_id, target.pk,
            'catalog_question_id must be the question pk, not the pool index')
        self.assertNotEqual(
            slot.catalog_question_id, 2,
            'storing the pool index is the bug this test exists for')

    def test_the_stored_id_resolves_back_to_the_posed_question(self):
        """The property every reader depends on."""
        from ai_tutor.apps.tutoring.models import TutorSession, InFlightQuestion
        from ai_tutor.apps.tutoring.simple_tutor.tools import (
            handle_pose_question_by_index,
        )
        from django.contrib.auth import get_user_model

        lesson = self.qs[0].exit_ticket.lesson
        inst = lesson.unit.course.institution
        user = get_user_model().objects.create_user('pk-stu2', password='x')
        session = TutorSession.objects.create(
            institution=inst, student=user, lesson=lesson, engine='simple')
        pool = list(reversed(self.qs))
        handle_pose_question_by_index(session, question_index=3, question_pool=pool)
        slot = InFlightQuestion.objects.get(session=session)
        back = ExitTicketQuestion.objects.get(pk=slot.catalog_question_id)
        self.assertEqual(back.question_text, pool[2].question_text)
        self.assertEqual(back.explanation, pool[2].explanation)


class TaskInstructionIsPlacedToBeReadTest(TestCase):
    """Placement, not wording, was why the explanation went unused.

    Measured 2026-08-24: the FIRST correct turn of 34 of 34 sessions reached
    for a templated acknowledgement — on a 4B AND a 27B, before any history
    existed. So it was not self-reinforcement from <recent_turns>, and not a
    small-model limit. Both branches arrived in one paragraph that opened with
    the wrong-answer ladder, so on a correct answer the model read the
    instruction that did not apply first, and the one that did was buried.
    """

    @classmethod
    def setUpTestData(cls):
        inst = Institution.objects.create(name='TP', slug='tp')
        course = Course.objects.create(title='C', institution=inst,
                                       grade_level='S3', is_published=True)
        unit = Unit.objects.create(course=course, title='U', order_index=0)
        lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                       order_index=0, is_published=True)
        ticket = ExitTicket.objects.create(lesson=lesson)
        cls.q = ExitTicketQuestion.objects.create(
            exit_ticket=ticket, question_type='mcq', question_text='Q?',
            option_a='a', option_b='b', option_c='c', option_d='d',
            correct_answer='B', explanation=EXPLANATION)

    def _fb(self, verdict):
        return fmt('record_answer', {
            'recorded': True, 'verdict': verdict, 'reference_answer': 'B',
            'question_text': 'Q?', 'question_type': 'mcq',
            'attempt_count_before': 0, 'catalog_question_id': self.q.pk})

    def test_the_task_is_the_last_thing_read(self):
        """Final tokens steer generation most. The task used to sit 6th of 8
        blocks, behind text about the branch that did not apply."""
        self.assertTrue(self._fb('correct').rstrip().endswith(
            'Do not reference older questions from <recent_turns>.'))
        self.assertIn('YOUR TASK THIS TURN',
                      self._fb('correct').split('\n')[-1])

    def test_explanation_sits_immediately_above_the_task(self):
        lines = [l for l in self._fb('correct').split('\n') if l.strip()]
        expl = next(i for i, l in enumerate(lines) if l.startswith('<explanation>'))
        task = next(i for i, l in enumerate(lines) if l.startswith('YOUR TASK'))
        self.assertEqual(task, expl + 1,
                         'the material and the task that uses it must be adjacent')

    def test_a_correct_verdict_never_sees_the_hint_ladder(self):
        """The whole failure: reading the inapplicable branch first."""
        out = self._fb('correct')
        self.assertNotIn('progressively deeper scaffolding', out)
        self.assertNotIn('Give one hint', out)

    def test_a_wrong_verdict_never_sees_the_explain_instruction(self):
        out = self._fb('incorrect')
        self.assertNotIn('Open with the REASON', out)
        self.assertNotIn(EXPLANATION, out)

    def test_the_instruction_is_framed_positively(self):
        """Naming the phrasing to avoid is how "(e.g. 'Try this:')" became the
        opener in 34 of 34 sessions. State what to do, not what not to say."""
        out = self._fb('correct')
        for banned in ("that's right", "Got it", "don't say", "Do not say"):
            self.assertNotIn(banned, out,
                             f'{banned!r} names a phrasing the model can copy')
