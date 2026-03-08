"""
Backfill StudentLessonProgress records for existing TutorSessions.

Idempotent — safe to run multiple times (uses get_or_create).
"""
from django.core.management.base import BaseCommand
from apps.tutoring.models import TutorSession, StudentLessonProgress


class Command(BaseCommand):
    help = 'Create missing StudentLessonProgress records for existing sessions'

    def handle(self, *args, **options):
        sessions = (
            TutorSession.objects
            .select_related('lesson', 'institution')
            .values_list('student_id', 'lesson_id', 'institution_id')
            .distinct()
        )

        created_count = 0
        for student_id, lesson_id, institution_id in sessions:
            _, created = StudentLessonProgress.objects.get_or_create(
                student_id=student_id,
                lesson_id=lesson_id,
                defaults={
                    'institution_id': institution_id,
                    'mastery_level': 'in_progress',
                },
            )
            if created:
                created_count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Backfill complete: {created_count} progress records created'
        ))
