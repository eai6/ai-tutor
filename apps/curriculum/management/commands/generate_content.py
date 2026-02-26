"""
Management command to generate lesson content.

Usage:
    # Generate for a single lesson
    python manage.py generate_content --lesson 3
    
    # Generate for all lessons in a course
    python manage.py generate_content --course 1
    
    # Generate for all lessons
    python manage.py generate_content --all
"""

from django.core.management.base import BaseCommand
from apps.curriculum.models import Course, Lesson, LessonStep
from apps.curriculum.content_generator import LessonContentGenerator
from apps.llm.models import ModelConfig
from apps.llm.client import get_llm_client


class Command(BaseCommand):
    help = 'Generate lesson content (steps, practice problems, exit tickets)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--lesson',
            type=int,
            help='Generate content for a specific lesson ID'
        )
        parser.add_argument(
            '--course',
            type=int,
            help='Generate content for all lessons in a course ID'
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Generate content for all lessons'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Regenerate even if content already exists'
        )

    def handle(self, *args, **options):
        # Get LLM client
        model_config = ModelConfig.get_for('generation')
        if not model_config:
            self.stderr.write(self.style.ERROR('No active LLM model configured!'))
            self.stderr.write('Create one in Django admin: /admin/llm/modelconfig/')
            return
        
        self.stdout.write(f'Using LLM: {model_config.name}')
        
        llm_client = get_llm_client(model_config)
        generator = LessonContentGenerator(llm_client)
        
        # Determine which lessons to process
        if options['lesson']:
            lessons = Lesson.objects.filter(id=options['lesson'])
        elif options['course']:
            lessons = Lesson.objects.filter(
                unit__course_id=options['course']
            ).order_by('unit__order_index', 'order_index')
        elif options['all']:
            lessons = Lesson.objects.all().order_by(
                'unit__course__id', 'unit__order_index', 'order_index'
            )
        else:
            self.stderr.write(self.style.ERROR(
                'Please specify --lesson, --course, or --all'
            ))
            return
        
        total = lessons.count()
        self.stdout.write(f'Processing {total} lesson(s)...\n')
        
        success_count = 0
        error_count = 0
        skipped_count = 0
        
        for i, lesson in enumerate(lessons, 1):
            # Check if already has content
            has_content = lesson.steps.count() > 5
            
            if has_content and not options['force']:
                self.stdout.write(
                    f'[{i}/{total}] SKIP: {lesson.title} (already has {lesson.steps.count()} steps)'
                )
                skipped_count += 1
                continue
            
            self.stdout.write(f'[{i}/{total}] Generating: {lesson.title}...')
            
            try:
                result = generator.generate_for_lesson(lesson, save_to_db=True)
                
                if result.get('success'):
                    steps = lesson.steps.count()
                    self.stdout.write(self.style.SUCCESS(
                        f'         ✓ Created {steps} steps'
                    ))
                    success_count += 1
                else:
                    self.stdout.write(self.style.ERROR(
                        f'         ✗ Failed: {result.get("error", "Unknown error")}'
                    ))
                    error_count += 1
                    
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'         ✗ Error: {str(e)}'
                ))
                error_count += 1
        
        # Summary
        self.stdout.write('\n' + '='*50)
        self.stdout.write(f'SUMMARY:')
        self.stdout.write(self.style.SUCCESS(f'  Success: {success_count}'))
        self.stdout.write(self.style.WARNING(f'  Skipped: {skipped_count}'))
        self.stdout.write(self.style.ERROR(f'  Errors:  {error_count}'))
