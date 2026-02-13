"""
Tutoring Views - Web endpoints for the tutoring interface.

For now, we'll use simple Django views with JSON responses.
This can later be upgraded to DRF or a websocket-based approach.
"""

import json
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson
from apps.tutoring.models import TutorSession, StudentLessonProgress
from apps.tutoring.engine import TutorEngine, create_tutor_session


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
    
    if existing:
        # Resume existing session
        session = existing
    else:
        # Create new session
        session = create_tutor_session(
            student=request.user,
            lesson=lesson,
            institution=institution,
        )
    
    # Start the engine
    engine = TutorEngine(session)
    
    # If new session, get the opening message
    if not existing:
        response = engine.start()
    else:
        # For existing session, get current state
        response = engine.start()  # TODO: Resume properly
    
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
    })


@login_required
@csrf_exempt  # For simplicity; use proper CSRF in production
@require_http_methods(["POST"])
def submit_answer(request, session_id):
    """Submit a student answer."""
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    
    try:
        data = json.loads(request.body)
        answer = data.get("answer", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    if not answer:
        return JsonResponse({"error": "Answer required"}, status=400)
    
    engine = TutorEngine(session)
    response = engine.process_student_answer(answer)
    
    return JsonResponse({
        "tutor_message": response.message,
        "step_index": response.step_index,
        "step_type": response.step_type,
        "is_waiting_for_answer": response.is_waiting_for_answer,
        "is_session_complete": response.is_session_complete,
        "mastery_achieved": response.mastery_achieved,
        "grading": {
            "result": response.grading.result.value,
            "feedback": response.grading.feedback,
            "score": response.grading.score,
        } if response.grading else None,
        "attempts_remaining": response.attempts_remaining,
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
    response = engine.advance_step()
    
    return JsonResponse({
        "tutor_message": response.message,
        "step_index": response.step_index,
        "step_type": response.step_type,
        "is_waiting_for_answer": response.is_waiting_for_answer,
        "is_session_complete": response.is_session_complete,
        "mastery_achieved": response.mastery_achieved,
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
        })
    
    institution = get_user_institution(request.user)
    if not institution:
        return render(request, 'tutoring/catalog.html', {
            "subjects": [],
            "selected_subject": None,
        })
    
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
        for unit in course.units.all():
            unit_lessons = []
            for lesson in unit.lessons.filter(is_published=True):
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
        subjects.append(subject_data)
        
        if selected_subject_id and str(course.id) == selected_subject_id:
            selected_subject = subject_data
    
    # Default to first subject if none selected
    if not selected_subject and subjects:
        selected_subject = subjects[0]
    
    return render(request, 'tutoring/catalog.html', {
        "subjects": subjects,
        "selected_subject": selected_subject,
    })
