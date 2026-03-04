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
            if existing_steps >= 5:
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
            system_prompt = get_prompt_or_default(
                _resolve_institution_id(lesson=lesson), 'exit_ticket_prompt',
                "You are an expert teacher creating assessment questions. Return ONLY valid JSON, no other text.",
                json_required=True,
            )
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

    # Mark as generating
    lesson.content_status = 'generating'
    lesson.save(update_fields=['content_status'])

    def _is_cancelled():
        """Check if lesson generation was cancelled (status changed externally)."""
        lesson.refresh_from_db()
        return lesson.content_status != 'generating'

    try:
        # Step 1: Generate lesson steps
        log(f"   [1/4] Generating lesson steps via LLM...")
        t0 = time.time()
        generator = LessonContentGenerator(institution_id=institution_id)
        result = generator.generate_for_lesson(lesson, save_to_db=True)
        elapsed = time.time() - t0

        if not result.get('success'):
            lesson.content_status = 'failed'
            lesson.save(update_fields=['content_status'])
            log(f"   ❌ [1/4] Step generation FAILED after {elapsed:.1f}s: {result.get('error', 'Unknown error')}")
            return {'lesson': lesson.title, 'success': False, 'error': result.get('error')}

        steps_generated = result.get('steps_generated', 0)
        log(f"   ✅ [1/4] {steps_generated} steps generated in {elapsed:.1f}s")

        # Check cancellation before media
        if _is_cancelled():
            log(f"   ⛔ {lesson.title}: cancelled before media")
            return {'lesson': lesson.title, 'success': True, 'steps': steps_generated, 'media': 0, 'exit_questions': 0, 'skills': 0}

        # Step 2: Generate media
        media_generated = 0
        log(f"   [2/4] Generating media assets...")
        t0 = time.time()
        try:
            from apps.tutoring.image_service import ImageGenerationService
            institution = _resolve_institution(institution_id=institution_id, lesson=lesson)

            steps_with_media = 0
            for step in lesson.steps.all():
                if not step.media:
                    continue
                images = step.media.get('images', [])
                if not images:
                    continue
                steps_with_media += 1
                media_updated = False
                for i, img in enumerate(images):
                    if img.get('url'):
                        log(f"      Step {step.order_index}, img {i}: already has URL, skipping")
                        continue
                    description = img.get('description', '')
                    if not description:
                        log(f"      Step {step.order_index}, img {i}: no description, skipping")
                        continue
                    log(f"      Step {step.order_index}, img {i}: generating '{description[:60]}...'")
                    img_t0 = time.time()
                    service = ImageGenerationService(lesson=lesson, institution=institution)
                    img_result = service.get_or_generate_image(
                        prompt=description,
                        category=img.get('type', 'diagram'),
                        generate_only=True,
                    )
                    img_elapsed = time.time() - img_t0
                    if img_result and img_result.get('url'):
                        img['url'] = img_result['url']
                        img['source'] = 'generated' if img_result.get('generated') else 'library'
                        media_updated = True
                        media_generated += 1
                        log(f"      Step {step.order_index}, img {i}: ✅ done in {img_elapsed:.1f}s")
                    else:
                        log(f"      Step {step.order_index}, img {i}: ⚠️ no result after {img_elapsed:.1f}s")
                if media_updated:
                    step.save()

            elapsed = time.time() - t0
            log(f"   ✅ [2/4] {media_generated} media assets from {steps_with_media} steps in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            log(f"   ⚠️ [2/4] Media generation error after {elapsed:.1f}s: {e}")
            import traceback
            logger.error(traceback.format_exc())

        # Check cancellation before exit tickets
        if _is_cancelled():
            log(f"   ⛔ {lesson.title}: cancelled before exit tickets")
            return {'lesson': lesson.title, 'success': True, 'steps': steps_generated, 'media': media_generated, 'exit_questions': 0, 'skills': 0}

        # Step 3: Generate exit tickets
        exit_questions = 0
        log(f"   [3/4] Generating exit ticket questions...")
        t0 = time.time()
        try:
            exit_questions = generate_exit_ticket_for_lesson(lesson, None)
            elapsed = time.time() - t0
            log(f"   ✅ [3/4] {exit_questions} exit questions in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            log(f"   ⚠️ [3/4] Exit ticket error after {elapsed:.1f}s: {e}")

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
        lesson.save(update_fields=['content_status'])

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
        lesson.save(update_fields=['content_status'])
        return {'lesson': lesson.title, 'success': False, 'error': str(e)}


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