"""
Management command to detect and backfill lesson prerequisites.

Uses existing skill prerequisite relationships (no LLM calls).

Usage:
    python manage.py detect_prerequisites              # all courses
    python manage.py detect_prerequisites --course 5   # specific course
    python manage.py detect_prerequisites --clear      # wipe + rebuild
"""

from django.core.management.base import BaseCommand
from apps.curriculum.models import Course
from apps.tutoring.skills_models import LessonPrerequisite
from apps.tutoring.skill_extraction import SkillExtractionService


class Command(BaseCommand):
    help = 'Detect and backfill lesson prerequisites from existing skill relationships'

    def add_arguments(self, parser):
        parser.add_argument(
            '--course',
            type=int,
            help='Only process a specific course by ID',
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Delete existing prerequisites before rebuilding',
        )

    def handle(self, *args, **options):
        course_id = options.get('course')
        clear = options.get('clear', False)

        if course_id:
            courses = Course.objects.filter(id=course_id)
            if not courses.exists():
                self.stderr.write(self.style.ERROR(f'Course {course_id} not found'))
                return
        else:
            courses = Course.objects.all()

        total_created = 0

        for course in courses:
            if clear:
                deleted, _ = LessonPrerequisite.objects.filter(
                    lesson__unit__course=course
                ).delete()
                self.stdout.write(f'  Cleared {deleted} existing prerequisites for "{course.title}"')

            institution_id = course.institution_id or 0
            service = SkillExtractionService(institution_id=institution_id)
            created = service.detect_course_prerequisites(course)
            total_created += created
            self.stdout.write(f'  {course.title}: {created} prerequisites created')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {total_created} total prerequisites created across {courses.count()} course(s).'
        ))
