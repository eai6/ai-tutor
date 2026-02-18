"""
Tutoring Views - Web endpoints for the tutoring interface.

Uses the step-based engine for predictable, curriculum-driven tutoring.
"""

import json
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson, LessonStep
from apps.tutoring.models import TutorSession, StudentLessonProgress
from apps.tutoring.engine import TutorEngine, create_tutor_session
from apps.media_library.models import StepMedia


def get_user_institution(user):
    """Get the user's active institution membership."""
    membership = Membership.objects.filter(
        user=user,
        is_active=True
    ).select_related('institution').first()
    return membership.institution if membership else None


def get_student_progress(user, institution):
    """Get progress for all lessons for a student."""
    progress = StudentLessonProgress.objects.filter(
        student=user,
        lesson__unit__course__institution=institution
    ).select_related('lesson')
    
    return {p.lesson_id: p for p in progress}


def get_step_media(step: LessonStep) -> list:
    """Get media attachments for a lesson step."""
    if not step:
        return []
    
    attachments = StepMedia.objects.filter(
        lesson_step=step
    ).select_related('media_asset').order_by('order_index')
    
    return [{
        'id': att.media_asset.id,
        'title': att.media_asset.title,
        'type': att.media_asset.asset_type,
        'url': att.media_asset.file.url if att.media_asset.file else None,
        'alt_text': att.media_asset.alt_text,
        'caption': att.media_asset.caption,
        'placement': att.placement,
    } for att in attachments]


def get_all_lesson_media(lesson: Lesson) -> list:
    """Get ALL media attachments for a lesson (all steps)."""
    attachments = StepMedia.objects.filter(
        lesson_step__lesson=lesson
    ).select_related('media_asset').order_by('lesson_step__order_index', 'order_index')
    
    return [{
        'id': att.media_asset.id,
        'title': att.media_asset.title,
        'type': att.media_asset.asset_type,
        'url': att.media_asset.file.url if att.media_asset.file else None,
        'alt_text': att.media_asset.alt_text,
        'caption': att.media_asset.caption,
        'placement': att.placement,
    } for att in attachments]


@login_required
def lesson_list(request):
    """List available lessons for the student."""
    institution = get_user_institution(request.user)
    if not institution:
        return JsonResponse({"error": "No institution membership"}, status=403)
    
    lessons = Lesson.objects.filter(
        unit__course__institution=institution,
        is_published=True
    ).select_related('unit', 'unit__course')
    
    data = [{
        "id": lesson.id,
        "title": lesson.title,
        "course": lesson.unit.course.title,
        "unit": lesson.unit.title,
        "objective": lesson.objective,
        "estimated_minutes": lesson.estimated_minutes,
    } for lesson in lessons]
    
    return JsonResponse({"lessons": data})


@login_required
@require_http_methods(["POST"])
def start_session(request, lesson_id):
    """Start a new tutoring session for a lesson."""
    institution = get_user_institution(request.user)
    if not institution:
        return JsonResponse({"error": "No institution membership"}, status=403)
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution,
        is_published=True
    )
    
    # Check for existing active session
    existing = TutorSession.objects.filter(
        student=request.user,
        lesson=lesson,
        status=TutorSession.Status.ACTIVE
    ).first()
    
    is_resume = False
    if existing:
        session = existing
        is_resume = True
    else:
        session = create_tutor_session(
            student=request.user,
            lesson=lesson,
            institution=institution,
        )
    
    # Use step-based engine
    engine = TutorEngine(session)
    
    if is_resume:
        response = engine.resume()
    else:
        response = engine.start()
    
    # Get media for current step
    current_step = engine.current_step
    media = get_step_media(current_step)
    
    return JsonResponse({
        "session_id": session.id,
        "lesson": {
            "id": lesson.id,
            "title": lesson.title,
            "objective": lesson.objective,
        },
        "tutor_message": response.message,
        "step_index": response.step_index,
        "step_type": response.step_type,
        "is_waiting_for_answer": response.is_waiting_for_answer,
        "is_session_complete": response.is_session_complete,
        "mastery_achieved": response.mastery_achieved,
        "phase": response.phase,
        "question": response.question,
        "commands": response.commands,
        "media": media,
        "is_resume": is_resume,
    })


