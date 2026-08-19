"""Give every lesson a warm-up step at order_index 0.

The lesson now opens on a warm-up drawn from something the student already
learned, and that slot is a real LessonStep so it advances like any other step.
Existing lessons have none, so every step shifts up by one to make room.

Two things make this safe to run against live data:

  * There is no unique constraint on (lesson, order_index) — LessonStep.Meta
    declares only `ordering` — so a bulk F()+1 needs no ordering games and
    cannot collide mid-update.
  * TutorSession.current_step_index is an INDEX, not a foreign key. Shifting
    the steps without shifting the sessions would silently move every student
    in an open lesson onto the next step's content. So sessions move too, in
    the same migration. Sessions sitting past the last step (exit ticket or
    remediation) stay past it, which is what +1 does for them as well.

SessionTurn.step is a real FK and follows its row, so it needs nothing.
"""
from django.db import migrations, models


WARM_UP = 'warm_up'


def add_warm_up_steps(apps, schema_editor):
    Lesson = apps.get_model('curriculum', 'Lesson')
    LessonStep = apps.get_model('curriculum', 'LessonStep')
    TutorSession = apps.get_model('tutoring', 'TutorSession')

    already = set(
        LessonStep.objects
        .filter(step_type=WARM_UP)
        .values_list('lesson_id', flat=True)
    )
    lesson_ids = list(
        Lesson.objects.exclude(id__in=already).values_list('id', flat=True)
    )
    if not lesson_ids:
        return

    LessonStep.objects.filter(lesson_id__in=lesson_ids).update(
        order_index=models.F('order_index') + 1
    )

    LessonStep.objects.bulk_create([
        LessonStep(
            lesson_id=lesson_id,
            order_index=0,
            step_type=WARM_UP,
            phase='engage',
            # No question and no enabling_objective on purpose: both belong to
            # the PRIOR lesson this warm-up recalls, and which lesson that is
            # differs per student. simple_tutor/warm_up.py resolves it at
            # runtime.
            question='',
            enabling_objective='',
            teacher_script='',
            priority=1,
            answer_type='multiple_choice',
        )
        for lesson_id in lesson_ids
    ])

    TutorSession.objects.filter(lesson_id__in=lesson_ids).update(
        current_step_index=models.F('current_step_index') + 1
    )


def remove_warm_up_steps(apps, schema_editor):
    LessonStep = apps.get_model('curriculum', 'LessonStep')
    TutorSession = apps.get_model('tutoring', 'TutorSession')

    lesson_ids = list(
        LessonStep.objects
        .filter(step_type=WARM_UP)
        .values_list('lesson_id', flat=True)
    )
    if not lesson_ids:
        return

    LessonStep.objects.filter(step_type=WARM_UP).delete()
    LessonStep.objects.filter(lesson_id__in=lesson_ids).update(
        order_index=models.F('order_index') - 1
    )
    # Floor at 0: a session that had advanced ONTO the warm-up step would
    # otherwise land on -1, and current_step_index is a PositiveIntegerField.
    TutorSession.objects.filter(
        lesson_id__in=lesson_ids, current_step_index__gt=0,
    ).update(current_step_index=models.F('current_step_index') - 1)


class Migration(migrations.Migration):

    dependencies = [
        ('curriculum', '0033_lessonstep_warm_up_type'),
        ('tutoring', '0036_add_client_uuid_sync_keys'),
    ]

    operations = [
        migrations.RunPython(add_warm_up_steps, remove_warm_up_steps),
    ]
