"""
Management command to generate media assets for lessons.

Usage:
    # Generate media for a specific lesson
    python manage.py generate_media --lesson 3
    
    # Generate media for all lessons in a course
    python manage.py generate_media --course 1
    
    # Only show what would be generated (dry run)
    python manage.py generate_media --lesson 3 --dry-run
"""

from django.core.management.base import BaseCommand
from apps.curriculum.models import Lesson, LessonStep
from apps.media_library.models import MediaAsset, StepMedia
from apps.tutoring.image_service import ImageGenerationService


class Command(BaseCommand):
    help = 'Generate media assets for lesson steps based on suggestions'

    def add_arguments(self, parser):
        parser.add_argument('--lesson', type=int, help='Lesson ID')
        parser.add_argument('--course', type=int, help='Course ID')
        parser.add_argument('--dry-run', action='store_true', help='Show what would be generated')
        parser.add_argument('--force', action='store_true', help='Regenerate existing media')

    def handle(self, *args, **options):
        if options['lesson']:
            lessons = Lesson.objects.filter(id=options['lesson'])
        elif options['course']:
            lessons = Lesson.objects.filter(unit__course_id=options['course'])
        else:
            self.stderr.write(self.style.ERROR('Please specify --lesson or --course'))
            return
        
        total_generated = 0
        total_skipped = 0
        total_failed = 0
        
        for lesson in lessons:
            self.stdout.write(f"\n📚 Processing: {lesson.title}")
            
            # Get media suggestions from metadata
            suggestions = lesson.metadata.get('media_suggestions', []) if lesson.metadata else []
            
            if not suggestions:
                self.stdout.write(self.style.WARNING("   No media suggestions found"))
                continue
            
            self.stdout.write(f"   Found {len(suggestions)} media suggestions")
            
            # Initialize image service
            service = ImageGenerationService(
                lesson=lesson,
                institution=lesson.institution
            )
            
            for i, suggestion in enumerate(suggestions, 1):
                step_id = suggestion.get('step_id')
                media_type = suggestion.get('type', 'diagram')
                description = suggestion.get('description', '')
                
                if not description:
                    continue
                
                # Check if media already exists for this step
                existing = StepMedia.objects.filter(lesson_step_id=step_id).exists()
                if existing and not options['force']:
                    self.stdout.write(f"   [{i}] SKIP: Media already exists for step {step_id}")
                    total_skipped += 1
                    continue
                
                if options['dry_run']:
                    self.stdout.write(f"   [{i}] WOULD GENERATE ({media_type}):")
                    self.stdout.write(f"       {description[:100]}...")
                    continue
                
                # Generate or find media
                self.stdout.write(f"   [{i}] Generating {media_type}...")
                
                result = service.get_or_generate_image(
                    prompt=description,
                    category=media_type,
                    prefer_existing=True
                )
                
                if result:
                    # Attach to step
                    try:
                        step = LessonStep.objects.get(id=step_id)
                        
                        # Find or create asset
                        if result.get('generated'):
                            # Already saved by image service
                            asset = MediaAsset.objects.filter(
                                title=result.get('title', description)[:100]
                            ).first()
                        else:
                            # Using existing asset
                            asset = MediaAsset.objects.filter(
                                file__contains=result.get('url', '').split('/')[-1]
                            ).first()
                        
                        if asset:
                            StepMedia.objects.get_or_create(
                                lesson_step=step,
                                media_asset=asset,
                                defaults={
                                    'placement': 'top',
                                    'order_index': 0
                                }
                            )
                        
                        status = "GENERATED" if result.get('generated') else "FOUND EXISTING"
                        self.stdout.write(self.style.SUCCESS(f"       ✓ {status}"))
                        total_generated += 1
                        
                    except LessonStep.DoesNotExist:
                        self.stdout.write(self.style.WARNING(f"       Step {step_id} not found"))
                        total_failed += 1
                else:
                    self.stdout.write(self.style.ERROR(f"       ✗ Failed to generate"))
                    total_failed += 1
        
        # Summary
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write("SUMMARY:")
        self.stdout.write(self.style.SUCCESS(f"  Generated/Found: {total_generated}"))
        self.stdout.write(self.style.WARNING(f"  Skipped: {total_skipped}"))
        self.stdout.write(self.style.ERROR(f"  Failed: {total_failed}"))
