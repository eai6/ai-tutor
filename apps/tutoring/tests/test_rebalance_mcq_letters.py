"""Tests for the MCQ letter-rebalance command.

The bank had 60.6% of 7,073 MCQs answering B — a student answering B to
everything scored 60.6% where blind guessing should score 25%. Since the tutor
became catalog-only, every question a student sees comes from this bank, so the
bias is no longer diluted by tutor-authored questions.

The invariant these tests exist for: **rebalancing must never change which
option is correct.** A swap that moved the letter without moving the text would
silently mark the right answer wrong across the whole bank — worse than the bias
it fixes.
"""
from __future__ import annotations

from collections import Counter
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase as DjangoTestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.tutoring.models import (
    ExitTicket, ExitTicketQuestion, TutorSession,
)

User = get_user_model()

_n = {'i': 0}


def _lesson():
    _n['i'] += 1
    i = _n['i']
    inst = Institution.objects.create(name=f'S{i}', slug=f's{i}')
    course = Course.objects.create(title=f'C{i}', institution=inst,
                                   grade_level='S3', is_published=True)
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                   order_index=0, is_published=True)
    LessonStep.objects.create(lesson=lesson, teacher_script='s', phase='engage',
                              order_index=0, enabling_objective=f'eo-{i}')
    return lesson, inst


def _mcq(ticket, *, correct='B', a='alpha', b='beta', c='gamma', d='delta',
         stem=None, order=0):
    return ExitTicketQuestion.objects.create(
        exit_ticket=ticket, question_type='mcq',
        question_text=stem or f'Q{order}?',
        option_a=a, option_b=b, option_c=c, option_d=d,
        correct_answer=correct, enabling_objective='eo', order_index=order,
    )


def _bank(n=40, correct='B'):
    lesson, inst = _lesson()
    ticket = ExitTicket.objects.create(lesson=lesson)
    return [
        _mcq(ticket, correct=correct, a=f'a{i}', b=f'b{i}', c=f'c{i}',
             d=f'd{i}', order=i)
        for i in range(n)
    ], lesson, inst


def _correct_text(q):
    return getattr(q, f'option_{(q.correct_answer or "").lower()}', None)


class RebalanceCorrectnessTest(DjangoTestCase):
    """The bank must mean the same thing afterwards."""

    def test_correct_option_text_is_unchanged(self):
        qs, _, _ = _bank(40)
        before = {q.id: _correct_text(q) for q in qs}

        call_command('rebalance_mcq_letters', '--apply', stdout=StringIO())

        for q in ExitTicketQuestion.objects.filter(id__in=before):
            self.assertEqual(
                _correct_text(q), before[q.id],
                'the correct answer must still be the same TEXT',
            )

    def test_option_texts_are_permuted_not_lost(self):
        qs, _, _ = _bank(40)
        before = {q.id: sorted([q.option_a, q.option_b, q.option_c, q.option_d])
                  for q in qs}

        call_command('rebalance_mcq_letters', '--apply', stdout=StringIO())

        for q in ExitTicketQuestion.objects.filter(id__in=before):
            self.assertEqual(
                sorted([q.option_a, q.option_b, q.option_c, q.option_d]),
                before[q.id], 'every option must survive, only positions move',
            )


class RebalanceDistributionTest(DjangoTestCase):

    def test_all_b_bank_flattens(self):
        _bank(80, correct='B')
        call_command('rebalance_mcq_letters', '--apply', stdout=StringIO())
        dist = Counter(
            ExitTicketQuestion.objects.values_list('correct_answer', flat=True))
        for letter in 'ABCD':
            share = dist.get(letter, 0) / 80
            self.assertGreater(share, 0.15, f'{letter} still under-represented')
            self.assertLess(share, 0.35, f'{letter} still over-represented')

    def test_same_seed_is_reproducible(self):
        _bank(40)
        call_command('rebalance_mcq_letters', '--apply', '--seed', '7',
                     stdout=StringIO())
        first = dict(ExitTicketQuestion.objects.values_list('id', 'correct_answer'))

        # Re-running with the same seed must not thrash the bank further.
        call_command('rebalance_mcq_letters', '--apply', '--seed', '7',
                     stdout=StringIO())
        second = dict(ExitTicketQuestion.objects.values_list('id', 'correct_answer'))
        self.assertEqual(first, second)


class RebalanceSkipTest(DjangoTestCase):

    def test_dry_run_writes_nothing(self):
        qs, _, _ = _bank(20)
        before = dict(ExitTicketQuestion.objects.values_list('id', 'correct_answer'))
        out = StringIO()
        call_command('rebalance_mcq_letters', stdout=out)
        self.assertIn('Dry run', out.getvalue())
        self.assertEqual(
            dict(ExitTicketQuestion.objects.values_list('id', 'correct_answer')),
            before)

    def test_positional_options_are_skipped(self):
        lesson, _ = _lesson()
        ticket = ExitTicket.objects.create(lesson=lesson)
        q = _mcq(ticket, correct='D', a='x', b='y', c='z', d='All of the above')
        call_command('rebalance_mcq_letters', '--apply', stdout=StringIO())
        q.refresh_from_db()
        self.assertEqual(q.correct_answer, 'D')
        self.assertEqual(q.option_d, 'All of the above')

    def test_sorted_numeric_options_are_skipped(self):
        lesson, _ = _lesson()
        ticket = ExitTicket.objects.create(lesson=lesson)
        q = _mcq(ticket, correct='B', a='10', b='20', c='30', d='40')
        call_command('rebalance_mcq_letters', '--apply', stdout=StringIO())
        q.refresh_from_db()
        self.assertEqual(q.correct_answer, 'B')
        self.assertEqual([q.option_a, q.option_b], ['10', '20'])

    def test_questions_already_shown_are_skipped(self):
        """A completed attempt records the letter the student picked, and the
        dashboard reconstructs their answer against the CURRENT option text.
        Moving options under an answered question misreports pilot data.
        """
        qs, lesson, inst = _bank(20)
        shown = qs[0]
        user = User.objects.create_user(username='stu-shown', password='x')
        TutorSession.objects.create(
            institution=inst, student=user, lesson=lesson, engine='simple',
            engine_state={'selected_exit_ticket_ids': [shown.id]},
        )
        before_text = _correct_text(shown)
        before_letter = shown.correct_answer

        call_command('rebalance_mcq_letters', '--apply', stdout=StringIO())

        shown.refresh_from_db()
        self.assertEqual(shown.correct_answer, before_letter)
        self.assertEqual(_correct_text(shown), before_text)

    def test_include_answered_overrides_the_skip(self):
        qs, lesson, inst = _bank(20)
        shown = qs[0]
        user = User.objects.create_user(username='stu-shown2', password='x')
        TutorSession.objects.create(
            institution=inst, student=user, lesson=lesson, engine='simple',
            engine_state={'selected_exit_ticket_ids': [shown.id]},
        )
        out = StringIO()
        call_command('rebalance_mcq_letters', '--apply', '--include-answered',
                     stdout=out)
        self.assertNotIn('already_shown', out.getvalue())
