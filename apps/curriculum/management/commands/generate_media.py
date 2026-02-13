"""
Generate educational images for curriculum lessons using AI.

This script:
1. Analyzes each lesson to determine what visuals would help
2. Uses Claude to create optimal image prompts
3. Uses DALL-E to generate the actual images
4. Saves images and links them to lesson steps

Requirements:
    pip install anthropic openai requests

Usage:
    # Generate images for all Geography lessons
    python manage.py generate_media --subject Geography
    
    # Generate for a specific lesson
    python manage.py generate_media --lesson-id 5
    
    # Dry run - see what would be generated without making images
    python manage.py generate_media --subject Geography --dry-run
    
    # Use a specific image style
    python manage.py generate_media --subject Mathematics --style "simple diagram, clean lines, educational"
"""

import os
import json
import time
import requests
from io import BytesIO
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.files.base import ContentFile
from django.db import transaction
from django.conf import settings

from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.accounts.models import Institution
from apps.media_library.models import MediaAsset, StepMedia


# Default style suffix for educational images
DEFAULT_STYLE = "educational illustration, clear and simple, suitable for secondary school students, clean design, no text overlays"

# Seychelles context for local relevance
SEYCHELLES_CONTEXT = """
When relevant, incorporate Seychelles context:
- Tropical island setting with granite rocks and palm trees
- Turquoise waters and coral reefs
- Local wildlife: giant tortoises, coco de mer palms, tropical fish
- Victoria city, Mahé, Praslin, La Digue islands
- Diverse population (Creole, African, Asian, European heritage)
- Fishing boats, tourism, tropical agriculture
"""


