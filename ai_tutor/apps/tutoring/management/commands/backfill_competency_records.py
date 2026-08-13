"""
Backfill StudentCompetencyRecord rows from existing mastery state.

Two passes, both idempotent (safe to re-run):

  1. Per-lesson — for every StudentLessonProgress with mastery_level
     == 'mastered', emit a LESSON-granularity competency record.
  2. Per-skill — for every StudentSkillMastery whose state is MASTERED,
     emit a SKILL-granularity competency record.

The unique constraints on StudentCompetencyRecord guarantee no
duplicates if this is run repeatedly. See
memory/student_competency_persistence_plan.md.
"""
from django.core.management.base import BaseCommand
from ai_tutor.apps.tutoring.models import StudentLessonProgress
from ai_tutor.apps.tutoring.skills_models import StudentSkillMastery, StudentCompetencyRecord


class Command(BaseCommand):
    help = 'Backfill StudentCompetencyRecord rows from existing mastered progress + skills'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print counts but do not write any records',
        )

    def handle(self, *args, **options):
        dry = options['dry_run']

        lesson_qs = (
            StudentLessonProgress.objects
            .filter(mastery_level='mastered')
            .select_related('student', 'lesson__unit__course', 'institution', 'last_completion_session')
        )
        skill_qs = (
            StudentSkillMastery.objects
            .filter(state=StudentSkillMastery.MasteryState.MASTERED)
            .select_related('student', 'skill__course', 'skill__unit', 'skill__primary_lesson', 'skill__institution')
        )

        self.stdout.write(f'Lesson-mastery rows to scan: {lesson_qs.count()}')
        self.stdout.write(f'Skill-mastery rows to scan:  {skill_qs.count()}')

        if dry:
            self.stdout.write(self.style.WARNING('Dry run — no records written.'))
            return

        lesson_written = 0
        lesson_skipped = 0
        for progress in lesson_qs.iterator():
            obj = StudentCompetencyRecord.record_lesson(
                progress, session=progress.last_completion_session
            )
            if obj is None:
                lesson_skipped += 1
            else:
                # get_or_create returns the row whether created or
                # found — only count "newly persisted" by checking
                # created_at proximity is unreliable, so just count
                # successful calls.
                lesson_written += 1

        skill_written = 0
        skill_skipped = 0
        for mastery in skill_qs.iterator():
            obj = StudentCompetencyRecord.record_skill(mastery)
            if obj is None:
                skill_skipped += 1
            else:
                skill_written += 1

        self.stdout.write(self.style.SUCCESS(
            f'Backfill complete. lesson: {lesson_written} processed, '
            f'{lesson_skipped} skipped. skill: {skill_written} processed, '
            f'{skill_skipped} skipped. (Re-running is safe; unique constraints '
            f'guarantee no duplicates.)'
        ))
