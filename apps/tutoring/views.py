"""
Tutoring Views - Web endpoints for the chat-based conversational tutor.
"""

import json
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.contrib import messages as django_messages
from django.db.models import Count, Q
from django.utils import timezone

from apps.accounts.models import Institution, Membership, StudentProfile, TutorPersonality
from apps.curriculum.models import Course, Lesson, LessonStep
from apps.tutoring.models import TutorSession, SessionTurn, StudentLessonProgress


import logging
import re
logger = logging.getLogger(__name__)

# Regex to strip media signal tags from history content
_MEDIA_TAG_RE = re.compile(
    r'\[SHOW_MEDIA\s*:[^\]]*\]|\|\|\|MEDIA\s*:\s*\d+\s*\|\|\|',
    re.IGNORECASE,
)


def _build_session_history(session):
    """Build a list of {role, content, media?} dicts from SessionTurn records.

    Skips system turns and strips media signal tags from content.
    Attaches media from engine_state turn_media map for artifact panel on resume.
    """
    turns = SessionTurn.objects.filter(session=session).order_by('created_at')
    engine_state = session.engine_state or {}
    turn_media = engine_state.get('turn_media', {})

    history = []
    idx = 0
    for turn in turns:
        if turn.role == 'system':
            continue
        content = _MEDIA_TAG_RE.sub('', turn.content).strip()
        if content:
            entry = {'role': turn.role, 'content': content}
            media = turn_media.get(str(idx))
            if media:
                entry['media'] = [media]
            history.append(entry)
        idx += 1
    return history


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
    progress = StudentLessonProgress.objects.filter(student=user)
    if institution:
        progress = progress.filter(
            Q(lesson__unit__course__institution=institution) | Q(lesson__unit__course__institution__isnull=True)
        )
    # If institution is None (super admin "All Schools"), return all progress
    progress = progress.select_related('lesson')

    return {p.lesson_id: p for p in progress}


@login_required
def lesson_list(request):
    """List available lessons for the student."""
    institution = get_user_institution(request.user)
    if not institution:
        return JsonResponse({"error": "No institution membership"}, status=403)

    lessons = Lesson.objects.filter(
        is_published=True
    ).filter(
        Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True)
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
    is_staff = request.user.is_staff

    if not institution and not is_staff:
        return render(request, 'tutoring/catalog.html', {
            "subjects": [],
            "selected_subject": None,
            "active_sessions": [],
        })

    # Staff school switcher: ?school=all (default) or ?school=<id>
    all_schools = []
    selected_school = None
    viewing_institution = institution  # For regular students, always their own
    if is_staff:
        all_schools = list(
            Institution.objects.exclude(slug=Institution.GLOBAL_SLUG)
            .filter(is_active=True).order_by('name')
        )
        school_param = request.GET.get('school', 'all')
        if school_param != 'all':
            try:
                selected_school = Institution.objects.get(id=int(school_param))
                viewing_institution = selected_school
            except (Institution.DoesNotExist, ValueError):
                pass

    # Get active sessions (incomplete) for resume
    active_sessions_qs = TutorSession.objects.filter(
        student=request.user,
        status=TutorSession.Status.ACTIVE
    )
    if viewing_institution:
        active_sessions_qs = active_sessions_qs.filter(institution=viewing_institution)
    active_sessions = active_sessions_qs.select_related(
        'lesson', 'lesson__unit', 'lesson__unit__course'
    ).order_by('-started_at')[:5]

    active_sessions_data = [{
        'session_id': s.id,
        'lesson_id': s.lesson.id,
        'lesson_title': s.lesson.title,
        'course_title': s.lesson.unit.course.title,
        'started_at': s.started_at,
        'phase': (s.engine_state.get('display_phase') or s.engine_state.get('phase', 'explain')) if s.engine_state else 'explain',
        'questions_correct': s.engine_state.get('questions_correct', 0) if s.engine_state else 0,
    } for s in active_sessions]

    # Get courses — staff with no specific school sees all, otherwise scoped
    if viewing_institution:
        courses = Course.objects.filter(
            Q(institution=viewing_institution) | Q(institution__isnull=True),
            is_published=True
        )
    else:
        courses = Course.objects.filter(is_published=True)
    courses = courses.prefetch_related('units__lessons').order_by('title')

    # Grade-based filtering: students only see courses matching their grade level
    from apps.curriculum.utils import parse_grade_level_string
    student_grade = ''
    if hasattr(request.user, 'student_profile'):
        student_grade = request.user.student_profile.grade_level or ''

    if student_grade and not is_staff:
        filtered = []
        for course in courses:
            course_grades = parse_grade_level_string(course.grade_level)
            if not course_grades or student_grade in course_grades:
                filtered.append(course)
        courses = filtered

    # Get student progress
    progress_map = get_student_progress(request.user, viewing_institution)

    # Prereq data: batch-fetch all direct prerequisites (1 query)
    from apps.tutoring.skills_models import LessonPrerequisite
    all_lesson_ids = []
    for course in courses:
        for unit in course.units.all():
            for lesson in unit.lessons.filter(is_published=True):
                all_lesson_ids.append(lesson.id)

    prereqs_qs = LessonPrerequisite.objects.filter(
        lesson_id__in=all_lesson_ids, is_direct=True
    ).select_related('prerequisite')

    prereq_map = {}  # {lesson_id: [prereq_lesson, ...]}
    for p in prereqs_qs:
        prereq_map.setdefault(p.lesson_id, []).append(p.prerequisite)

    mastered_ids = {lid for lid, prog in progress_map.items() if prog.mastery_level == 'mastered'}

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
            # Skip units not matching student's grade
            if student_grade and not is_staff:
                unit_grades = parse_grade_level_string(unit.grade_level)
                if unit_grades and student_grade not in unit_grades:
                    continue
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

                # Competency indicator (C5): best_score as 0-100% for the card.
                competency_pct = None
                attempts_count = 0
                if progress and progress.best_score is not None:
                    competency_pct = round((progress.best_score or 0.0) * 100)
                    attempts_count = progress.attempts_count or 0

                # Check prerequisites
                unmet = [
                    pr for pr in prereq_map.get(lesson.id, [])
                    if pr.id not in mastered_ids
                ]

                unit_lessons.append({
                    'id': lesson.id,
                    'title': lesson.title,
                    'objective': lesson.objective,
                    'estimated_minutes': lesson.estimated_minutes,
                    'status': status,
                    'competency_pct': competency_pct,
                    'attempts_count': attempts_count,
                    'locked': len(unmet) > 0,
                    'unmet_prerequisites': [{'id': u.id, 'title': u.title} for u in unmet],
                })

            if unit_lessons:
                units_data.append({
                    'title': unit.title,
                    'description': unit.description,
                    'order_index': unit.order_index,
                    'lessons': unit_lessons,
                })

        from apps.curriculum.utils import format_grade_display
        # Show student's grade if filtering is active, otherwise full course range
        if student_grade and not is_staff:
            grade_display = student_grade
        else:
            grade_display = format_grade_display(course.grade_level)
        subject_data = {
            'id': course.id,
            'title': course.title,
            'description': course.description,
            'total_lessons': total_lessons,
            'completed_lessons': completed_lessons,
            'in_progress_lessons': in_progress_lessons,
            'progress_percent': int((completed_lessons / total_lessons * 100)) if total_lessons > 0 else 0,
            'units': units_data,
            'grade_display': grade_display,
        }

        # Only add courses that have at least 1 published lesson
        if total_lessons > 0:
            subjects.append(subject_data)

        if selected_subject_id and str(course.id) == selected_subject_id:
            selected_subject = subject_data

    # Default to first subject if none selected
    if not selected_subject and subjects:
        selected_subject = subjects[0]

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "active_sessions": active_sessions_data,
        "student_grade": student_grade,
    }
    # Staff school switcher
    if is_staff:
        context["all_schools"] = all_schools
        context["selected_school"] = selected_school
        context["is_staff_viewer"] = True

    return render(request, 'tutoring/catalog.html', context)


