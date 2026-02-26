"""
Tutoring Views - Web endpoints for the chat-based conversational tutor.
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


import logging
logger = logging.getLogger(__name__)


def check_lesson_prerequisites(student, lesson):
    """
    Check if student meets prerequisites for a lesson (R7).
    Returns (met, unmet_lessons) where unmet_lessons is a list of dicts.
    Fails open -- returns (True, []) if the check itself fails.
    """
    try:
        from apps.tutoring.skills_models import LessonPrerequisite

        prerequisites = LessonPrerequisite.objects.filter(
            lesson=lesson,
            is_direct=True,
        ).select_related('prerequisite')

        if not prerequisites.exists():
            return True, []

        unmet = []
        for prereq in prerequisites:
            progress = StudentLessonProgress.objects.filter(
                student=student,
                lesson=prereq.prerequisite,
                mastery_level='mastered',
            ).first()

            if not progress:
                unmet.append({
                    'lesson_id': prereq.prerequisite.id,
                    'lesson_title': prereq.prerequisite.title,
                    'strength': prereq.strength,
                })

        return len(unmet) == 0, unmet
    except Exception as e:
        logger.warning(f"Prerequisite check failed: {e}")
        return True, []


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


# ---- Image Generation Endpoint ----

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def generate_image(request):
    """Generate an educational image using Gemini Imagen."""
    import os

    try:
        data = json.loads(request.body)
        prompt = data.get("prompt", "").strip()
        session_id = data.get("session_id")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if not prompt:
        return JsonResponse({"error": "Prompt required"}, status=400)

    # Image safety check
    from apps.safety import ImageSafetyFilter, SafetyAuditLog
    safety_result = ImageSafetyFilter.check_image_request(prompt)
    if safety_result.blocked:
        SafetyAuditLog.log(
            'image_blocked',
            user=request.user,
            details={'prompt': prompt[:200], 'reason': safety_result.block_reason},
            severity='warning',
            request=request,
        )
        return JsonResponse({"error": safety_result.block_reason}, status=400)

    google_key = os.environ.get('GOOGLE_API_KEY')
    if not google_key:
        return JsonResponse({"error": "Image generation not configured"}, status=503)

    try:
        from google import genai
        from google.genai import types
        from django.core.files.base import ContentFile
        from apps.media_library.models import MediaAsset

        client = genai.Client(api_key=google_key)

        full_prompt = f"A high-quality detailed educational illustration, clean lines, vibrant colours, suitable for a secondary school textbook. {prompt}"

        response = client.models.generate_images(
            model='imagen-4.0-generate-001',
            prompt=full_prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                safety_filter_level="BLOCK_LOW_AND_ABOVE",
                person_generation="DONT_ALLOW",
            ),
        )

        if not response.generated_images:
            return JsonResponse({"error": "Image generation was blocked by safety filters"}, status=400)

        image_bytes = response.generated_images[0].image.image_bytes

        # Save as MediaAsset
        institution = get_user_institution(request.user)
        import hashlib
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
        filename = f"generated_{prompt_hash}.png"

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
            ContentFile(image_bytes),
            save=True
        )

        return JsonResponse({
            "url": media_asset.file.url,
            "title": media_asset.title,
            "caption": media_asset.caption,
        })

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


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
    from apps.safety import RateLimiter, SafetyAuditLog

    # Rate limiting (R8)
    allowed, reason = RateLimiter.check_rate_limit(request.user.id)
    if not allowed:
        SafetyAuditLog.log(
            'rate_limited',
            user=request.user,
            details={'reason': reason, 'endpoint': 'chat_start_session'},
            severity='warning',
            request=request,
        )
        return JsonResponse({"error": reason, "rate_limited": True}, status=429)

    RateLimiter.record_message(request.user.id)

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

    # Prerequisite gating -- only for new sessions, not resume (R7)
    if not existing:
        prereqs_met, unmet_prereqs = check_lesson_prerequisites(request.user, lesson)
        if not prereqs_met:
            return JsonResponse({
                "error": "prerequisite_not_met",
                "message": "You need to complete prerequisite lessons first.",
                "unmet_prerequisites": unmet_prereqs,
            }, status=400)

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
    """Handle student message in conversational tutoring (streaming SSE)."""
    from django.http import StreamingHttpResponse
    from apps.tutoring.conversational_tutor import ConversationalTutor
    from apps.safety import (
        ContentSafetyFilter, RateLimiter, SafetyAuditLog
    )

    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
    )

    # Handle completed sessions (non-streaming)
    if session.status == TutorSession.Status.COMPLETED:
        return JsonResponse({
            "message": "This lesson is already complete! Great work!",
            "phase": "completed",
            "is_complete": True,
        })

    # Rate limiting (non-streaming)
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
        message = data.get("message", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if not message:
        return JsonResponse({"error": "Message required"}, status=400)

    # Content safety check (non-streaming)
    safety_result = ContentSafetyFilter.check_content(message, context="student_input")

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

    if safety_result.blocked:
        safe_response = ContentSafetyFilter.get_safe_response(safety_result.flags[0])
        return JsonResponse({
            "message": safe_response,
            "phase": "safety",
            "media": [],
            "show_exit_ticket": False,
            "exit_ticket": None,
            "is_complete": False,
        })

    # Use filtered content
    message = safety_result.filtered_content

    # Stream the LLM response via SSE
    tutor = ConversationalTutor(session)

    def event_stream():
        for chunk in tutor.respond_stream(message):
            yield f"data: {chunk}\n\n"

    response = StreamingHttpResponse(
        event_stream(), content_type='text/event-stream'
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


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
