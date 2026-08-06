"""Remediation must open with something for the student to DO.

A failed exit ticket used to end on "You scored 4 out of 10. Let's revisit the
concepts you missed." and stop — no in-flight slot, no question, nothing to
reply to. Remediation only began if the student typed something unprompted,
and since there was no slot even that had nothing to grade.

Proactive remediation was removed on 2026-05-26 because it added a synchronous
5-15s LLM call on top of deterministic MCQ grading and made the grading spinner
look hung. Since the tutor became catalog-only the server picks the question
itself, so the opener is a DB read and the original objection is gone.

Every tutor turn owes the student a question or an action. These tests hold that
line for the remediation opener.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.tutoring.models import (
    ExitTicket, ExitTicketQuestion, InFlightQuestion, TutorSession,
)
from apps.tutoring.simple_tutor.exit_ticket import _remediation_opening_question
from apps.tutoring.simple_tutor.tools import _norm_q

User = get_user_model()
_n = {'i': 0}

OBJECTIVE = 'Determine the easting value'


def _session_with_bank(n_questions=3):
    _n['i'] += 1
    i = _n['i']
    inst = Institution.objects.create(name=f'S{i}', slug=f's{i}')
    user = User.objects.create_user(username=f'stu-rem-{i}', password='x')
    course = Course.objects.create(title=f'C{i}', institution=inst,
                                   grade_level='S3', is_published=True)
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                   order_index=0, is_published=True)
    LessonStep.objects.create(lesson=lesson, teacher_script='s', phase='engage',
                              order_index=0, enabling_objective=OBJECTIVE)
    ticket = ExitTicket.objects.create(lesson=lesson)
    questions = [
        ExitTicketQuestion.objects.create(
            exit_ticket=ticket, question_type='mcq',
            question_text=f'Easting question {j}?',
            option_a=f'a{j}', option_b=f'b{j}', option_c=f'c{j}', option_d=f'd{j}',
            correct_answer='C', enabling_objective=OBJECTIVE, order_index=j,
        )
        for j in range(n_questions)
    ]
    session = TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson, engine='simple')
    return session, questions


def _competency(*, asked=2, correct=0, failed_ids=None):
    return {OBJECTIVE: {'asked': asked, 'correct': correct,
                        'failed_question_ids': failed_ids or []}}


class RemediationOpenerTest(DjangoTestCase):

    def test_opener_poses_a_question_and_renders_it(self):
        session, questions = _session_with_bank()
        out = _remediation_opening_question(session, _competency())

        self.assertTrue(out, 'remediation must not open with nothing to do')
        self.assertIn(OBJECTIVE, out)
        slot = InFlightQuestion.objects.get(session=session)
        self.assertEqual(slot.source, 'catalog')
        self.assertIn(slot.question_text, out)
        # The student needs the options in front of them, not just the stem.
        for letter in ('A)', 'B)', 'C)', 'D)'):
            self.assertIn(letter, out)

    def test_prefers_a_question_the_student_did_not_just_fail(self):
        session, questions = _session_with_bank(3)
        failed = questions[0]
        out = _remediation_opening_question(
            session, _competency(failed_ids=[failed.pk]))
        slot = InFlightQuestion.objects.get(session=session)
        self.assertNotEqual(slot.question_text, failed.question_text)
        self.assertIn(slot.question_text, out)

    def test_never_reopens_on_a_question_already_answered_correctly(self):
        """The anti-repeat guard flags these as already_correct. Opening
        remediation with one wastes the turn and reads as the tutor not having
        noticed what the student already knows.
        """
        session, questions = _session_with_bank(3)
        known = questions[0]
        session.engine_state = {'answered_correct': [_norm_q(known.question_text)]}
        session.save(update_fields=['engine_state'])

        _remediation_opening_question(session, _competency())
        slot = InFlightQuestion.objects.get(session=session)
        self.assertNotEqual(slot.question_text, known.question_text)

    def test_falls_back_when_every_candidate_was_failed(self):
        """Re-asking a failed item is the fallback, not a reason to give up."""
        session, questions = _session_with_bank(2)
        out = _remediation_opening_question(
            session, _competency(failed_ids=[q.pk for q in questions]))
        self.assertTrue(out)
        self.assertTrue(InFlightQuestion.objects.filter(session=session).exists())

    def test_no_missed_objectives_means_no_opener(self):
        session, _ = _session_with_bank()
        out = _remediation_opening_question(
            session, _competency(asked=2, correct=2))
        self.assertEqual(out, '')
        self.assertFalse(InFlightQuestion.objects.filter(session=session).exists())

    def test_objective_with_no_bank_questions_is_silent(self):
        session, _ = _session_with_bank()
        out = _remediation_opening_question(
            session, {'An objective with no questions': {
                'asked': 1, 'correct': 0, 'failed_question_ids': []}})
        self.assertEqual(out, '')

    def test_worst_objective_is_targeted_first(self):
        session, questions = _session_with_bank(1)
        # A second objective the student did better on.
        other = 'Read the northing value'
        ticket = ExitTicket.objects.get(lesson=session.lesson)
        ExitTicketQuestion.objects.create(
            exit_ticket=ticket, question_type='mcq',
            question_text='Northing question?', option_a='a', option_b='b',
            option_c='c', option_d='d', correct_answer='A',
            enabling_objective=other, order_index=9,
        )
        out = _remediation_opening_question(session, {
            OBJECTIVE: {'asked': 4, 'correct': 0, 'failed_question_ids': []},
            other: {'asked': 4, 'correct': 3, 'failed_question_ids': []},
        })
        self.assertIn(OBJECTIVE, out)

    def test_never_raises_on_malformed_competency(self):
        session, _ = _session_with_bank()
        for bad in ({}, None, {'x': 'not-a-dict'}, {'': {'asked': 1}}):
            self.assertEqual(_remediation_opening_question(session, bad), '')
