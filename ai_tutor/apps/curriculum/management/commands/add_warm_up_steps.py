"""Give any lesson that lacks one a warm-up step at order_index 0.

Migration 0034 does this for every lesson that existed when it ran. This command
is the same operation, runnable again — for lessons imported from a content pack
built before the migration, or created by any path that does not know to add one.

Idempotent: a lesson that already has a warm-up step is left alone, so running
it twice cannot shift a lesson's steps twice.

    python manage.py add_warm_up_steps [--dry-run]
"""
from django.core.management.base import BaseCommand
from django.db import models, transaction

from ai_tutor.apps.curriculum.models import Lesson, LessonStep
from ai_tutor.apps.tutoring.models import TutorSession


class Command(BaseCommand):
    help = "Add a warm-up step at order_index 0 to lessons that lack one."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        have = set(
            LessonStep.objects
            .filter(step_type=LessonStep.StepType.WARM_UP)
            .values_list('lesson_id', flat=True)
        )
        lesson_ids = list(
            Lesson.objects.exclude(id__in=have).values_list('id', flat=True)
        )

        if not lesson_ids:
            self.stdout.write(self.style.SUCCESS(
                "Every lesson already has a warm-up step. Nothing to do."))
            return

        sessions = TutorSession.objects.filter(lesson_id__in=lesson_ids).count()
        self.stdout.write(
            f"{len(lesson_ids)} lesson(s) need a warm-up step; "
            f"{sessions} session(s) will have current_step_index bumped."
        )

        if options['dry_run']:
            self.stdout.write(self.style.WARNING("--dry-run: nothing written."))
            return

        with transaction.atomic():
            LessonStep.objects.filter(lesson_id__in=lesson_ids).update(
                order_index=models.F('order_index') + 1
            )
            LessonStep.objects.bulk_create([
                LessonStep(
                    lesson_id=lesson_id,
                    order_index=0,
                    step_type=LessonStep.StepType.WARM_UP,
                    phase='engage',
                    question='',
                    enabling_objective='',
                    teacher_script='',
                    priority=LessonStep.Priority.REQUIRED,
                    answer_type=LessonStep.AnswerType.MULTIPLE_CHOICE,
                )
                for lesson_id in lesson_ids
            ])
            # The index is a position, not a foreign key — leaving sessions
            # behind would move every open lesson onto the next step's content.
            TutorSession.objects.filter(lesson_id__in=lesson_ids).update(
                current_step_index=models.F('current_step_index') + 1
            )

        self.stdout.write(self.style.SUCCESS(
            f"Added {len(lesson_ids)} warm-up step(s)."))
