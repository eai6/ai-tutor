"""
Background Task Runner

Simple async task execution for long-running operations like content generation.
Uses threading for simplicity - can be replaced with Celery for production.

Usage:
    from apps.dashboard.background_tasks import run_async
    
    run_async(generate_all_content, course_id=5, upload_id=10)
"""

import threading
import logging
from functools import wraps
from django.db import connection

logger = logging.getLogger(__name__)


def run_async(func, *args, **kwargs):
    """
    Run a function in a background thread.
    
    The function will run independently of the HTTP request.
    """
    def wrapper():
        try:
            # Close any existing DB connections (thread safety)
            connection.close()
            
            # Run the function
            result = func(*args, **kwargs)
            logger.info(f"Background task {func.__name__} completed: {result}")
            return result
        except Exception as e:
            logger.error(f"Background task {func.__name__} failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise
    
    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()
    logger.info(f"Started background task: {func.__name__}")
    return thread


def generate_all_content_async(course_id: int, upload_id: int = None, generate_media: bool = True):
    """
    Generate content for all lessons in a course (runs in background).
    
    Args:
        course_id: Course to generate content for
        upload_id: Optional CurriculumUpload to update with progress
        generate_media: Whether to also generate media assets
    """
    from apps.curriculum.models import Course, Lesson
    from apps.curriculum.content_generator import LessonContentGenerator
    from apps.dashboard.models import CurriculumUpload
    
    logger.info(f"Starting async content generation for course {course_id}")
    
    # Get course
    course = Course.objects.get(id=course_id)
    institution_id = course.institution_id
    
    # Get upload if provided (for progress tracking)
    upload = None
    if upload_id:
        try:
            upload = CurriculumUpload.objects.get(id=upload_id)
        except CurriculumUpload.DoesNotExist:
            pass
    
    def log(message):
        """Log to both logger and upload record."""
        logger.info(message)
        if upload:
            upload.add_log(message)
    
    try:
        # Initialize generator
        generator = LessonContentGenerator(institution_id=institution_id)
        
        # Get all lessons
        lessons = Lesson.objects.filter(
            unit__course=course
        ).order_by('unit__order_index', 'order_index')
        
        total = lessons.count()
        log(f"📝 Phase 1: Generating tutoring content for {total} lessons...")
        
        if upload:
            upload.current_step = 4
            upload.status = 'processing'
            upload.save()
        
        success = 0
        failed = 0
        skipped = 0
        total_steps = 0
        
        for idx, lesson in enumerate(lessons):
            # Skip if already has content
            existing_steps = lesson.steps.count()
            if existing_steps >= 5:
                skipped += 1
                log(f"   [{idx+1}/{total}] ⏭️ {lesson.title} (already has {existing_steps} steps)")
                continue
            
            try:
                log(f"   [{idx+1}/{total}] 🔄 {lesson.title}...")
                
                result = generator.generate_for_lesson(lesson, save_to_db=True)
                
                if result.get('success'):
                    steps = result.get('steps_generated', 0)
                    total_steps += steps
                    success += 1
                    log(f"   [{idx+1}/{total}] ✓ {lesson.title}: {steps} steps")
                else:
                    failed += 1
                    error = result.get('error', 'Unknown error')
                    log(f"   [{idx+1}/{total}] ⚠️ {lesson.title}: {error}")
                    
            except Exception as e:
                failed += 1
                log(f"   [{idx+1}/{total}] ❌ {lesson.title}: {str(e)}")
                logger.error(f"Content generation error: {e}")
        
        log(f"")
        log(f"📊 Content Generation Summary:")
        log(f"   ✓ Success: {success} lessons ({total_steps} steps)")
        log(f"   ⏭️ Skipped: {skipped} (already had content)")
        log(f"   ❌ Failed: {failed}")
        
        # Phase 2: Generate media if requested
        media_generated = 0
        if generate_media:
            log(f"")
            log(f"🖼️ Phase 2: Generating media assets...")
            
            try:
                media_result = generate_media_for_lessons(course_id, upload)
                media_generated = media_result.get('generated', 0)
                log(f"   ✓ Generated {media_generated} media assets")
            except Exception as e:
                log(f"   ⚠️ Media generation error: {str(e)}")
                logger.error(f"Media generation error: {e}")
        
        # Phase 3: Generate exit tickets
        exit_tickets_generated = 0
        log(f"")
        log(f"📝 Phase 3: Generating exit tickets...")
        
        try:
            exit_result = generate_exit_tickets_for_lessons(course_id, upload)
            exit_tickets_generated = exit_result.get('generated', 0)
            log(f"   ✓ Generated exit tickets for {exit_tickets_generated} lessons")
        except Exception as e:
            log(f"   ⚠️ Exit ticket generation error: {str(e)}")
            logger.error(f"Exit ticket generation error: {e}")
        
        # Phase 4: Extract skills and build knowledge graph
        skills_extracted = 0
        prerequisites_created = 0
        log(f"")
        log(f"🧠 Phase 4: Extracting skills and building knowledge graph...")

        try:
            from apps.tutoring.skill_extraction import SkillExtractionService
            skill_service = SkillExtractionService(institution_id=institution_id)
            skill_result = skill_service.extract_skills_for_course(course)
            skills_extracted = skill_result.get('skills_created', 0)
            prerequisites_created = skill_result.get('prerequisites_created', 0)
            log(f"   ✓ Extracted {skills_extracted} skills, {prerequisites_created} prerequisites")
            if skill_result.get('errors'):
                for error in skill_result['errors'][:5]:
                    log(f"   ⚠️ {error}")
        except Exception as e:
            log(f"   ⚠️ Skill extraction error: {str(e)}")
            logger.error(f"Skill extraction error: {e}")

        # Summary
        log(f"")
        log(f"🎉 All Done!")
        log(f"   📚 {success} lessons with tutoring content")
        log(f"   📝 {total_steps} total tutoring steps")
        if generate_media:
            log(f"   🖼️ {media_generated} media assets")
        log(f"   ❓ {exit_tickets_generated} exit tickets")
        log(f"   🧠 {skills_extracted} skills extracted, {prerequisites_created} prerequisites")

        # Update upload record
        if upload:
            upload.steps_created = total_steps
            upload.status = 'completed'
            from django.utils import timezone
            upload.completed_at = timezone.now()
            upload.save()

        return {
            'success': success,
            'failed': failed,
            'skipped': skipped,
            'total_steps': total_steps,
            'media_generated': media_generated,
            'exit_tickets_generated': exit_tickets_generated,
            'skills_extracted': skills_extracted,
            'prerequisites_created': prerequisites_created,
        }
        
    except Exception as e:
        log(f"❌ Fatal error: {str(e)}")
        if upload:
            upload.status = 'failed'
            upload.error_message = str(e)
            upload.save()
        raise


def generate_media_for_lessons(course_id: int, upload=None) -> dict:
    """
    Generate media assets for lessons in a course.
    
    Looks at media descriptions in lesson steps and generates images.
    """
    from apps.curriculum.models import Lesson, LessonStep
    from apps.accounts.models import Institution
    
    def log(message):
        logger.info(message)
        if upload:
            upload.add_log(message)
    
    # Get institution
    from apps.curriculum.models import Course
    course = Course.objects.get(id=course_id)
    institution = course.institution
    
    lessons = Lesson.objects.filter(unit__course_id=course_id)
    generated = 0
    failed = 0
    skipped = 0
    
    for lesson in lessons:
        steps = lesson.steps.all()
        
        for step in steps:
            if not step.media:
                continue
            
            images = step.media.get('images', [])
            for img in images:
                # Skip if already has URL
                if img.get('url'):
                    skipped += 1
                    continue
                
                description = img.get('description', '')
                if not description:
                    continue
                
                try:
                    # Try to generate image
                    from apps.tutoring.image_service import ImageGenerationService
                    
                    service = ImageGenerationService(
                        lesson=lesson,
                        institution=institution
                    )
                    
                    # Always generate fresh images (don't use potentially mismatched existing ones)
                    result = service.get_or_generate_image(
                        prompt=description,
                        category=img.get('type', 'diagram'),
                        generate_only=True  # Always generate new, don't find existing
                    )
                    
                    if result and result.get('url'):
                        img['url'] = result['url']
                        img['source'] = 'generated' if result.get('generated') else 'library'
                        generated += 1
                        
                        # Save updated media back to step
                        step.save()
                        
                except Exception as e:
                    logger.warning(f"Failed to generate image for {lesson.title}: {e}")
                    failed += 1
    
    log(f"   Media: {generated} generated, {skipped} already had URLs, {failed} failed")
    return {'generated': generated, 'failed': failed, 'skipped': skipped}


def generate_exit_tickets_for_lessons(course_id: int, upload=None) -> dict:
    """
    Generate exit ticket questions for all lessons in a course.
    """
    from apps.curriculum.models import Lesson
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client
    import json
    
    def log(message):
        logger.info(message)
        if upload:
            upload.add_log(message)
    
    # Get LLM config
    config = ModelConfig.objects.filter(is_active=True).first()
    if not config:
        logger.error("No active LLM model configured for exit ticket generation")
        return {'generated': 0, 'failed': 0, 'skipped': 0}
    
    client = get_llm_client(config)
    
    lessons = Lesson.objects.filter(unit__course_id=course_id)
    generated = 0
    failed = 0
    skipped = 0
    
    for lesson in lessons:
        # Skip if already has exit ticket
        if ExitTicket.objects.filter(lesson=lesson).exists():
            skipped += 1
            continue
        
        # Skip if no content yet
        if lesson.steps.count() < 5:
            skipped += 1
            continue
        
        try:
            # Query KB for additional context from teaching materials
            kb_context = ""
            exam_context = ""
            try:
                from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
                course = lesson.unit.course
                kb = CurriculumKnowledgeBase(institution_id=course.institution_id)

                # Get textbook/teaching material context
                kb_result = kb.query_for_content_generation(
                    lesson_title=lesson.title,
                    lesson_objective=lesson.objective or '',
                    unit_title=lesson.unit.title,
                    subject=course.title,
                    grade_level=course.grade_level or '',
                    n_results=15,
                )
                if kb_result.chunks:
                    kb_context = "\n\nADDITIONAL CONTEXT FROM TEXTBOOKS/MATERIALS:\n"
                    for chunk in kb_result.chunks[:10]:
                        kb_context += f"- {chunk.get('content', '')[:200]}...\n"

                # Get real exam questions for grounding
                exam_questions = kb.query_for_exit_ticket_generation(
                    lesson_title=lesson.title,
                    lesson_objective=lesson.objective or '',
                    subject=course.title,
                    grade_level=course.grade_level or '',
                    n_results=5,
                )
                exam_context = kb.format_exam_questions_for_prompt(exam_questions)
                if exam_context:
                    exam_context = "\n\n" + exam_context + "\n"
            except Exception as e:
                logger.warning(f"KB query for exit tickets failed: {e}")

            prompt = f"""Generate 35 multiple choice exit ticket questions for this lesson.

Lesson: {lesson.title}
Objective: {lesson.objective}
Subject: {lesson.unit.course.title}
{kb_context}{exam_context}

Generate 35 questions that cover ALL key concepts in this lesson. Each question should have:
- A clear question
- 4 answer choices (A, B, C, D)
- The correct answer letter (just the letter: A, B, C, or D)
- Brief explanation
- A concept_tag identifying which learning objective/concept it assesses

Ensure broad coverage: at least 2-3 questions per major concept.
Mix difficulty levels: ~10 easy (recall), ~15 medium (apply), ~10 hard (analyze).

Return as JSON array:
[
  {{
    "question": "What is...",
    "option_a": "First option",
    "option_b": "Second option",
    "option_c": "Third option",
    "option_d": "Fourth option",
    "correct_answer": "A",
    "explanation": "Brief explanation of why A is correct",
    "concept_tag": "Name of the concept this tests",
    "difficulty": "easy"
  }}
]

Return ONLY the JSON array, no other text."""

            system_prompt = "You are an expert teacher creating assessment questions. Return ONLY valid JSON, no other text."
            messages = [{"role": "user", "content": prompt}]

            response = client.generate(messages, system_prompt)
            response_text = response.content.strip()

            # Handle markdown code blocks
            if '```json' in response_text:
                response_text = response_text.split('```json')[1].split('```')[0]
            elif '```' in response_text:
                parts = response_text.split('```')
                if len(parts) >= 2:
                    response_text = parts[1]

            response_text = response_text.strip()
            questions_data = json.loads(response_text)

            if not questions_data or not isinstance(questions_data, list):
                failed += 1
                continue

            num_questions = len(questions_data)

            # Create exit ticket
            exit_ticket = ExitTicket.objects.create(
                lesson=lesson,
                passing_score=8,
                time_limit_minutes=15,
                instructions=f"Answer all 10 questions. You need 8 correct to pass. (Selected from a bank of {num_questions})"
            )

            # Create questions (up to 40)
            for i, q in enumerate(questions_data[:40]):
                # Map difficulty string
                diff = q.get('difficulty', 'medium').lower()
                if diff not in ('easy', 'medium', 'hard'):
                    diff = 'medium'

                ExitTicketQuestion.objects.create(
                    exit_ticket=exit_ticket,
                    question_text=q.get('question', ''),
                    option_a=q.get('option_a', ''),
                    option_b=q.get('option_b', ''),
                    option_c=q.get('option_c', ''),
                    option_d=q.get('option_d', ''),
                    correct_answer=q.get('correct_answer', 'A')[:1].upper(),
                    explanation=q.get('explanation', ''),
                    concept_tag=q.get('concept_tag', '')[:200],
                    difficulty=diff,
                    order_index=i,
                )

            generated += 1
            log(f"   ✓ {lesson.title}: {min(num_questions, 40)} questions ({len(set(q.get('concept_tag','') for q in questions_data))} concepts)")
            
        except Exception as e:
            failed += 1
            logger.warning(f"Exit ticket generation failed for {lesson.title}: {e}")
    
    return {'generated': generated, 'failed': failed, 'skipped': skipped}


def generate_single_lesson_async(lesson_id: int):
    """Generate content for a single lesson in background."""
    from apps.curriculum.models import Lesson
    from apps.curriculum.content_generator import LessonContentGenerator
    
    lesson = Lesson.objects.get(id=lesson_id)
    institution_id = lesson.unit.course.institution_id
    
    generator = LessonContentGenerator(institution_id=institution_id)
    result = generator.generate_for_lesson(lesson, save_to_db=True)
    
    logger.info(f"Generated content for {lesson.title}: {result}")
    return result


def generate_media_async(course_id: int, upload_id: int = None, force_regenerate: bool = False):
    """
    Generate media for all lessons in a course (runs in background with progress logging).
    
    Args:
        course_id: Course to generate media for
        upload_id: CurriculumUpload record for progress tracking
        force_regenerate: If True, regenerate even if images already have URLs
    """
    from apps.curriculum.models import Course, Lesson
    from apps.dashboard.models import CurriculumUpload
    from django.utils import timezone
    
    logger.info(f"Starting async media generation for course {course_id}")
    
    # Get course
    course = Course.objects.get(id=course_id)
    institution = course.institution
    
    # Get upload for progress tracking
    upload = None
    if upload_id:
        try:
            upload = CurriculumUpload.objects.get(id=upload_id)
        except CurriculumUpload.DoesNotExist:
            pass
    
    def log(message):
        """Log to both logger and upload record."""
        logger.info(message)
        if upload:
            upload.add_log(message)
            upload.save()
    
    try:
        lessons = Lesson.objects.filter(unit__course=course).order_by('unit__order_index', 'order_index')
        total_lessons = lessons.count()
        
        log(f"📊 Found {total_lessons} lessons to process")
        log(f"")
        
        results = {
            'media_generated': 0,
            'media_found': 0,
            'media_failed': 0,
            'media_skipped': 0,
        }
        
        lesson_num = 0
        for lesson in lessons:
            lesson_num += 1
            steps = lesson.steps.all()
            
            # Count images in this lesson
            images_in_lesson = 0
            for step in steps:
                if step.media and step.media.get('images'):
                    images_in_lesson += len(step.media['images'])
            
            if images_in_lesson == 0:
                continue
                
            log(f"[{lesson_num}/{total_lessons}] {lesson.title} ({images_in_lesson} images)")
            
            for step in steps:
                if not step.media:
                    continue
                
                images = step.media.get('images', [])
                if not images:
                    continue
                
                media_updated = False
                
                for img in images:
                    # Skip if already has URL (unless force_regenerate)
                    if img.get('url') and not force_regenerate:
                        results['media_skipped'] += 1
                        continue
                    
                    description = img.get('description', '')
                    if not description:
                        continue
                    
                    # Generate image
                    try:
                        from apps.tutoring.image_service import ImageGenerationService
                        
                        service = ImageGenerationService(
                            lesson=lesson,
                            institution=institution
                        )
                        
                        # If force_regenerate, use generate_only to skip existing media lookup
                        result = service.get_or_generate_image(
                            prompt=description,
                            category=img.get('type', 'diagram'),
                            prefer_existing=not force_regenerate,
                            generate_only=force_regenerate
                        )
                        
                        if result and result.get('url'):
                            img['url'] = result['url']
                            img['source'] = 'generated' if result.get('generated') else 'library'
                            media_updated = True
                            
                            if result.get('generated'):
                                results['media_generated'] += 1
                                log(f"   ✓ Generated: {img.get('type', 'image')}")
                            else:
                                results['media_found'] += 1
                                log(f"   ✓ Found: {img.get('type', 'image')}")
                        else:
                            results['media_failed'] += 1
                            log(f"   ⚠️ Failed: {description[:40]}...")
                            
                    except Exception as e:
                        results['media_failed'] += 1
                        log(f"   ❌ Error: {str(e)[:50]}")
                        logger.error(f"Media generation error: {e}")
                
                # Save step if media was updated
                if media_updated:
                    step.save()
        
        # Summary
        log(f"")
        log(f"🎉 Media Generation Complete!")
        log(f"   ✓ Generated: {results['media_generated']}")
        log(f"   📁 Found existing: {results['media_found']}")
        log(f"   ⏭️ Skipped: {results['media_skipped']}")
        log(f"   ❌ Failed: {results['media_failed']}")
        
        # Update upload record
        if upload:
            upload.status = 'completed'
            upload.completed_at = timezone.now()
            upload.save()
        
        return results
        
    except Exception as e:
        log(f"❌ Fatal error: {str(e)}")
        logger.error(f"Media generation error: {e}")
        if upload:
            upload.status = 'failed'
            upload.error_message = str(e)
            upload.save()
        raise