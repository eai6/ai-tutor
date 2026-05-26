"""
Tutoring Views - Web endpoints for the chat-based conversational tutor.
"""

import json
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
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

    Media resolution prefers the per-turn `attached_media` field on
    SessionTurn.metadata (the authoritative source set by
    ConversationalTutor since 2026-05-08). Falls back to the legacy
    `engine_state.turn_media` map indexed by visible-turn position
    so older sessions still restore their figures on reload.
    """
    turns = SessionTurn.objects.filter(session=session).order_by('created_at')
    engine_state = session.engine_state or {}
    turn_media = engine_state.get('turn_media', {})

    history = []
    idx = 0
    for turn in turns:
        if turn.role == 'system':
            continue
        # R1 (2026-05-15): synthetic student turns (e.g. injected by
        # the difficulty button) are persisted for tutor context +
        # analytics but should NOT re-render as chat bubbles on
        # session resume — the click was the visual signal, not the
        # text. Skip them here so the chat UI sees only real student
        # messages.
        meta = turn.metadata or {}
        if turn.role == 'student' and meta.get('synthetic_source'):
            idx += 1
            continue
        content = _MEDIA_TAG_RE.sub('', turn.content).strip()
        if content:
            entry = {'role': turn.role, 'content': content}
            # Prefer per-turn metadata media (authoritative since
            # 2026-05-08); fall back to engine_state.turn_media for
            # sessions that started before that change.
            md_media = meta.get('attached_media') or []
            if md_media:
                entry['media'] = list(md_media)
            else:
                legacy = turn_media.get(str(idx))
                if legacy:
                    entry['media'] = [legacy]
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
        # Teacher-controlled per-course override (Course.prerequisites_enabled).
        # When the course disables gating, every lesson is unlocked
        # regardless of LessonPrerequisite rows. Used for review courses,
        # exam-prep collections, demo content, etc.
        try:
            if lesson.unit and lesson.unit.course and not lesson.unit.course.prerequisites_enabled:
                return True, []
        except Exception:
            pass

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

                # Check prerequisites — short-circuit when the course
                # has prerequisites_enabled=False (teacher opt-out).
                if not course.prerequisites_enabled:
                    unmet = []
                else:
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

        # Baseline-summative gate signals for the UI: locked = hide
        # lesson list + show "Take baseline" CTA.
        from apps.tutoring.competency_tracker import baseline_required_for, student_skills_snapshot
        baseline_summative = baseline_required_for(request.user, course) if request.user.is_authenticated else None

        # Targeted recommendations: once the baseline is done, mark the
        # lesson covering the student's weakest objective as the
        # "recommended next" lesson. Boosts time-on-weak-skill.
        recommended_lesson_id = None
        recommended_reason = ''
        if request.user.is_authenticated and not baseline_summative:
            try:
                # Prefer the denormalized snapshot on StudentProfile
                # (cheap read). Fall back to live aggregation if it's
                # empty for this course (no attempts yet OR pre-rollout
                # data without a populated snapshot).
                profile = StudentProfile.objects.filter(user=request.user).first()
                snapshot = (profile.skills_snapshot or {}).get(str(course.id)) if profile else None
                if not snapshot:
                    snapshot = student_skills_snapshot(request.user, course)
                if snapshot:
                    norm = lambda s: ' '.join((s or '').split()).strip()
                    # Walk lessons in this course; for each, check the
                    # student's worst objective. Pick the lesson whose
                    # worst-objective pct is the lowest (and not yet
                    # mastered in StudentLessonProgress).
                    candidates = []
                    from apps.curriculum.content_generator import combined_objectives_for_lesson
                    for unit in course.units.prefetch_related('lessons').order_by('order_index'):
                        for lesson in unit.lessons.order_by('order_index'):
                            if not lesson.is_published:
                                continue
                            prog = progress_map.get(lesson.id)
                            if prog and prog.mastery_level == 'mastered':
                                continue
                            objs = combined_objectives_for_lesson(lesson)
                            worst_pct = None
                            worst_obj = None
                            for obj in objs:
                                info = snapshot.get(norm(obj))
                                if info and (worst_pct is None or info['pct'] < worst_pct):
                                    worst_pct = info['pct']
                                    worst_obj = obj
                            if worst_pct is not None:
                                candidates.append((worst_pct, lesson.id, worst_obj))
                    if candidates:
                        candidates.sort()  # lowest pct first
                        worst_pct, recommended_lesson_id, worst_obj = candidates[0]
                        recommended_reason = (
                            f"You're at {worst_pct:.0f}% on \"{worst_obj}\" — let's drill it."
                        )
            except Exception as e:
                logger.debug(f"Recommendation computation failed: {e}")

        # Tag the recommended lesson in units_data so the template can highlight it.
        if recommended_lesson_id:
            for unit in units_data:
                for lesson in unit['lessons']:
                    if lesson['id'] == recommended_lesson_id:
                        lesson['recommended'] = True
                        lesson['recommended_reason'] = recommended_reason
                        break

        # Summative status for the catalog "📝 Course exam" link.
        # Shown once baseline_required is False (i.e. student has at
        # least the baseline attempt) so retakes are discoverable.
        summative_attempts_count = 0
        summative_published = False
        if request.user.is_authenticated:
            try:
                from apps.tutoring.models import ExitTicket as _ET, ExitTicketAttempt as _ETA
                _sum = _ET.objects.filter(
                    course=course,
                    assessment_type=_ET.AssessmentType.SUMMATIVE,
                    is_published=True,
                ).first()
                summative_published = bool(_sum)
                if _sum:
                    summative_attempts_count = _ETA.objects.filter(
                        exit_ticket=_sum,
                        student=request.user,
                        completed_at__isnull=False,
                    ).count()
            except Exception:
                pass

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
            'baseline_required': bool(baseline_summative),
            'baseline_url': f"/tutor/summative/{course.id}/" if baseline_summative else None,
            'summative_published': summative_published,
            'summative_attempts_count': summative_attempts_count,
            'summative_url': f"/tutor/summative/{course.id}/" if summative_published else None,
            'recommended_lesson_id': recommended_lesson_id,
            'recommended_reason': recommended_reason,
        }

        # Only add courses that have at least 1 published lesson
        if total_lessons > 0:
            subjects.append(subject_data)

        if selected_subject_id and str(course.id) == selected_subject_id:
            selected_subject = subject_data

    # Default to first subject if none selected
    if not selected_subject and subjects:
        selected_subject = subjects[0]

    # This week's assignments — surfaces teacher-assigned lessons at the
    # top of the catalog so students see exactly what they need to do.
    weekly_assignments_this_week = []
    try:
        from apps.dashboard.models import WeeklyAssignment
        from apps.tutoring.models import StudentLessonProgress
        wa_qs = WeeklyAssignment.for_student_this_week(request.user)
        # Pre-compute completion state per lesson for the badge display.
        lesson_ids = [l.id for wa in wa_qs for l in wa.lessons.all()]
        progress_map = {
            p.lesson_id: p.mastery_level
            for p in StudentLessonProgress.objects.filter(
                student=request.user, lesson_id__in=lesson_ids,
            )
        }
        for wa in wa_qs:
            lessons_payload = []
            for lesson in wa.lessons.all():
                lessons_payload.append({
                    'id': lesson.id,
                    'title': lesson.title,
                    'mastery_level': progress_map.get(lesson.id, 'not_started'),
                })
            weekly_assignments_this_week.append({
                'course': wa.course,
                'week_start': wa.week_start,
                'week_end': wa.week_end,
                'notes': wa.notes,
                'lessons': lessons_payload,
            })
    except Exception:
        # Never fail the catalog render on assignment lookup error.
        pass

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "active_sessions": active_sessions_data,
        "student_grade": student_grade,
        "weekly_assignments": weekly_assignments_this_week,
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

    # Baseline is now a SOFT recommendation, not a gate. Students can
    # start any lesson immediately so teachers can demo without forcing
    # the pilot through the summative first. The persistent
    # baseline-recommend banner (templates/_includes/_baseline_recommend_banner.html,
    # included from base.html) keeps nudging the student to take it.
    # See memory/feedback_dev_collaboration.md for the pilot decision.

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

    # Whether to render the duration picker — controlled by the
    # teacher via Course.allow_student_duration_override. When False,
    # the chat page hides the picker entirely and the session uses the
    # teacher-configured lesson.estimated_minutes.
    #
    # Also: skip the picker when the student already has an active
    # OR completed session for this lesson (i.e. they're reloading
    # mid-session, not starting fresh). Otherwise reloading would
    # block the chat-history restore behind another duration click.
    allow_duration_picker = (
        lesson.unit.course.allow_student_duration_override
        if lesson.unit and lesson.unit.course else True
    )
    if has_session:
        allow_duration_picker = False

    return render(request, 'tutoring/chat_tutor.html', {
        "lesson": lesson,
        "is_math_lesson": is_math_lesson,
        "allow_duration_picker": allow_duration_picker,
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

    # Baseline is a SOFT recommendation, not a gate. Lesson sessions
    # start immediately; the persistent banner keeps nudging the
    # student to take their baseline. See chat_tutor_interface.

    # Resolve institution for session creation (super admins use Global)
    session_institution = institution or lesson.unit.course.institution or Institution.get_global()

    if existing:
        # Resume active session — include conversation history
        session = existing
        # v2 dispatch (sticky-per-session). Phase 1 only ensures the
        # engine_version is read; routing back to legacy is the
        # default for sessions started before the new engine landed.
        from apps.tutoring.v2.routing import (
            ensure_engine_version_set,
            is_v2_session,
            v2_placeholder_response,
        )
        ensure_engine_version_set(session)
        if is_v2_session(session):
            payload = v2_placeholder_response(session, kind="resume")
            payload["history"] = _build_session_history(session)
            return JsonResponse(payload)
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
            # R5 (2026-05-15): if a bank question was awaiting an
            # answer when the student left, the artifact panel
            # re-renders it on load so they don't lose their place.
            # The pending_question is computed deterministically from
            # engine_state.awaiting_answer (set by R2's
            # _record_bank_question_on_turn) — survives session
            # reloads since engine_state is persisted.
            "pending_question": getattr(response, 'pending_question', None)
                or tutor._build_pending_question_payload(),
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

        # Optional per-session duration override — student-picked
        # "I have N minutes today" from the chat-page picker. The tutor
        # engine reads engine_state['target_minutes_override'] in
        # _target_minutes_for_session and uses it to select the right
        # subset of the max-depth step bundle. No LLM regen needed.
        #
        # Course-level policy: if Course.allow_student_duration_override
        # is False, the picker is hidden client-side AND we ignore any
        # `target_minutes` sent on the request (defense in depth — a
        # crafted POST shouldn't bypass the teacher's lockdown).
        course_allows_pick = (
            lesson.unit.course.allow_student_duration_override
            if lesson.unit and lesson.unit.course else True
        )
        if course_allows_pick:
            try:
                _body = json.loads(request.body or "{}")
            except (ValueError, TypeError):
                _body = {}
            try:
                _override = int(_body.get('target_minutes') or 0)
            except (TypeError, ValueError):
                _override = 0
            if 5 <= _override <= 120:
                engine_state = session.engine_state or {}
                engine_state['target_minutes_override'] = _override
                session.engine_state = engine_state
                session.save(update_fields=['engine_state'])

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

        # v2 dispatch — set the sticky engine_version field for this
        # brand-new session. NEW_TUTOR=off (default) routes to legacy;
        # NEW_TUTOR=on routes to v2 and initializes runtime_state.
        from apps.tutoring.v2.routing import (
            ensure_engine_version_set,
            is_v2_session,
            v2_placeholder_response,
        )
        ensure_engine_version_set(session)
        if is_v2_session(session):
            return JsonResponse(v2_placeholder_response(session, kind="start"))

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
            # Same artifact-panel payload the resume path returns
            # (line 745). Without this, the opener's pose_question
            # tool call sets awaiting_answer in engine_state but the
            # frontend never knows there's a pending question to
            # render — student sees only the tutor's lead-in and an
            # empty artifact panel. Pilot e2e 2026-05-16.
            "pending_question": getattr(response, 'pending_question', None)
                or tutor._build_pending_question_payload(),
        })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def chat_restart_session(request, lesson_id):
    """Archive the student's current session for this lesson and start fresh.

    Marks ANY of the student's existing sessions for this lesson
    (Active or Completed) as ABANDONED-on-restart so the next call
    to chat_start_session creates a brand-new session.

    What's PRESERVED across restart (the user has been emphatic):
      - StudentLessonProgress (mastery_level + best_score, monotonic)
      - StudentCompetencyRecord (permanent transcript)
      - StudentSkillMastery (skill graph)
      - All ExitTicketAttempt rows (every purpose)
      - The old TutorSession row + its SessionTurn rows (historical)

    What gets reset:
      - A NEW TutorSession is created on the next chat_start_session
        call (engine_state={}, current_step_index=0).
    """
    institution = get_user_institution(request.user)
    if institution:
        lesson = get_object_or_404(
            Lesson.objects.filter(
                Q(unit__course__institution=institution)
                | Q(unit__course__institution__isnull=True)
            ),
            id=lesson_id,
            is_published=True,
        )
    else:
        lesson = get_object_or_404(Lesson, id=lesson_id, is_published=True)

    now = timezone.now()
    archived = 0
    qs = TutorSession.objects.filter(
        student=request.user,
        lesson=lesson,
        status__in=[
            TutorSession.Status.ACTIVE,
            TutorSession.Status.COMPLETED,
        ],
    )
    for sess in qs:
        sess.status = TutorSession.Status.ABANDONED
        if sess.ended_at is None:
            sess.ended_at = now
        sess.save(update_fields=['status', 'ended_at'])
        archived += 1

    return JsonResponse({
        "restarted": True,
        "lesson_id": lesson.id,
        "sessions_archived": archived,
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

    # v2 dispatch (sticky-per-session). Phase 1: route v2 sessions
    # to the placeholder; Phase 2 wires TutorEngine.respond() here.
    from apps.tutoring.v2.routing import (
        is_v2_session,
        v2_placeholder_response,
    )
    if is_v2_session(session):
        return JsonResponse(v2_placeholder_response(session, kind="respond"))

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

    # Content safety check — LLM-based safety judge (apps/tutoring/judges/safety.py).
    # Replaces the previous regex-only ContentSafetyFilter scan: an LLM
    # catches paraphrased / subtle violations the regex couldn't see.
    # On flag: write SafetyAuditLog + set SessionTurn.is_flagged so
    # the message surfaces at /dashboard/flagged/ for teacher review.
    # `critical` severity (HARMFUL) BLOCKS the request and sends a
    # stock safety reply — same blocking behaviour as before. `warning`
    # severity (INAPPROPRIATE / MANIPULATION) flags but does not block.
    from apps.tutoring.judges.safety import run_safety_judge
    from apps.llm.models import ModelConfig as _MC
    from apps.llm.client import get_llm_client as _get_client

    _safety_cfg = (
        _MC.objects.filter(purpose='judge', is_active=True).first()
        or _MC.objects.filter(purpose='tutoring', is_active=True).first()
    )
    _safety_client = None
    if _safety_cfg is not None:
        try:
            _safety_client = _get_client(_safety_cfg)
        except Exception as _e:
            logger.warning("[Safety] could not load judge client: %s", _e)

    safety_result = run_safety_judge(
        message, role="student", llm_client=_safety_client,
    )
    safety_blocked = (safety_result.severity == "critical")

    if safety_result.severity != "safe" and safety_result.categories:
        SafetyAuditLog.log(
            'content_flagged',
            user=request.user,
            session_id=session.id,
            details={
                'flags': list(safety_result.categories),
                'severity': safety_result.severity,
                'reasoning': safety_result.reasoning,
                'source': 'student_input_llm',
            },
            severity='critical' if safety_blocked else 'warning',
            request=request,
        )
        # Flag the session
        if not session.is_flagged:
            session.is_flagged = True
            session.flag_reason = ', '.join(safety_result.categories)
            session.flagged_at = timezone.now()
            session.save(update_fields=['is_flagged', 'flag_reason', 'flagged_at'])
        # Flag the student turn (find most recent student turn)
        student_turn = SessionTurn.objects.filter(
            session=session, role='student'
        ).order_by('-created_at').first()
        if student_turn:
            student_turn.is_flagged = True
            student_turn.flag_type = safety_result.categories[0]
            student_turn.save(update_fields=['is_flagged', 'flag_type'])

    if safety_blocked:
        # Stock age-appropriate safety reply. The previous code path
        # used ContentSafetyFilter.get_safe_response with regex flag
        # categories; we keep that lookup as a fallback by category
        # name when present, else default to a neutral message.
        from apps.safety import ContentFlag
        try:
            flag_enum = ContentFlag(safety_result.categories[0])
            safe_response = ContentSafetyFilter.get_safe_response(flag_enum)
        except (ValueError, IndexError):
            safe_response = (
                "I'm here to help you learn. Let's stay focused on the "
                "lesson — what part of this topic would you like to "
                "work through together?"
            )

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

    # PII redaction stays as a separate deterministic regex pass —
    # the LLM safety judge focuses on harmful/inappropriate/manipulation
    # content, not personal-info redaction. We pipe the message
    # through the legacy ContentSafetyFilter ONLY for the redaction
    # side-effect; flag handling above already came from the LLM judge.
    _pii_pass = ContentSafetyFilter.check_content(message, context="student_input")
    if _pii_pass.filtered_content and _pii_pass.filtered_content != message:
        message = _pii_pass.filtered_content

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

        # NOTE: post-response AI safety is now handled INSIDE
        # ConversationalTutor.respond() via the safety judge in
        # run_all_judges. When the judge flags a response, the
        # validator raises ISSUE_TUTOR_UNSAFE → triggers the regen
        # ensemble → the unsafe text is rewritten before reaching
        # the student. Per Edward (2026-05-07): we don't need to
        # flag tutor output for teacher review since unsafe text
        # never reaches the student; only student input is flagged
        # for /dashboard/flagged/.

        # 2026-05-17 (task #182): optional follow-up TutorMessage —
        # emitted as a second tutor bubble when the move-on flow splits
        # acknowledgement from new-question pose. Frontend renders
        # follow_up_message after the main content.
        _follow_up = getattr(result, 'follow_up', None)
        _follow_up_payload = None
        if _follow_up is not None:
            _follow_up_payload = {
                "message": _follow_up.content,
                "media": _follow_up.media,
                "pending_question": getattr(_follow_up, 'pending_question', None),
                "is_correct": _follow_up.is_correct,
            }

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
            # Easy-mode interactive probe (MCQ / fill-in-blank widget).
            "probe": getattr(result, 'probe', None),
            # R2 (2026-05-15): bank question awaiting answer — the
            # frontend artifact panel (R3) renders this as a question
            # widget instead of relying on inline prose. None when no
            # question is in flight.
            "pending_question": getattr(result, 'pending_question', None),
            # 2026-05-17 (task #182): second bubble for move-on split.
            "follow_up_message": _follow_up_payload,
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

    # v2 dispatch (sticky-per-session). Phase 1: route v2 sessions
    # to the placeholder.
    from apps.tutoring.v2.routing import (
        is_v2_session,
        v2_placeholder_response,
    )
    if is_v2_session(session):
        payload = v2_placeholder_response(session, kind="review")
        payload["artifact_html"] = None
        return JsonResponse(payload)

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
    """Handle student difficulty signal (too easy / too hard / reset).

    R1 (2026-05-15): in addition to bumping engine_state.difficulty_level,
    we INJECT a synthetic student turn so the tutor responds immediately
    instead of waiting for the next real student message. The synthetic
    turn is marked metadata.synthetic_source='difficulty_button' so the
    chat UI can suppress re-displaying its literal text — the button
    click is the visual signal.
    """
    from apps.tutoring.conversational_tutor import ConversationalTutor

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
    else:  # reset
        current_level = 0

    state['difficulty_level'] = current_level
    session.engine_state = state
    session.save(update_fields=['engine_state'])

    # Synthetic student message → drive an immediate tutor response.
    # Phrasing matches how a real student might say it; the LLM handles
    # paraphrases robustly and the difficulty context block will already
    # be loaded with the new level.
    synthetic_text = {
        "too_easy": "This is too easy — could you make it more challenging?",
        "too_hard": "This is too hard for me — could you go simpler?",
        "reset": "Let's go back to a normal pace.",
    }[signal]

    tutor_message = None
    try:
        tutor = ConversationalTutor(session)
        result = tutor.respond(
            synthetic_text,
            student_metadata={
                'synthetic_source': 'difficulty_button',
                'difficulty_signal': signal,
                'difficulty_level_after': current_level,
            },
        )
        tutor_message = {
            "message": result.content,
            "phase": result.phase,
            "media": result.media,
            "show_exit_ticket": result.show_exit_ticket,
            "exit_ticket": result.exit_ticket_data,
            "is_complete": result.is_complete,
            "step_number": result.step_number,
            "total_steps": result.total_steps,
            "is_correct": result.is_correct,
            "streak_count": result.streak_count,
            "practice_score": result.practice_score,
            "milestone": result.milestone,
            "artifact_html": getattr(result, 'artifact_html', None),
            "probe": getattr(result, 'probe', None),
            "pending_question": getattr(result, 'pending_question', None),
        }
    except Exception as exc:
        # Fail-soft: even if the synthetic-turn generation fails, the
        # difficulty level update succeeded. Frontend will fall back to
        # the legacy "wait for next student message" path.
        import logging as _lg
        _lg.getLogger('apps').warning(
            f"[chat_difficulty_signal] synthetic respond failed: {exc}",
            exc_info=True,
        )

    return JsonResponse({
        "ok": True,
        "signal": signal,
        "difficulty_level": current_level,
        "tutor_message": tutor_message,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def chat_answer_bank_question(request, session_id):
    """R4 (2026-05-15): the artifact-panel Submit button posts here
    instead of routing the picked answer through the legacy
    chat_respond text path. Eliminates MCQ false-rejects + over-eager
    show-working class entirely:

      1. Look up the bank entry by question_id + kind from the
         pending_question metadata the artifact UI received.
      2. Grade the answer DETERMINISTICALLY via bank_grader
         (same path the post-lesson exit-ticket uses today —
         already proven in production).
      3. Persist the answer + verdict on a new SessionTurn marked
         metadata.synthetic_source='bank_answer' so the chat UI
         doesn't double-render and the analytics layer can slice
         by submit-via-artifact vs typed.
      4. Inject a synthetic student turn that summarises the
         attempt for the conversation history (e.g. "I picked B"
         / "I answered: 905"); fire respond() so the tutor reacts
         using the verified verdict + R2 active_bank_question
         scaffolding rules (no probing on correct, hint-then-probe
         on wrong, etc).
      5. Return BOTH the verdict AND the new tutor_message in one
         payload so the frontend can show ✓/✗ feedback then render
         the tutor's reply.

    Body (JSON or form-encoded):
      question_id (int, required)
      kind (str, required) — 'lesson_step' | 'exit_ticket_question'
      answer (str | list) — for MCQ a letter A/B/C/D; for
        fill_in_blank a comma-string or list; for short_numeric a
        value; for short_answer a free-text string.
      show_working (str, optional) — R6 will use this; today it's
        accepted but only persisted on the turn metadata for
        analytics.
    """
    from apps.tutoring.conversational_tutor import ConversationalTutor

    session = get_object_or_404(
        TutorSession, id=session_id, student=request.user,
    )

    try:
        if request.content_type and 'application/json' in request.content_type:
            data = json.loads(request.body or b'{}')
        else:
            data = {
                'question_id': request.POST.get('question_id'),
                'kind': request.POST.get('kind'),
                'answer': request.POST.get('answer', ''),
                'show_working': request.POST.get('show_working', ''),
            }
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    raw_qid = data.get('question_id')
    kind = (data.get('kind') or '').strip().lower()
    answer = data.get('answer')
    show_working = (data.get('show_working') or '').strip()

    if not raw_qid or kind not in ('lesson_step', 'exit_ticket_question'):
        return JsonResponse({
            'error': 'question_id + kind=lesson_step|exit_ticket_question required',
        }, status=400)
    try:
        question_id = int(raw_qid)
    except (TypeError, ValueError):
        return JsonResponse({'error': 'question_id must be an integer'}, status=400)

    # Resolve the bank entry. Fail-loud if it's gone (deleted course
    # mid-session) — the frontend should surface the error so the
    # student knows to refresh.
    if kind == 'lesson_step':
        from apps.curriculum.models import LessonStep
        question = LessonStep.objects.filter(id=question_id).first()
    else:
        from apps.tutoring.models import ExitTicketQuestion
        question = ExitTicketQuestion.objects.filter(id=question_id).first()
    if question is None:
        return JsonResponse({
            'error': f'{kind} #{question_id} not found',
        }, status=404)

    # Grade. Use the lesson_step grader for slot 0; the bank grader for
    # exit-ticket questions — same dispatch the legacy
    # _grade_against_last_bank_question path does.
    from apps.tutoring.bank_grader import (
        grade_bank_response, grade_lesson_step_response,
    )
    if kind == 'lesson_step':
        # Lesson-step grader expects a string.
        verdict = grade_lesson_step_response(question, str(answer or ''))
    else:
        # Bank grader handles list / dict / str dispatch by question_type.
        # Pass a judge_client so text-content types use the SAME LLM
        # batch grader the exit ticket uses (no false-NEGs on
        # paraphrased / synonym / partial-credit answers). Pilot
        # directive 2026-05-16. Build the client here from the JUDGE
        # ModelConfig — mirrors ConversationalTutor.judge_client.
        _judge_llm_client = None
        try:
            from apps.llm.models import ModelConfig
            from apps.llm.client import get_llm_client
            _judge_cfg = ModelConfig.get_for('judge')
            if _judge_cfg is not None:
                _judge_llm_client = get_llm_client(_judge_cfg)
        except Exception as _e:
            import logging as _lg
            _lg.getLogger('apps').warning(
                f"[chat_answer_bank_question] judge_client init failed: {_e} "
                f"— falling back to deterministic grader"
            )
        _is_math = (
            session.lesson.unit.course.is_math
            if session.lesson.unit and session.lesson.unit.course
            else False
        )
        verdict = grade_bank_response(
            question, answer,
            llm_client=_judge_llm_client,
            is_math=_is_math,
        )

    # Build the synthetic student message — short summary of what
    # they picked, prefixed with a question reference so the LLM
    # anchors its reply to THIS specific question. Without the
    # reference the LLM re-derived against the most-recent context
    # in chat (pilot 2026-05-16: student answered FIB "90, 170"
    # correctly but the LLM responded as if they were answering an
    # earlier "Find x" question and contradicted the CORRECT verdict).
    #
    # Format: "[Re: <30-char snippet of question>...] I answered: …"
    # — short enough not to clutter chat history, long enough that
    # the LLM can disambiguate from any older question it sees.
    if kind == 'exit_ticket_question' and (
        getattr(question, 'question_type', '') == 'mcq'
    ):
        summary = f"I picked {str(answer).strip().upper()}."
    elif isinstance(answer, list):
        summary = "I answered: " + ", ".join(str(a) for a in answer)
    else:
        summary = f"I answered: {str(answer).strip()}"
    # Question reference snippet. Use stem fields most likely populated
    # for each kind. Truncate to 80 chars so the conversation history
    # stays readable.
    q_stem = (
        (getattr(question, 'question_text', None) or '')
        or (getattr(question, 'question', None) or '')
        or (getattr(question, 'teacher_script', None) or '')
    ).strip()
    if q_stem:
        snippet = q_stem[:80].rstrip()
        if len(q_stem) > 80:
            snippet += '…'
        summary = f"[Answering: \"{snippet}\"]\n{summary}"
    if show_working:
        summary += f"\n\nMy working:\n{show_working}"

    # Drive the tutor's reaction through the existing respond() loop.
    # The synthetic student turn carries the structured verdict in
    # metadata so the active_bank_question system-prompt block (R2)
    # can render student_status='answered_correct' / 'answered_wrong'
    # — that's what makes the tutor confirm + explain (correct) or
    # acknowledge + probe (wrong) without re-asking.
    student_metadata = {
        'synthetic_source': 'bank_answer',
        'bank_question_id': question.id,
        'bank_question_kind': kind,
        'bank_grade_verdict': verdict.to_metadata(),
    }
    if show_working:
        student_metadata['show_working'] = show_working[:2000]

    tutor_message = None
    try:
        tutor = ConversationalTutor(session)
        # Pre-load the verdict on the engine so _grade_against_last_bank_question
        # doesn't re-grade against the synthetic turn's text.
        tutor._pending_bank_grade = verdict
        tutor._pending_bank_question = question

        result = tutor.respond(summary, student_metadata=student_metadata)
        tutor_message = {
            'message': result.content,
            'phase': result.phase,
            'media': result.media,
            'show_exit_ticket': result.show_exit_ticket,
            'exit_ticket': result.exit_ticket_data,
            'is_complete': result.is_complete,
            'step_number': result.step_number,
            'total_steps': result.total_steps,
            'is_correct': result.is_correct,
            'streak_count': result.streak_count,
            'practice_score': result.practice_score,
            'milestone': result.milestone,
            'artifact_html': getattr(result, 'artifact_html', None),
            'probe': getattr(result, 'probe', None),
            'pending_question': getattr(result, 'pending_question', None),
        }
    except Exception as exc:
        import logging as _lg
        _lg.getLogger('apps').warning(
            f"[chat_answer_bank_question] respond failed: {exc}",
            exc_info=True,
        )

    # Surface the synthetic student message as a `student_display`
    # so the frontend can render it as a chat bubble — the artifact
    # submission should appear in the conversation history just like
    # a typed chat reply. Pilot directive 2026-05-16: "return the
    # student answer in the artifact into the chat history. so that
    # we have a complete unbroken history".
    return JsonResponse({
        'ok': True,
        'verdict': verdict.to_metadata(),
        'tutor_message': tutor_message,
        'student_display': summary,
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


# ============================================================================
# Course Summative Exam (student-facing)
# ============================================================================

@login_required
def summative_take(request, course_id):
    """Student opens a course summative — picks 30 questions, renders the
    exam. Reuses the in-progress attempt if the student already started.
    """
    from apps.curriculum.models import Course
    from apps.tutoring.models import ExitTicket, ExitTicketAttempt
    from apps.tutoring.summative_selection import select_questions_for_attempt

    course = get_object_or_404(Course, id=course_id)

    # Same-institution gate: students can only take their school's
    # summative or platform-wide ones.
    if course.institution_id is not None:
        in_school = request.user.memberships.filter(
            institution_id=course.institution_id, is_active=True,
        ).exists()
        if not in_school and not request.user.is_staff:
            return render(request, 'tutoring/summative/not_available.html', {
                'course': course,
                'reason': 'You are not enrolled in this school.',
            }, status=403)

    summative = ExitTicket.objects.filter(
        course=course,
        assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
    ).first()
    if not summative or not summative.is_published:
        return render(request, 'tutoring/summative/not_available.html', {
            'course': course,
            'reason': "This course's summative exam isn't published yet. Check back when your teacher releases it.",
        })

    # In-progress attempt? Reuse its question list so a refresh doesn't re-shuffle.
    attempt = ExitTicketAttempt.objects.filter(
        exit_ticket=summative,
        student=request.user,
        completed_at__isnull=True,
    ).order_by('-started_at').first()

    if attempt and attempt.answers.get('selected_question_ids'):
        from apps.tutoring.models import ExitTicketQuestion
        ids = attempt.answers.get('selected_question_ids') or []
        q_map = {q.id: q for q in ExitTicketQuestion.objects.filter(id__in=ids)}
        questions = [q_map[i] for i in ids if i in q_map]
    else:
        # Pick stratified questions. Per-student-AND-attempt deterministic
        # so a baseline vs final attempt see *different* shuffles (they're
        # both supposed to test the same competencies, just at different
        # points in time).
        prior_completed = ExitTicketAttempt.objects.filter(
            exit_ticket=summative,
            student=request.user,
            completed_at__isnull=False,
        ).count()
        questions = select_questions_for_attempt(
            summative,
            count=summative.questions_per_attempt,
            seed=request.user.id * 1_000_003 + prior_completed,
        )
        if not attempt:
            # First completed attempt = baseline; second = final; rest = retakes.
            if prior_completed == 0:
                purpose = ExitTicketAttempt.Purpose.BASELINE
            elif prior_completed == 1:
                purpose = ExitTicketAttempt.Purpose.FINAL
            else:
                purpose = ExitTicketAttempt.Purpose.RETAKE
            attempt = ExitTicketAttempt.objects.create(
                exit_ticket=summative,
                student=request.user,
                purpose=purpose,
                answers={
                    'selected_question_ids': [q.id for q in questions],
                    'responses': {},
                },
            )

    # Carry a `?next=` so the student returns to the lesson they tried
    # to start (when this is a baseline gate redirect).
    next_url = request.GET.get('next', '')
    return render(request, 'tutoring/summative/take.html', {
        'course': course,
        'summative': summative,
        'attempt': attempt,
        'questions': questions,
        'total_q': len(questions),
        'pass_threshold': summative.passing_score,
        'is_baseline': attempt.purpose == 'baseline',
        'next_url': next_url,
    })


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def summative_submit(request, course_id):
    """Submit a summative attempt. Grades all answers deterministically
    and redirects to the review page."""
    from apps.curriculum.models import Course
    from apps.tutoring.models import ExitTicket, ExitTicketAttempt, ExitTicketQuestion
    from apps.tutoring.summative_grading import grade_attempt
    from django.utils import timezone as _tz

    course = get_object_or_404(Course, id=course_id)
    summative = get_object_or_404(
        ExitTicket,
        course=course,
        assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
    )
    attempt = ExitTicketAttempt.objects.filter(
        exit_ticket=summative,
        student=request.user,
        completed_at__isnull=True,
    ).order_by('-started_at').first()
    if not attempt:
        return JsonResponse({"error": "No in-progress attempt to submit."}, status=400)

    try:
        body = json.loads(request.body or "{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    raw_answers = body.get("answers") or {}
    # Normalize keys to int (HTML form encoding may stringify them).
    answers_by_id = {}
    for k, v in raw_answers.items():
        try:
            answers_by_id[int(k)] = v
        except (TypeError, ValueError):
            continue

    selected_ids = attempt.answers.get('selected_question_ids') or []
    q_map = {q.id: q for q in ExitTicketQuestion.objects.filter(id__in=selected_ids)}
    questions = [q_map[i] for i in selected_ids if i in q_map]

    result = grade_attempt(questions, answers_by_id)
    correct = result['correct']
    passed = correct >= (summative.passing_score or 0)

    attempt.score = correct
    attempt.passed = passed
    attempt.completed_at = _tz.now()
    # Preserve `next_url` so the review page can offer "continue to your lesson".
    next_url = body.get("next_url") or ''
    attempt.answers = {
        'selected_question_ids': selected_ids,
        'responses': {str(k): v for k, v in answers_by_id.items()},
        'result': result,
        'next_url': next_url if next_url.startswith('/') else '',
    }
    attempt.save()

    # Denormalize the per-objective snapshot onto StudentProfile so the
    # tutor + catalog read it without re-aggregating.
    try:
        from apps.tutoring.competency_tracker import refresh_student_snapshot
        refresh_student_snapshot(request.user, course)
    except Exception as e:
        logger.warning(f"snapshot refresh failed after summative submit: {e}")

    review_url = f"/tutor/summative/{course.id}/review/{attempt.id}/"
    return JsonResponse({
        "ok": True,
        "redirect": review_url,
        "score": correct,
        "total": result['total'],
        "passed": passed,
    })


@login_required
def summative_review(request, course_id, attempt_id):
    """Show the student their summative results."""
    from apps.curriculum.models import Course
    from apps.tutoring.models import ExitTicketAttempt, ExitTicketQuestion

    course = get_object_or_404(Course, id=course_id)
    attempt = get_object_or_404(
        ExitTicketAttempt,
        id=attempt_id,
        student=request.user,
        exit_ticket__course=course,
    )
    summative = attempt.exit_ticket
    selected_ids = attempt.answers.get('selected_question_ids') or []
    q_map = {q.id: q for q in ExitTicketQuestion.objects.filter(id__in=selected_ids)}
    questions = [q_map[i] for i in selected_ids if i in q_map]

    per_q = (attempt.answers.get('result') or {}).get('per_question') or []
    per_q_by_id = {row['question_id']: row for row in per_q}

    # Show all the student's completed attempts on this summative so they
    # can track growth across baseline → final → any retakes.
    prior_attempts = list(
        ExitTicketAttempt.objects.filter(
            exit_ticket=summative,
            student=request.user,
            completed_at__isnull=False,
        ).order_by('completed_at')
    )
    for p in prior_attempts:
        # Stamp questions_per_attempt for the template (no FK trip).
        p.questions_per_attempt = summative.questions_per_attempt

    return render(request, 'tutoring/summative/review.html', {
        'course': course,
        'summative': summative,
        'attempt': attempt,
        'questions': questions,
        'per_q_by_id': per_q_by_id,
        'result': attempt.answers.get('result') or {},
        'prior_attempts': prior_attempts,
    })


# ============================================================================
# Pre-test diagnostic — students take 10 questions BEFORE starting the lesson.
# Pass → lesson marked mastered. Fail → tutor session starts with the per-EO
# sub-skill map primed in engine_state. Pre-test and post-test sample disjoint
# question sets so the post-test isn't biased.
# ============================================================================

@login_required
def lesson_pretest(request, lesson_id):
    """GET: render the pre-test page (uses the shared exit-ticket modal).
    POST (JSON): grade, save attempt, return JSON with redirect URL."""
    from apps.tutoring.models import (
        ExitTicket, ExitTicketQuestion, ExitTicketAttempt, StudentLessonProgress,
    )
    import json as _json
    import random as _random

    institution = get_user_institution(request.user)
    lookup = {'id': lesson_id, 'is_published': True}
    if institution and not request.user.is_staff:
        lesson = get_object_or_404(
            Lesson.objects.filter(
                Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True)
            ), **lookup,
        )
    else:
        lesson = get_object_or_404(Lesson, **lookup)

    exit_ticket = ExitTicket.objects.filter(lesson=lesson).first()
    if not exit_ticket:
        django_messages.warning(request, "This lesson doesn't have a question bank yet.")
        return redirect('tutoring:catalog')

    # Already mastered → no point pre-testing again.
    progress = StudentLessonProgress.objects.filter(
        student=request.user, lesson=lesson,
    ).first()
    if progress and progress.mastery_level == 'mastered':
        django_messages.info(request, f"You've already mastered '{lesson.title}'.")
        return redirect('tutoring:catalog')

    PRETEST_SIZE = 10
    catalog_url = reverse('tutoring:catalog')
    tutor_url = reverse('tutoring:tutor_interface', kwargs={'lesson_id': lesson.id})

    if request.method == 'POST':
        # Modal posts JSON: { answers: [...], selected_question_ids: [...] }.
        # answers[i] aligns with selected_question_ids[i] — same flat index
        # the modal uses internally.
        try:
            payload = _json.loads(request.body.decode('utf-8') or '{}')
        except (UnicodeDecodeError, ValueError):
            return JsonResponse({'error': 'invalid_json'}, status=400)

        raw_ids = payload.get('selected_question_ids') or []
        selected_ids = []
        for x in raw_ids:
            try:
                selected_ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not selected_ids:
            return JsonResponse({'error': 'pretest_session_expired'}, status=400)

        answers = payload.get('answers') or []
        if len(answers) != len(selected_ids):
            return JsonResponse({'error': 'answer_count_mismatch'}, status=400)

        questions = list(ExitTicketQuestion.objects.filter(id__in=selected_ids))
        q_by_id = {q.id: q for q in questions}
        # Preserve the order the client used (matches its `answers` array).
        pairs = [(qid, ans) for qid, ans in zip(selected_ids, answers) if qid in q_by_id]

        correct = 0
        per_question = []
        results = []
        achieved_eos = set()
        failed_eos = set()
        for index, (qid, raw_answer) in enumerate(pairs):
            q = q_by_id[qid]
            answer_str = _normalize_pretest_answer(q, raw_answer)
            is_correct = _grade_pretest_question(q, answer_str)
            if is_correct:
                correct += 1
            tag = (q.concept_tag or '').strip()
            if tag:
                (achieved_eos if is_correct else failed_eos).add(tag)
            per_question.append({
                'question_id': q.id,
                'concept_tag': tag,
                'student_answer': answer_str,
                'correct': is_correct,
            })
            results.append({
                'index': index,
                'question_type': q.question_type or 'mcq',
                'is_correct': is_correct,
                'selected': raw_answer if isinstance(raw_answer, str) else '',
                'explanation': '',  # No answer leak before the tutor session.
            })

        passing = exit_ticket.passing_score or 8
        if PRETEST_SIZE != 10 and passing > PRETEST_SIZE:
            passing = max(1, int(round(passing * PRETEST_SIZE / 10)))
        total = len(pairs)
        passed = correct >= passing

        ExitTicketAttempt.objects.create(
            exit_ticket=exit_ticket,
            student=request.user,
            session=None,  # diagnostic happens before any tutor session
            purpose=ExitTicketAttempt.Purpose.DIAGNOSTIC,
            score=correct,
            passed=passed,
            answers={
                'selected_question_ids': [qid for qid, _ in pairs],
                'per_question': per_question,
                'achieved_eos': sorted(achieved_eos),
                'failed_eos': sorted(failed_eos),
                'total': total,
                'passing_score': passing,
            },
            completed_at=timezone.now(),
        )

        if passed:
            # Lesson considered mastered — pre-test demonstrated the
            # competency. Mirrors _update_competency from the engine.
            score_pct = correct / total if total else 0.0
            score_pct = max(0.0, min(1.0, round(score_pct, 4)))
            sess_inst = lesson.unit.course.institution if lesson.unit else None
            prog, _ = StudentLessonProgress.objects.get_or_create(
                student=request.user,
                lesson=lesson,
                defaults={'institution': sess_inst, 'mastery_level': 'mastered'},
            )
            prog.best_score = score_pct
            prog.attempts_count = (prog.attempts_count or 0) + 1
            prog.last_attempt_at = timezone.now()
            prog.mastery_level = 'mastered'
            prog.save()

            try:
                from apps.tutoring.competency_tracker import refresh_student_snapshot
                if lesson.unit and lesson.unit.course:
                    refresh_student_snapshot(request.user, lesson.unit.course)
            except Exception:
                pass

            message = f"Pre-test passed ({correct}/{total}) — '{lesson.title}' marked complete!"
            redirect_url = catalog_url
        else:
            message = f"Got {correct}/{total}. Let's work on the parts you missed."
            redirect_url = tutor_url

        return JsonResponse({
            'passed': passed,
            'score': correct,
            'total': total,
            'passing_score': passing,
            'message': message,
            'redirect_url': redirect_url,
            'exit_ticket': {
                'results': results,
            },
        })

    # GET — sample 10 questions and render. Sample anew each visit so a
    # student abandoning + retrying doesn't see the same set.
    pool = list(
        ExitTicketQuestion.objects.filter(exit_ticket=exit_ticket)
        .exclude(question_type='data_interpretation')
    )
    if len(pool) < PRETEST_SIZE:
        django_messages.warning(
            request,
            f"This lesson's bank has {len(pool)} questions — too few for a pre-test. Start the lesson directly.",
        )
        return redirect('tutoring:tutor_interface', lesson_id=lesson.id)

    sampled = _random.sample(pool, PRETEST_SIZE)
    exit_ticket_data = _serialize_pretest_questions_for_modal(sampled)
    return render(request, 'tutoring/pretest.html', {
        'lesson': lesson,
        'exit_ticket_data': exit_ticket_data,
    })


def _serialize_pretest_questions_for_modal(questions):
    """Build the same shape `_handle_exit_ticket` produces in the engine
    so the shared exit-modal partial can render the pre-test directly."""
    out_questions = []
    for i, q in enumerate(questions):
        q_type = (q.question_type or 'mcq')
        q_data = {
            'index': i,
            'question_type': q_type,
            'question': q.question_text,
        }
        if q_type == 'mcq':
            q_data['options'] = [
                {'letter': 'A', 'text': q.option_a},
                {'letter': 'B', 'text': q.option_b},
                {'letter': 'C', 'text': q.option_c},
                {'letter': 'D', 'text': q.option_d},
            ]
            if q.answer_data and q.answer_data.get('source'):
                q_data['source'] = q.answer_data['source']
        else:
            q_data['answer_data'] = q.answer_data or {}
        out_questions.append(q_data)
    return {
        'questions': out_questions,
        'total': len(out_questions),
        'selected_question_ids': [q.id for q in questions],
    }


def _normalize_pretest_answer(q, raw_answer) -> str:
    """The modal posts answers in the same per-type shape the live exit
    ticket uses; the pretest grader takes a single string. Convert."""
    import json as _json
    qtype = (q.question_type or 'mcq').lower()
    if qtype == 'mcq':
        return str(raw_answer or '').strip()
    if qtype == 'fill_in_blank':
        if isinstance(raw_answer, list):
            # Single-blank pre-test grading: take first non-empty entry.
            for v in raw_answer:
                if str(v).strip():
                    return str(v).strip()
            return ''
        return str(raw_answer or '').strip()
    if qtype == 'matching':
        if isinstance(raw_answer, dict):
            return _json.dumps(raw_answer)
        return str(raw_answer or '').strip()
    return str(raw_answer or '').strip()


def _grade_pretest_question(q, student_answer: str) -> bool:
    """Deterministic per-question grading for the pre-test. Mirrors the
    answer-matching logic the post-test exit-ticket flow uses but kept
    standalone so the diagnostic doesn't depend on tutor session state."""
    qtype = (q.question_type or 'mcq').lower()
    sa = (student_answer or '').strip()
    if not sa:
        return False
    if qtype == 'mcq':
        return sa.upper() == (q.correct_answer or '').upper()
    if qtype == 'fill_in_blank':
        ad = q.answer_data or {}
        blanks = [str(b).strip().lower() for b in (ad.get('blanks') or [])]
        if not blanks:
            return False
        # Single-blank fill: accept if matches blank or any alternate.
        student_norm = sa.lower().replace(' ', '').replace(',', '')
        target_norm = blanks[0].replace(' ', '').replace(',', '')
        if student_norm == target_norm:
            return True
        for alt_list in (ad.get('accept_alternatives') or []):
            for alt in (alt_list if isinstance(alt_list, list) else [alt_list]):
                if str(alt).strip().lower().replace(' ', '').replace(',', '') == student_norm:
                    return True
        return False
    if qtype == 'short_answer':
        ad = q.answer_data or {}
        keywords = [str(k).strip().lower() for k in (ad.get('keywords') or []) if str(k).strip()]
        min_kw = max(1, int(ad.get('min_keywords') or 1))
        sa_lower = sa.lower()
        hits = sum(1 for k in keywords if k in sa_lower)
        return hits >= min_kw
    if qtype == 'matching':
        # Matching: a JSON dict {left: right}; serialize to compare.
        ad = q.answer_data or {}
        pairs = ad.get('pairs') or []
        try:
            import json as _json
            student_map = _json.loads(sa) if sa.startswith('{') else {}
        except Exception:
            return False
        for p in pairs:
            left = str(p.get('left') or '')
            right = str(p.get('right') or '').strip().lower()
            if str(student_map.get(left, '')).strip().lower() != right:
                return False
        return True
    return False