@login_required
@csrf_exempt  # For simplicity; use proper CSRF in production
@require_http_methods(["POST"])
def submit_answer(request, session_id):
    """Submit a student answer with safety checks."""
    from apps.safety import (
        ContentSafetyFilter, RateLimiter, SafetyAuditLog,
        ChildProtection, ContentFlag
    )
    
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    
    # Rate limiting
    allowed, reason = RateLimiter.check_rate_limit(request.user.id)
    if not allowed:
        SafetyAuditLog.log(
            'rate_limited',
            user=request.user,
            session_id=session.id,
            details={'reason': reason},
            severity='warning',
            request=request,
        )
        return JsonResponse({"error": reason, "rate_limited": True}, status=429)
    
    RateLimiter.record_message(request.user.id)
    
    try:
        data = json.loads(request.body)
        answer = data.get("answer", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    if not answer:
        return JsonResponse({"error": "Answer required"}, status=400)
    
    # Content safety check
    safety_result = ContentSafetyFilter.check_content(answer, context="student_input")
    
    if safety_result.flags:
        SafetyAuditLog.log(
            'content_flagged',
            user=request.user,
            session_id=session.id,
            details={
                'flags': [f.value for f in safety_result.flags],
                'warnings': safety_result.warnings,
            },
            severity='warning' if not safety_result.blocked else 'critical',
            request=request,
        )
    
    # Handle blocked content
    if safety_result.blocked:
        safe_response = ContentSafetyFilter.get_safe_response(safety_result.flags[0])
        return JsonResponse({
            "tutor_message": safe_response,
            "step_index": session.current_step_index,
            "step_type": "safety",
            "is_waiting_for_answer": True,
            "is_session_complete": False,
            "mastery_achieved": False,
            "safety_warning": True,
        })
    
    # Use filtered content
    safe_answer = safety_result.filtered_content
    
    # Use step-based engine
    engine = TutorEngine(session)
    response = engine.process_answer(safe_answer)
    
    # Get media for current step
    current_step = engine.current_step
    media = get_step_media(current_step)
    
    return JsonResponse({
        "tutor_message": response.message,
        "step_index": response.step_index,
        "step_type": response.step_type,
        "phase": response.phase,
        "is_waiting_for_answer": response.is_waiting_for_answer,
        "is_session_complete": response.is_session_complete,
        "mastery_achieved": response.mastery_achieved,
        "question": response.question,
        "grading": {
            "result": response.grading.result.value,
            "feedback": response.grading.feedback,
            "score": response.grading.score,
        } if response.grading else None,
        "attempts_remaining": response.attempts_remaining,
        "hint": response.hint,
        "commands": response.commands,
        "media": media,
    })


@login_required
@require_http_methods(["POST"])
def advance_step(request, session_id):
    """Advance to the next step (for steps that don't require answers)."""
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    
    engine = TutorEngine(session)
    response = engine.advance()
    
    # Get media for current step
    current_step = engine.current_step
    media = get_step_media(current_step)
    
    return JsonResponse({
        "tutor_message": response.message,
        "step_index": response.step_index,
        "step_type": response.step_type,
        "is_waiting_for_answer": response.is_waiting_for_answer,
        "is_session_complete": response.is_session_complete,
        "mastery_achieved": response.mastery_achieved,
        "media": media,
    })


@login_required
def session_status(request, session_id):
    """Get current session status."""
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user
    )
    
    return JsonResponse({
        "session_id": session.id,
        "status": session.status,
        "current_step_index": session.current_step_index,
        "mastery_achieved": session.mastery_achieved,
        "started_at": session.started_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
    })


# ---- Simple HTML interface ----

