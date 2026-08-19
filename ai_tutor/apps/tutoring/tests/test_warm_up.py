"""Warm-up step selection.

The warm-up opens every lesson with a question from something the student has
already learned. Selection is deterministic and server-side; these tests pin
the tier order, the determinism, the institution boundary, and the two ways it
is allowed to decline (no history, nothing answerable).
"""
import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.tutoring.models import (
    ExitTicket, ExitTicketQuestion, StudentLessonProgress, TutorSession,
)
from ai_tutor.apps.tutoring.simple_tutor.warm_up import select_warm_up_question
from ai_tutor.apps.tutoring.skills_models import LessonPrerequisite

pytestmark = pytest.mark.django_db


def _lesson(course, title, order_index=0):
    unit = Unit.objects.create(course=course, title=f'U-{title}', order_index=order_index)
    return Lesson.objects.create(unit=unit, title=title, order_index=order_index)


def _bank(lesson, stems, difficulty='easy'):
    """One exit ticket per lesson (the FK is OneToOne), N questions on it.

    ``stems`` items may be a plain string or a (stem, difficulty) pair when a
    test needs a mixed-difficulty bank.
    """
    ticket = ExitTicket.objects.create(
        lesson=lesson,
        assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
    )
    made = []
    for i, stem in enumerate(stems):
        text, level = stem if isinstance(stem, tuple) else (stem, difficulty)
        made.append(ExitTicketQuestion.objects.create(
            exit_ticket=ticket,
            question_type=ExitTicketQuestion.QuestionType.MCQ,
            question_text=text,
            option_a='a', option_b='b', option_c='c', option_d='d',
            correct_answer='B',
            difficulty=level,
            order_index=i,
        ))
    return made


def _mastered(student, lesson, institution, when=None):
    return StudentLessonProgress.objects.create(
        institution=institution,
        student=student,
        lesson=lesson,
        mastery_level=StudentLessonProgress.MasteryLevel.MASTERED,
        last_attempt_at=when or timezone.now(),
    )


@pytest.fixture
def world(db):
    inst = Institution.objects.create(name='Test School', slug='test-school')
    student = User.objects.create_user('warmup-student', password='x')
    course = Course.objects.create(institution=inst, title='Geography')
    today = _lesson(course, 'Land Use', order_index=3)
    session = TutorSession.objects.create(
        institution=inst, student=student, lesson=today,
    )
    return {
        'inst': inst, 'student': student, 'course': course,
        'today': today, 'session': session,
    }


def test_returns_none_without_history(world):
    """A student's first-ever lesson has nothing to recall."""
    assert select_warm_up_question(world['session']) is None


def test_prefers_a_mastered_prerequisite_over_a_more_recent_lesson(world):
    prereq = _lesson(world['course'], 'Map Symbols', order_index=1)
    unrelated = _lesson(world['course'], 'Climate', order_index=2)
    _bank(prereq, ['What does a key show?'])
    _bank(unrelated, ['What is rainfall?'])

    # The unrelated lesson is the more recent one, so recency alone would pick
    # it. The prerequisite must win.
    _mastered(world['student'], prereq, world['inst'],
              when=timezone.now() - timezone.timedelta(days=10))
    _mastered(world['student'], unrelated, world['inst'], when=timezone.now())
    LessonPrerequisite.objects.create(
        lesson=world['today'], prerequisite=prereq, strength=1.0, is_direct=True,
    )

    chosen = select_warm_up_question(world['session'])
    assert chosen is not None
    assert chosen.exit_ticket.lesson_id == prereq.id


def test_falls_back_to_most_recent_when_no_prerequisite(world):
    older = _lesson(world['course'], 'Older', order_index=1)
    newer = _lesson(world['course'], 'Newer', order_index=2)
    _bank(older, ['old question'])
    _bank(newer, ['new question'])
    _mastered(world['student'], older, world['inst'],
              when=timezone.now() - timezone.timedelta(days=30))
    _mastered(world['student'], newer, world['inst'], when=timezone.now())

    chosen = select_warm_up_question(world['session'])
    assert chosen is not None
    assert chosen.exit_ticket.lesson_id == newer.id


def test_is_deterministic_for_a_session(world):
    prior = _lesson(world['course'], 'Prior', order_index=1)
    _bank(prior, [f'q{i}' for i in range(8)])
    _mastered(world['student'], prior, world['inst'])

    picks = {select_warm_up_question(world['session']).pk for _ in range(5)}
    assert len(picks) == 1, "same session must resolve the same warm-up"


