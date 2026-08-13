"""Per-session question order in ``build_question_pool``.

The pool used to come back in ``(order_index, id)`` — the authoring order,
identical for every student and every retake. That was tolerable while the
tutor authored and adapted its own questions. Since catalog-only (f59bdb7) it
selects a pool INDEX, so the authoring order IS the teaching order: everyone
met question 1 first, forever.

Two properties have to hold together, and the interesting bugs break exactly
one of them:

  - different session → different order (the point)
  - same session → same order every turn (or the pool the model read at
    index 2 last turn is something else this turn)

and one that outranks both: the tiers are pedagogy. Questions on THIS step's
enabling_objective come before the rest of the lesson's. Shuffling across
tiers would trade an on-objective question for an off-objective one, which is
a worse bug than the one being fixed.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketQuestion, TutorSession
from ai_tutor.apps.tutoring.simple_tutor.tools import build_question_pool

User = get_user_model()

_n = {'i': 0}

ON_OBJ = 'Locate a four-figure grid reference'
OFF_OBJ = 'Measure a curved distance'


def _lesson_with_questions(*, n_on: int = 6, n_off: int = 4):
    """A lesson whose exit ticket carries questions on two objectives."""
    _n['i'] += 1
    i = _n['i']
    inst = Institution.objects.create(name=f'I{i}', slug=f'i{i}')
    course = Course.objects.create(
        title=f'C{i}', institution=inst, grade_level='S3', is_published=True)
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='L', objective='o', order_index=0, is_published=True)
    LessonStep.objects.create(
        lesson=lesson, order_index=0, phase='explain',
        teacher_script='s', enabling_objective=ON_OBJ,
    )
    et = ExitTicket.objects.create(lesson=lesson, passing_score=70)
    for k in range(n_on):
        ExitTicketQuestion.objects.create(
            exit_ticket=et, order_index=k, question_type='mcq',
            question_text=f'ON-{k}', enabling_objective=ON_OBJ,
            option_a='a', option_b='b', option_c='c', option_d='d',
            correct_answer='A',
        )
    for k in range(n_off):
        ExitTicketQuestion.objects.create(
            exit_ticket=et, order_index=100 + k, question_type='mcq',
            question_text=f'OFF-{k}', enabling_objective=OFF_OBJ,
            option_a='a', option_b='b', option_c='c', option_d='d',
            correct_answer='A',
        )
    return inst, lesson


def _session(inst, lesson):
    _n['i'] += 1
    user = User.objects.create_user(username=f'stu-qp-{_n["i"]}', password='x')
    return TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson, engine='simple',
        current_step_index=0,
    )


def _texts(session):
    return [q.question_text for q in build_question_pool(session)]


class QuestionPoolOrderTest(DjangoTestCase):

    def test_same_session_gets_the_same_order_every_call(self):
        """pose_question(question_index=N) is only meaningful if the pool the
        model read is the pool the index lands in. A free rng would also churn
        the ordering the tutor sees from turn to turn."""
        inst, lesson = _lesson_with_questions()
        s = _session(inst, lesson)
        self.assertEqual(_texts(s), _texts(s))
        self.assertEqual(_texts(s), _texts(s))

    def test_different_sessions_get_different_orders(self):
        """The actual ask. Retaking a lesson is a new session, so this is also
        what stops a retake replaying the same question first.

        Asserted over several sessions rather than a pair: two seeds landing
        on the same permutation is a 1-in-720 coincidence here, not a bug, and
        a pairwise assert would flake at that rate.
        """
        inst, lesson = _lesson_with_questions()
        orders = {tuple(_texts(_session(inst, lesson))) for _ in range(8)}
        self.assertGreater(
            len(orders), 1,
            'every session produced an identical pool — not randomised',
        )

    def test_on_objective_questions_still_outrank_the_rest_of_the_lesson(self):
        """The tier boundary is pedagogy and the shuffle must not cross it.

        Six questions sit on this step's objective and the pool holds six, so
        a correct implementation never reaches the other objective at all.
        """
        inst, lesson = _lesson_with_questions(n_on=6, n_off=4)
        for _ in range(8):
            texts = _texts(_session(inst, lesson))
            self.assertTrue(
                all(t.startswith('ON-') for t in texts),
                f'off-objective question displaced an on-objective one: {texts}',
            )

    def test_off_objective_questions_only_fill_what_is_left(self):
        """With too few on-objective questions the pool falls through to the
        rest of the lesson — but the on-objective ones all come first."""
        inst, lesson = _lesson_with_questions(n_on=2, n_off=4)
        texts = _texts(_session(inst, lesson))
        self.assertEqual(len(texts), 6)
        self.assertTrue(all(t.startswith('ON-') for t in texts[:2]))
        self.assertTrue(all(t.startswith('OFF-') for t in texts[2:]))

    def test_every_on_objective_question_can_come_first(self):
        """Shuffling only the tail would pass the 'orders differ' test while
        still showing everyone the same opening question."""
        inst, lesson = _lesson_with_questions(n_on=4, n_off=0)
        firsts = {_texts(_session(inst, lesson))[0] for _ in range(40)}
        self.assertGreater(
            len(firsts), 1,
            f'the opening question never varied: {firsts}',
        )