# ---- Image Generation Endpoint ----

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def generate_image(request):
    """Generate an educational image using Gemini native generation."""
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

    try:
        from apps.tutoring.image_service import ImageGenerationService

        institution = get_user_institution(request.user)
        service = ImageGenerationService(institution=institution)

        if not service.available:
            return JsonResponse({"error": "Image generation not configured or disabled"}, status=503)

        result = service.get_or_generate_image(
            prompt=prompt,
            category=data.get('category', 'illustration'),
        )

        if not result or not result.get('url'):
            return JsonResponse({"error": "Image generation failed"}, status=500)

        return JsonResponse({
            "url": result['url'],
            "title": result.get('title', ''),
            "caption": result.get('caption', ''),
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
    if not institution and not request.user.is_staff:
        return render(request, 'tutoring/error.html', {"message": "No institution"})

    if institution:
        lesson = get_object_or_404(
            Lesson.objects.filter(
                Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True)
            ),
            id=lesson_id,
            is_published=True
        )
    else:
        # Super admin — can access any published lesson
        lesson = get_object_or_404(Lesson, id=lesson_id, is_published=True)

    # Prerequisite check — only block if no existing session
    has_session = TutorSession.objects.filter(
        student=request.user, lesson=lesson,
        status__in=[TutorSession.Status.ACTIVE, TutorSession.Status.COMPLETED],
    ).exists()
    if not has_session:
        prereqs_met, unmet = check_lesson_prerequisites(request.user, lesson)
        if not prereqs_met:
            names = ', '.join(u['lesson_title'] for u in unmet)
            django_messages.warning(
                request,
                f"You need to complete these prerequisites first: {names}"
            )
            return redirect('tutoring:catalog')

    # Detect math lessons for KaTeX rendering
    is_math_lesson = lesson.unit.course.is_math

    return render(request, 'tutoring/chat_tutor.html', {
        "lesson": lesson,
        "is_math_lesson": is_math_lesson,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def chat_start_session(request, lesson_id):
    """Start or resume a conversational tutoring session."""
    from apps.tutoring.conversational_tutor import ConversationalTutor
    from apps.safety import RateLimiter, SafetyAuditLog

    # Check if student is suspended from tutor
    try:
        from apps.accounts.models import StudentProfile
        profile = StudentProfile.objects.filter(user=request.user).first()
        if profile and profile.is_tutor_suspended:
            return JsonResponse({
                "error": "Your tutor access has been temporarily paused. Please speak with your teacher.",
                "suspended": True,
            }, status=403)
    except Exception:
        pass

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
    if not institution and not request.user.is_staff:
        return JsonResponse({"error": "No institution membership"}, status=403)

    if institution:
        lesson = get_object_or_404(
            Lesson.objects.filter(
                Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True)
            ),
            id=lesson_id,
            is_published=True
        )
    else:
        # Super admin — can access any published lesson
        lesson = get_object_or_404(Lesson, id=lesson_id, is_published=True)

    # Check for existing active session
    existing = TutorSession.objects.filter(
        student=request.user,
        lesson=lesson,
        status=TutorSession.Status.ACTIVE
    ).first()

    # If no active session, check for a completed one (for review)
    completed_session = None
    if not existing:
        completed_session = TutorSession.objects.filter(
            student=request.user,
            lesson=lesson,
            status=TutorSession.Status.COMPLETED
        ).order_by('-ended_at').first()

    # Prerequisite gating -- only for brand-new sessions (R7)
    if not existing and not completed_session:
        prereqs_met, unmet_prereqs = check_lesson_prerequisites(request.user, lesson)
        if not prereqs_met:
            return JsonResponse({
                "error": "prerequisite_not_met",
                "message": "You need to complete prerequisite lessons first.",
                "unmet_prerequisites": unmet_prereqs,
            }, status=400)

    # Resolve institution for session creation (super admins use Global)
    session_institution = institution or lesson.unit.course.institution or Institution.get_global()

    if existing:
        # Resume active session — include conversation history
        session = existing
        history = _build_session_history(session)
        tutor = ConversationalTutor(session)
        response = tutor.resume()

        return JsonResponse({
            "session_id": session.id,
            "message": response.content,
            "phase": response.phase,
            "media": response.media,
            "show_exit_ticket": response.show_exit_ticket,
            "exit_ticket": response.exit_ticket_data,
            "is_complete": response.is_complete,
            "step_number": response.step_number,
            "total_steps": response.total_steps,
            "history": history,
        })

    elif completed_session:
        # Completed session — return history + review available
        session = completed_session
        history = _build_session_history(session)

        return JsonResponse({
            "session_id": session.id,
            "message": "You've already completed this lesson! You can review it to strengthen your understanding.",
            "phase": "completed",
            "media": [],
            "show_exit_ticket": False,
            "exit_ticket": None,
            "is_complete": True,
            "review_available": True,
            "history": history,
        })

    else:
        # Create new session
        session = TutorSession.objects.create(
            student=request.user,
            lesson=lesson,
            institution=session_institution,
            status=TutorSession.Status.ACTIVE,
        )

        # Record the primary participant (G1). Every session has at least
        # one SessionParticipant — the owner.
        from apps.tutoring.models import SessionParticipant
        SessionParticipant.objects.get_or_create(
            session=session,
            student=request.user,
            defaults={'is_primary': True, 'is_active': True},
        )

        # Optional: initial_participants in request body to start a group
        # session in one call. Each entry is {user_id} for a teacher-
        # pre-approved groupmate physically on the same device. Rejected
        # if the lesson disallows group mode, the school is in
        # 'individual' session mode, or the user isn't in the host's
        # active StudentGroup.
        try:
            body = json.loads(request.body or "{}")
        except (ValueError, TypeError):
            body = {}
        initial = body.get("initial_participants") or []
        if initial and lesson.allow_group_mode:
            for entry in initial[: max(lesson.max_group_size - 1, 0)]:
                _try_add_participant(session, entry, institution)

        # Ensure a progress record exists (won't downgrade if already mastered)
        StudentLessonProgress.objects.get_or_create(
            student=request.user,
            lesson=lesson,
            defaults={
                'institution': session_institution,
                'mastery_level': 'in_progress',
            },
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
            "step_number": response.step_number,
            "total_steps": response.total_steps,
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

    # Check if student is suspended
    try:
        from apps.accounts.models import StudentProfile
        profile = StudentProfile.objects.filter(user=request.user).first()
        if profile and profile.is_tutor_suspended:
            return JsonResponse({
                "message": "Your tutor access has been temporarily paused. Please speak with your teacher.",
                "phase": "suspended",
                "is_complete": True,
                "suspended": True,
            })
    except Exception:
        pass

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
        # Flag the session
        if not session.is_flagged:
            session.is_flagged = True
            session.flag_reason = ', '.join(f.value for f in safety_result.flags)
            session.flagged_at = timezone.now()
            session.save(update_fields=['is_flagged', 'flag_reason', 'flagged_at'])
        # Flag the student turn (find most recent student turn)
        student_turn = SessionTurn.objects.filter(
            session=session, role='student'
        ).order_by('-created_at').first()
        if student_turn:
            student_turn.is_flagged = True
            student_turn.flag_type = safety_result.flags[0].value
            student_turn.save(update_fields=['is_flagged', 'flag_type'])

    if safety_result.blocked:
        safe_response = ContentSafetyFilter.get_safe_response(safety_result.flags[0])

        # Count safety strikes in this session (flagged student turns)
        strike_count = SessionTurn.objects.filter(
            session=session, role='student', is_flagged=True
        ).count()

        if strike_count >= 2:
            # Auto-end session and suspend student
            session.status = TutorSession.Status.ABANDONED
            session.ended_at = timezone.now()
            session.save(update_fields=['status', 'ended_at'])

            try:
                from apps.accounts.models import StudentProfile
                profile, _ = StudentProfile.objects.get_or_create(user=request.user)
                if not profile.is_tutor_suspended:
                    profile.is_tutor_suspended = True
                    profile.tutor_suspended_at = timezone.now()
                    profile.tutor_suspended_reason = (
                        f"Automatic suspension after {strike_count} safety violations in session {session.id}. "
                        f"Reasons: {session.flag_reason}. "
                        f"Teacher review required before re-enabling access."
                    )
                    profile.save(update_fields=['is_tutor_suspended', 'tutor_suspended_at', 'tutor_suspended_reason'])
            except Exception as e:
                logger.warning(f"Failed to suspend student {request.user.id}: {e}")

            return JsonResponse({
                "message": "This session has been ended due to repeated safety concerns. Your teacher has been notified and will discuss this with you.",
                "phase": "suspended",
                "media": [],
                "show_exit_ticket": False,
                "exit_ticket": None,
                "is_complete": True,
                "suspended": True,
            })

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

    # Generate response (non-streaming for Azure Container Apps compatibility)
    import logging
    import time
    logger = logging.getLogger('apps')
    logger.info(f"[respond] Starting for session {session_id}, message: {message[:50]}")

    tutor = ConversationalTutor(session)

    try:
        t0 = time.time()
        result = tutor.respond(message)
        elapsed = time.time() - t0
        logger.info(f"[respond] Completed in {elapsed:.1f}s, phase={result.phase}")

        # Safety check LLM response
        ai_safety = ContentSafetyFilter.check_content(result.content, context="ai_output")
        if ai_safety.flags:
            SafetyAuditLog.log(
                'ai_content_flagged',
                user=request.user,
                session_id=session.id,
                details={
                    'flags': [f.value for f in ai_safety.flags],
                    'warnings': ai_safety.warnings,
                    'source': 'ai_output',
                },
                severity='warning',
                request=request,
            )
            if not session.is_flagged:
                session.is_flagged = True
                session.flag_reason = 'AI output: ' + ', '.join(f.value for f in ai_safety.flags)
                session.flagged_at = timezone.now()
                session.save(update_fields=['is_flagged', 'flag_reason', 'flagged_at'])
            tutor_turn = SessionTurn.objects.filter(
                session=session, role='tutor'
            ).order_by('-created_at').first()
            if tutor_turn:
                tutor_turn.is_flagged = True
                tutor_turn.flag_type = ai_safety.flags[0].value
                tutor_turn.save(update_fields=['is_flagged', 'flag_type'])

        return JsonResponse({
            "message": result.content,
            "phase": result.phase,
            "media": result.media,
            "show_exit_ticket": result.show_exit_ticket,
            "exit_ticket": result.exit_ticket_data,
            "is_complete": result.is_complete,
            "step_number": result.step_number,
            "total_steps": result.total_steps,
            # In-conversation gamification
            "is_correct": result.is_correct,
            "streak_count": result.streak_count,
            "practice_score": result.practice_score,
            "milestone": result.milestone,
            # Rich HTML artifact (rendered in sandboxed iframe)
            "artifact_html": getattr(result, 'artifact_html', None),
        })
    except Exception as e:
        logger.error(f"[respond] Failed: {e}", exc_info=True)
        return JsonResponse({"error": "Something went wrong. Please try again."}, status=500)


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def chat_start_review(request, session_id):
    """Start a review session for a completed lesson."""
    from apps.tutoring.conversational_tutor import ConversationalTutor

    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
    )

    tutor = ConversationalTutor(session)
    result = tutor.start_review()

    return JsonResponse({
        "message": result.content,
        "phase": result.phase,
        "media": result.media,
        "show_exit_ticket": False,
        "exit_ticket": None,
        "is_complete": False,
        "step_number": result.step_number,
        "total_steps": result.total_steps,
        "artifact_html": getattr(result, 'artifact_html', None),
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def chat_difficulty_signal(request, session_id):
    """Handle student difficulty signal (too easy / too hard)."""
    session = get_object_or_404(
        TutorSession,
        id=session_id,
        student=request.user,
    )

    try:
        data = json.loads(request.body)
        signal = data.get("signal", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if signal not in ("too_easy", "too_hard", "reset"):
        return JsonResponse({"error": "Invalid signal"}, status=400)

    state = session.engine_state or {}
    current_level = state.get('difficulty_level', 0)

    if signal == "too_easy":
        current_level = min(current_level + 1, 2)
    elif signal == "too_hard":
        current_level = max(current_level - 1, -2)
    else:
        current_level = 0

    state['difficulty_level'] = current_level
    session.engine_state = state
    session.save(update_fields=['engine_state'])

    return JsonResponse({
        "ok": True,
        "signal": signal,
        "difficulty_level": current_level,
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

    try:
        tutor = ConversationalTutor(session)
        response = tutor.submit_exit_ticket(answers)

        # Enrich response with competency snapshot (C3): score_pct, threshold_pct,
        # per_concept, best_score_pct, mastery_level. See
        # memory/lesson_competency_plan.md.
        from apps.tutoring.competency import attempt_response_block
        from apps.tutoring.models import ExitTicket, StudentLessonProgress
        exit_ticket = ExitTicket.objects.filter(lesson=session.lesson).first()
        progress = StudentLessonProgress.objects.filter(
            student=request.user, lesson=session.lesson,
        ).first()
        results = (response.exit_ticket_data or {}).get("results", [])
        score = (response.exit_ticket_data or {}).get("score", 0)
        competency = attempt_response_block(score, results, exit_ticket, progress)

        enriched_exit_ticket = dict(response.exit_ticket_data or {})
        enriched_exit_ticket["competency"] = competency

        return JsonResponse({
            "message": response.content,
            "phase": response.phase,
            "exit_ticket": enriched_exit_ticket,
            "is_complete": response.is_complete,
        })
    except Exception as e:
        import traceback
        print(f"[ExitTicket] VIEW CRASH: {e}", flush=True)
        traceback.print_exc()
        return JsonResponse({
            "message": "Your quiz answers were saved but scoring encountered an error. Please continue.",
            "phase": "completed",
            "exit_ticket": {"results": [], "score": 0, "passed": False},
            "is_complete": True,
        })


def _try_add_participant(session, entry: dict, primary_institution) -> dict:
    """Helper: add a teacher-pre-approved groupmate to the session.

    The session host (primary student) belongs to a teacher-formed
    StudentGroup. The only students they can add are other members of
    that same group. The school's `session_mode` must be 'shared_device'
    — 'individual' schools never allow adds. See
    `memory/pilot_launch_execution.md`.
    """
    from django.contrib.auth.models import User
    from apps.tutoring.models import SessionParticipant
    from apps.accounts.models import StudentGroup, Institution

    user_id = (entry or {}).get("user_id")
    try:
        user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    if not user_id:
        return {"ok": False, "error": "user_id_required"}

    # H2: Lock participants once the lesson has actually started (any
    # student message sent). The group must be formed BEFORE the first
    # turn — initial_participants on chat_start_session is the sanctioned
    # path; mid-lesson adds are rejected.
    state = session.engine_state or {}
    if (state.get('exchange_count', 0) or 0) > 0:
        return {"ok": False, "error": "lesson_already_started"}

    # School session-mode gate: 'individual' schools never allow add.
    inst = primary_institution
    if inst is None:
        # Fall back to host's primary institution from session.
        inst = session.institution
    if inst is not None and inst.session_mode == Institution.SessionMode.INDIVIDUAL:
        return {"ok": False, "error": "individual_mode_school"}

    user = User.objects.filter(id=user_id, is_active=True).first()
    if not user:
        return {"ok": False, "error": "user_not_found"}
    if user.id == session.student_id:
        return {"ok": False, "error": "already_primary"}

    # Same-institution gate
    if inst is not None:
        in_same_institution = user.memberships.filter(
            institution=inst, is_active=True,
        ).exists()
        if not in_same_institution:
            return {"ok": False, "error": "different_institution"}

    # Pre-approved-groupmate gate. The candidate must share an active
    # StudentGroup with the session host.
    host = session.student
    host_group = StudentGroup.get_active_group_for(host)
    if host_group is None:
        return {"ok": False, "error": "host_has_no_group"}
    if not host_group.students.filter(id=user.id).exists():
        return {"ok": False, "error": "not_in_host_group"}

    # Max group size gate
    lesson = session.lesson
    if not lesson.allow_group_mode:
        return {"ok": False, "error": "group_mode_disabled"}
    current_active = SessionParticipant.objects.filter(
        session=session, is_active=True,
    ).count()
    if current_active >= (lesson.max_group_size or 4):
        return {"ok": False, "error": "group_full"}

    # Already a participant?
    existing = SessionParticipant.objects.filter(
        session=session, student=user,
    ).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.left_at = None
            existing.save(update_fields=["is_active", "left_at"])
        return {
            "ok": True,
            "requires_approval": False,
            "participant": {
                "id": existing.id, "user_id": user.id, "username": user.username,
                "is_primary": existing.is_primary, "is_active": existing.is_active,
            },
        }

    participant = SessionParticipant.objects.create(
        session=session, student=user, is_active=True, is_primary=False,
    )
    return {
        "ok": True,
        "requires_approval": False,
        "participant": {
            "id": participant.id, "user_id": user.id, "username": user.username,
            "is_primary": False, "is_active": True,
        },
    }


@login_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
def session_participants(request, session_id):
    """List participants (GET) or add one (POST) for a session."""
    from apps.tutoring.models import SessionParticipant

    session = get_object_or_404(
        TutorSession, id=session_id, student=request.user,
    )

    if request.method == "GET":
        from apps.accounts.models import StudentGroup, Institution

        rows = SessionParticipant.objects.filter(session=session).select_related("student")
        state = session.engine_state or {}
        lesson_started = (state.get('exchange_count', 0) or 0) > 0

        active_user_ids = {p.student_id for p in rows if p.is_active}
        # School session mode (drives whether the chat UI shows the Add button)
        inst = session.institution
        session_mode = (
            inst.session_mode if inst else Institution.SessionMode.SHARED_DEVICE
        )
        # Groupmates the host can pick from (excludes the host + already-active members)
        host_group = StudentGroup.get_active_group_for(session.student)
        groupmates = []
        if host_group and session_mode == Institution.SessionMode.SHARED_DEVICE:
            for s in host_group.students.exclude(pk=session.student_id).order_by('first_name', 'last_name'):
                groupmates.append({
                    "user_id": s.id,
                    "username": s.username,
                    "display_name": (s.get_full_name() or s.username),
                    "in_session": s.id in active_user_ids,
                })

        return JsonResponse({
            "session_id": session.id,
            "is_group": session.is_group,
            "max_group_size": session.lesson.max_group_size,
            "allow_group_mode": session.lesson.allow_group_mode,
            "lesson_started": lesson_started,
            "session_mode": session_mode,
            "host_group_name": host_group.name if host_group else None,
            "groupmates": groupmates,
            "participants": [
                {
                    "id": p.id,
                    "user_id": p.student_id,
                    "username": p.student.username,
                    "is_primary": p.is_primary,
                    "is_active": p.is_active,
                    "joined_at": p.joined_at.isoformat() if p.joined_at else None,
                }
                for p in rows
            ],
        })

    # POST: add a participant
    try:
        body = json.loads(request.body or "{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    institution = get_user_institution(request.user)
    result = _try_add_participant(session, body, institution)
    if not result.get("ok"):
        status = 400
        if result.get("error") == "invalid_credentials":
            status = 401
        elif result.get("error") == "group_full":
            status = 409
        elif result.get("error") == "lesson_already_started":
            status = 409
        return JsonResponse({"error": result.get("error")}, status=status)
    return JsonResponse(result)


@login_required
@csrf_exempt
@require_http_methods(["DELETE", "POST"])
def session_participant_remove(request, session_id, user_id):
    """Mark a participant as left (is_active=False, left_at=now).

    The primary student cannot leave; they must end the session instead.
    Accessible to the primary student of the session.
    """
    from apps.tutoring.models import SessionParticipant

    session = get_object_or_404(
        TutorSession, id=session_id, student=request.user,
    )
    participant = get_object_or_404(
        SessionParticipant, session=session, student_id=user_id,
    )
    if participant.is_primary:
        return JsonResponse({"error": "cannot_remove_primary"}, status=400)
    participant.is_active = False
    participant.left_at = timezone.now()
    participant.save(update_fields=["is_active", "left_at"])
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["GET"])
def lesson_competency(request, lesson_id):
    """Return the student's competency snapshot for a single lesson.

    Used by the student progress UI and the teacher dashboard. Sourced
    entirely from ExitTicketAttempt rows (single source of truth).
    """
    from django.db.models import Q
    from apps.curriculum.models import Lesson
    from apps.tutoring.competency import competency_snapshot

    student = request.user
    # Institution scoping: only allow access to lessons the student can see.
    memberships = student.memberships.filter(is_active=True)
    allowed_institutions = [m.institution_id for m in memberships]
    lesson = get_object_or_404(
        Lesson.objects.filter(
            Q(unit__course__institution_id__in=allowed_institutions)
            | Q(unit__course__institution__isnull=True),
        ),
        id=lesson_id,
    )
    return JsonResponse(competency_snapshot(student, lesson))


# =============================================================================
# AUDIO — STT (transcribe) + TTS (speak)
# =============================================================================

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def transcribe_audio(request, session_id):
    """Transcribe uploaded audio to text via faster-whisper."""
    from apps.safety import RateLimiter

    # Validate session ownership
    session = get_object_or_404(TutorSession, id=session_id, student=request.user)

    # Rate limit
    allowed, reason = RateLimiter.check_rate_limit(request.user.id)
    if not allowed:
        return JsonResponse({"error": reason}, status=429)
    RateLimiter.record_message(request.user.id)

    audio_file = request.FILES.get("audio")
    if not audio_file:
        return JsonResponse({"error": "No audio file provided"}, status=400)

    # 10 MB max
    if audio_file.size > 10 * 1024 * 1024:
        return JsonResponse({"error": "Audio file too large (10MB max)"}, status=400)

    from apps.tutoring.audio_service import transcribe
    text = transcribe(audio_file.read(), audio_file.content_type or "audio/webm")

    if not text:
        return JsonResponse({"error": "Could not transcribe audio"}, status=422)

    return JsonResponse({"text": text})


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def speak_text(request):
    """Synthesize text to audio via configured TTS backend."""
    from apps.safety import RateLimiter

    allowed, reason = RateLimiter.check_rate_limit(request.user.id)
    if not allowed:
        return JsonResponse({"error": reason}, status=429)
    RateLimiter.record_message(request.user.id)

    try:
        data = json.loads(request.body)
        text = data.get("text", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if not text:
        return JsonResponse({"error": "Text required"}, status=400)
    if len(text) > 2000:
        return JsonResponse({"error": "Text too long (2000 char max)"}, status=400)

    # Strip emojis/icons so TTS doesn't try to read them aloud
    import re
    text = re.sub(
        r'[\U0001F600-\U0001F64F'   # emoticons
        r'\U0001F300-\U0001F5FF'     # symbols & pictographs
        r'\U0001F680-\U0001F6FF'     # transport & map
        r'\U0001F1E0-\U0001F1FF'     # flags
        r'\U0001F900-\U0001F9FF'     # supplemental symbols
        r'\U0001FA00-\U0001FA6F'     # chess symbols
        r'\U0001FA70-\U0001FAFF'     # symbols extended-A
        r'\U00002702-\U000027B0'     # dingbats
        r'\U0000FE00-\U0000FE0F'     # variation selectors
        r'\U0000200D'                # zero-width joiner
        r'\U000023E9-\U000023F3'     # misc symbols
        r'\U00002600-\U000026FF'     # misc symbols
        r'\U00002700-\U000027BF'     # dingbats
        r']+', '', text
    ).strip()

    if not text:
        return JsonResponse({"error": "Text required"}, status=400)

    # Try timestamp-enriched synthesis first (ElevenLabs only)
    from apps.tutoring.audio_service import synthesize, synthesize_with_timestamps
    ts_result = synthesize_with_timestamps(text)
    if ts_result:
        return JsonResponse({
            'audio_base64': ts_result['audio_base64'],
            'content_type': ts_result['content_type'],
            'word_timings': ts_result['word_timings'],
        })

    # Fallback: raw audio bytes (Piper or ElevenLabs failure)
    from django.http import HttpResponse
    audio_bytes, content_type = synthesize(text)

    if not audio_bytes:
        return JsonResponse({"error": "TTS unavailable"}, status=503)

    return HttpResponse(audio_bytes, content_type=content_type)


# =============================================================================
# PERSONALITY SELECTION
# =============================================================================

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def set_personality(request):
    """Set or clear the student's tutor personality preference."""
    try:
        data = json.loads(request.body)
        personality_id = data.get('personality_id')
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    profile, _ = StudentProfile.objects.get_or_create(user=request.user)

    if personality_id:
        personality = TutorPersonality.objects.filter(id=personality_id, is_active=True).first()
        if not personality:
            return JsonResponse({"error": "Personality not found"}, status=404)
        profile.tutor_personality = personality
    else:
        profile.tutor_personality = None
    profile.save(update_fields=['tutor_personality'])

    return JsonResponse({
        "selected": {
            "id": profile.tutor_personality.id,
            "name": profile.tutor_personality.name,
            "emoji": profile.tutor_personality.emoji,
        } if profile.tutor_personality else None
    })


# =============================================================================
# GAMIFICATION DATA
# =============================================================================

@login_required
@require_http_methods(["GET"])
def get_gamification_data(request):
    """Return XP, level, streak, achievements, and personality data for the current student."""
    from apps.tutoring.skills_models import StudentKnowledgeProfile, StudentAchievement, Achievement

    # Aggregate XP across all courses
    profiles = StudentKnowledgeProfile.objects.filter(student=request.user)
    total_xp = sum(p.total_xp for p in profiles)
    level = (total_xp // 1000) + 1
    xp_in_level = total_xp % 1000
    max_streak = max((p.current_streak_days for p in profiles), default=0)

    # Unseen achievements — mark them seen
    new_achievements = []
    unseen = StudentAchievement.objects.filter(
        student=request.user, is_seen=False
    ).select_related('achievement')
    for sa in unseen:
        new_achievements.append({
            'name': sa.achievement.name,
            'emoji': sa.achievement.emoji,
            'description': sa.achievement.description,
        })
    unseen.update(is_seen=True)

    # All earned achievements
    all_earned = StudentAchievement.objects.filter(
        student=request.user
    ).select_related('achievement').order_by('-earned_at')
    earned_list = [{
        'code': sa.achievement.code,
        'name': sa.achievement.name,
        'emoji': sa.achievement.emoji,
        'description': sa.achievement.description,
        'earned_at': sa.earned_at.isoformat(),
    } for sa in all_earned]

    # All active achievements (for trophy case with progress hints)
    all_achievements_qs = Achievement.objects.filter(is_active=True).order_by('sort_order', 'name')
    earned_codes = {sa.achievement.code for sa in all_earned}
    mastered_lessons_count = StudentLessonProgress.objects.filter(
        student=request.user, mastery_level='mastered'
    ).count()

    all_achievements_list = []
    for ach in all_achievements_qs:
        entry = {
            'code': ach.code,
            'name': ach.name,
            'emoji': ach.emoji,
            'description': ach.description,
            'category': ach.category,
            'trigger_type': ach.trigger_type,
            'trigger_value': ach.trigger_value,
            'earned': ach.code in earned_codes,
        }
        # Add current progress for unearned achievements
        if not entry['earned']:
            if ach.trigger_type == 'lessons_completed':
                entry['current'] = mastered_lessons_count
            elif ach.trigger_type == 'streak_days':
                entry['current'] = max_streak
            elif ach.trigger_type == 'xp_threshold':
                entry['current'] = total_xp
            elif ach.trigger_type == 'level_reached':
                entry['current'] = level
        all_achievements_list.append(entry)

    # Active personalities + student's current pick
    personalities = list(
        TutorPersonality.objects.filter(is_active=True).values('id', 'name', 'emoji', 'description')
    )
    selected_personality = None
    profile = StudentProfile.objects.select_related('tutor_personality').filter(user=request.user).first()
    if profile and profile.tutor_personality:
        selected_personality = {
            'id': profile.tutor_personality.id,
            'name': profile.tutor_personality.name,
            'emoji': profile.tutor_personality.emoji,
        }

    # Analytics: practice time, sessions, quiz accuracy.
    # Practice time is computed from actual session timestamps rather than
    # the dead total_practice_time_minutes field (never updated). Uses the
    # same active-engagement clip as the live monitor so multi-day idle
    # sessions don't inflate the total.
    sessions_for_user = TutorSession.objects.filter(student=request.user)
    IDLE_CAP_SECONDS = 5 * 60
    total_practice_minutes = 0.0
    for s in sessions_for_user:
        if s.started_lesson_at and s.completed_lesson_at:
            delta = (s.completed_lesson_at - s.started_lesson_at).total_seconds()
            total_practice_minutes += min(delta, IDLE_CAP_SECONDS * 100) / 60
        elif s.started_lesson_at and s.ended_at:
            delta = (s.ended_at - s.started_lesson_at).total_seconds()
            total_practice_minutes += min(delta, IDLE_CAP_SECONDS * 100) / 60
    total_practice_minutes = round(total_practice_minutes, 1)
    total_sessions = sum(p.total_sessions for p in profiles)

    # Quiz accuracy: average best_score across completed lessons.
    # best_score is stored as a 0.0-1.0 fraction (since C1 of the
    # competency plan); multiply by 100 for percentage display.
    completed_progress = StudentLessonProgress.objects.filter(
        student=request.user, mastery_level='mastered'
    )
    scores = [
        p.best_score for p in completed_progress
        if p.best_score is not None
    ]
    quiz_accuracy = round(sum(scores) / len(scores) * 100) if scores else None

    return JsonResponse({
        'total_xp': total_xp,
        'level': level,
        'xp_in_level': xp_in_level,
        'xp_to_next_level': 1000,
        'streak_days': max_streak,
        'new_achievements': new_achievements,
        'achievements': earned_list,
        'all_achievements': all_achievements_list,
        'mastered_lessons_count': mastered_lessons_count,
        'total_practice_minutes': total_practice_minutes,
        'total_sessions': total_sessions,
        'quiz_accuracy': quiz_accuracy,
        'personalities': personalities,
        'selected_personality': selected_personality,
    })


@login_required
@require_http_methods(["GET"])
def leaderboard(request):
    """Return top 50 students by XP within the user's institution, anonymized."""
    from apps.tutoring.skills_models import StudentKnowledgeProfile
    from django.db.models import Sum
    from django.contrib.auth.models import User

    # Get the student's institution(s)
    memberships = Membership.objects.filter(user=request.user, role='student')
    institution_ids = list(memberships.values_list('institution_id', flat=True))

    if institution_ids:
        student_ids = Membership.objects.filter(
            institution_id__in=institution_ids, role='student'
        ).values_list('user_id', flat=True)
    else:
        student_ids = [request.user.id]

    # Aggregate XP per student
    rankings = (
        StudentKnowledgeProfile.objects
        .filter(student_id__in=student_ids)
        .values('student_id')
        .annotate(xp=Sum('total_xp'))
        .order_by('-xp')[:50]
    )

    # Fetch user names for anonymization
    user_ids = [r['student_id'] for r in rankings]
    users = {u.id: u for u in User.objects.filter(id__in=user_ids)}

    # Get per-student max streak
    from django.db.models import Max
    streak_map = dict(
        StudentKnowledgeProfile.objects
        .filter(student_id__in=user_ids)
        .values('student_id')
        .annotate(max_streak=Max('current_streak_days'))
        .values_list('student_id', 'max_streak')
    )

    entries = []
    for rank, r in enumerate(rankings, 1):
        user = users.get(r['student_id'])
        if user:
            last_initial = f" {user.last_name[0]}." if user.last_name else ""
            name = f"{user.first_name}{last_initial}" if user.first_name else user.username
        else:
            name = "Unknown"
        xp = r['xp'] or 0
        entries.append({
            'rank': rank,
            'name': name,
            'xp': xp,
            'level': (xp // 1000) + 1,
            'streak': streak_map.get(r['student_id'], 0) or 0,
            'is_you': r['student_id'] == request.user.id,
        })

    return JsonResponse({'leaderboard': entries})
