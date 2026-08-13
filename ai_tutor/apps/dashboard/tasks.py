"""
Curriculum Processing Tasks

This module connects the staff upload feature to the unified curriculum parser.

WORKFLOW:
1. Staff uploads curriculum PDF/DOCX via dashboard
2. CurriculumUpload record created
3. process_curriculum_upload() called (sync or async)
4. Parser extracts rich structure (units, objectives, strategies)
5. Database records created with metadata
6. Content generator creates tutoring steps
7. Media generator creates visuals

All using the same unified system!
"""

import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def process_curriculum_upload(upload_id: int, skip_review: bool = False) -> dict:
    """
    Main entry point for processing uploaded curriculum.

    Wired to the v2 parser (memory/curriculum_parser_v2_plan.md):
    locale-aware, LLM-first structure extraction; no regex fallback;
    per-grade Course fanout on Approve. Replaces the older
    ai_tutor.apps.curriculum.pipeline.process_curriculum_upload chain which
    did extract → vectorize-into-KB → AI-generate-lessons → create-rows.

    The KB-vectorize step from the old pipeline (which hung locally
    during M7 E2E on dev SQLite) is intentionally NOT part of v2 —
    that's a separate concern handled by the curriculum_knowledge_base
    indexing job, decoupled from the parse path.
    """
    from ai_tutor.apps.curriculum.curriculum_parser import process_curriculum_upload as run_v2

    return run_v2(upload_id, skip_review=skip_review)


def generate_content_for_course(course_id: int) -> dict:
    """
    Generate tutoring content for all lessons in a course.
    
    Called after curriculum structure is created, or can be
    triggered separately from dashboard.
    """
    from ai_tutor.apps.curriculum.content_generator import generate_content_for_course as gen_course
    
    return gen_course(course_id, force=False)


def generate_media_for_course(course_id: int, force_regenerate: bool = False) -> dict:
    """
    Generate media assets for all lessons in a course.
    
    Args:
        course_id: The course to generate media for
        force_regenerate: If True, regenerate even if images already have URLs
    
    Uses media stored in lesson step's media JSONField.
    """
    from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep
    
    logger.info(f"Starting media generation for course {course_id} (force={force_regenerate})")
    
    course = Course.objects.get(id=course_id)
    institution = course.institution
    lessons = Lesson.objects.filter(unit__course=course)
    
    results = {
        'course': course.title,
        'total_lessons': lessons.count(),
        'media_generated': 0,
        'media_found': 0,
        'media_failed': 0,
        'media_skipped': 0,
        'steps_checked': 0,
        'images_processed': 0,
    }
    
    logger.info(f"Found {lessons.count()} lessons to process")
    
    for lesson in lessons:
        steps = lesson.steps.all()
        logger.info(f"Processing lesson: {lesson.title} with {steps.count()} steps")
        
        for step in steps:
            results['steps_checked'] += 1
            
            if not step.media:
                continue
            
            images = step.media.get('images', [])
            if not images:
                continue
                
            logger.info(f"  Step {step.order_index}: found {len(images)} images")
            media_updated = False
            
            for img in images:
                # Skip if already has URL (unless force_regenerate)
                if img.get('url') and not force_regenerate:
                    results['media_skipped'] += 1
                    logger.debug(f"    Image already has URL, skipping")
                    continue
                
                description = img.get('description', '')
                if not description:
                    logger.debug(f"    Image has no description, skipping")
                    continue
                
                results['images_processed'] += 1
                logger.info(f"    Generating image: {description[:50]}...")
                
                # Try to generate image
                try:
                    from ai_tutor.apps.tutoring.image_service import ImageGenerationService
                    
                    service = ImageGenerationService(
                        lesson=lesson,
                        institution=institution
                    )
                    
                    # If force_regenerate, always generate new (don't prefer existing)
                    result = service.get_or_generate_image(
                        prompt=description,
                        category=img.get('type', 'diagram'),
                        prefer_existing=not force_regenerate
                    )
                    
                    if result and result.get('url'):
                        img['url'] = result['url']
                        img['source'] = 'generated' if result.get('generated') else 'library'
                        media_updated = True
                        
                        if result.get('generated'):
                            results['media_generated'] += 1
                            logger.info(f"    ✓ Generated: {result['url']}")
                        else:
                            results['media_found'] += 1
                            logger.info(f"    ✓ Found existing: {result['url']}")
                    else:
                        results['media_failed'] += 1
                        logger.warning(f"    ✗ Failed to generate - no URL returned")
                        
                except ImportError as e:
                    logger.error(f"    ✗ ImageGenerationService not available: {e}")
                    results['media_failed'] += 1
                except Exception as e:
                    logger.warning(f"    ✗ Media generation failed: {e}")
                    results['media_failed'] += 1
            
            # Save step if media was updated
            if media_updated:
                step.save()
                logger.info(f"  Saved step {step.order_index} with updated media")
    
    logger.info(f"Media generation complete: {results}")
    return results


def regenerate_lesson_content(lesson_id: int, force: bool = False) -> dict:
    """
    Regenerate content for a single lesson.
    
    Useful for updating content after curriculum changes.
    """
    from ai_tutor.apps.curriculum.content_generator import generate_content_for_lesson
    
    return generate_content_for_lesson(lesson_id, force=force)


# ============================================================================
# LEGACY FUNCTIONS (kept for backward compatibility)
# ============================================================================

def extract_text_from_file(file_path: str) -> str:
    """Legacy wrapper - use curriculum_parser.extract_text_from_file instead."""
    from ai_tutor.apps.curriculum.curriculum_parser import extract_text_from_file as new_extract
    text, _ = new_extract(file_path)
    return text


def parse_curriculum_with_ai(text: str, subject_name: str, grade_level: str) -> dict:
    """Legacy wrapper - use curriculum_parser.parse_curriculum_with_ai instead."""
    from ai_tutor.apps.curriculum.curriculum_parser import parse_curriculum_with_ai as new_parse
    return new_parse(text, subject_name, grade_level, doc_type='general')


def create_curriculum_from_structure(structure: dict, institution, upload) -> dict:
    """Legacy wrapper - use curriculum_parser.create_curriculum_from_structure instead."""
    from ai_tutor.apps.curriculum.curriculum_parser import create_curriculum_from_structure as new_create
    return new_create(structure, institution, upload, generate_content=False)


def generate_exit_ticket_for_lesson(lesson) -> bool:
    """Generate exit ticket - now handled by content generator."""
    # Exit tickets are now created as part of content generation
    # This is kept for backward compatibility
    from ai_tutor.apps.tutoring.models import ExitTicket
    
    if ExitTicket.objects.filter(lesson=lesson).exists():
        return False
    
    # Trigger content regeneration which includes exit ticket
    try:
        result = regenerate_lesson_content(lesson.id, force=True)
        return result.get('success', False)
    except Exception as e:
        logger.error(f"Exit ticket generation failed: {e}")
        return False