def test_skips_figure_dependent_questions(world):
    """The prior lesson's figures are not loaded, so its diagram questions are
    unanswerable here."""
    prior = _lesson(world['course'], 'Prior', order_index=1)
    _bank(prior, [
        'Look at Figure 3 — what does it show?',
        'Using the diagram above, name the river.',
    ])
    _mastered(world['student'], prior, world['inst'])

    assert select_warm_up_question(world['session']) is None


def test_never_selects_another_institutions_lesson(world):
    other_inst = Institution.objects.create(name='Other School', slug='other-school')
    other_course = Course.objects.create(institution=other_inst, title='Theirs')
    theirs = _lesson(other_course, 'Their Lesson', order_index=1)
    _bank(theirs, ['their question'])
    StudentLessonProgress.objects.create(
        institution=other_inst,
        student=world['student'],
        lesson=theirs,
        mastery_level=StudentLessonProgress.MasteryLevel.MASTERED,
        last_attempt_at=timezone.now(),
    )

    assert select_warm_up_question(world['session']) is None


def test_platform_wide_lessons_are_visible(world):
    """institution=None means 'all schools' and must stay selectable."""
    global_course = Course.objects.create(institution=None, title='Shared')
    shared = _lesson(global_course, 'Shared Lesson', order_index=1)
    _bank(shared, ['shared question'])
    _mastered(world['student'], shared, world['inst'])

    chosen = select_warm_up_question(world['session'])
    assert chosen is not None
    assert chosen.exit_ticket.lesson_id == shared.id


def test_prefers_easy_over_hard(world):
    prior = _lesson(world['course'], 'Prior', order_index=1)
    _bank(prior, [('hard one', 'hard'), ('easy one', 'easy')])
    _mastered(world['student'], prior, world['inst'])

    chosen = select_warm_up_question(world['session'])
    assert chosen is not None
    assert chosen.difficulty == 'easy'


def test_hard_only_bank_is_declined(world):
    """'hard' is excluded outright — a warm-up should not gate entry."""
    prior = _lesson(world['course'], 'Prior', order_index=1)
    _bank(prior, ['hard one', 'another hard one'], difficulty='hard')
    _mastered(world['student'], prior, world['inst'])

    assert select_warm_up_question(world['session']) is None


def test_warm_up_step_pool_uses_the_prior_lesson(world):
    """build_question_pool must serve the warm-up, not this lesson's bank."""
    from ai_tutor.apps.tutoring.simple_tutor.tools import build_question_pool

    LessonStep.objects.create(
        lesson=world['today'], order_index=0,
        step_type=LessonStep.StepType.WARM_UP, phase='engage',
    )
    LessonStep.objects.create(
        lesson=world['today'], order_index=1,
        step_type=LessonStep.StepType.TEACH, phase='explain',
        enabling_objective='today objective',
    )
    _bank(world['today'], ['a question about today'])

    prior = _lesson(world['course'], 'Prior', order_index=1)
    _bank(prior, ['a question about last week'])
    _mastered(world['student'], prior, world['inst'])

    pool = build_question_pool(world['session'])
    # The warm-up leads; today's questions follow it, so a correct recall can
    # hand straight over to the lesson in the same turn instead of re-posing
    # the question just answered.
    assert pool[0].exit_ticket.lesson_id == prior.id
    assert [q.exit_ticket.lesson_id for q in pool[1:]] == [world['today'].id]


def test_correct_warm_up_answer_advances_one_step(world):
    """The warm-up advances like any other step — out of it, into step 1."""
    from ai_tutor.apps.tutoring.models import SessionTurn
    from ai_tutor.apps.tutoring.simple_tutor.tools import maybe_advance_step

    warm = LessonStep.objects.create(
        lesson=world['today'], order_index=0,
        step_type=LessonStep.StepType.WARM_UP, phase='engage',
    )
    LessonStep.objects.create(
        lesson=world['today'], order_index=1,
        step_type=LessonStep.StepType.TEACH, phase='explain',
    )
    SessionTurn.objects.create(
        session=world['session'], role=SessionTurn.Role.TUTOR,
        content='graded', step=warm,
        judge_outputs={'grader': {'verdict': 'correct'}},
    )

    assert maybe_advance_step(world['session']) is True
    world['session'].refresh_from_db()
    assert world['session'].current_step_index == 1