@login_required
def tutor_interface(request, lesson_id):
    """Render the tutoring interface page."""
    institution = get_user_institution(request.user)
    if not institution:
        return render(request, 'tutoring/error.html', {"message": "No institution"})
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution,
        is_published=True
    )
    
    return render(request, 'tutoring/session.html', {
        "lesson": lesson,
        "user": request.user,
    })


def lesson_catalog(request):
    """Subject-based lesson catalog with progress tracking."""
    if not request.user.is_authenticated:
        return render(request, 'tutoring/catalog.html', {
            "subjects": [],
            "selected_subject": None,
            "active_sessions": [],
        })
    
    institution = get_user_institution(request.user)
    if not institution:
        return render(request, 'tutoring/catalog.html', {
            "subjects": [],
            "selected_subject": None,
            "active_sessions": [],
        })
    
    # Get active sessions (incomplete) for resume
    active_sessions = TutorSession.objects.filter(
        student=request.user,
        institution=institution,
        status=TutorSession.Status.ACTIVE
    ).select_related('lesson', 'lesson__unit', 'lesson__unit__course').order_by('-started_at')[:5]
    
    active_sessions_data = [{
        'session_id': s.id,
        'lesson_id': s.lesson.id,
        'lesson_title': s.lesson.title,
        'course_title': s.lesson.unit.course.title,
        'started_at': s.started_at,
        'phase': s.engine_state.get('phase', 'retrieval') if s.engine_state else 'retrieval',
        'questions_correct': s.engine_state.get('questions_correct', 0) if s.engine_state else 0,
    } for s in active_sessions]
    
    # Get all courses (subjects) for this institution
    courses = Course.objects.filter(
        institution=institution,
        is_published=True
    ).prefetch_related(
        'units__lessons'
    ).order_by('title')
    
    # Get student progress
    progress_map = get_student_progress(request.user, institution)
    
    # Get selected subject from query param
    selected_subject_id = request.GET.get('subject')
    selected_subject = None
    
    # Build subjects with lesson counts and progress
    subjects = []
    for course in courses:
        total_lessons = 0
        completed_lessons = 0
        in_progress_lessons = 0
        
        units_data = []
        for unit in course.units.all().order_by('order_index'):
            unit_lessons = []
            for lesson in unit.lessons.filter(is_published=True).order_by('order_index'):
                total_lessons += 1
                
                # Check progress
                progress = progress_map.get(lesson.id)
                if progress:
                    if progress.mastery_level == 'mastered':
                        completed_lessons += 1
                        status = 'completed'
                    elif progress.mastery_level == 'in_progress':
                        in_progress_lessons += 1
                        status = 'in_progress'
                    else:
                        status = 'not_started'
                else:
                    status = 'not_started'
                
                unit_lessons.append({
                    'id': lesson.id,
                    'title': lesson.title,
                    'objective': lesson.objective,
                    'estimated_minutes': lesson.estimated_minutes,
                    'status': status,
                })
            
            if unit_lessons:
                units_data.append({
                    'title': unit.title,
                    'lessons': unit_lessons,
                })
        
        subject_data = {
            'id': course.id,
            'title': course.title,
            'description': course.description,
            'total_lessons': total_lessons,
            'completed_lessons': completed_lessons,
            'in_progress_lessons': in_progress_lessons,
            'progress_percent': int((completed_lessons / total_lessons * 100)) if total_lessons > 0 else 0,
            'units': units_data,
        }
        
        # Only add courses that have at least 1 published lesson
        if total_lessons > 0:
            subjects.append(subject_data)
        
        if selected_subject_id and str(course.id) == selected_subject_id:
            selected_subject = subject_data
    
    # Default to first subject if none selected
    if not selected_subject and subjects:
        selected_subject = subjects[0]
    
    return render(request, 'tutoring/catalog.html', {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "active_sessions": active_sessions_data,
    })


# ---- Streaming endpoint ----

