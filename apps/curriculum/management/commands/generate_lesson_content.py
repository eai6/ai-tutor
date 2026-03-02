"""
Management command to generate content for lessons.

Usage:
    # Generate for a single lesson
    python manage.py generate_lesson_content --lesson-id 123
    
    # Generate for all lessons in a course
    python manage.py generate_lesson_content --course-id 5
    
    # Generate for a single lesson (dry run - don't save)
    python manage.py generate_lesson_content --lesson-id 123 --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from apps.curriculum.models import Course, Lesson


class Command(BaseCommand):
    help = 'Generate tutoring content (steps, exit tickets) for lessons'

    def add_arguments(self, parser):
        parser.add_argument(
            '--lesson-id',
            type=int,
            help='Generate content for a specific lesson'
        )
        parser.add_argument(
            '--course-id',
            type=int,
            help='Generate content for all lessons in a course'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview without saving to database'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Regenerate even if content exists'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Limit number of lessons to process'
        )

    def handle(self, *args, **options):
        from apps.curriculum.content_generator import LessonContentGenerator
        
        lesson_id = options.get('lesson_id')
        course_id = options.get('course_id')
        dry_run = options.get('dry_run')
        force = options.get('force')
        limit = options.get('limit')
        
        if not lesson_id and not course_id:
            raise CommandError('Please specify --lesson-id or --course-id')
        
        if lesson_id:
            lessons = Lesson.objects.filter(id=lesson_id)
        else:
            lessons = Lesson.objects.filter(unit__course_id=course_id)
        
        if not lessons.exists():
            raise CommandError('No lessons found')
        
        # Get institution from first lesson (fallback to Global for platform-wide content)
        from apps.accounts.models import Institution
        first_lesson = lessons.first()
        institution_id = first_lesson.unit.course.institution_id or Institution.get_global().id
        
        self.stdout.write(f"Found {lessons.count()} lesson(s)")
        self.stdout.write(f"Institution ID: {institution_id}")
        self.stdout.write(f"Dry run: {dry_run}")
        self.stdout.write("-" * 50)
        
        # Initialize generator
        try:
            generator = LessonContentGenerator(institution_id=institution_id)
            self.stdout.write(self.style.SUCCESS("✓ Generator initialized"))
        except Exception as e:
            raise CommandError(f"Failed to initialize generator: {e}")
        
        # Process lessons
        total_steps = 0
        success_count = 0
        fail_count = 0
        
        lessons_to_process = lessons
        if limit > 0:
            lessons_to_process = lessons[:limit]
        
        for idx, lesson in enumerate(lessons_to_process):
            self.stdout.write(f"\n[{idx+1}/{lessons_to_process.count()}] {lesson.title}")
            
            # Check if already has content
            existing_steps = lesson.steps.count()
            if existing_steps > 0 and not force:
                self.stdout.write(f"   Skipping - already has {existing_steps} steps (use --force to regenerate)")
                continue
            
            try:
                result = generator.generate_for_lesson(
                    lesson=lesson,
                    save_to_db=not dry_run
                )
                
                if result.get('success'):
                    steps = result.get('steps_generated', 0)
                    exit_q = result.get('exit_ticket_questions', 0)
                    total_steps += steps
                    success_count += 1
                    
                    self.stdout.write(self.style.SUCCESS(
                        f"   ✓ Generated {steps} steps, {exit_q} exit questions"
                    ))
                    
                    if dry_run:
                        # Show preview of first step
                        steps_data = result.get('steps', [])
                        if steps_data:
                            first_step = steps_data[0]
                            self.stdout.write(f"   Preview: [{first_step.get('phase')}] {first_step.get('teacher_script', '')[:100]}...")
                else:
                    fail_count += 1
                    self.stdout.write(self.style.ERROR(
                        f"   ✗ Failed: {result.get('error', 'Unknown error')}"
                    ))
                    
            except Exception as e:
                fail_count += 1
                self.stdout.write(self.style.ERROR(f"   ✗ Error: {e}"))
                import traceback
                self.stdout.write(traceback.format_exc())
        
        # Summary
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(f"Total: {success_count} succeeded, {fail_count} failed")
        self.stdout.write(f"Steps generated: {total_steps}")
        
        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry run - nothing was saved"))
