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
from django.utils import timezone

logger = logging.getLogger(__name__)


def _resolve_institution_id(institution_id=None, course=None, lesson=None):
    """Resolve institution_id, falling back to Global institution if needed.

    Priority: explicit institution_id → course.institution → Global.
    Never returns None.
    """
    if institution_id:
        return institution_id
    if course and course.institution_id:
        return course.institution_id
    if lesson and lesson.unit and lesson.unit.course and lesson.unit.course.institution_id:
        return lesson.unit.course.institution_id
    from apps.accounts.models import Institution
    return Institution.get_global().id


def _resolve_institution(institution_id=None, course=None, lesson=None):
    """Resolve Institution object, falling back to Global if needed.
    Never returns None.
    """
    from apps.accounts.models import Institution
    if institution_id:
        inst = Institution.objects.filter(id=institution_id).first()
        if inst:
            return inst
    if course and course.institution:
        return course.institution
    if lesson and lesson.unit and lesson.unit.course and lesson.unit.course.institution:
        return lesson.unit.course.institution
    return Institution.get_global()


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
            print(f"[ContentGen] Background task {func.__name__} completed: {result}", flush=True)
            logger.info(f"Background task {func.__name__} completed: {result}")
            return result
        except Exception as e:
            print(f"[ContentGen] Background task {func.__name__} FAILED: {e}", flush=True)
            logger.error(f"Background task {func.__name__} failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            traceback.print_exc()
            raise

    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()
    print(f"[ContentGen] Started background task: {func.__name__}", flush=True)
    logger.info(f"Started background task: {func.__name__}")
    return thread


def generate_all_content_async(course_id: int, upload_id: int = None, generate_media: bool = True):
    """
    Generate content for all lessons in a course using parallel processing.

    Uses ThreadPoolExecutor(max_workers=3) to process lessons concurrently.
    Each lesson runs the full pipeline: steps -> media -> exit tickets -> skills.

    Args:
        course_id: Course to generate content for
        upload_id: Optional CurriculumUpload to update with progress
        generate_media: Whether to also generate media assets
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from apps.curriculum.models import Course, Lesson
    from apps.dashboard.models import CurriculumUpload

    logger.info(f"Starting parallel content generation for course {course_id}")

    # Get course
    course = Course.objects.get(id=course_id)
    institution_id = _resolve_institution_id(course=course)

    # Get upload if provided (for progress tracking)
    upload = None
    if upload_id:
        try:
            upload = CurriculumUpload.objects.get(id=upload_id)
        except CurriculumUpload.DoesNotExist:
            pass

    # Thread-safe logging
    _log_lock = threading.Lock()

    def log(message):
        """Thread-safe log to both logger and upload record."""
        logger.info(message)
        if upload:
            with _log_lock:
                upload.add_log(message)

    try:
        # Get all lessons
        lessons = Lesson.objects.filter(
            unit__course=course
        ).order_by('unit__order_index', 'order_index')

        total = lessons.count()

        if upload:
            upload.current_step = 4
            upload.status = 'processing'
            upload.save()

        # Separate lessons that need generation from those that can be skipped
        to_generate = []
        skipped = 0
        for lesson in lessons:
            existing_steps = lesson.steps.count()
            if lesson.content_status == 'completed' and existing_steps >= 5:
                skipped += 1
                log(f"   ⏭️ {lesson.title} (completed with {existing_steps} steps)")
            elif existing_steps >= 5:
                skipped += 1
                log(f"   ⏭️ {lesson.title} (already has {existing_steps} steps)")
            else:
                to_generate.append(lesson.id)

        log(f"📝 Generating content for {len(to_generate)} lessons ({skipped} skipped, {total} total)...")
        log(f"   Using 2 parallel workers")
        log(f"")

        # Process lessons in parallel
        success = 0
        failed = 0
        total_steps = 0
        total_media = 0
        total_exit = 0
        total_skills = 0

        cancelled = False
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(
                    generate_complete_lesson, lesson_id, institution_id, log
                ): lesson_id
                for lesson_id in to_generate
            }

            for future in as_completed(futures):
                lesson_id = futures[future]
                try:
                    result = future.result()
                    if result.get('success'):
                        success += 1
                        total_steps += result.get('steps', 0)
                        total_media += result.get('media', 0)
                        total_exit += result.get('exit_questions', 0)
                        total_skills += result.get('skills', 0)
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    log(f"   ❌ Lesson {lesson_id}: {str(e)}")
                    logger.error(f"Parallel generation error for lesson {lesson_id}: {e}")

                # Check for cancellation after each completed future
                if upload:
                    upload.refresh_from_db()
                    if upload.is_cancelled:
                        log(f"⛔ Generation cancelled by teacher.")
                        cancelled = True
                        # Cancel remaining futures
                        for f in futures:
                            f.cancel()
                        break

        # If cancelled, reset any remaining 'generating' lessons
        if cancelled:
            Lesson.objects.filter(
                unit__course=course,
                content_status='generating',
            ).update(content_status='empty')

        # Course-level prerequisite detection (uses skill graph, no LLM)
        prereqs_created = 0
        try:
            from apps.tutoring.skill_extraction import SkillExtractionService
            skill_service = SkillExtractionService(institution_id=institution_id)
            prereqs_created = skill_service.detect_course_prerequisites(course)
            log(f"🔗 Detected {prereqs_created} lesson prerequisites from skill graph")
        except Exception as e:
            log(f"⚠️ Prerequisite detection error: {e}")
            logger.error(f"Prerequisite detection error for course {course_id}: {e}")

        # Summary
        log(f"")
        log(f"🎉 All Done!")
        log(f"   📚 {success} lessons with content")
        log(f"   📝 {total_steps} total tutoring steps")
        log(f"   🖼️ {total_media} media assets")
        log(f"   ❓ {total_exit} exit tickets")
        log(f"   🧠 {total_skills} skills extracted")
        log(f"   🔗 {prereqs_created} prerequisites detected")
        log(f"   ⏭️ {skipped} skipped (already had content)")
        log(f"   ❌ {failed} failed")

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
            'media_generated': total_media,
            'exit_tickets_generated': total_exit,
            'skills_extracted': total_skills,
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
                    log(f"   ⚠️ {lesson.title}: image failed — {e}")
                    failed += 1

    log(f"   📊 Media: {generated} generated, {skipped} already had URLs, {failed} failed")
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
    
    # Get LLM config (prefer exit_tickets purpose, fallback to any active)
    config = ModelConfig.get_for('exit_tickets')
    if not config:
        logger.error("No active LLM model configured for exit ticket generation")
        return {'generated': 0, 'failed': 0, 'skipped': 0}

    client = get_llm_client(config)

    lessons = Lesson.objects.filter(unit__course_id=course_id)
    generated = 0
    failed = 0
    skipped = 0
    
    total_lessons = lessons.count()
    log(f"   Processing {total_lessons} lessons...")

    for idx, lesson in enumerate(lessons):
        step_count = lesson.steps.count()

        # Skip if already has exit ticket
        if ExitTicket.objects.filter(lesson=lesson).exists():
            skipped += 1
            log(f"   [{idx+1}/{total_lessons}] ⏭️ {lesson.title} (already has exit ticket)")
            continue

        # Skip if no content yet
        if step_count == 0:
            skipped += 1
            log(f"   [{idx+1}/{total_lessons}] ⏭️ {lesson.title} (no steps yet)")
            continue

        log(f"   [{idx+1}/{total_lessons}] 🔄 {lesson.title} ({step_count} steps)...")

        try:
            # Query KB for additional context from teaching materials
            kb_context = ""
            exam_context = ""
            try:
                from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
                course = lesson.unit.course
                kb = CurriculumKnowledgeBase(institution_id=_resolve_institution_id(course=course))

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
                log(f"      ⚠️ KB query failed (continuing without): {e}")

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

            from apps.llm.prompts import get_prompt_or_default
            from apps.curriculum.dok_framework import dok_guidance_for
            system_prompt = get_prompt_or_default(
                _resolve_institution_id(lesson=lesson), 'exit_ticket_prompt',
                "You are an expert teacher creating assessment questions. Return ONLY valid JSON, no other text.",
                json_required=True,
            )
            # Webb's DOK rubric — every question targets a stated cognitive level.
            system_prompt = system_prompt + "\n\n" + dok_guidance_for("assessment")
            messages = [{"role": "user", "content": prompt}]

            response = client.generate(messages, system_prompt, max_tokens=16000)
            response_text = response.content.strip()

            log(f"      LLM response: {len(response_text)} chars, stop={response.stop_reason}")

            from apps.llm.json_utils import parse_llm_json
            questions_data = parse_llm_json(response_text, expect_array=True)

            if not questions_data or not isinstance(questions_data, list):
                log(f"   [{idx+1}/{total_lessons}] ✗ {lesson.title}: Failed to parse JSON from LLM response")
                log(f"      First 200 chars: {response_text[:200]}")
                failed += 1
                continue

            log(f"      Parsed {len(questions_data)} questions")

            num_questions = len(questions_data)

            # Create exit ticket
            exit_ticket = ExitTicket.objects.create(
                lesson=lesson,
                passing_score=8,
                time_limit_minutes=15,
                instructions=f"Answer all 10 questions. You need 8 correct to pass. (Selected from a bank of {num_questions})"
            )

            # Create questions (up to 40)
            questions_with_figures = []
            for i, q in enumerate(questions_data[:40]):
                # Map difficulty string
                diff = q.get('difficulty', 'medium').lower()
                if diff not in ('easy', 'medium', 'hard'):
                    diff = 'medium'

                question_obj = ExitTicketQuestion.objects.create(
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

                # Track questions that need figure generation
                figure_prompt = q.get('figure_prompt')
                if figure_prompt:
                    questions_with_figures.append((question_obj, figure_prompt))

            # Generate figures for questions that need them
            figures_generated = 0
            for question_obj, figure_prompt in questions_with_figures:
                try:
                    from apps.tutoring.image_service import ImageGenerationService
                    from django.core.files.base import ContentFile

                    service = ImageGenerationService(
                        lesson=lesson,
                        institution=_resolve_institution(lesson=lesson),
                    )

                    # Build textbook context from KB figure descriptions
                    textbook_ctx = ""
                    try:
                        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
                        kb = CurriculumKnowledgeBase(institution_id=_resolve_institution_id(lesson=lesson))
                        fig_descs = kb.query_for_figure_descriptions(
                            topic=lesson.title,
                            subject=lesson.unit.course.title,
                            n_results=2,
                        )
                        if fig_descs:
                            textbook_ctx = fig_descs[0].get('description', '')
                    except Exception:
                        pass

                    category = _detect_figure_category(figure_prompt)
                    result = service.get_or_generate_image(
                        prompt=figure_prompt,
                        category=category,
                        textbook_context=textbook_ctx,
                    )

                    if result and result.get('url'):
                        # Download and save to question's image field
                        import requests
                        from django.conf import settings
                        import os

                        image_url = result['url']
                        # If it's a local media URL, read from filesystem
                        if image_url.startswith('/media/'):
                            image_path = os.path.join(settings.MEDIA_ROOT, image_url.lstrip('/media/'))
                            if os.path.exists(image_path):
                                with open(image_path, 'rb') as f:
                                    image_bytes = f.read()
                                filename = os.path.basename(image_path)
                                question_obj.image.save(filename, ContentFile(image_bytes), save=True)
                                figures_generated += 1
                except Exception as e:
                    log(f"      ⚠️ Figure generation failed: {e}")

            generated += 1
            concepts = len(set(q.get('concept_tag', '') for q in questions_data if q.get('concept_tag')))
            fig_msg = f", {figures_generated} figures" if figures_generated else ""
            log(f"   [{idx+1}/{total_lessons}] ✓ {lesson.title}: {min(num_questions, 40)} questions ({concepts} concepts){fig_msg}")

        except Exception as e:
            failed += 1
            log(f"   [{idx+1}/{total_lessons}] ❌ {lesson.title}: {e}")
            import traceback
            logger.error(f"Exit ticket generation failed for {lesson.title}: {traceback.format_exc()}")

    log(f"   📊 Exit tickets: {generated} generated, {failed} failed, {skipped} skipped")
    return {'generated': generated, 'failed': failed, 'skipped': skipped}


def generate_single_lesson_async(lesson_id: int):
    """Generate content for a single lesson in background."""
    from apps.curriculum.models import Lesson
    from apps.curriculum.content_generator import LessonContentGenerator
    
    lesson = Lesson.objects.get(id=lesson_id)
    institution_id = _resolve_institution_id(lesson=lesson)

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


def generate_exit_ticket_for_lesson(lesson, institution) -> int:
    """
    Generate exit ticket MCQs for a lesson.
    Returns the number of questions generated.
    """
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client
    import json

    # Get LLM config (prefer exit_tickets purpose, fallback to any active)
    config = ModelConfig.get_for('exit_tickets')
    if not config:
        logger.error("No active LLM model configured for exit ticket generation")
        return 0

    # Build prompt for exit questions (35 for question bank, 10 selected per session)
    prompt = f"""Generate exactly 35 multiple choice exit ticket questions for this lesson.

Lesson: {lesson.title}
Objective: {lesson.objective}
Subject: {lesson.unit.course.title}

REQUIREMENTS:
1. Generate EXACTLY 35 questions
2. Each question must have exactly 4 options (A, B, C, D)
3. Include one correct answer per question
4. Include a short concept_tag for each question (the specific concept it tests)
5. Questions should directly assess the lesson objective
6. Vary question phrasing — avoid repetitive stems

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
    "difficulty": "easy",
    "concept_tag": "key concept tested"
  }}
]