from django.http import StreamingHttpResponse
from apps.llm.prompts import build_tutor_message


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def submit_answer_stream(request, session_id):
    """Submit a student answer and stream the response with safety checks."""
    from apps.safety import (
        ContentSafetyFilter, RateLimiter, SafetyAuditLog,
        ChildProtection, ContentFlag
    )
    
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    
    # Rate limiting
    allowed, reason = RateLimiter.check_rate_limit(request.user.id)
    if not allowed:
        SafetyAuditLog.log(
            'rate_limited',
            user=request.user,
            session_id=session.id,
            details={'reason': reason},
            severity='warning',
            request=request,
        )
        return JsonResponse({"error": reason, "rate_limited": True}, status=429)
    
    RateLimiter.record_message(request.user.id)
    
    try:
        data = json.loads(request.body)
        answer = data.get("answer", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    if not answer:
        return JsonResponse({"error": "Answer required"}, status=400)
    
    # Content safety check
    safety_result = ContentSafetyFilter.check_content(answer, context="student_input")
    
    if safety_result.flags:
        SafetyAuditLog.log(
            'content_flagged',
            user=request.user,
            session_id=session.id,
            details={
                'flags': [f.value for f in safety_result.flags],
                'warnings': safety_result.warnings,
            },
            severity='warning' if not safety_result.blocked else 'critical',
            request=request,
        )
    
    # Handle blocked content with streaming response
    if safety_result.blocked:
        safe_response = ContentSafetyFilter.get_safe_response(safety_result.flags[0])
        def blocked_stream():
            yield f"data: {json.dumps({'chunk': safe_response})}\n\n"
            yield f"data: {json.dumps({'done': True, 'is_session_complete': False, 'safety_warning': True})}\n\n"
        
        response = StreamingHttpResponse(blocked_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        return response
    
    # Use filtered content
    safe_answer = safety_result.filtered_content
    
    def generate_stream():
        """Generator that yields SSE formatted chunks."""
        from apps.tutoring.engine import TutorEngine
        from apps.tutoring.models import SessionTurn
        
        engine = TutorEngine(session)
        
        # Process answer through engine (handles phase tracking, commands, etc.)
        response = engine.process_answer(safe_answer)
        
        # Stream the response content
        # For now, yield the full message (can be chunked later if needed)
        yield f"data: {json.dumps({'chunk': response.message})}\n\n"
        
        # Send commands for artifact updates
        if response.commands:
            yield f"data: {json.dumps({'commands': response.commands})}\n\n"
        
        # Send final metadata including question data for UI update
        final_data = {
            'done': True, 
            'is_session_complete': response.is_session_complete,
            'phase': response.phase,
            'step_index': response.step_index,
            'step_type': response.step_type,
            'is_waiting_for_answer': response.is_waiting_for_answer,
            'commands': response.commands,
        }
        
        # Include question data if present
        if response.question:
            final_data['question'] = response.question
        
        # Include grading feedback if present
        if response.grading:
            final_data['grading'] = response.grading.value
        if response.hint:
            final_data['hint'] = response.hint
        if response.attempts_remaining is not None:
            final_data['attempts_remaining'] = response.attempts_remaining
        
        yield f"data: {json.dumps(final_data)}\n\n"
    
    response = StreamingHttpResponse(
        generate_stream(),
        content_type='text/event-stream'
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


# ---- Image Generation Endpoint ----

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def generate_image(request):
    """Generate an educational image using DALL-E."""
    import os
    
    try:
        data = json.loads(request.body)
        prompt = data.get("prompt", "").strip()
        session_id = data.get("session_id")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    if not prompt:
        return JsonResponse({"error": "Prompt required"}, status=400)
    
    # Get OpenAI key
    openai_key = os.environ.get('OPENAI_API_KEY')
    if not openai_key:
        return JsonResponse({"error": "Image generation not configured"}, status=503)
    
    try:
        from openai import OpenAI
        import requests
        from django.core.files.base import ContentFile
        from apps.media_library.models import MediaAsset
        
        client = OpenAI(api_key=openai_key)
        
        # Add educational style to prompt
        full_prompt = f"{prompt}. Style: educational illustration, clear and simple, suitable for secondary school students, no text overlays."
        
        # Generate image
        response = client.images.generate(
            model="dall-e-3",
            prompt=full_prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        
        image_url = response.data[0].url
        
        # Download image
        image_response = requests.get(image_url)
        image_response.raise_for_status()
        
        # Save as MediaAsset
        institution = get_user_institution(request.user)
        safe_title = "".join(c if c.isalnum() else "_" for c in prompt[:50])
        filename = f"generated_{safe_title}.png"
        
        media_asset = MediaAsset.objects.create(
            institution=institution,
            title=prompt[:100],
            asset_type='image',
            alt_text=prompt[:200],
            caption=f"AI-generated: {prompt[:100]}",
            tags="ai-generated, educational",
        )
        
        media_asset.file.save(
            filename,
            ContentFile(image_response.content),
            save=True
        )
        
        return JsonResponse({
            "url": media_asset.file.url,
            "title": media_asset.title,
            "caption": media_asset.caption,
        })
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


# ---- Structured Session Views ----

@login_required
def tutor_interface_v2(request, lesson_id):
    """Render the new split-panel tutoring interface."""
    institution = get_user_institution(request.user)
    if not institution:
        return render(request, 'tutoring/error.html', {"message": "No institution"})
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution,
        is_published=True
    )
    
    return render(request, 'tutoring/session_v2.html', {
        "lesson": lesson,
        "user": request.user,
    })


@login_required
@require_http_methods(["POST"])
def start_structured_session(request, lesson_id):
    """Start a structured tutoring session."""
    from apps.tutoring.structured_engine import StructuredSessionEngine
    
    institution = get_user_institution(request.user)
    if not institution:
        return JsonResponse({"error": "No institution membership"}, status=403)
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution,
        is_published=True
    )
    
    # Check for existing active session
    existing = TutorSession.objects.filter(
        student=request.user,
        lesson=lesson,
        status=TutorSession.Status.ACTIVE
    ).first()
    
    if existing:
        session = existing
    else:
        session = create_tutor_session(
            student=request.user,
            lesson=lesson,
            institution=institution,
        )
    
    # Use structured engine
    engine = StructuredSessionEngine(session)
    
    if not existing:
        response = engine.start()
    else:
        # Resume - get current state
        response = engine.start()
    
    # Get all lesson media
    all_media = get_all_lesson_media(lesson)
    
    return JsonResponse({
        "session_id": session.id,
        "lesson": {
            "id": lesson.id,
            "title": lesson.title,
            "objective": lesson.objective,
        },
        "chat_message": response.chat_message,
        "commands": response.commands,
        "phase": response.phase.value,
        "is_complete": response.is_complete,
        "all_media": all_media,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def structured_session_input(request, session_id):
    """Process input in a structured session."""
    from apps.tutoring.structured_engine import StructuredSessionEngine
    
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    
    try:
        data = json.loads(request.body)
        user_input = data.get("input", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    if not user_input:
        return JsonResponse({"error": "Input required"}, status=400)
    
    engine = StructuredSessionEngine(session)
    response = engine.process_input(user_input)
    
    return JsonResponse({
        "chat_message": response.chat_message,
        "commands": response.commands,
        "phase": response.phase.value,
        "is_complete": response.is_complete,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def structured_session_input_stream(request, session_id):
    """Process input in a structured session with streaming response."""
    from apps.tutoring.structured_engine import StructuredSessionEngine, SessionPhase
    
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    
    try:
        data = json.loads(request.body)
        user_input = data.get("input", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    if not user_input:
        return JsonResponse({"error": "Input required"}, status=400)
    
    def generate_stream():
        engine = StructuredSessionEngine(session)
        
        try:
            response = engine.process_input(user_input)
            
            # Stream the chat message in chunks (simulate streaming for now)
            # TODO: Implement true streaming in structured engine
            words = response.chat_message.split(' ')
            for i in range(0, len(words), 3):
                chunk = ' '.join(words[i:i+3]) + ' '
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"
            
            # Send commands and final state
            yield f"data: {json.dumps({'commands': response.commands, 'phase': response.phase.value, 'done': True, 'is_complete': response.is_complete})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    response = StreamingHttpResponse(
        generate_stream(),
        content_type='text/event-stream'
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


# =============================================================================
# V3 API - Clean Tutor Engine
# =============================================================================

@login_required
def tutor_interface_v3(request, lesson_id):
    """Render the clean tutoring interface."""
    institution = get_user_institution(request.user)
    if not institution:
        return render(request, 'tutoring/error.html', {"message": "No institution"})
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution,
        is_published=True
    )
    
    # Get step count for template
    total_steps = lesson.steps.count()
    
    return render(request, 'tutoring/session_clean.html', {
        "lesson": lesson,
        "total_steps": total_steps,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def start_session_v3(request, lesson_id):
    """Start or resume a tutoring session using clean engine."""
    from apps.tutoring.engine import TutorEngine, create_tutor_session
    
    institution = get_user_institution(request.user)
    if not institution:
        return JsonResponse({"error": "No institution membership"}, status=403)
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution,
        is_published=True
    )
    
    # Check for existing active session
    existing = TutorSession.objects.filter(
        student=request.user,
        lesson=lesson,
        status=TutorSession.Status.ACTIVE
    ).first()
    
    is_resume = False
    if existing:
        session = existing
        is_resume = True
    else:
        session = create_tutor_session(
            student=request.user,
            lesson=lesson,
            institution=institution,
        )
    
    # Use engine
    engine = TutorEngine(session)
    
    if is_resume:
        response = engine.resume()
    else:
        response = engine.start()
    
    # Get media for current step
    current_step = engine.current_step
    media = []
    if current_step and current_step.media:
        for img in current_step.media.get('images', []):
            if img.get('url'):
                media.append({
                    'type': 'image',
                    'url': img['url'],
                    'alt': img.get('alt', ''),
                    'caption': img.get('caption', ''),
                })
    
    return JsonResponse({
        "session_id": session.id,
        "message": response.message,
        "step_index": response.step_index,
        "total_steps": len(engine.steps),
        "phase": response.phase,
        "media": media,
        "is_question": response.is_waiting_for_answer,
        "question_type": response.step_type if response.is_waiting_for_answer else "",
        "choices": response.question.get('choices', []) if response.question else [],
        "awaiting_response": response.is_waiting_for_answer,
        "is_complete": response.is_session_complete,
        "mastery_achieved": response.mastery_achieved,
        "exit_ticket": None,
        "is_resume": is_resume,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def session_input_v3(request, session_id):
    """Handle student input in tutoring session."""
    from apps.tutoring.engine import TutorEngine
    
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    
    try:
        data = json.loads(request.body)
        user_input = data.get("input", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    if not user_input:
        return JsonResponse({"error": "Input required"}, status=400)
    
    engine = TutorEngine(session)
    response = engine.process_answer(user_input)
    
    # Get media for current step
    current_step = engine.current_step
    media = []
    if current_step and current_step.media:
        for img in current_step.media.get('images', []):
            if img.get('url'):
                media.append({
                    'type': 'image',
                    'url': img['url'],
                    'alt': img.get('alt', ''),
                    'caption': img.get('caption', ''),
                })
    
    return JsonResponse({
        "message": response.message,
        "step_index": response.step_index,
        "total_steps": len(engine.steps),
        "phase": response.phase,
        "media": media,
        "is_question": response.is_waiting_for_answer,
        "question_type": response.step_type if response.is_waiting_for_answer else "",
        "choices": response.question.get('choices', []) if response.question else [],
        "awaiting_response": response.is_waiting_for_answer,
        "is_complete": response.is_session_complete,
        "mastery_achieved": response.mastery_achieved,
        "is_correct": response.grading.result.value == 'correct' if response.grading else None,
        "feedback": response.grading.feedback if response.grading else "",
        "hint": response.hint or "",
        "exit_ticket": None,
        "attempts_remaining": response.attempts_remaining,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def session_advance_v3(request, session_id):
    """Advance to next step (for non-question steps)."""
    from apps.tutoring.engine import TutorEngine
    
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    
    engine = TutorEngine(session)
    response = engine.advance()
    
    # Get media for current step
    current_step = engine.current_step
    media = []
    if current_step and current_step.media:
        for img in current_step.media.get('images', []):
            if img.get('url'):
                media.append({
                    'type': 'image',
                    'url': img['url'],
                    'alt': img.get('alt', ''),
                    'caption': img.get('caption', ''),
                })
    
    return JsonResponse({
        "message": response.message,
        "step_index": response.step_index,
        "total_steps": len(engine.steps),
        "phase": response.phase,
        "media": media,
        "is_question": response.is_waiting_for_answer,
        "question_type": response.step_type if response.is_waiting_for_answer else "",
        "choices": response.question.get('choices', []) if response.question else [],
        "awaiting_response": response.is_waiting_for_answer,
        "is_complete": response.is_session_complete,
        "mastery_achieved": response.mastery_achieved,
        "exit_ticket": None,
    })


# =============================================================================
# CHAT-BASED CONVERSATIONAL AI TUTOR API
# =============================================================================

@login_required
def chat_tutor_interface(request, lesson_id):
    """Render the chat-based tutoring interface."""
    institution = get_user_institution(request.user)
    if not institution:
        return render(request, 'tutoring/error.html', {"message": "No institution"})
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution,
        is_published=True
    )
    
    return render(request, 'tutoring/chat_tutor.html', {
        "lesson": lesson,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def chat_start_session(request, lesson_id):
    """Start or resume a conversational tutoring session."""
    from apps.tutoring.conversational_tutor import ConversationalTutor
    
    institution = get_user_institution(request.user)
    if not institution:
        return JsonResponse({"error": "No institution membership"}, status=403)
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution,
        is_published=True
    )
    
    # Check for existing active session
    existing = TutorSession.objects.filter(
        student=request.user,
        lesson=lesson,
        status=TutorSession.Status.ACTIVE
    ).first()
    
    if existing:
        session = existing
        tutor = ConversationalTutor(session)
        response = tutor.resume()
    else:
        # Create new session
        session = TutorSession.objects.create(
            student=request.user,
            lesson=lesson,
            institution=institution,
            status=TutorSession.Status.ACTIVE,
        )
        tutor = ConversationalTutor(session)
        response = tutor.start()
    
    return JsonResponse({
        "session_id": session.id,
        "message": response.content,
        "phase": response.phase,
        "media": response.media,
        "show_exit_ticket": response.show_exit_ticket,
        "exit_ticket": response.exit_ticket_data,
        "is_complete": response.is_complete,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def chat_respond(request, session_id):
    """Handle student message in conversational tutoring."""
    from apps.tutoring.conversational_tutor import ConversationalTutor
    
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
    )
    
    # Handle completed sessions
    if session.status == TutorSession.Status.COMPLETED:
        return JsonResponse({
            "message": "🎉 This lesson is already complete! Great work!",
            "phase": "completed",
            "is_complete": True,
        })
    
    try:
        data = json.loads(request.body)
        message = data.get("message", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    if not message:
        return JsonResponse({"error": "Message required"}, status=400)
    
    tutor = ConversationalTutor(session)
    response = tutor.respond(message)
    
    return JsonResponse({
        "message": response.content,
        "phase": response.phase,
        "media": response.media,
        "show_exit_ticket": response.show_exit_ticket,
        "exit_ticket": response.exit_ticket_data,
        "is_complete": response.is_complete,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def chat_exit_ticket(request, session_id):
    """Submit exit ticket answers."""
    from apps.tutoring.conversational_tutor import ConversationalTutor
    
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
    )
    
    try:
        data = json.loads(request.body)
        answers = data.get("answers", [])
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    tutor = ConversationalTutor(session)
    response = tutor.submit_exit_ticket(answers)
    
    return JsonResponse({
        "message": response.content,
        "phase": response.phase,
        "exit_ticket": response.exit_ticket_data,
        "is_complete": response.is_complete,
    })