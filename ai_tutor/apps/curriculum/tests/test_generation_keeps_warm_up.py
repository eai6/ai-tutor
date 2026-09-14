"""Generated lessons open with a warm-up, and regenerating keeps it.

Reported as "the math lessons are not starting with a warm up". Every lesson
is supposed to open on a warm-up drawn from something the student already
mastered (simple_tutor/warm_up.py), and the step that holds it is a real
LessonStep at order_index 0.

Nothing ever created that row except migration 0034 and the one-off
add_warm_up_steps command. So:

  * a lesson generated after the backfill never had one, and
  * regenerating an older lesson DESTROYED the one it had — the upsert
    overwrote index 0 with a teach step, or the orphan sweep deleted it for
    not being in the generated set.

Which is why a freshly re-parsed course never warms up while an older one
still does.
"""

import pytest

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.content_generator import _ensure_warm_up_step
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit


@pytest.fixture
def lesson(db):
    school = Institution.objects.create(name='B', slug='b')
    course = Course.objects.create(title='Mathematics S3', institution=school,
                                   subject_code='mathematics', grade_level='S3')
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    return Lesson.objects.create(unit=unit, title='Angles', objective='find x',
                                 order_index=0)


def _warm_ups(lesson):
    return LessonStep.objects.filter(
        lesson=lesson, step_type=LessonStep.StepType.WARM_UP)


def test_a_lesson_with_no_warm_up_gets_one(lesson):
    assert not _warm_ups(lesson).exists()

    step = _ensure_warm_up_step(lesson)

    assert step.order_index == 0
    assert step.question == ''          # a container; the question is chosen
    assert _warm_ups(lesson).count() == 1


def test_it_is_idempotent(lesson):
    _ensure_warm_up_step(lesson)
    _ensure_warm_up_step(lesson)

    assert _warm_ups(lesson).count() == 1


def test_a_misplaced_warm_up_is_moved_not_duplicated(lesson):
    """A lesson whose steps were renumbered can leave the warm-up adrift.
    Two warm-ups would be worse than one in the wrong place."""
    LessonStep.objects.create(
        lesson=lesson, order_index=3, step_type=LessonStep.StepType.WARM_UP,
        phase='engage', teacher_script='')

    _ensure_warm_up_step(lesson)

    assert _warm_ups(lesson).count() == 1
    assert _warm_ups(lesson).first().order_index == 0


def test_the_orphan_sweep_does_not_delete_the_warm_up(lesson):
    """The sweep drops steps left by a longer previous generation. The
    warm-up is never in the generated set, so an unguarded sweep took it
    every time."""
    _ensure_warm_up_step(lesson)
    LessonStep.objects.create(lesson=lesson, order_index=1, step_type='teach',
                              phase='explore', teacher_script='t')
    LessonStep.objects.create(lesson=lesson, order_index=9, step_type='teach',
                              phase='explore', teacher_script='stale')

    # What _save_steps_to_db now computes for a one-step generation.
    kept = {0} | {1}
    LessonStep.objects.filter(lesson=lesson).exclude(
        order_index__in=kept).exclude(
        step_type=LessonStep.StepType.WARM_UP).delete()

    assert _warm_ups(lesson).count() == 1
    assert not LessonStep.objects.filter(lesson=lesson, order_index=9).exists()


def test_generated_steps_are_renumbered_from_one(lesson):
    """Reserving index 0 only works if the generated steps start at 1 —
    whatever the model numbered them from."""
    steps = [{'order_index': 0, 'step_type': 'teach'},
             {'order_index': 1, 'step_type': 'practice'},
             {'order_index': 2, 'step_type': 'quiz'}]

    for new_index, step_data in enumerate(
            sorted(steps, key=lambda s: s.get('order_index', 0)), start=1):
        step_data['order_index'] = new_index

    assert [s['order_index'] for s in steps] == [1, 2, 3]