DIFFICULTY DISTRIBUTION (out of 35):
- Questions 1-12: easy (recall facts)
- Questions 13-25: medium (apply concepts)
- Questions 26-35: hard (analyze/evaluate)

Return ONLY the JSON array, no other text."""

    try:
        client = get_llm_client(config)

        system_prompt = "You are an expert teacher creating assessment questions. Return ONLY valid JSON, no other text."
        messages = [{"role": "user", "content": prompt}]

        response = client.generate(messages, system_prompt, max_tokens=16000)
        response_text = response.content.strip()

        logger.info(f"Exit ticket response: {len(response_text)} chars, stop={response.stop_reason}")

        from apps.llm.json_utils import parse_llm_json
        questions_data = parse_llm_json(response_text, expect_array=True)

        if not questions_data or not isinstance(questions_data, list):
            logger.warning(f"Failed to parse exit ticket JSON for {lesson.title}")
            return 0

        logger.info(f"Parsed {len(questions_data)} questions for {lesson.title}")

        # Delete existing exit ticket and questions
        ExitTicket.objects.filter(lesson=lesson).delete()

        # Create new exit ticket
        exit_ticket = ExitTicket.objects.create(
            lesson=lesson,
            passing_score=8,
            time_limit_minutes=15,
            instructions="Answer 10 questions. You need 8 correct to pass."
        )

        # Create questions (up to 35 in the bank, 10 selected per session)
        questions_created = 0
        for i, q in enumerate(questions_data[:35]):
            try:
                ExitTicketQuestion.objects.create(
                    exit_ticket=exit_ticket,
                    question_text=q.get('question', ''),
                    option_a=q.get('option_a', ''),
                    option_b=q.get('option_b', ''),
                    option_c=q.get('option_c', ''),
                    option_d=q.get('option_d', ''),
                    correct_answer=q.get('correct_answer', 'A')[:1].upper(),
                    explanation=q.get('explanation', ''),
                    concept_tag=q.get('concept_tag', ''),
                    difficulty=q.get('difficulty', 'medium'),
                    order_index=i
                )
                questions_created += 1
            except Exception as e:
                logger.warning(f"Failed to create question {i}: {e}")

        logger.info(f"Created {questions_created} exit questions for {lesson.title}")
        return questions_created

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error for exit ticket: {e}")
        return 0
    except Exception as e:
        logger.error(f"Exit ticket generation failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 0


def regenerate_lesson_exit_ticket_only(
    lesson_id: int, institution_id: int, log_fn=None,
):
    """Regenerate ONLY the exit-ticket question bank for one lesson —
    steps are preserved. Mirrors the exit-ticket-only path inside
    generate_complete_course but scoped to a single lesson so the
    teacher can fix a bad question bank without paying for a full
    step regen.

    Sets ``content_status='generating'`` for the duration so the
    dashboard's per-lesson spinner + page-level banner fire, then
    restores the prior status when done. Uses ``force_regenerate=True``
    on ``generate_exit_ticket_for_lesson`` to do an in-place replace
    of the questions; ``ExitTicketAttempt`` history survives because
    the FK is to ``ExitTicket`` (not ``ExitTicketQuestion``).
    """
    from apps.curriculum.models import Lesson
    from apps.curriculum.content_generator import (
        generate_exit_ticket_for_lesson,
    )

    connection.close()

    def log(msg):
        print(f"[ExitTicketRegen] {msg}", flush=True)
        if log_fn:
            log_fn(msg)
        else:
            logger.info(msg)

    try:
        lesson = Lesson.objects.get(id=lesson_id)
    except Lesson.DoesNotExist:
        log(f"lesson {lesson_id} disappeared")
        return {'success': False, 'error': 'lesson disappeared'}

    prev_status = lesson.content_status
    marked_generating = False
    if prev_status != 'generating':
        lesson.content_status = 'generating'
        lesson.updated_at = timezone.now()
        lesson.save(update_fields=['content_status', 'updated_at'])
        marked_generating = True

    log(f"🔁 {lesson.title} — exit ticket only (steps preserved)")
    try:
        result = generate_exit_ticket_for_lesson(
            lesson, institution_id, force_regenerate=True,
        )
        ok = bool(result.get('success'))
        if not ok:
            log(f"   ⚠️ {lesson.title}: {result.get('error')}")
    except Exception as e:
        log(f"   ❌ {lesson.title}: crashed — {e}")
        result = {'success': False, 'error': str(e)}
        ok = False

    if marked_generating:
        try:
            lesson.refresh_from_db()
            if lesson.content_status == 'generating':
                lesson.content_status = (
                    prev_status if prev_status and prev_status != 'generating'
                    else 'ready'
                )
                lesson.updated_at = timezone.now()
                lesson.save(update_fields=['content_status', 'updated_at'])
        except Exception:
            pass

    return {'success': ok, **(result or {})}


def generate_complete_lesson(lesson_id: int, institution_id: int, log_fn=None):
    """
    Atomic function that generates all content for one lesson.
    Designed to be called from ThreadPoolExecutor.

    Pipeline: steps -> media -> exit tickets -> skills
    """
    import time
    from apps.curriculum.models import Lesson
    from apps.curriculum.content_generator import LessonContentGenerator

    # Close DB connection for thread safety
    connection.close()

    lesson = Lesson.objects.get(id=lesson_id)
    pipeline_start = time.time()

    def log(msg):
        print(f"[ContentGen] {msg}", flush=True)
        if log_fn:
            log_fn(msg)
        else:
            logger.info(msg)

    log(f"📋 Starting pipeline for '{lesson.title}' (id={lesson_id}, status={lesson.content_status})")

    # Guard: skip if already generating (another worker got here first)
    if lesson.content_status == 'generating':
        log(f"   ⏭️ {lesson.title} (already generating, skipping)")
        return {'lesson': lesson.title, 'success': True, 'skipped': True, 'steps': 0, 'media': 0, 'exit_questions': 0, 'skills': 0}

    # Atomic CAS: flip to 'generating' ONLY if status is still
    # non-'generating'. If two workers race here, only one update
    # succeeds — the other gets 0 rows and bails out. Without the
    # atomic update, both workers' read-then-write would race and
    # both proceed, producing the multi-spawn pattern we saw in logs
    # (3 parallel pipelines, duplicate exit-ticket key violation,
    # one worker reporting media=2 and another media=0).
    #
    # Bumping updated_at is also required: the course-detail page's
    # auto-recovery resets 'generating' lessons whose updated_at is
    # 10+ min old, and Django doesn't auto-bump updated_at when
    # update_fields is specified.
    from apps.curriculum.models import Lesson as _LessonModel
    rows = _LessonModel.objects.filter(
        id=lesson_id,
    ).exclude(content_status='generating').update(
        content_status='generating',
        updated_at=timezone.now(),
    )
    if rows == 0:
        log(f"   ⏭️ {lesson.title} (CAS lost — another worker is running)")
        return {'lesson': lesson.title, 'success': True, 'skipped': True, 'steps': 0, 'media': 0, 'exit_questions': 0, 'skills': 0}
    lesson.refresh_from_db()

    def _is_cancelled():
        """Return True only when the teacher explicitly cancelled.

        Before: any non-'generating' status counted as a cancel
        signal — but races between view-side resets ('empty') and
        worker-side status writes ('generating') triggered spurious
        cancellations mid-pipeline. Now we only honour the explicit
        'cancelled' sentinel set by `cancel_lesson_generation`.
        """
        lesson.refresh_from_db()
        return lesson.content_status == 'cancelled'

    try:
        # Step 1: Generate lesson steps
        log(f"   [1/4] Generating lesson steps via LLM...")
        t0 = time.time()
        generator = LessonContentGenerator(institution_id=institution_id)
        result = generator.generate_for_lesson(lesson, save_to_db=True)
        elapsed = time.time() - t0

        if not result.get('success'):
            lesson.content_status = 'failed'
            lesson.updated_at = timezone.now()
            lesson.save(update_fields=['content_status', 'updated_at'])
            log(f"   ❌ [1/4] Step generation FAILED after {elapsed:.1f}s: {result.get('error', 'Unknown error')}")
            return {'lesson': lesson.title, 'success': False, 'error': result.get('error')}

        steps_generated = result.get('steps_generated', 0)
        log(f"   ✅ [1/4] {steps_generated} steps generated in {elapsed:.1f}s")

        # Check cancellation before media
        if _is_cancelled():
            log(f"   ⛔ {lesson.title}: cancelled before media")
            return {'lesson': lesson.title, 'success': True, 'steps': steps_generated, 'media': 0, 'exit_questions': 0, 'skills': 0}

        # Step 2: Generate media for *lesson tutoring steps* only.
        # Exit-ticket figures never use raster gen (templates only).
        # Per-image timeout + total budget keep step 2 from blocking
        # the whole lesson on a slow Gemini call.
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
        IMG_TIMEOUT_S = 45
        STEP2_BUDGET_S = 240  # whole step caps at 4 minutes

        media_generated = 0
        log(f"   [2/4] Generating lesson-step media...")
        t0 = time.time()
        try:
            from apps.tutoring.image_service import ImageGenerationService
            institution = _resolve_institution(institution_id=institution_id, lesson=lesson)

            # Collect (step, img_index, description) tuples.
            jobs = []
            for step in lesson.steps.all():
                if not step.media:
                    continue
                images = step.media.get('images', [])
                if not images:
                    continue
                for i, img in enumerate(images):
                    if img.get('url'):
                        continue
                    desc = img.get('description', '')
                    if not desc:
                        continue
                    jobs.append((step, i, img, desc))

            def _do_one(step, idx, img, desc):
                svc = ImageGenerationService(lesson=lesson, institution=institution)
                return idx, svc.get_or_generate_image(
                    prompt=desc, category=img.get('type', 'diagram'),
                    generate_only=True,
                )

            steps_to_save = set()
            if jobs:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = {
                        pool.submit(_do_one, s, i, img, desc): (s, i, img, desc)
                        for (s, i, img, desc) in jobs
                    }
                    try:
                        for fut in as_completed(futures, timeout=STEP2_BUDGET_S):
                            step, i, img, desc = futures[fut]
                            try:
                                _, result = fut.result(timeout=0)
                            except Exception as e:
                                log(f"      Step {step.order_index}, img {i}: ⚠️ skipped — {e}")
                                continue
                            if result and result.get('url'):
                                img['url'] = result['url']
                                img['source'] = 'generated' if result.get('generated') else 'library'
                                steps_to_save.add(step)
                                media_generated += 1
                                log(f"      Step {step.order_index}, img {i}: ✅")
                            else:
                                log(f"      Step {step.order_index}, img {i}: ⚠️ no result")
                    except FuturesTimeout:
                        log(f"      ⏱  [2/4] step-2 budget ({STEP2_BUDGET_S}s) exceeded — moving on")
                        for f in futures:
                            f.cancel()

            for step in steps_to_save:
                step.save()

            elapsed = time.time() - t0
            log(f"   ✅ [2/4] {media_generated} media assets in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            log(f"   ⚠️ [2/4] Media generation error after {elapsed:.1f}s: {e}")
            import traceback
            logger.error(traceback.format_exc())

        # Check cancellation before exit tickets
        if _is_cancelled():
            log(f"   ⛔ {lesson.title}: cancelled before exit tickets")
            return {'lesson': lesson.title, 'success': True, 'steps': steps_generated, 'media': media_generated, 'exit_questions': 0, 'skills': 0}

        # Step 3: Exit tickets — already generated by content generator in Step 1
        # (generate_for_lesson calls _generate_exit_ticket with multi-format + EO prompt)
        from apps.tutoring.models import ExitTicket
        et = ExitTicket.objects.filter(lesson=lesson).first()
        exit_questions = et.questions.count() if et else 0
        log(f"   ✅ [3/4] {exit_questions} exit questions (generated in Step 1)")

        # Check cancellation before skills
        if _is_cancelled():
            log(f"   ⛔ {lesson.title}: cancelled before skills")
            return {'lesson': lesson.title, 'success': True, 'steps': steps_generated, 'media': media_generated, 'exit_questions': exit_questions, 'skills': 0}

        # Step 4: Extract skills
        skills_extracted = 0
        log(f"   [4/4] Extracting skills...")
        t0 = time.time()
        try:
            from apps.tutoring.skill_extraction import SkillExtractionService
            resolved_inst_id = _resolve_institution_id(institution_id=institution_id, lesson=lesson)
            skill_service = SkillExtractionService(institution_id=resolved_inst_id)
            skills = skill_service.extract_skills_for_lesson(lesson)
            skills_extracted = len(skills)
            elapsed = time.time() - t0
            log(f"   ✅ [4/4] {skills_extracted} skills in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            log(f"   ⚠️ [4/4] Skill extraction error after {elapsed:.1f}s: {e}")

        # Mark as ready
        lesson.content_status = 'ready'
        lesson.updated_at = timezone.now()
        lesson.save(update_fields=['content_status', 'updated_at'])

        total_elapsed = time.time() - pipeline_start
        log(f"🎉 Pipeline COMPLETE for '{lesson.title}' in {total_elapsed:.1f}s "
            f"(steps={steps_generated}, media={media_generated}, exit={exit_questions}, skills={skills_extracted})")

        return {
            'lesson': lesson.title,
            'success': True,
            'steps': steps_generated,
            'media': media_generated,
            'exit_questions': exit_questions,
            'skills': skills_extracted,
        }

    except Exception as e:
        total_elapsed = time.time() - pipeline_start
        log(f"💥 Pipeline FAILED for '{lesson.title}' after {total_elapsed:.1f}s: {e}")
        import traceback
        logger.error(traceback.format_exc())
        lesson.content_status = 'failed'
        lesson.updated_at = timezone.now()
        lesson.save(update_fields=['content_status', 'updated_at'])
        return {'lesson': lesson.title, 'success': False, 'error': str(e)}


def generate_complete_course(
    course_id: int,
    institution_id: int,
    log_fn=None,
    max_workers: int = 10,
    *,
    regen_steps: bool = True,
    regen_exit_tickets: bool = True,
    regen_summative: bool = True,
):
    """
    Regenerate course content. Three independent toggles select
    which phases to run — matches the checkboxes on the "Regenerate
    content" form on the course detail page.

      regen_steps          — Phase 1 (LessonStep wipe + regen)
      regen_exit_tickets   — Phase 2 (ExitTicketQuestion bank
                             force-replace; drops attempt history
                             on affected lessons)
      regen_summative      — Phase 3 (re-sample summative bank
                             from the current lesson exit tickets;
                             math courses only; drops summative
                             attempt rows)

    All-three is the typical "regenerate everything" flow. Steps-
    off + tickets-on is the "fix exit tickets without paying for
    step regen" flow. Summative-only requires the lesson exit
    tickets to already be the correct version (no LLM call).

    Three phases:
      1. Steps — wipes LessonStep rows per lesson, regenerates via
         the standard pipeline (parallel across lessons via
         ThreadPoolExecutor). Activates Layer 1 + Layer 3 defenses
         on the new step content.
      2. Exit tickets — for each lesson that has steps regenerated,
         force-regenerate its ExitTicketQuestion bank with
         force_regenerate=True. This is an in-place replace: the
         OLD questions are deleted but the ExitTicket row is kept,
         and ExitTicketAttempt has its FK to ExitTicket (not to
         ExitTicketQuestion) — so attempt rows SURVIVE the regen.
         Fixed in commit 25c62a2 after a regression that wiped
         attempts. Activates Layers 1 + 2 + 4 on the new questions.
      3. Summative bank — for math courses, re-sample the summative
         bank from the (now-fresh) lesson exit tickets via
         generate_summative_for_course. No LLM call — it's
         deterministic sampling.

    What's PRESERVED across all three phases:
      - Lesson row (PK stable; FK targets survive)
      - ExitTicket row (lesson + summative; in-place question
        replacement keeps attempt history attached)
      - ExitTicketAttempt rows (every purpose — practice, baseline,
        final, retake, diagnostic — survives the regen)
      - StudentLessonProgress, StudentSkillMastery, TutorSession
      - StudentCompetencyRecord (permanent mastery transcript)

    What's WIPED:
      - LessonStep rows (replaced with regen)
      - ExitTicketQuestion rows (replaced; attempts hang off
        ExitTicket so they survive)

    max_workers default 10 — bumped from 3 (2026-05-01) per pilot
    feedback that 3 was bottlenecking course-wide regen. Anthropic
    rate limits comfortably handle 10 parallel content_generation
    calls; the AnthropicClient retry-with-backoff loop catches the
    occasional 429. Dial back if we see sustained rate-limit pressure.

    Returns a summary dict that the calling view can flash to the user.
    """
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from apps.curriculum.models import Course, Lesson
    from apps.tutoring.models import ExitTicket
    from apps.curriculum.content_generator import (
        generate_exit_ticket_for_lesson,
    )

    connection.close()

    course = Course.objects.get(id=course_id)
    pipeline_start = time.time()

    # Resolve the three flags. If the caller passed all-False
    # (shouldn't happen — view validates) we'd silently no-op;
    # log a warning so it's visible.
    do_steps = bool(regen_steps)
    do_exit_tickets = bool(regen_exit_tickets)
    do_summative = bool(regen_summative)
    if not (do_steps or do_exit_tickets or do_summative):
        logger.warning(
            "[CourseRegen] %s: no phases selected — nothing to do",
            course.title,
        )
        return {
            'course': course.title, 'total': 0, 'success': 0,
            'failed': 0, 'skipped': 0, 'lessons': [],
            'exit_tickets_ok': 0, 'exit_tickets_failed': 0,
            'summative_questions': 0, 'summative_error': None,
        }

    log_lock = threading.Lock()

    def log(msg):
        with log_lock:
            print(f"[CourseRegen] {msg}", flush=True)
            if log_fn:
                log_fn(msg)
            else:
                logger.info(msg)

    lessons = list(
        Lesson.objects.filter(unit__course=course).order_by('unit__order_index', 'order_index')
    )
    scope_label = "steps + exit tickets + summative" if do_steps else "exit tickets + summative (steps preserved)"
    log(f"🚀 Starting course regen for '{course.title}' — {len(lessons)} lesson(s), scope: {scope_label}, {max_workers} parallel workers")

    summary = {
        'course': course.title,
        'total': len(lessons),
        'success': 0,
        'failed': 0,
        'skipped': 0,
        # Phase 2 + 3 counters (populated below).
        'exit_tickets_ok': 0,
        'exit_tickets_failed': 0,
        'summative_questions': 0,
        'summative_error': None,
        'lessons': [],
    }
    summary_lock = threading.Lock()

    def _process(lesson_id: int) -> dict:
        """Wipe + pipeline + exit-ticket regen for one lesson.
        Runs in a worker thread."""
        # Each thread needs its own DB connection (mirrors the pattern
        # in generate_complete_lesson).
        connection.close()
        try:
            lesson = Lesson.objects.get(id=lesson_id)
        except Lesson.DoesNotExist:
            return {'lesson_id': lesson_id, 'status': 'failed', 'error': 'lesson disappeared'}

        if lesson.content_status == 'generating':
            log(f"   ⏭️ {lesson.title} (already generating — skipping)")
            return {'lesson': lesson.title, 'status': 'skipped'}

        # Phase 1 — wipe lesson STEPS, run the standard pipeline.
        # The pipeline's _generate_exit_ticket has skip-if-exists
        # semantics, which is fine here — we replace the questions
        # explicitly in phase 2 below with force_regenerate=True.
        # Skipped when scope='exit_tickets' — steps stay as-is.
        if do_steps:
            lesson.steps.all().delete()
            lesson.content_status = 'empty'
            lesson.updated_at = timezone.now()
            lesson.save(update_fields=['content_status', 'updated_at'])

            try:
                result = generate_complete_lesson(lesson.id, institution_id, log_fn=log_fn)
                ok = bool(result.get('success'))
            except Exception as e:
                log(f"   ❌ {lesson.title}: {e}")
                return {'lesson': lesson.title, 'status': 'failed', 'error': str(e)}

            if not ok:
                return {'lesson': lesson.title, 'status': 'failed',
                        'error': result.get('error')}

        # Phase 2 — force-regenerate the exit ticket so Layers 1+2+4
        # apply to the question bank. Failures here don't fail the
        # whole lesson — steps are already regenerated and saved;
        # the exit ticket can be retried independently. Skipped
        # when do_exit_tickets is False.
        #
        # Even when do_steps is False (exit-ticket-only regen), we
        # mark content_status='generating' for the duration so the
        # dashboard's per-lesson spinner + page-level banner fire.
        # Without this the regen ran silently and the teacher had no
        # idea anything was happening. Status is restored at the end.
        et_status = None
        et_error = None
        if do_exit_tickets:
            et_status = 'ok'
            prev_status = None
            marked_generating = False
            if not do_steps:
                # Steps phase already set 'generating'; only mark
                # here for exit-ticket-only runs.
                try:
                    lesson.refresh_from_db()
                    prev_status = lesson.content_status
                    if prev_status != 'generating':
                        lesson.content_status = 'generating'
                        lesson.updated_at = timezone.now()
                        lesson.save(update_fields=['content_status', 'updated_at'])
                        marked_generating = True
                except Exception:
                    pass

            try:
                lesson.refresh_from_db()
                et_result = generate_exit_ticket_for_lesson(
                    lesson, institution_id, force_regenerate=True,
                )
                if not et_result.get('success'):
                    et_status = 'failed'
                    et_error = et_result.get('error')
                    log(f"   ⚠️ {lesson.title}: exit-ticket regen failed — {et_error}")
            except Exception as e:
                et_status = 'failed'
                et_error = str(e)
                log(f"   ⚠️ {lesson.title}: exit-ticket regen crashed — {e}")

            # Restore content_status. generate_exit_ticket_for_lesson
            # may have already set READY_WITH_WARNINGS — in which case
            # leave it. Otherwise fall back to the prior status (likely
            # 'ready') or 'ready' itself.
            if marked_generating:
                try:
                    lesson.refresh_from_db()
                    if lesson.content_status == 'generating':
                        restore_to = (
                            prev_status if prev_status and prev_status != 'generating'
                            else 'ready'
                        )
                        lesson.content_status = restore_to
                        lesson.updated_at = timezone.now()
                        lesson.save(update_fields=['content_status', 'updated_at'])
                except Exception:
                    pass

        return {
            'lesson': lesson.title,
            'status': 'ok',
            'exit_ticket_status': et_status,
            'exit_ticket_error': et_error,
        }

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process, l.id): l.id for l in lessons}
        for future in as_completed(futures):
            res = future.result()
            with summary_lock:
                completed += 1
                if res['status'] == 'ok':
                    summary['success'] += 1
                    if res.get('exit_ticket_status') == 'ok':
                        summary['exit_tickets_ok'] += 1
                    elif res.get('exit_ticket_status') == 'failed':
                        summary['exit_tickets_failed'] += 1
                elif res['status'] == 'skipped':
                    summary['skipped'] += 1
                else:
                    summary['failed'] += 1
                summary['lessons'].append(res)
            et_msg = ''
            if res.get('exit_ticket_status') == 'ok':
                et_msg = ' + ET ok'
            elif res.get('exit_ticket_status') == 'failed':
                et_msg = ' + ET failed'
            log(f"   [{completed}/{len(lessons)}] {res.get('lesson', '?')} → {res['status']}{et_msg}")

    # Phase 3 — rebuild the summative bank by sampling from the
    # (now-fresh) lesson exit tickets. Free, no LLM call. Math-only,
    # and only when the caller asked for it.
    if course.is_math and do_summative:
        try:
            from apps.tutoring.summative_generator import (
                generate_summative_for_course,
            )
            log(f"📚 Rebuilding summative bank for '{course.title}'…")
            sum_result = generate_summative_for_course(course)
            if sum_result.get('success'):
                summary['summative_questions'] = sum_result.get(
                    'questions_created', 0,
                )
                log(
                    f"   ✓ summative: "
                    f"{summary['summative_questions']} questions "
                    f"sampled across "
                    f"{sum_result.get('lessons_processed', 0)} lessons"
                )
            else:
                summary['summative_error'] = sum_result.get('error')
                log(f"   ⚠️ summative rebuild failed: {summary['summative_error']}")
        except Exception as e:
            summary['summative_error'] = str(e)
            log(f"   ⚠️ summative rebuild crashed: {e}")

    elapsed = time.time() - pipeline_start
    log(
        f"✅ Course regen done in {elapsed:.1f}s — "
        f"steps {summary['success']}/{len(lessons)} ok · "
        f"exit-tickets {summary['exit_tickets_ok']}/{len(lessons)} ok · "
        f"summative {summary['summative_questions']} questions"
    )
    return summary


def _detect_figure_category(prompt: str) -> str:
    """Detect the image category from a figure generation prompt."""
    prompt_lower = prompt.lower()
    # Check photo first (before chart) since "photograph" contains "graph"
    if any(kw in prompt_lower for kw in ['photo', 'photograph', 'real image']):
        return 'photo'
    if any(kw in prompt_lower for kw in ['graph', 'chart', 'bar chart', 'pie', 'histogram', 'line graph']):
        return 'chart'
    if any(kw in prompt_lower for kw in ['map', 'geographic', 'contour', 'relief']):
        return 'map'
    return 'diagram'