class Command(BaseCommand):
    help = 'Generate educational images for lessons using Claude + DALL-E'

    def add_arguments(self, parser):
        parser.add_argument(
            '--subject',
            type=str,
            help='Generate images for a specific subject (e.g., Geography, Mathematics)'
        )
        parser.add_argument(
            '--lesson-id',
            type=int,
            help='Generate images for a specific lesson ID'
        )
        parser.add_argument(
            '--unit',
            type=str,
            help='Generate images for lessons in a specific unit (partial match)'
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Generate images for all lessons'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be generated without creating images'
        )
        parser.add_argument(
            '--style',
            type=str,
            default=DEFAULT_STYLE,
            help='Style suffix to append to all image prompts'
        )
        parser.add_argument(
            '--images-per-lesson',
            type=int,
            default=2,
            help='Number of images to generate per lesson (default: 2)'
        )
        parser.add_argument(
            '--skip-existing',
            action='store_true',
            default=True,
            help='Skip lessons that already have images attached'
        )
        parser.add_argument(
            '--image-size',
            type=str,
            default='1024x1024',
            choices=['1024x1024', '1792x1024', '1024x1792'],
            help='DALL-E image size'
        )

    def handle(self, *args, **options):
        # Validate API keys
        self.anthropic_key = os.environ.get('ANTHROPIC_API_KEY')
        self.openai_key = os.environ.get('OPENAI_API_KEY')
        
        if not self.anthropic_key:
            raise CommandError('ANTHROPIC_API_KEY environment variable not set')
        if not self.openai_key:
            raise CommandError('OPENAI_API_KEY environment variable not set')
        
        # Import clients
        try:
            import anthropic
            from openai import OpenAI
            self.claude = anthropic.Anthropic(api_key=self.anthropic_key)
            self.openai = OpenAI(api_key=self.openai_key)
        except ImportError as e:
            raise CommandError(f'Required package not installed: {e}. Run: pip install anthropic openai')
        
        # Get lessons to process
        lessons = self._get_lessons(options)
        
        if not lessons:
            self.stdout.write(self.style.WARNING('No lessons found matching criteria'))
            return
        
        # Filter out lessons with existing images if skip_existing
        if options['skip_existing']:
            lessons = [l for l in lessons if not self._has_images(l)]
            if not lessons:
                self.stdout.write(self.style.SUCCESS('All lessons already have images!'))
                return
        
        self.stdout.write(f'Found {len(lessons)} lessons to process\n')
        
        if options['dry_run']:
            self.stdout.write(self.style.WARNING('DRY RUN - No images will be generated\n'))
        
        # Process each lesson
        success_count = 0
        error_count = 0
        total_images = 0
        
        for i, lesson in enumerate(lessons, 1):
            self.stdout.write(f'\n[{i}/{len(lessons)}] {lesson.unit.course.title} > {lesson.title}')
            
            try:
                num_images = self._process_lesson(
                    lesson, 
                    options['dry_run'],
                    options['style'],
                    options['images_per_lesson'],
                    options['image_size']
                )
                success_count += 1
                total_images += num_images
                
                # Rate limiting
                if not options['dry_run'] and i < len(lessons):
                    time.sleep(2)  # Be nice to APIs
                    
            except Exception as e:
                error_count += 1
                self.stdout.write(self.style.ERROR(f'  ✗ Error: {str(e)}'))
        
        # Summary
        self.stdout.write(f'\n{"="*50}')
        self.stdout.write(self.style.SUCCESS(
            f'Completed: {success_count} lessons, {total_images} images generated, {error_count} errors'
        ))
    
    def _get_lessons(self, options):
        """Get lessons based on command options."""
        queryset = Lesson.objects.filter(is_published=True)
        
        if options['lesson_id']:
            queryset = queryset.filter(id=options['lesson_id'])
        elif options['subject']:
            queryset = queryset.filter(unit__course__title__icontains=options['subject'])
        elif options['unit']:
            queryset = queryset.filter(unit__title__icontains=options['unit'])
        elif not options['all']:
            raise CommandError('Specify --subject, --unit, --lesson-id, or --all')
        
        return list(queryset.select_related('unit__course').order_by(
            'unit__course', 'unit__order_index', 'order_index'
        ))
    
    def _has_images(self, lesson: Lesson) -> bool:
        """Check if lesson already has images attached."""
        return StepMedia.objects.filter(
            lesson_step__lesson=lesson,
            media_asset__asset_type='image'
        ).exists()
    
    def _process_lesson(self, lesson: Lesson, dry_run: bool, style: str, 
                        num_images: int, image_size: str) -> int:
        """Generate images for a single lesson."""
        
        # Step 1: Get image ideas from Claude
        image_specs = self._get_image_specs(lesson, num_images)
        
        if not image_specs:
            self.stdout.write('  No images needed for this lesson')
            return 0
        
        self.stdout.write(f'  Generating {len(image_specs)} images...')
        
        if dry_run:
            for spec in image_specs:
                self.stdout.write(f'    → {spec["title"]}')
                self.stdout.write(f'      Prompt: {spec["prompt"][:80]}...')
            return len(image_specs)
        
        # Step 2: Generate images with DALL-E
        generated_count = 0
        institution = lesson.unit.course.institution
        first_step = lesson.steps.first()
        
        for idx, spec in enumerate(image_specs):
            try:
                # Add style to prompt
                full_prompt = f"{spec['prompt']}. Style: {style}"
                
                # Generate image
                self.stdout.write(f'    Generating: {spec["title"]}...')
                
                response = self.openai.images.generate(
                    model="dall-e-3",
                    prompt=full_prompt,
                    size=image_size,
                    quality="standard",
                    n=1,
                )
                
                image_url = response.data[0].url
                
                # Download image
                image_response = requests.get(image_url)
                image_response.raise_for_status()
                
                # Save to MediaAsset
                with transaction.atomic():
                    # Create safe filename
                    safe_title = "".join(c if c.isalnum() else "_" for c in spec['title'])
                    filename = f"{lesson.id}_{safe_title}.png"
                    
                    # Create MediaAsset
                    media_asset = MediaAsset.objects.create(
                        institution=institution,
                        title=spec['title'],
                        asset_type='image',
                        alt_text=spec.get('alt_text', spec['title']),
                        caption=spec.get('caption', ''),
                        tags=f"{lesson.unit.course.title.lower()}, {lesson.unit.title.lower()}, ai-generated",
                    )
                    
                    # Save file
                    media_asset.file.save(
                        filename,
                        ContentFile(image_response.content),
                        save=True
                    )
                    
                    # Link to lesson step
                    if first_step:
                        StepMedia.objects.create(
                            lesson_step=first_step,
                            media_asset=media_asset,
                            placement='top',
                            order_index=idx,
                        )
                    
                    generated_count += 1
                    self.stdout.write(self.style.SUCCESS(f'      ✓ Created: {media_asset.title}'))
                
                # Rate limit between images
                time.sleep(1)
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'      ✗ Failed: {str(e)}'))
        
        return generated_count
    
    def _get_image_specs(self, lesson: Lesson, num_images: int) -> list:
        """Use Claude to determine what images would help this lesson."""
        
        prompt = f"""Analyze this lesson and suggest {num_images} educational images that would help students understand the content.

SUBJECT: {lesson.unit.course.title}
UNIT: {lesson.unit.title}  
LESSON: {lesson.title}
OBJECTIVE: {lesson.objective}

{SEYCHELLES_CONTEXT}

For each image, provide:
1. title: Short descriptive title
2. prompt: Detailed DALL-E prompt (be specific about what to show, composition, style)
3. alt_text: Accessibility description
4. caption: Brief caption to show students

IMPORTANT for DALL-E prompts:
- Be specific and descriptive
- Describe composition (close-up, wide shot, diagram, etc.)
- Avoid text in images (DALL-E handles text poorly)
- For diagrams, describe the visual elements clearly
- For Geography: include maps, landscapes, processes
- For Math: show visual representations, not equations

Return as JSON array:
[
    {{
        "title": "...",
        "prompt": "...",
        "alt_text": "...",
        "caption": "..."
    }}
]

Return ONLY valid JSON, no other text."""

        response = self.claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        try:
            content = response.content[0].text.strip()
            
            # Handle markdown code blocks
            if content.startswith('```'):
                content = content.split('```')[1]
                if content.startswith('json'):
                    content = content[4:]
                content = content.strip()
            
            return json.loads(content)
            
        except json.JSONDecodeError as e:
            self.stdout.write(self.style.WARNING(f'  Failed to parse Claude response: {e}'))
            return []


# Standalone function for use in scripts
def generate_single_image(prompt: str, openai_key: str, size: str = '1024x1024') -> bytes:
    """
    Generate a single image with DALL-E.
    
    Args:
        prompt: Image description
        openai_key: OpenAI API key
        size: Image size
        
    Returns:
        Image bytes (PNG)
    """
    from openai import OpenAI
    
    client = OpenAI(api_key=openai_key)
    
    response = client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        size=size,
        quality="standard",
        n=1,
    )
    
    image_url = response.data[0].url
    image_response = requests.get(image_url)
    image_response.raise_for_status()
    
    return image_response.content