"""
Staff Dashboard Views

Provides:
- Dashboard overview with key metrics
- Student progress tracking
- Curriculum management (upload & auto-generate)
- Class/course management
"""

import json
import logging
import os
import zoneinfo
from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Count, Avg, Q, F, Max
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST

from apps.accounts.models import Institution, Membership, StudentProfile, PlatformConfig
from apps.curriculum.models import Course, Unit, Lesson
from apps.tutoring.models import TutorSession, StudentLessonProgress
from django.contrib.auth.models import User
from django.contrib.auth import update_session_auth_hash, logout

logger = logging.getLogger(__name__)


def get_staff_context(request):
    """Get common context for staff views.

    Supports multi-school via session-stored ``selected_school_id``.
    When no school is selected (or value is ``'all'``), ``institution``
    is ``None`` which means aggregated / all-schools mode.
    """
    selected = request.session.get('selected_school_id')

    if request.user.is_staff:
        # Superadmin — platform-wide access
        all_schools = list(Institution.objects.filter(is_active=True).order_by('name'))

        if selected and selected != 'all':
            institution = Institution.objects.filter(id=selected, is_active=True).first()
        else:
            institution = None  # aggregated mode

        flag_qs = TutorSession.objects.filter(is_flagged=True, flag_reviewed=False)
        if institution is not None:
            flag_qs = flag_qs.filter(institution=institution)

        # Validator flags removed from the safety badge per Edward
        # (2026-05-07) — flagged dashboard is safety-only.
        return {
            'membership': None,
            'institution': institution,
            'role': 'superadmin',
            'all_schools': all_schools,
            'is_aggregated': institution is None,
            'unreviewed_flag_count': _safety_flag_count(institution),
            'can_edit_content': True,  # Superadmin always has full access
            'can_upload_curriculum': True,
            'can_regenerate_courses': True,
        }

    # Regular staff — may belong to multiple schools
    memberships = list(
        Membership.objects.filter(
            user=request.user,
            role='staff',
            is_active=True
        ).select_related('institution')
    )
    if not memberships:
        return None

    staff_schools = [m.institution for m in memberships if m.institution.is_active]

    if selected and selected != 'all':
        institution = next((s for s in staff_schools if str(s.id) == str(selected)), None)
        if not institution:
            institution = staff_schools[0] if staff_schools else memberships[0].institution
    else:
        institution = staff_schools[0] if staff_schools else memberships[0].institution

    membership = next((m for m in memberships if m.institution == institution), memberships[0])

    from apps.accounts.models import PlatformConfig
    config = PlatformConfig.load()

    # Edward (2026-05-07): teachers should NOT be able to edit
    # lessons, regenerate, or upload curriculum — those are platform-
    # admin operations only. The PlatformConfig flags
    # (teachers_can_*) are now ignored for staff; only superusers
    # see editing affordances. Hard removal vs config flag was
    # explicitly requested to remove the operational risk.
    return {
        'membership': membership,
        'institution': institution,
        'role': 'staff',
        'all_schools': staff_schools if len(staff_schools) > 1 else [],
        'is_aggregated': False,
        'unreviewed_flag_count': _safety_flag_count(institution),
        'can_edit_content': False,
        'can_upload_curriculum': False,
        'can_regenerate_courses': False,
    }


def _safety_flag_count(institution) -> int:
    """Count of unreviewed sessions with at least one harmful /
    inappropriate / manipulation flag from the safety judge.

    Per Edward (2026-05-07), the nav badge is safety-only — validator
    flags (curriculum-contradicted etc.) do NOT contribute. The legacy
    helper `_validator_flagged_count` was removed; if a future caller
    needs that count, query SessionTurn.metadata directly.
    """
    from apps.tutoring.models import SessionTurn

    SAFETY_FLAG_TYPES = ('harmful', 'inappropriate', 'manipulation')
    session_ids = set(
        SessionTurn.objects
        .filter(is_flagged=True, flag_type__in=SAFETY_FLAG_TYPES)
        .values_list('session_id', flat=True)
        .distinct()
    )
    qs = TutorSession.objects.filter(
        id__in=session_ids, is_flagged=True, flag_reviewed=False,
    )
    if institution is not None:
        qs = qs.filter(institution=institution)
    return qs.count()


def _inherited_materials_summary(course):
    """For a school course, summarise platform-wide materials it inherits.

    R2.3 (memory/curriculum_material_sharing_plan.md). Returns:
        {
            'platform_courses': [Course, Course, ...],   # matching platform-wide courses
            'material_count': int,                         # total inherited TeachingMaterialUploads
        }
    or None when the course has no subject_code / grade_levels (legacy row).

    Cheap query — no ChromaDB hit, just DB joins on (subject_code, grade_levels).
    Same matching logic as CurriculumKnowledgeBase._global_upload_ids_matching_course
    so the badge count matches what the engine actually sees.
    """
    if not course or not getattr(course, 'subject_code', ''):
        return None
    course_grades = set(course.grade_levels or [])

    from apps.curriculum.models import Course as CourseModel
    from apps.dashboard.models import TeachingMaterialUpload

    platform_courses = list(CourseModel.objects.filter(
        institution__isnull=True,
        subject_code=course.subject_code,
    ).only('id', 'title', 'grade_levels'))

    matching = []
    for pc in platform_courses:
        pc_grades = set(pc.grade_levels or [])
        if not course_grades or not pc_grades or (course_grades & pc_grades):
            matching.append(pc)

    if not matching:
        return None

    material_count = TeachingMaterialUpload.objects.filter(
        course_id__in=[c.id for c in matching],
    ).count()

    return {
        'platform_courses': matching,
        'material_count': material_count,
    }


def filter_by_institution(queryset, institution, field='institution'):
    """Filter queryset by institution. If institution is None (aggregated), return all."""
    if institution is not None:
        return queryset.filter(**{field: institution})
    return queryset


def get_scoped_object_or_404(model, institution, **kwargs):
    """get_object_or_404 with optional institution scoping.

    When *institution* is not None the lookup includes an ``institution``
    filter (or ``course__institution`` for Unit, ``unit__course__institution``
    for Lesson, etc. – callers pass kwargs directly).  When *institution* is
    None (aggregated mode) the institution filter is omitted.
    """
    if institution is not None:
        kwargs['institution'] = institution
    return get_object_or_404(model, **kwargs)


def staff_required(view_func):
    """Decorator to require staff role."""
    @login_required
    def wrapper(request, *args, **kwargs):
        ctx = get_staff_context(request)
        if not ctx:
            messages.error(request, "You don't have staff access.")
            return redirect('tutoring:catalog')
        request.staff_ctx = ctx
        return view_func(request, *args, **kwargs)
    return wrapper


# Alias for backwards compatibility
teacher_required = staff_required


@login_required
@require_POST
def switch_school(request):
    """Store selected school in session."""
    school_id = request.POST.get('school_id', 'all')
    request.session['selected_school_id'] = school_id
    # Redirect back to the page they came from, or dashboard home
    next_url = request.POST.get('next', request.META.get('HTTP_REFERER', ''))
    if next_url:
        return redirect(next_url)
    return redirect('dashboard:home')


# ============================================================================
# Dashboard Home
# ============================================================================

@staff_required
def dashboard_home(request):
    """Main dashboard with overview metrics."""
    institution = request.staff_ctx['institution']

    # Date ranges
    today = timezone.now().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    # Get all students in institution (or all if aggregated)
    student_memberships = filter_by_institution(
        Membership.objects.filter(role='student', is_active=True),
        institution
    ).select_related('user')

    student_ids = list(student_memberships.values_list('user_id', flat=True))
    total_students = len(student_ids)

    # Active students (had session in last 7 days)
    active_students = filter_by_institution(
        TutorSession.objects.filter(student_id__in=student_ids, started_at__date__gte=week_ago),
        institution
    ).values('student').distinct().count()

    # Sessions stats
    total_sessions = filter_by_institution(
        TutorSession.objects.filter(started_at__date__gte=month_ago),
        institution
    ).count()

    completed_sessions = filter_by_institution(
        TutorSession.objects.filter(status='completed', started_at__date__gte=month_ago),
        institution
    ).count()

    mastery_sessions = filter_by_institution(
        TutorSession.objects.filter(status='completed', mastery_achieved=True, started_at__date__gte=month_ago),
        institution
    ).count()

    # Progress stats
    progress_stats = filter_by_institution(
        StudentLessonProgress.objects.all(), institution
    ).aggregate(
        total=Count('id'),
        mastered=Count('id', filter=Q(mastery_level='mastered')),
        in_progress=Count('id', filter=Q(mastery_level='in_progress')),
    )

    # Total available published lessons (true denominator for mastery %)
    total_available_lessons = filter_by_institution(
        Lesson.objects.filter(is_published=True), institution, field='unit__course__institution'
    ).count()

    # "X% students mastered" — % of ATTEMPTING students who mastered
    # at least one lesson. Edward, 2026-05-08: was previously
    # `mastered_cells / (students × lessons) × 100`, which read 7%
    # when 7 student-lesson cells were mastered out of 80 (20 students
    # × 4 lessons), even though most students hadn't started most
    # lessons yet. The cell-coverage definition didn't match what
    # the "students mastered" label implied. Now the metric counts
    # students who have crossed the mastery threshold on at least
    # ONE lesson, divided by students who have attempted any lesson.
    avg_mastery = 0
    students_with_progress = (
        filter_by_institution(
            StudentLessonProgress.objects.exclude(best_score__isnull=True),
            institution,
        )
        .values('student_id').distinct().count()
    )
    students_who_mastered = (
        filter_by_institution(
            StudentLessonProgress.objects.filter(mastery_level='mastered'),
            institution,
        )
        .values('student_id').distinct().count()
    )
    if students_with_progress > 0:
        avg_mastery = round((students_who_mastered / students_with_progress) * 100)

    # avg_competency (C4): average best_score across all populated progress rows,
    # as a percentage. Source of truth = exit ticket attempts via StudentLessonProgress.
    from django.db.models import Avg
    avg_competency_data = filter_by_institution(
        StudentLessonProgress.objects.exclude(best_score__isnull=True),
        institution,
    ).aggregate(avg=Avg('best_score'))
    avg_competency = round((avg_competency_data['avg'] or 0.0) * 100)

    # Students at risk (started but no activity in 7 days)
    at_risk_students = filter_by_institution(
        TutorSession.objects.filter(student_id__in=student_ids),
        institution
    ).exclude(
        started_at__date__gte=week_ago
    ).values('student').distinct().count()

    # Recent activity
    recent_sessions = filter_by_institution(
        TutorSession.objects.all(), institution
    ).select_related('student', 'lesson').prefetch_related(
        'participants__student'
    ).order_by('-started_at')[:10]
    # Annotate each session with its active participant usernames so the
    # template can show the full list under group sessions without an
    # N+1 query (G6 polish).
    for s in recent_sessions:
        active_users = [
            p.student for p in s.participants.all() if p.is_active
        ]
        s.active_participant_users = active_users
        if len(active_users) > 1:
            primary = next(
                (u for u in active_users if u.id == s.student_id), None,
            )
            others = [u for u in active_users if u.id != s.student_id]
            s.group_label = ", ".join(
                ([primary.username] if primary else []) + [u.username for u in others]
            )

    # Activity chart data (last 14 days)
    activity_data = []
    for i in range(14, -1, -1):
        date = today - timedelta(days=i)
        count = filter_by_institution(
            TutorSession.objects.filter(started_at__date=date),
            institution
        ).count()
        activity_data.append({
            'date': date.strftime('%b %d'),
            'sessions': count
        })
    
    context = {
        **request.staff_ctx,
        'total_students': total_students,
        'active_students': active_students,
        'total_sessions': total_sessions,
        'completed_sessions': completed_sessions,
        'mastery_sessions': mastery_sessions,
        'avg_mastery': avg_mastery,
        'avg_competency': avg_competency,
        'at_risk_count': at_risk_students,
        'recent_sessions': recent_sessions,
        'activity_data': json.dumps(activity_data),
        'progress_stats': progress_stats,
    }

    return render(request, 'dashboard/home.html', context)


# ============================================================================
# Student Groups (paired/grouped sessions — Seychelles pilot)
# ============================================================================

@teacher_required
def student_groups_list(request):
    """List all student groups for the current institution."""
    from apps.accounts.models import StudentGroup

    institution = request.staff_ctx['institution']
    qs = StudentGroup.objects.all()
    if institution is not None:
        qs = qs.filter(institution=institution)
    qs = qs.prefetch_related('students').order_by('-is_active', 'name')

    # Roster of students for the create-form
    roster_q = Membership.objects.filter(role='student', is_active=True)
    if institution is not None:
        roster_q = roster_q.filter(institution=institution)
    roster = list(roster_q.select_related('user').order_by('user__last_name', 'user__first_name'))

    # Map student id → active group (for "already in" hints)
    active_group_by_user = {}
    for g in qs.filter(is_active=True):
        for s in g.students.all():
            active_group_by_user[s.id] = g

    context = {
        **request.staff_ctx,
        'groups': qs,
        'roster': roster,
        'active_group_by_user': active_group_by_user,
        'session_modes': [
            ('shared_device', 'Shared device (paired sessions)'),
            ('individual', 'Individual accounts & devices'),
        ],
    }
    return render(request, 'dashboard/student_groups/list.html', context)


@teacher_required
@require_POST
def student_group_create(request):
    """Create a new group with the selected students."""
    from apps.accounts.models import StudentGroup

    institution = request.staff_ctx['institution']
    if institution is None:
        messages.error(request, "Pick a specific school before creating groups.")
        return redirect('dashboard:student_groups')

    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, "Group name is required.")
        return redirect('dashboard:student_groups')

    student_ids = [int(s) for s in request.POST.getlist('student_ids') if s.isdigit()]

    if StudentGroup.objects.filter(institution=institution, name__iexact=name).exists():
        messages.error(request, f"A group named '{name}' already exists.")
        return redirect('dashboard:student_groups')

    group = StudentGroup.objects.create(
        institution=institution,
        name=name,
        created_by=request.user,
    )
    if student_ids:
        # Move each chosen student out of any prior active group at this
        # institution. A student belongs to one active group at a time.
        StudentGroup.students.through.objects.filter(
            user_id__in=student_ids,
            studentgroup__institution=institution,
            studentgroup__is_active=True,
        ).delete()
        group.students.add(*student_ids)

    messages.success(request, f"Group '{group.name}' created with {group.member_count} student(s).")
    return redirect('dashboard:student_groups')


@teacher_required
@require_POST
def student_group_update(request, group_id):
    """Rename a group or replace its membership."""
    from apps.accounts.models import StudentGroup

    institution = request.staff_ctx['institution']
    qs = StudentGroup.objects.all()
    if institution is not None:
        qs = qs.filter(institution=institution)
    group = get_object_or_404(qs, id=group_id)

    new_name = (request.POST.get('name') or '').strip()
    if new_name and new_name != group.name:
        if StudentGroup.objects.filter(
            institution=group.institution, name__iexact=new_name,
        ).exclude(id=group.id).exists():
            messages.error(request, f"Another group named '{new_name}' already exists.")
            return redirect('dashboard:student_groups')
        group.name = new_name

    if 'student_ids' in request.POST:
        student_ids = [int(s) for s in request.POST.getlist('student_ids') if s.isdigit()]
        # Move students out of any *other* active group at this institution.
        if student_ids:
            StudentGroup.students.through.objects.filter(
                user_id__in=student_ids,
                studentgroup__institution=group.institution,
                studentgroup__is_active=True,
            ).exclude(studentgroup_id=group.id).delete()
        group.students.set(student_ids)

    group.save()
    messages.success(request, f"Group '{group.name}' updated.")
    return redirect('dashboard:student_groups')


@teacher_required
@require_POST
def student_group_archive(request, group_id):
    """Soft-archive a group (sets is_active=False). Reversible."""
    from apps.accounts.models import StudentGroup

    institution = request.staff_ctx['institution']
    qs = StudentGroup.objects.all()
    if institution is not None:
        qs = qs.filter(institution=institution)
    group = get_object_or_404(qs, id=group_id)

    group.is_active = not group.is_active
    group.save(update_fields=['is_active'])
    state = 'restored' if group.is_active else 'archived'
    messages.success(request, f"Group '{group.name}' {state}.")
    return redirect('dashboard:student_groups')


@teacher_required
@require_POST
def institution_session_mode(request):
    """Toggle the institution's session_mode (shared_device | individual)."""
    institution = request.staff_ctx['institution']
    if institution is None:
        messages.error(request, "Pick a specific school first.")
        return redirect('dashboard:student_groups')

    mode = request.POST.get('session_mode', '').strip()
    valid = {c[0] for c in Institution.SessionMode.choices}
    if mode not in valid:
        messages.error(request, "Invalid session mode.")
        return redirect('dashboard:student_groups')

    institution.session_mode = mode
    institution.save(update_fields=['session_mode'])
    messages.success(request, f"Session mode set to '{institution.get_session_mode_display()}'.")
    return redirect('dashboard:student_groups')


# ============================================================================
# Student Management
# ============================================================================

@teacher_required
def student_list(request):
    """List all students with progress summary."""
    institution = request.staff_ctx['institution']
    
    # Get students with their progress
    students = filter_by_institution(
        Membership.objects.filter(role='student', is_active=True),
        institution
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    # Total available published lessons (denominator for all students)
    total_available = filter_by_institution(
        Lesson.objects.filter(is_published=True), institution, field='unit__course__institution'
    ).count()

    # Enrich with progress data
    student_data = []
    for membership in students:
        user = membership.user

        # Get progress stats
        mastered_count = filter_by_institution(
            StudentLessonProgress.objects.filter(student=user, mastery_level='mastered'),
            institution
        ).count()

        # Get recent session
        last_session = filter_by_institution(
            TutorSession.objects.filter(student=user),
            institution
        ).order_by('-started_at').first()

        # Get profile
        profile = getattr(user, 'student_profile', None)

        student_data.append({
            'user': user,
            'profile': profile,
            'lessons_mastered': mastered_count,
            'lessons_total': total_available,
            'last_active': last_session.started_at if last_session else None,
            'mastery_pct': round((mastered_count / total_available) * 100) if total_available else 0,
        })
    
    # Pagination
    paginator = Paginator(student_data, 20)
    page = request.GET.get('page', 1)
    students_page = paginator.get_page(page)
    
    context = {
        **request.staff_ctx,
        'students': students_page,
        'total_count': len(student_data),
    }
    
    return render(request, 'dashboard/students/list.html', context)


@teacher_required
def student_detail(request, student_id):
    """Detailed view of a student's progress."""
    institution = request.staff_ctx['institution']
    
    student = get_object_or_404(User, id=student_id)

    # Verify student belongs to this institution (or any if aggregated)
    membership = filter_by_institution(
        Membership.objects.filter(user=student, role='student'),
        institution
    ).first()

    if not membership:
        messages.error(request, "Student not found.")
        return redirect('dashboard:student_list')

    # Get all progress
    progress_list = filter_by_institution(
        StudentLessonProgress.objects.filter(student=student),
        institution
    ).select_related('lesson', 'lesson__unit', 'lesson__unit__course').order_by(
        'lesson__unit__course__title',
        'lesson__unit__order_index',
        'lesson__order_index'
    )

    # Get all sessions — include group sessions where the student was a
    # secondary participant (not just sessions they own). A secondary
    # participant gets credit toward mastery + the group session shows up
    # in their activity. See memory/group_lessons_plan.md.
    sessions_q = Q(student=student) | Q(participants__student=student, participants__is_active=True)
    sessions = (
        filter_by_institution(
            TutorSession.objects.filter(sessions_q).distinct(),
            institution,
        )
        .select_related('lesson')
        .prefetch_related('participants__student')
        .order_by('-started_at')[:20]
    )
    for s in sessions:
        s.is_group_for_student = (
            s.participants.filter(is_active=True).count() > 1
            and s.student_id != student.id
        )

    # Stats
    all_sessions_qs = filter_by_institution(
        TutorSession.objects.filter(sessions_q).distinct(),
        institution,
    )
    stats = {
        'total_sessions': all_sessions_qs.count(),
        'completed_sessions': all_sessions_qs.filter(status='completed').count(),
        'mastered_lessons': progress_list.filter(mastery_level='mastered').count(),
        'in_progress_lessons': progress_list.filter(mastery_level='in_progress').count(),
    }
    
    # Get total lessons per course (published + draft) for the
    # denominator. Edward, 2026-05-07: show the full course
    # structure so teachers see how far through the course a
    # student is even when later lessons aren't published yet.
    course_lesson_counts = {}
    courses_qs = filter_by_institution(
        Course.objects.all(), institution
    ).prefetch_related('units__lessons')
    for course in courses_qs:
        count = 0
        for unit in course.units.all():
            count += unit.lessons.count()
        if count > 0:
            course_lesson_counts[course.id] = {'course': course, 'count': count}

    # Group progress by course (with rich competency breakdown — C4)
    from apps.tutoring.competency import best_attempt, per_concept_breakdown
    courses_progress = {}
    for p in progress_list:
        course = p.lesson.unit.course
        if course.id not in courses_progress:
            # Resolve the denominator. course_lesson_counts only includes
            # courses scoped to the teacher's institution — if the
            # course is platform-wide / owned by another institution
            # (a global course the student worked through), we need
            # to count its lessons directly so we don't render
            # "1/0 lessons". Fixed 2026-05-07.
            total_in_course = course_lesson_counts.get(course.id, {}).get('count')
            if not total_in_course:
                total_in_course = sum(
                    u.lessons.count() for u in course.units.all()
                )
            courses_progress[course.id] = {
                'course': course,
                'lessons': [],
                'mastered': 0,
                'total': total_in_course,
            }
        # Annotate progress with per-concept weak areas (lazy: only for
        # lessons that have an attempt).
        if p.best_score is not None:
            p.best_score_pct = round((p.best_score or 0.0) * 100)
            attempt = best_attempt(student, p.lesson)
            if attempt:
                rows = per_concept_breakdown(attempt)
                p.weak_concepts = [r['concept'] for r in rows if r['pct'] < 0.7][:3]
            else:
                p.weak_concepts = []
        else:
            p.best_score_pct = None
            p.weak_concepts = []
        courses_progress[course.id]['lessons'].append(p)
        if p.mastery_level == 'mastered':
            courses_progress[course.id]['mastered'] += 1

    # Add courses that have no progress records yet
    for cid, info in course_lesson_counts.items():
        if cid not in courses_progress:
            courses_progress[cid] = {
                'course': info['course'],
                'lessons': [],
                'mastered': 0,
                'total': info['count'],
            }
    
    # ── Competency breakdown per course ──
    from apps.tutoring.skills_models import Skill, StudentSkillMastery
    from apps.accounts.models import PlatformConfig
    config = PlatformConfig.load()

    # Lessons where this student has at least one exit-ticket attempt.
    # Drives the UN (Unassessed) category — a student who hasn't taken
    # the ticket isn't "below expectation", they just haven't been
    # measured. Mirrors the same UN logic on the lesson-level report.
    from apps.tutoring.models import ExitTicketAttempt as _ETAttempt
    attempted_lesson_ids = set(
        _ETAttempt.objects
        .filter(student=student)
        .values_list('exit_ticket__lesson_id', flat=True)
    )
    UN_CATEGORY = {
        'code': 'UN',
        'label': 'Unassessed',
        'color': '#6b7280',
        'description': 'Has not yet taken the exit ticket.',
    }

    # Competency Breakdown widget was removed from the student
    # detail page (Edward, 2026-05-07) — same family as the per-EO
    # tables dropped from the session report. The Course Progress
    # card already conveys per-lesson mastery. Skipping the
    # per-EO calculation saves ~N×M Skill / StudentSkillMastery
    # queries per page render.
    competency_data: list = []

    # Promote / demote action labels — staff use these one-off on the
    # student page (vs the bulk flow on the class page). An empty
    # grade means the student already graduated → only "demote back to
    # S5" makes sense.
    profile = getattr(student, 'student_profile', None)
    current_grade = (profile.grade_level if profile else '') or ''
    GRADE_ORDER = ['S1', 'S2', 'S3', 'S4', 'S5']
    promote_label = ''
    demote_label = ''
    if current_grade in GRADE_ORDER:
        idx = GRADE_ORDER.index(current_grade)
        if idx < len(GRADE_ORDER) - 1:
            promote_label = f'Promote to {GRADE_ORDER[idx + 1]}'
        else:
            promote_label = 'Graduate'
        if idx > 0:
            demote_label = f'Demote to {GRADE_ORDER[idx - 1]}'
    elif current_grade == '':
        # Graduated — can be reactivated to S5.
        demote_label = 'Reactivate at S5'

    context = {
        **request.staff_ctx,
        'student': student,
        'profile': profile,
        'stats': stats,
        'sessions': sessions,
        'courses_progress': courses_progress.values(),
        'competency_data': competency_data,
        'current_grade': current_grade,
        'promote_label': promote_label,
        'demote_label': demote_label,
    }

    return render(request, 'dashboard/students/detail.html', context)


# ============================================================================
# Curriculum Management
# ============================================================================

@teacher_required
def curriculum_list(request):
    """List all courses grouped by grade level."""
    institution = request.staff_ctx['institution']

    # Include platform-wide courses (institution=None) alongside school courses
    if institution is not None:
        courses_qs = Course.objects.filter(
            Q(institution=institution) | Q(institution__isnull=True)
        )
    else:
        courses_qs = Course.objects.all()

    courses = courses_qs.prefetch_related('units__lessons').order_by('grade_level', 'title')

    from apps.dashboard.models import TeachingMaterialUpload

    is_superadmin = request.user.is_staff

    # Enrich with stats + per-course materials
    course_data = []
    for course in courses:
        total_lessons = Lesson.objects.filter(unit__course=course).count()
        published_lessons = Lesson.objects.filter(unit__course=course, is_published=True).count()
        materials = TeachingMaterialUpload.objects.filter(course=course)
        is_platform_wide = course.institution is None

        course_data.append({
            'course': course,
            'unit_count': course.units.count(),
            'total_lessons': total_lessons,
            'published_lessons': published_lessons,
            'materials': materials,
            'material_count': materials.count(),
            'is_platform_wide': is_platform_wide,
            'read_only': is_platform_wide and not is_superadmin,
        })

    if institution is not None:
        unlinked_materials = TeachingMaterialUpload.objects.filter(
            Q(institution=institution) | Q(institution__isnull=True),
            course__isnull=True,
        )
    else:
        unlinked_materials = TeachingMaterialUpload.objects.filter(course__isnull=True)

    context = {
        **request.staff_ctx,
        'courses': course_data,
        'unlinked_materials': unlinked_materials,
    }

    return render(request, 'dashboard/curriculum/list.html', context)


@teacher_required
def course_detail(request, course_id):
    """View and manage a course's units and lessons."""
    institution = request.staff_ctx['institution']
    is_superadmin = request.user.is_staff

    if institution is not None:
        # Staff can see their school's courses AND platform-wide courses
        course = get_object_or_404(
            Course, Q(institution=institution) | Q(institution__isnull=True), id=course_id
        )
    else:
        course = get_object_or_404(Course, id=course_id)

    # Platform-wide courses are read-only for non-superadmins
    is_platform_wide = course.institution is None
    course_read_only = is_platform_wide and not is_superadmin
    
    units = course.units.prefetch_related('lessons', 'lessons__steps').order_by('grade_level', 'order_index')
    
    # Get progress stats and content stats per lesson
    from apps.tutoring.models import ExitTicket

    lesson_stats = {}
    for unit in units:
        for lesson in unit.lessons.all():
            # Progress stats
            progress = filter_by_institution(
                StudentLessonProgress.objects.filter(lesson=lesson),
                institution
            ).aggregate(
                total=Count('id'),
                mastered=Count('id', filter=Q(mastery_level='mastered')),
            )
            
            # Content stats
            steps_count = lesson.steps.count()
            # Has-content threshold matches the lesson-step floor in
            # content_generator (`max_steps = max(4, ...)`). Pre-bump
            # lessons that produced only 4 steps still count.
            has_content = steps_count >= 3
            
            # Media stats - count images with URLs in step.media JSONField
            media_count = 0
            media_pending = 0
            for step in lesson.steps.all():
                if step.media and step.media.get('images'):
                    for img in step.media['images']:
                        if img.get('url'):
                            media_count += 1
                        else:
                            media_pending += 1
            
            # Exit ticket
            has_exit_ticket = ExitTicket.objects.filter(lesson=lesson).exists()
            
            lesson_stats[lesson.id] = {
                'students_started': progress['total'] or 0,
                'students_mastered': progress['mastered'] or 0,
                'steps_count': steps_count,
                'has_content': has_content,
                'media_count': media_count,
                'media_pending': media_pending,
                'has_exit_ticket': has_exit_ticket,
                'content_status': lesson.content_status,
            }
    
    # Course-level stats
    total_lessons = sum(unit.lessons.count() for unit in units)
    lessons_with_content = sum(1 for stats in lesson_stats.values() if stats['has_content'])
    lessons_without_content = total_lessons - lessons_with_content
    total_media = sum(stats['media_count'] for stats in lesson_stats.values())
    total_media_pending = sum(stats['media_pending'] for stats in lesson_stats.values())
    
    # Check if any lesson is currently generating (course-wide generation in progress)
    from apps.dashboard.models import TeachingMaterialUpload, CurriculumUpload

    # Find the curriculum upload that created this course
    upload_id = course.curriculum_upload_id
    if not upload_id:
        cu = CurriculumUpload.objects.filter(created_course=course).first()
        upload_id = cu.id if cu else None

    # Clean up wrongly-linked materials (from previous broad subject matching bug)
    # Only keep materials that were uploaded with this curriculum or directly to this course
    if upload_id:
        wrongly_linked = TeachingMaterialUpload.objects.filter(
            course=course
        ).exclude(
            curriculum_upload_id=upload_id
        ).exclude(
            curriculum_upload__isnull=True  # keep materials uploaded directly to course
        )
        bad_count = wrongly_linked.count()
        if bad_count > 0:
            wrongly_linked.update(course=None)
            logger.info(f"Unlinked {bad_count} wrongly-linked materials from course {course.id}")

    # Show materials linked to this course or from its curriculum upload
    material_q = Q(course=course)
    if upload_id:
        material_q |= Q(curriculum_upload_id=upload_id)
    materials = TeachingMaterialUpload.objects.filter(material_q).distinct()

    # Auto-recover lessons stuck in `generating` after a worker recycle.
    # Daemon background threads die with the gunicorn worker, so a deploy
    # mid-generation leaves Lesson.content_status='generating' forever.
    # Any lesson that hasn't bumped `updated_at` in 10+ min is orphaned —
    # reset to 'empty' so the teacher can regenerate. Steps already
    # written to the DB stay intact.
    from datetime import timedelta
    stale_cutoff = timezone.now() - timedelta(minutes=5)
    lesson_stale_cutoff = timezone.now() - timedelta(minutes=10)
    Lesson.objects.filter(
        unit__course=course,
        content_status='generating',
        updated_at__lt=lesson_stale_cutoff,
    ).update(content_status='empty')

    active_upload = CurriculumUpload.objects.filter(
        created_course=course, status='processing',
        updated_at__gte=stale_cutoff,
    ).order_by('-created_at').first()

    is_generating = (
        any(s['content_status'] == 'generating' for s in lesson_stats.values())
        or active_upload is not None
    )

    # Course-level content quality tier (based on what materials are available)
    completed_materials = materials.filter(status='completed')
    mat_types = set(completed_materials.values_list('material_type', flat=True))
    has_worksheets = bool(mat_types & {'worksheet', 'question_bank'})
    has_textbooks = bool(mat_types & {'textbook', 'notes'})
    has_curriculum = True  # Always true — course exists from curriculum upload

    if has_worksheets and has_textbooks:
        course_tier = 'tier_1'  # Fully Resourced
    elif has_worksheets or has_textbooks:
        course_tier = 'tier_2'  # Syllabus + Materials
    elif has_curriculum:
        course_tier = 'tier_3'  # Syllabus Only
    else:
        course_tier = 'tier_4'  # Framework Only
    tier_labels = {
        'tier_1': ('Fully Resourced', '#065f46', '#d1fae5'),
        'tier_2': ('Syllabus + Materials', '#1e40af', '#dbeafe'),
        'tier_3': ('Syllabus Only', '#92400e', '#fef3c7'),
        'tier_4': ('Framework Only', '#991b1b', '#fee2e2'),
    }
    course_tier_label, course_tier_color, course_tier_bg = tier_labels.get(
        course_tier, ('Tier 3', '#92400e', '#fef3c7')
    )

    # Weekly assignments — current + next 4 weeks (so the teacher can
    # plan ahead). Each row shows lessons + a quick edit button.
    from apps.dashboard.models import WeeklyAssignment
    from datetime import timedelta
    this_monday = WeeklyAssignment.current_week_start()
    week_starts = [this_monday + timedelta(weeks=i) for i in range(0, 5)]
    existing = {
        wa.week_start: wa for wa in
        WeeklyAssignment.objects.filter(
            course=course, week_start__in=week_starts,
        ).prefetch_related('lessons')
    }
    weekly_assignments = [
        {
            'week_start': ws,
            'week_end': ws + timedelta(days=6),
            'assignment': existing.get(ws),
            'is_current': ws == this_monday,
        }
        for ws in week_starts
    ]
    course_lessons = list(
        Lesson.objects.filter(unit__course=course).order_by('unit__order_index', 'order_index')
    )

    context = {
        **request.staff_ctx,
        'course': course,
        'units': units,
        'lesson_stats': lesson_stats,
        'total_lessons': total_lessons,
        'lessons_with_content': lessons_with_content,
        'lessons_without_content': lessons_without_content,
        'total_media': total_media,
        'total_media_pending': total_media_pending,
        'is_generating': is_generating,
        'active_upload': active_upload,
        'materials': materials,
        'materials_processing': materials.filter(
            status='processing', updated_at__gte=stale_cutoff,
        ).exists(),
        'material_types': TeachingMaterialUpload.MaterialType.choices,
        # R2 — subject + grade dropdowns for the course edit form
        'subject_code_choices': Course.SubjectCode.choices,
        'secondary_year_choices': Course.SecondaryYear.choices,
        # R2.3 — inherited materials from platform-wide courses matching
        # this course's subject_code + grade_levels. Surfaced as a badge
        # so teachers see they're not orphaned when they didn't upload
        # textbooks themselves. Computed only when not platform-wide.
        'inherited_materials': _inherited_materials_summary(course) if not is_platform_wide else None,
        'is_platform_wide': is_platform_wide,
        'course_read_only': course_read_only,
        'course_tier_label': course_tier_label,
        'course_tier_color': course_tier_color,
        'course_tier_bg': course_tier_bg,
        # Weekly assignment block
        'weekly_assignments': weekly_assignments,
        'course_lessons': course_lessons,
    }

    return render(request, 'dashboard/curriculum/course_detail.html', context)


@teacher_required
def curriculum_upload(request):
    """Upload curriculum document with optional teaching material attachment."""
    institution = request.staff_ctx['institution']
    is_superadmin = request.user.is_staff

    # Pilot-mode gate: teachers can review + edit courses but cannot
    # upload new curriculum (it rebuilds the whole course). Toggle the
    # platform flag teachers_can_upload_curriculum to True to lift.
    if not request.staff_ctx.get('can_upload_curriculum'):
        messages.warning(
            request,
            "Curriculum uploads are restricted to platform admins during the pilot."
        )
        return redirect('dashboard:curriculum_list')

    if institution is None and not is_superadmin:
        messages.warning(request, "Please select a specific school before uploading curriculum.")
        return redirect('dashboard:curriculum_list')

    if request.method == 'POST':
        from apps.curriculum.models import Course
        uploaded_file = request.FILES.get('curriculum_file')
        subject_code = request.POST.get('subject_code', '').strip()
        subject_name = request.POST.get('subject_name', '').strip()
        grade_levels = request.POST.getlist('grade_level')
        grade_level = ','.join(grade_levels) if grade_levels else ''

        if not uploaded_file:
            messages.error(request, "Please upload a curriculum file.")
            return redirect('dashboard:curriculum_upload')

        # Validate subject_code against the SubjectCode enum.
        valid_subjects = {c[0] for c in Course.SubjectCode.choices}
        subject_label = ''
        if subject_code:
            if subject_code not in valid_subjects:
                messages.error(request, f"Invalid subject: {subject_code!r}.")
                return redirect('dashboard:curriculum_upload')
            subject_label = dict(Course.SubjectCode.choices).get(subject_code, '')

        # subject_name (display) — auto-derive from the dropdown label when
        # the optional override is blank. Either path satisfies the
        # downstream "must have a subject_name" requirement.
        if not subject_name:
            subject_name = subject_label

        if not subject_name:
            messages.error(request, "Please select a subject.")
            return redirect('dashboard:curriculum_upload')

        if not grade_level:
            messages.error(request, "Please select at least one grade level.")
            return redirect('dashboard:curriculum_upload')

        # Save curriculum file
        import os
        from django.conf import settings

        upload_dir = os.path.join(settings.MEDIA_ROOT, 'curriculum_uploads')
        os.makedirs(upload_dir, exist_ok=True)

        file_path = os.path.join(upload_dir, uploaded_file.name)
        with open(file_path, 'wb+') as dest:
            for chunk in uploaded_file.chunks():
                dest.write(chunk)

        from apps.dashboard.models import CurriculumUpload, TeachingMaterialUpload

        lesson_duration = int(request.POST.get('lesson_duration', 20))

        upload_record = CurriculumUpload.objects.create(
            institution=institution,
            uploaded_by=request.user,
            file_path=file_path,
            subject_name=subject_name,
            subject_code=subject_code,
            grade_level=grade_level,
            lesson_duration_minutes=lesson_duration,
            status='pending'
        )

        # Handle optional material attachments (per-entry: files + title + type + grade).
        # Each entry has its own file input named `material_files_<entry_idx>`
        # so the server can match files to that entry's metadata. The
        # parallel scalar lists (titles/types/grades) preserve DOM order;
        # the entry's index in those lists matches the file-input suffix.
        materials_saved = 0
        if request.POST.get('attach_material'):
            material_titles = request.POST.getlist('material_titles')
            material_types = request.POST.getlist('material_types')
            material_grades = request.POST.getlist('material_grades')

            mat_dir = os.path.join(settings.MEDIA_ROOT, 'material_uploads')
            os.makedirs(mat_dir, exist_ok=True)

            for entry_idx, entry_title in enumerate(material_titles):
                entry_files = request.FILES.getlist(f'material_files_{entry_idx}')
                if not entry_files:
                    continue
                file_type = material_types[entry_idx] if entry_idx < len(material_types) else 'textbook'
                file_grade = material_grades[entry_idx] if entry_idx < len(material_grades) else grade_level
                mat_grade = file_grade or grade_level

                multi = len(entry_files) > 1
                for material_file in entry_files:
                    mat_path = os.path.join(mat_dir, material_file.name)
                    with open(mat_path, 'wb+') as dest:
                        for chunk in material_file.chunks():
                            dest.write(chunk)

                    base_title = entry_title.strip() or os.path.splitext(material_file.name)[0]
                    final_title = (
                        f"{base_title} - {os.path.splitext(material_file.name)[0]}"
                        if multi else base_title
                    )

                    TeachingMaterialUpload.objects.create(
                        institution=institution,
                        uploaded_by=request.user,
                        file_path=mat_path,
                        original_filename=material_file.name,
                        title=final_title,
                        subject_name=subject_name,
                        grade_level=mat_grade,
                        material_type=file_type,
                        description='',
                        status='pending',  # Not processed yet
                        curriculum_upload=upload_record,
                    )
                    materials_saved += 1

        mat_msg = f" {materials_saved} teaching material(s) saved (will be processed after curriculum)." if materials_saved else ""
        messages.success(request, f"Curriculum uploaded! Processing will begin shortly.{mat_msg}")
        return redirect('dashboard:curriculum_process', upload_id=upload_record.id)

    # GET - show upload form
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.curriculum.models import Course

    context = {
        **request.staff_ctx,
        'grade_levels': PlatformConfig.get_grade_choices(),
        'material_types': TeachingMaterialUpload.MaterialType.choices,
        'subject_code_choices': Course.SubjectCode.choices,
    }

    return render(request, 'dashboard/curriculum/upload.html', context)


@teacher_required
def curriculum_process(request, upload_id):
    """Process uploaded curriculum and show progress."""
    import traceback as tb
    try:
        institution = request.staff_ctx['institution']

        from apps.dashboard.models import CurriculumUpload

        # Allow platform-wide uploads (institution=None) for any staff
        if institution is not None:
            upload = get_object_or_404(
                CurriculumUpload,
                Q(institution=institution) | Q(institution__isnull=True),
                id=upload_id,
            )
        else:
            upload = get_object_or_404(CurriculumUpload, id=upload_id)
    except Exception as e:
        logger.error(f"curriculum_process CRASHED for upload {upload_id}: {e}\n{tb.format_exc()}")
        print(f"[ERROR] curriculum_process({upload_id}): {e}\n{tb.format_exc()}", flush=True)
        raise

    # Prepare context based on status
    try:
        context = {
            **request.staff_ctx,
            'upload': upload,
        }

        # If in review state, add parsed data for display
        if upload.status == 'review' and upload.parsed_data:
            parsed = upload.parsed_data
            context['parsed_data'] = parsed
            context['total_lessons'] = sum(
                len(u.get('lessons', [])) for u in parsed.get('units', [])
            )
            context['text_length'] = upload.extracted_text_length

        return render(request, 'dashboard/curriculum/process.html', context)
    except Exception as e:
        logger.error(f"curriculum_process RENDER failed for upload {upload_id}: {e}\n{tb.format_exc()}")
        print(f"[ERROR] curriculum_process render({upload_id}): {e}\n{tb.format_exc()}", flush=True)
        raise


@teacher_required
def curriculum_generate(request, upload_id):
    """API endpoint to start curriculum generation."""
    institution = request.staff_ctx['institution']

    from apps.dashboard.models import CurriculumUpload
    from apps.dashboard.tasks import process_curriculum_upload

    if institution is not None:
        upload = get_object_or_404(
            CurriculumUpload,
            Q(institution=institution) | Q(institution__isnull=True),
            id=upload_id,
        )
    else:
        upload = get_object_or_404(CurriculumUpload, id=upload_id)

    if upload.status not in ('pending', 'failed', 'processing'):
        return JsonResponse({'error': 'Already processing'}, status=400)

    # Start processing in background thread (avoids Gunicorn 120s timeout)
    try:
        upload.status = 'processing'
        upload.save()

        from apps.dashboard.background_tasks import run_async
        run_async(process_curriculum_upload, upload.id)

        return JsonResponse({
            'status': 'success',
            'success': True,
            'message': 'Processing started in background. This page will refresh automatically.',
        })
    except Exception as e:
        upload.status = 'failed'
        upload.error_message = str(e)
        upload.save()
        return JsonResponse({'error': str(e)}, status=500)


@teacher_required
@require_POST
def curriculum_approve(request, upload_id):
    """
    Approve the parsed curriculum and create database records.
    
    Accepts edited structure from the review form and optionally
    generates lesson content (steps, exit tickets).
    """
    institution = request.staff_ctx['institution']
    
    from apps.dashboard.models import CurriculumUpload
    from apps.curriculum.models import Course, Unit, Lesson, LessonStep
    
    lookup = {'id': upload_id}
    if institution is not None:
        lookup['institution'] = institution
    upload = get_object_or_404(CurriculumUpload, **lookup)

    if upload.status != 'review':
        return JsonResponse({'error': 'Not in review state'}, status=400)
    
    try:
        # Get data from request
        data = json.loads(request.body) if request.body else {}
        
        # Get the edited units (or use original parsed_data)
        units_data = data.get('units')
        if not units_data and upload.parsed_data:
            units_data = upload.parsed_data.get('units', [])
        
        if not units_data:
            return JsonResponse({'error': 'No units to create'}, status=400)
        
        # Update status to processing
        upload.status = 'processing'
        upload.current_step = 3
        upload.add_log("💾 Creating curriculum records...")
        upload.save()
        
        # Create or update course
        from apps.curriculum.utils import format_grade_display
        subject = upload.subject_name
        grade_display = format_grade_display(upload.grade_level)
        course_title = f"{subject} {grade_display}"

        # Use upload's institution (None for platform-wide)
        course_institution = upload.institution

        course, created = Course.objects.update_or_create(
            institution=course_institution,
            title=course_title,
            defaults={
                'description': f"{subject} curriculum for {grade_display}",
                'grade_level': upload.grade_level,
                'is_published': False,
            }
        )
        
        upload.created_course = course
        upload.add_log(f"   {'Created' if created else 'Updated'} course: {course.title}")

        # Link any teaching materials uploaded with this curriculum to the new course
        from apps.dashboard.models import TeachingMaterialUpload
        linked_count = TeachingMaterialUpload.objects.filter(
            curriculum_upload=upload, course__isnull=True
        ).update(course=course)
        if linked_count:
            upload.add_log(f"   📎 Linked {linked_count} teaching material(s) to course")

        units_created = 0
        lessons_created = 0
        
        # Create units and lessons from edited data
        for unit_idx, unit_data in enumerate(units_data):
            unit_title = unit_data.get('title', '').strip()
            if not unit_title:
                continue
            
            unit, u_created = Unit.objects.update_or_create(
                course=course,
                title=unit_title,
                defaults={
                    'description': unit_data.get('description', ''),
                    'order_index': unit_idx,
                }
            )
            
            if u_created:
                units_created += 1
            
            upload.add_log(f"   📁 {unit.title}")
            
            for lesson_idx, lesson_data in enumerate(unit_data.get('lessons', [])):
                lesson_title = lesson_data.get('title', '').strip()
                if not lesson_title:
                    continue
                
                # enabling_objectives — per-lesson Terminal Objectives
                # the teacher edited in the review UI. Validate as a list
                # of strings, dedupe case-insensitively, drop empties.
                raw_eos = lesson_data.get('enabling_objectives') or []
                if not isinstance(raw_eos, list):
                    raw_eos = []
                seen = set()
                lesson_eos = []
                for eo in raw_eos:
                    if not isinstance(eo, str):
                        continue
                    text = eo.strip()
                    if not text:
                        continue
                    key = ' '.join(text.lower().split())
                    if key in seen:
                        continue
                    seen.add(key)
                    lesson_eos.append(text)

                lesson, l_created = Lesson.objects.update_or_create(
                    unit=unit,
                    title=lesson_title,
                    defaults={
                        'objective': lesson_data.get('objective', ''),
                        'enabling_objectives': lesson_eos,
                        'order_index': lesson_idx,
                        'estimated_minutes': 20,
                        'is_published': False,
                        'metadata': {
                            'key_concepts': lesson_data.get('key_concepts', []),
                            'from_curriculum_upload': upload.id,
                        }
                    }
                )
                
                if l_created:
                    lessons_created += 1
        
        upload.units_created = units_created
        upload.lessons_created = lessons_created
        upload.add_log(f"   ✓ Created {units_created} units, {lessons_created} lessons")
        upload.save()
        
        # Check if content generation was requested (default: yes)
        generate_content = data.get('generate_steps', True)

        # Start background content generation for all lessons
        lessons_in_course = Lesson.objects.filter(unit__course=course).count()

        if generate_content and lessons_in_course > 0:
            upload.status = 'processing'
            upload.current_step = 4
            upload.add_log(f"📝 Starting background content generation for {lessons_in_course} lessons...")
            upload.save()

            from apps.dashboard.background_tasks import run_async, generate_all_content_async
            run_async(
                generate_all_content_async,
                course_id=course.id,
                upload_id=upload.id,
                generate_media=True,
            )

            return JsonResponse({
                'success': True,
                'status': 'processing',
                'message': f'Course created. Generating content for {lessons_in_course} lessons in the background.',
                'course_id': course.id,
                'units_created': units_created,
                'lessons_created': lessons_created,
            })
        else:
            upload.status = 'completed'
            upload.steps_created = 0
            upload.completed_at = timezone.now()
            upload.add_log(f"✅ Course '{course.title}' created (no lessons to generate).")
            upload.save()

            return JsonResponse({
                'success': True,
                'status': 'completed',
                'course_id': course.id,
                'units_created': units_created,
                'lessons_created': lessons_created,
                'steps_created': 0,
            })
        
    except Exception as e:
        import traceback
        upload.status = 'failed'
        upload.error_message = str(e)
        upload.add_log(f"❌ Error: {str(e)}")
        upload.save()
        
        return JsonResponse({'error': str(e)}, status=500)


@teacher_required
@require_POST
def curriculum_process_api(request, upload_id):
    """
    Step-by-step curriculum processing API.
    
    Steps:
    1. extract - Extract text from document
    2. parse - Parse curriculum structure (units, objectives)
    3. create_lessons - Create lesson structures
    4. save - Save to database
    """
    from apps.dashboard.models import CurriculumUpload
    from apps.curriculum.curriculum_parser import (
        extract_text_from_file,
        parse_mathematics_curriculum,
        parse_generic_curriculum,
        detect_subject,
        create_lessons_from_objectives,
        create_curriculum_from_structure
    )
    
    institution = request.staff_ctx['institution']

    if institution is not None:
        upload = get_object_or_404(
            CurriculumUpload,
            Q(institution=institution) | Q(institution__isnull=True),
            id=upload_id,
        )
    else:
        upload = get_object_or_404(CurriculumUpload, id=upload_id)

    try:
        data = json.loads(request.body)
        step = data.get('step', 'extract')
        
        if step == 'extract':
            # Step 1: Extract text from document
            text, file_type = extract_text_from_file(upload.file_path)
            
            if not text or len(text) < 100:
                return JsonResponse({
                    'error': 'Could not extract text from document. Please check the file format.'
                }, status=400)
            
            # Store in session for next steps
            request.session[f'curriculum_{upload_id}_text'] = text[:50000]  # Limit size
            
            return JsonResponse({
                'success': True,
                'step': 'extract',
                'chars_extracted': len(text),
                'text_preview': text[:2000],
                'text': text[:50000],
            })
        
        elif step == 'parse':
            # Step 2: Parse curriculum structure
            text = data.get('text') or request.session.get(f'curriculum_{upload_id}_text', '')
            
            if not text:
                return JsonResponse({'error': 'No text to parse'}, status=400)
            
            detected_subject = detect_subject(text, upload.subject_name)
            grade_level = upload.grade_level or 'S1'

            # Use dedicated parsers for pilot subjects, generic for others
            curriculum = None
            if 'math' in detected_subject.lower():
                try:
                    curriculum = parse_mathematics_curriculum(text, grade_level)
                except Exception:
                    pass
            elif 'geo' in detected_subject.lower():
                try:
                    from apps.curriculum.curriculum_parser import parse_geography_curriculum
                    curriculum = parse_geography_curriculum(text, grade_level)
                except Exception:
                    pass

            if not curriculum or not curriculum.units:
                curriculum = parse_generic_curriculum(text, detected_subject, grade_level)
            
            # Convert to dict
            from dataclasses import asdict
            curriculum_dict = asdict(curriculum)
            
            units_count = len(curriculum_dict.get('units', []))
            objectives_count = sum(
                len(u.get('terminal_objectives', [])) 
                for u in curriculum_dict.get('units', [])
            )
            
            # Store for next step
            request.session[f'curriculum_{upload_id}_structure'] = curriculum_dict
            
            return JsonResponse({
                'success': True,
                'step': 'parse',
                'units_count': units_count,
                'objectives_count': objectives_count,
                'units': curriculum_dict.get('units', []),
                'subject': curriculum_dict.get('subject'),
                'grade_level': curriculum_dict.get('grade_level'),
            })
        
        elif step == 'create_lessons':
            # Step 3: Create lesson structures
            units = data.get('units') or []
            
            if not units:
                structure = request.session.get(f'curriculum_{upload_id}_structure', {})
                units = structure.get('units', [])
            
            lessons = []
            lesson_order = 0
            
            for unit in units:
                for objective in unit.get('terminal_objectives', []):
                    lesson_order += 1
                    
                    # Create lesson title from objective
                    title = objective
                    prefixes = [
                        "demonstrate the understanding of",
                        "understand and use", "use with confidence",
                        "apply", "solve problems involving",
                    ]
                    for prefix in prefixes:
                        if objective.lower().startswith(prefix):
                            title = objective[len(prefix):].strip()
                            break
                    
                    if title:
                        title = title[0].upper() + title[1:]
                    if len(title) > 60:
                        title = title[:57] + "..."
                    
                    lessons.append({
                        'order': lesson_order,
                        'unit': unit.get('title', 'General'),
                        'title': title,
                        'objective': objective,
                    })
            
            # Store for save step
            request.session[f'curriculum_{upload_id}_lessons'] = lessons
            
            return JsonResponse({
                'success': True,
                'step': 'create_lessons',
                'lessons_count': len(lessons),
                'lessons': lessons,
            })
        
        elif step == 'save':
            # Step 4: Save to database
            structure = request.session.get(f'curriculum_{upload_id}_structure', {})
            lessons = data.get('lessons') or request.session.get(f'curriculum_{upload_id}_lessons', [])
            
            if not structure:
                return JsonResponse({'error': 'No curriculum structure to save'}, status=400)
            
            # Add lessons back to structure
            lessons_by_unit = {}
            for lesson in lessons:
                unit_title = lesson.get('unit', 'General')
                if unit_title not in lessons_by_unit:
                    lessons_by_unit[unit_title] = []
                lessons_by_unit[unit_title].append(lesson)
            
            for unit in structure.get('units', []):
                unit['lessons'] = lessons_by_unit.get(unit.get('title'), [])
            
            # Save to database
            result = create_curriculum_from_structure(
                structure=structure,
                institution=institution or upload.institution,
                upload=upload
            )
            
            # Update upload status
            upload.status = 'completed'
            upload.completed_at = timezone.now()
            upload.lessons_created = result.get('lessons_created', 0)
            upload.units_created = result.get('units_created', 0)
            upload.save()
            
            # Clean up session
            for key in [f'curriculum_{upload_id}_text', 
                       f'curriculum_{upload_id}_structure',
                       f'curriculum_{upload_id}_lessons']:
                if key in request.session:
                    del request.session[key]
            
            return JsonResponse({
                'success': True,
                'step': 'save',
                'course_id': result.get('course_id'),
                'course_name': result.get('course_name'),
                'units_created': result.get('units_created', 0),
                'lessons_created': result.get('lessons_created', 0),
            })
        
        else:
            return JsonResponse({'error': f'Unknown step: {step}'}, status=400)
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'error': str(e)}, status=500)


# ============================================================================
# Class Management
# ============================================================================

@teacher_required
def class_list(request):
    """All classes overview — one summary card per grade. The student
    list + promote action live on the dedicated class detail page; this
    page is just for picking which class to drill into."""
    institution = request.staff_ctx['institution']

    counts = {}
    memberships = filter_by_institution(
        Membership.objects.filter(role='student', is_active=True),
        institution,
    ).select_related('user__student_profile')
    for m in memberships:
        profile = getattr(m.user, 'student_profile', None)
        grade = (profile.grade_level if profile else '') or 'Unassigned'
        counts[grade] = counts.get(grade, 0) + 1

    # Canonical S1..S5 order, with Unassigned at the end. Skip grades
    # that have zero students so the grid doesn't show empty cards.
    canonical = ['S1', 'S2', 'S3', 'S4', 'S5']
    classes = []
    for g in canonical:
        if counts.get(g):
            classes.append({'grade': g, 'count': counts[g], 'is_grade': True})
    if counts.get('Unassigned'):
        classes.append({
            'grade': 'Unassigned', 'count': counts['Unassigned'], 'is_grade': False,
        })
    # Anything custom (e.g. legacy "Form 4") that isn't in the canonical list
    for g, n in counts.items():
        if g in canonical or g == 'Unassigned':
            continue
        classes.append({'grade': g, 'count': n, 'is_grade': True})

    context = {
        **request.staff_ctx,
        'classes': classes,
        'total_students': sum(counts.values()),
    }
    return render(request, 'dashboard/classes/list.html', context)


@teacher_required
def class_detail(request, grade):
    """Detail page for one class — e.g. '/classes/S3/'. Shows every
    course generated for this grade with the class mastery average,
    plus the same roster + selective-promote flow as class_list, but
    scoped to one grade only.

    Class mastery average per course = (mastered lesson cells) /
    (students in class × published lessons in course). Same shape as
    the legacy course-progress metric, scoped to one grade so the
    denominator isn't inflated by off-grade students.
    """
    from apps.tutoring.models import StudentLessonProgress
    from apps.curriculum.models import Lesson

    institution = request.staff_ctx['institution']

    # Roster — institution-scoped students whose grade_level matches.
    student_qs = filter_by_institution(
        Membership.objects.filter(role='student', is_active=True),
        institution,
    ).select_related('user', 'user__student_profile')
    students = []
    for m in student_qs:
        profile = getattr(m.user, 'student_profile', None)
        if profile and (profile.grade_level or '').strip() == grade:
            students.append(m.user)
    students.sort(key=lambda u: ((u.first_name or '').lower(), (u.last_name or '').lower(), u.username))
    student_ids = [s.id for s in students]

    # Courses generated for this grade. Course.grade_level is comma-
    # separated; empty means grade-agnostic (still shown).
    # Include platform-wide courses (institution=None) alongside
    # the teacher's school-specific courses — global content should
    # be visible to every school. Competency stats below are still
    # filtered to the teacher's institution roster, so the numbers
    # show how THEIR students are doing on the global course.
    if institution is not None:
        courses_qs = Course.objects.filter(
            Q(institution=institution) | Q(institution__isnull=True)
        )
    else:
        courses_qs = Course.objects.all()
    course_stats = []
    for course in courses_qs.order_by('title'):
        course_grades = {
            g.strip() for g in (course.grade_level or '').split(',') if g.strip()
        }
        if course_grades and grade not in course_grades:
            continue
        # Count ALL lessons (published + draft) so the class page
        # shows the full course structure regardless of publish state.
        # Edward, 2026-05-07.
        lesson_ids = list(
            Lesson.objects.filter(unit__course=course)
            .values_list('id', flat=True)
        )
        total_lessons = len(lesson_ids)
        if total_lessons and student_ids:
            mastered = StudentLessonProgress.objects.filter(
                lesson_id__in=lesson_ids,
                student_id__in=student_ids,
                mastery_level='mastered',
            ).count()
            avg_pct = round((mastered / (total_lessons * len(student_ids))) * 100)
            students_with_attempts = StudentLessonProgress.objects.filter(
                lesson_id__in=lesson_ids,
                student_id__in=student_ids,
            ).values('student_id').distinct().count()
        else:
            avg_pct = 0
            students_with_attempts = 0
            mastered = 0
        course_stats.append({
            'course': course,
            'lesson_count': total_lessons,
            'avg_mastery_pct': avg_pct,
            'students_with_attempts': students_with_attempts,
            'mastered_cells': mastered,
        })

    GRADE_ORDER = ['S1', 'S2', 'S3', 'S4', 'S5']
    if grade in GRADE_ORDER and GRADE_ORDER.index(grade) < len(GRADE_ORDER) - 1:
        next_grade = GRADE_ORDER[GRADE_ORDER.index(grade) + 1]
        next_action = f'Promote to {next_grade}'
    elif grade == 'S5':
        next_grade = 'Graduate'
        next_action = 'Graduate'
    else:
        next_grade = ''
        next_action = ''

    if grade in GRADE_ORDER and GRADE_ORDER.index(grade) > 0:
        prev_grade = GRADE_ORDER[GRADE_ORDER.index(grade) - 1]
        prev_action = f'Demote to {prev_grade}'
    else:
        prev_grade = ''
        prev_action = ''

    # Recent activity — top 5 lessons across all courses for this
    # grade where students have actually started a session this
    # week. Gives teachers a one-glance "what's the class working on"
    # widget with quick links to monitor / report. Sorted by most
    # recent activity first.
    from django.utils import timezone
    from datetime import timedelta
    recent_activity = []
    if student_ids:
        week_ago = timezone.now() - timedelta(days=7)
        # Pull recent non-abandoned sessions for these students,
        # group by lesson, take the top 5 by most-recent activity.
        recent_sessions = (
            TutorSession.objects
            .filter(student_id__in=student_ids, started_at__gte=week_ago)
            .exclude(status='abandoned')
            .select_related('lesson', 'lesson__unit', 'lesson__unit__course')
            .order_by('-started_at')
        )
        per_lesson: dict = {}
        for s in recent_sessions:
            lid = s.lesson_id
            entry = per_lesson.setdefault(lid, {
                'lesson': s.lesson,
                'course': s.lesson.unit.course,
                'session_count': 0,
                'unique_students': set(),
                'completed_count': 0,
                'most_recent': s.started_at,
            })
            entry['session_count'] += 1
            entry['unique_students'].add(s.student_id)
            if s.status == 'completed':
                entry['completed_count'] += 1
            if s.started_at and s.started_at > entry['most_recent']:
                entry['most_recent'] = s.started_at
        recent_activity = sorted(
            per_lesson.values(), key=lambda e: e['most_recent'], reverse=True,
        )[:5]
        # Convert sets to counts for the template.
        for e in recent_activity:
            e['unique_students'] = len(e['unique_students'])

    context = {
        **request.staff_ctx,
        'grade': grade,
        'students': students,
        'student_count': len(students),
        'course_stats': course_stats,
        'next_grade': next_grade,
        'next_action': next_action,
        'prev_grade': prev_grade,
        'prev_action': prev_action,
        'recent_activity': recent_activity,
    }
    return render(request, 'dashboard/classes/detail.html', context)


@teacher_required
@require_POST
def promote_students(request):
    """Move students up or down a grade.

    ``direction``:
      - ``'up'`` (default) — promote to the next grade. S5 → graduate
        (clears grade_level + deactivates membership).
      - ``'down'`` — demote to the previous grade. S1 cannot be
        demoted further. Demoting an empty/graduated grade reactivates
        the student at S5.
    """
    student_ids = request.POST.getlist('student_ids')
    from_grade = request.POST.get('from_grade', '')
    direction = (request.POST.get('direction') or 'up').lower()

    GRADE_ORDER = ['S1', 'S2', 'S3', 'S4', 'S5']

    # Where to send the user after the action — defaults to the
    # all-classes index, but the class detail page sets ``redirect_to``
    # to its own URL so the user lands back on the same scope.
    redirect_to = request.POST.get('redirect_to') or ''

    def _redirect_back():
        if redirect_to:
            return redirect(redirect_to)
        return redirect('dashboard:class_list')

    if not student_ids:
        messages.warning(request, "No students selected.")
        return _redirect_back()

    # Allow empty `from_grade` only when demoting a graduated student
    # back to S5. Other operations require a valid source grade.
    if direction == 'up':
        if from_grade not in GRADE_ORDER:
            messages.error(request, f"Invalid grade: {from_grade}")
            return _redirect_back()
        idx = GRADE_ORDER.index(from_grade)
        if idx >= len(GRADE_ORDER) - 1:
            # S5 graduation
            updated = StudentProfile.objects.filter(
                user_id__in=student_ids, grade_level=from_grade,
            ).update(grade_level='')
            Membership.objects.filter(
                user_id__in=student_ids, role='student', is_active=True,
            ).update(is_active=False)
            messages.success(request, f"Graduated {updated} student(s) from {from_grade}.")
        else:
            next_grade = GRADE_ORDER[idx + 1]
            updated = StudentProfile.objects.filter(
                user_id__in=student_ids, grade_level=from_grade,
            ).update(grade_level=next_grade)
            messages.success(
                request,
                f"Promoted {updated} student(s) from {from_grade} to {next_grade}.",
            )
        return _redirect_back()

    if direction == 'down':
        # Empty source grade = graduated student returning to S5.
        if not from_grade:
            updated = StudentProfile.objects.filter(
                user_id__in=student_ids, grade_level='',
            ).update(grade_level='S5')
            # Reactivate any deactivated graduate memberships
            Membership.objects.filter(
                user_id__in=student_ids, role='student', is_active=False,
            ).update(is_active=True)
            messages.success(
                request,
                f"Reactivated {updated} graduated student(s) at S5.",
            )
            return _redirect_back()

        if from_grade not in GRADE_ORDER:
            messages.error(request, f"Invalid grade: {from_grade}")
            return _redirect_back()
        idx = GRADE_ORDER.index(from_grade)
        if idx == 0:
            messages.warning(request, "S1 students can't be demoted further.")
            return _redirect_back()
        prev_grade = GRADE_ORDER[idx - 1]
        updated = StudentProfile.objects.filter(
            user_id__in=student_ids, grade_level=from_grade,
        ).update(grade_level=prev_grade)
        messages.success(
            request,
            f"Demoted {updated} student(s) from {from_grade} to {prev_grade}.",
        )
        return _redirect_back()

    messages.error(request, f"Unknown direction: {direction}")
    return _redirect_back()


# ============================================================================
# Reports
# ============================================================================

@teacher_required
def reports_overview(request):
    """Generate reports on student progress."""
    institution = request.staff_ctx['institution']
    
    # Get date range from request
    days = int(request.GET.get('days', 30))
    start_date = timezone.now().date() - timedelta(days=days)
    
    # Sessions by day
    sessions_by_day = filter_by_institution(
        TutorSession.objects.filter(started_at__date__gte=start_date),
        institution
    ).annotate(
        date=TruncDate('started_at')
    ).values('date').annotate(
        count=Count('id'),
        completed=Count('id', filter=Q(status='completed')),
        mastered=Count('id', filter=Q(mastery_achieved=True))
    ).order_by('date')

    # Top performing students
    top_students = filter_by_institution(
        StudentLessonProgress.objects.filter(mastery_level='mastered'),
        institution
    ).values('student__first_name', 'student__last_name', 'student__id').annotate(
        mastered_count=Count('id')
    ).order_by('-mastered_count')[:10]

    # Lessons completion rate
    lessons = filter_by_institution(
        Lesson.objects.filter(is_published=True),
        institution, field='unit__course__institution'
    ).annotate(
        attempts=Count('sessions'),
        completions=Count('sessions', filter=Q(sessions__mastery_achieved=True))
    ).order_by('-attempts')[:20]
    
    # Courses with competency data (for readiness report links)
    from apps.curriculum.models import Course
    from apps.tutoring.skills_models import Skill
    courses_with_eo = []
    course_qs = filter_by_institution(
        Course.objects.filter(is_published=True),
        institution,
    ).order_by('title')
    for course in course_qs:
        eo_count = Skill.objects.filter(course=course, is_enabling_objective=True).count()
        session_count = filter_by_institution(
            TutorSession.objects.filter(lesson__unit__course=course),
            institution,
        ).count()
        courses_with_eo.append({
            'course': course,
            'eo_count': eo_count,
            'session_count': session_count,
        })

    # Lessons with session report data (recent lessons with sessions)
    recent_lessons = filter_by_institution(
        Lesson.objects.filter(
            is_published=True,
            sessions__started_at__date__gte=start_date,
        ),
        institution, field='unit__course__institution'
    ).annotate(
        recent_sessions=Count('sessions', filter=Q(sessions__started_at__date__gte=start_date)),
        recent_completions=Count('sessions', filter=Q(sessions__started_at__date__gte=start_date, sessions__status='completed')),
    ).filter(recent_sessions__gt=0).order_by('-recent_sessions').distinct()[:10]

    context = {
        **request.staff_ctx,
        'days': days,
        'sessions_by_day': list(sessions_by_day),
        'top_students': top_students,
        'lessons': lessons,
        'courses_with_eo': courses_with_eo,
        'recent_lessons': recent_lessons,
    }

    return render(request, 'dashboard/reports/overview.html', context)


@teacher_required
def class_readiness_report(request, course_id):
    """Class readiness report showing enabling objective mastery across students (P2.4)."""
    from apps.curriculum.models import Course
    from apps.tutoring.skills_models import Skill, StudentSkillMastery
    from apps.tutoring.models import TutorSession
    from django.db.models import Avg, Count, Q

    institution = request.staff_ctx['institution']

    lookup = {'id': course_id}
    if institution is not None:
        lookup['institution'] = institution
    course = get_object_or_404(Course, **lookup)

    # Get all EO skills for this course
    eo_skills = Skill.objects.filter(
        course=course,
        is_enabling_objective=True,
    ).select_related('unit', 'primary_lesson').order_by('unit__order_index', 'primary_lesson__order_index')

    # Get all students who have sessions in this course
    student_ids = TutorSession.objects.filter(
        lesson__unit__course=course,
    ).values_list('student_id', flat=True).distinct()
    total_students = len(set(student_ids))

    # Build mastery data for each EO skill
    mastery_threshold = 0.8  # Consider "mastered" at 80%
    struggle_threshold = 0.4

    objectives_data = []
    total_mastered_pct = 0

    for skill in eo_skills:
        masteries = StudentSkillMastery.objects.filter(
            skill=skill,
            student_id__in=student_ids,
        )
        total_with_data = masteries.count()
        mastered = masteries.filter(mastery_level__gte=mastery_threshold).count()
        struggling = masteries.filter(mastery_level__lt=struggle_threshold).count()
        avg_mastery = masteries.aggregate(avg=Avg('mastery_level'))['avg'] or 0

        mastered_pct = (mastered / total_students * 100) if total_students > 0 else 0
        struggling_pct = (struggling / total_students * 100) if total_students > 0 else 0
        total_mastered_pct += mastered_pct

        # Determine color: green (>70% mastered), yellow (40-70%), red (<40%)
        if mastered_pct >= 70:
            color = 'green'
        elif mastered_pct >= 40:
            color = 'yellow'
        else:
            color = 'red'

        objectives_data.append({
            'skill': skill,
            'unit_title': skill.unit.title if skill.unit else '',
            'lesson_title': skill.primary_lesson.title if skill.primary_lesson else '',
            'objective_text': skill.enabling_objective_text,
            'bloom_level': skill.bloom_level,
            'total_with_data': total_with_data,
            'mastered': mastered,
            'struggling': struggling,
            'mastered_pct': round(mastered_pct),
            'struggling_pct': round(struggling_pct),
            'avg_mastery': round(avg_mastery * 100),
            'color': color,
        })

    # Class readiness score
    class_readiness = round(total_mastered_pct / len(eo_skills)) if eo_skills else 0

    # Generate recommendation
    struggling_objectives = [o for o in objectives_data if o['color'] == 'red']
    if not struggling_objectives:
        recommendation = "Class is ready to move on — strong mastery across all objectives."
        recommendation_type = 'success'
    elif len(struggling_objectives) <= 2:
        names = ', '.join(f"'{o['objective_text'][:60]}'" for o in struggling_objectives[:2])
        recommendation = f"Consider revisiting: {names} before moving forward."
        recommendation_type = 'warning'
    else:
        recommendation = f"{len(struggling_objectives)} objectives need attention. Consider revisiting this unit."
        recommendation_type = 'danger'

    # Individual student gaps (students with mastery < 0.5 on any EO)
    student_gaps = []
    if eo_skills.exists():
        from django.contrib.auth.models import User
        for student_id in student_ids:
            weak_skills = StudentSkillMastery.objects.filter(
                student_id=student_id,
                skill__in=eo_skills,
                mastery_level__lt=0.5,
            ).select_related('skill')
            if weak_skills.exists():
                student = User.objects.filter(id=student_id).first()
                student_gaps.append({
                    'student': student,
                    'weak_count': weak_skills.count(),
                    'weak_objectives': [ws.skill.enabling_objective_text[:80] for ws in weak_skills[:5]],
                })
        student_gaps.sort(key=lambda x: -x['weak_count'])

    context = {
        **request.staff_ctx,
        'course': course,
        'objectives_data': objectives_data,
        'total_students': total_students,
        'total_objectives': len(eo_skills),
        'class_readiness': class_readiness,
        'recommendation': recommendation,
        'recommendation_type': recommendation_type,
        'student_gaps': student_gaps[:20],
    }

    return render(request, 'dashboard/class_readiness.html', context)


@teacher_required
def lesson_session_report(request, lesson_id):
    """Post-session report for a specific lesson — the teacher's decision view.

    Shows after the 40-minute tutor lab session:
    - How many students completed the session
    - Per-objective competency: X/Y students achieved each enabling objective
    - Per-student competency: each student's X/Y objectives achieved
    - Clear recommendation: move to next lesson or revisit with focus areas
    """
    from apps.curriculum.models import Lesson
    from apps.tutoring.models import TutorSession, ExitTicketAttempt
    from django.db.models import Q
    from django.contrib.auth.models import User

    from apps.accounts.models import PlatformConfig
    config = PlatformConfig.load()

    institution = request.staff_ctx['institution']
    if institution is not None:
        lesson = get_object_or_404(
            Lesson,
            Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True),
            id=lesson_id,
        )
    else:
        lesson = get_object_or_404(Lesson, id=lesson_id)

    course = lesson.unit.course
    mastery_threshold = config.threshold_me_min / 100.0  # Default 0.8

    # ── Students who had tutor sessions for this lesson ──
    sessions = TutorSession.objects.filter(lesson=lesson).select_related('student')
    student_ids = list(sessions.values_list('student_id', flat=True).distinct())
    total_students = len(student_ids)

    completed_sessions = sessions.filter(status='completed').values_list('student_id', flat=True).distinct()
    completed_count = len(set(completed_sessions))

    # ── Enabling Objectives: canonical source of truth ──
    # Use `combined_objectives_for_lesson` — the same helper used by
    # summative tagging and the cross-lesson competency matrix. Reading
    # from `lesson.metadata.teaching_steps` or `LessonStep.enabling_objective`
    # produces strings that don't always textually match the per-question
    # `concept_tag` stored on attempts (granular sub-skills, rephrased
    # during content gen, whitespace/case drift) — which silently zeroed
    # every objective even when students passed the exit ticket.
    from apps.curriculum.content_generator import combined_objectives_for_lesson
    teaching_steps = combined_objectives_for_lesson(lesson)
    total_objectives = len(teaching_steps)

    def _norm_tag(s: str) -> str:
        return ' '.join((s or '').split()).strip().lower()

    primary_obj_norm = _norm_tag(teaching_steps[0]) if teaching_steps else ''

    # ── Per-objective competency (C4: exit-ticket only, single source of truth) ──
    # Match concept_tag → eo_text using normalized comparison. For the
    # lesson's PRIMARY canonical objective, also apply the lesson-level
    # rollup that competency_tracker._lesson_objective uses: every correct
    # answer in a per-lesson exit ticket attempt counts toward the
    # lesson's primary objective, regardless of the per-question tag
    # (which is intentionally fine-grained for tutor scaffolding but
    # never the cross-attempt aggregation key — see
    # apps/tutoring/competency_tracker.py:103-110).
    from apps.tutoring.competency import best_attempt
    objectives_data = []
    for eo_text in teaching_steps:
        eo_norm = _norm_tag(eo_text)
        is_primary = bool(eo_norm) and eo_norm == primary_obj_norm
        achieved = 0
        for sid in student_ids:
            student = User.objects.filter(id=sid).first()
            if not student:
                continue
            attempt = best_attempt(student, lesson)
            if not attempt or not attempt.answers:
                continue
            for ans in (attempt.answers if isinstance(attempt.answers, list) else []):
                if not isinstance(ans, dict):
                    continue
                if not ans.get('correct'):
                    continue
                ans_tag_norm = _norm_tag(ans.get('concept_tag', ''))
                if ans_tag_norm == eo_norm or is_primary:
                    achieved += 1
                    break
        not_achieved = total_students - achieved
        pct = round(achieved / total_students * 100) if total_students else 0
        objectives_data.append({
            'objective': eo_text,
            'objective_type': 'enabling',
            'achieved': achieved,
            'not_achieved': not_achieved,
            'total': total_students,
            'pct': pct,
            'avg_mastery': pct,
            'color': 'green' if pct >= 70 else ('yellow' if pct >= 40 else 'red'),
        })

    # ── Per-student competency ──
    students_data = []
    for sid in student_ids:
        student = User.objects.filter(id=sid).first()
        if not student:
            continue

        # Per-student EO achievement — mirrors the per-objective block
        # above: normalized concept_tag matching + primary-objective
        # rollup (every correct answer counts toward the lesson's
        # primary canonical objective). Single source of truth = the
        # exit ticket attempt; legacy fallbacks (covered_enabling_objectives
        # in engine_state, StudentSkillMastery) were removed per
        # memory/lesson_competency_plan.md.
        achieved_count = 0
        weak_objectives = []
        student_attempt = best_attempt(student, lesson)
        student_rows = (
            student_attempt.answers
            if student_attempt and isinstance(student_attempt.answers, list)
            else []
        )
        for eo_text in teaching_steps:
            eo_norm = _norm_tag(eo_text)
            is_primary = bool(eo_norm) and eo_norm == primary_obj_norm
            eo_achieved = False
            for ans in student_rows:
                if not isinstance(ans, dict):
                    continue
                if not ans.get('correct'):
                    continue
                ans_tag_norm = _norm_tag(ans.get('concept_tag', ''))
                if ans_tag_norm == eo_norm or is_primary:
                    eo_achieved = True
                    break

            if eo_achieved:
                achieved_count += 1
            else:
                weak_objectives.append(eo_text[:60])

        # Exit ticket score
        exit_attempt = ExitTicketAttempt.objects.filter(
            exit_ticket__lesson=lesson,
            student_id=sid,
        ).order_by('-completed_at').first()

        session = sessions.filter(student_id=sid).order_by('-started_at').first()

        # Competency percentage:
        # - When the student has taken the exit ticket, use the actual
        #   score / served-question-count. That's the real signal.
        # - Fall back to the objective-coverage ratio only when no exit
        #   attempt exists (and even then, no `pct` should imply BE —
        #   we treat that as Unassessed).
        # The earlier behaviour bucketed an 80% exit ticket as BE because
        # `total_objectives` was 0 → pct=0 → BE category. Fixed.
        if exit_attempt:
            served = (
                exit_attempt.exit_ticket.questions_per_attempt
                if exit_attempt.exit_ticket and exit_attempt.exit_ticket.questions_per_attempt
                else 10
            )
            pct = round(((exit_attempt.score or 0) / served) * 100) if served else 0
        else:
            pct = round(achieved_count / total_objectives * 100) if total_objectives else 0

        # Calculate exit ticket completion time
        exit_time_minutes = None
        if exit_attempt and exit_attempt.started_at and exit_attempt.completed_at:
            delta = (exit_attempt.completed_at - exit_attempt.started_at).total_seconds() / 60.0
            exit_time_minutes = round(delta, 1)

        # Categorize student (BE/AE/ME/EE) — but ONLY when there's an
        # actual exit-ticket attempt. Without one we have no real
        # competency signal, so calling them Below Expectation is
        # misleading; they're just Unassessed.
        if exit_attempt is None:
            category = {
                'code': 'UN',
                'label': 'Unassessed',
                'color': '#6b7280',
                'description': 'Has not yet taken the exit ticket.',
            }
        else:
            category = config.categorize_student(
                pct,
                exit_time_minutes if exit_attempt.passed else None,
            )

        students_data.append({
            'student': student,
            'achieved': achieved_count,
            'total': total_objectives,
            'pct': pct,
            'category': category,
            'exit_score': (
                f"{exit_attempt.score}/{(exit_attempt.exit_ticket.questions_per_attempt or 10) if exit_attempt.exit_ticket else 10}"
                if exit_attempt else '—'
            ),
            'exit_passed': exit_attempt.passed if exit_attempt else None,
            'exit_time': f"{exit_time_minutes:.0f} min" if exit_time_minutes else '—',
            'session_status': session.status if session else 'not_started',
            'weak_objectives': weak_objectives,
            'has_exit_attempt': exit_attempt is not None,
        })

    students_data.sort(key=lambda s: s['pct'])

    # Dedupe by display name — test environments accumulate multiple
    # User rows that share a full_name (e.g. several "Edward Amoah"
    # accounts). A student must appear in exactly ONE category bucket;
    # keep the row with the highest pct (so if one of the duplicates
    # has assessment data, that's the one we surface instead of an
    # unassessed twin).
    _by_name = {}
    for s in students_data:
        u = s['student']
        key = ((u.get_full_name() or u.username) or '').strip().lower()
        if not key:
            key = f"id:{u.id}"
        existing = _by_name.get(key)
        if existing is None or s['pct'] > existing['pct'] or (
            s['pct'] == existing['pct']
            and s['has_exit_attempt'] and not existing['has_exit_attempt']
        ):
            _by_name[key] = s
    students_data = sorted(_by_name.values(), key=lambda s: s['pct'])
    total_students = len(students_data)

    # ── Category counts ──
    category_counts = {'EE': 0, 'ME': 0, 'AE': 0, 'BE': 0, 'UN': 0}
    for s in students_data:
        category_counts[s['category']['code']] += 1

    # Split assessed vs unassessed so the move-on logic ignores
    # students who haven't taken the exit ticket — those students
    # aren't "below threshold", they just haven't been measured yet.
    assessed_students = [s for s in students_data if s['category']['code'] != 'UN']
    unassessed_students = [s for s in students_data if s['category']['code'] == 'UN']
    unassessed_count = len(unassessed_students)
    assessed_count = len(assessed_students)

    # ── Class competency: every assessed student must meet threshold ──
    move_on_threshold = config.threshold_move_on  # Default 70%
    students_below = [s for s in assessed_students if s['pct'] < move_on_threshold]
    all_assessed_above_threshold = len(students_below) == 0 and assessed_count > 0

    # Class average — only count assessed students. Including 0% from
    # unassessed students would drag the average down artificially.
    avg_pct = round(sum(s['pct'] for s in assessed_students) / assessed_count) if assessed_count else 0

    # ── Recommendation ──
    weak_objectives = [o for o in objectives_data if o['pct'] < 50]
    if assessed_count == 0 and unassessed_count > 0:
        # No one has taken the exit ticket yet.
        recommendation = (
            f"No students have taken the exit ticket yet. Wait for at least "
            f"{min(unassessed_count, 5)} student(s) to complete it, then re-run this report."
        )
        recommendation_type = 'warning'
        recommendation_action = 'wait'
    elif all_assessed_above_threshold and unassessed_count == 0:
        recommendation = (
            f"All {total_students} students have achieved at least {move_on_threshold}% of enabling objectives. "
            f"Class is ready to move to the next lesson."
        )
        recommendation_type = 'success'
        recommendation_action = 'proceed'
    elif all_assessed_above_threshold and unassessed_count > 0:
        # Everyone who took the ticket passed, but some haven't tried.
        recommendation = (
            f"All {assessed_count} assessed students have reached {move_on_threshold}%, but "
            f"{unassessed_count} student(s) still need to take the exit ticket before the class is fully ready."
        )
        recommendation_type = 'warning'
        recommendation_action = 'wait'
    elif len(students_below) <= 3 and total_students > 5:
        names = ', '.join(s['student'].get_full_name() or s['student'].username for s in students_below[:3])
        unassessed_note = (
            f" ({unassessed_count} also haven't taken the ticket.)" if unassessed_count else ""
        )
        recommendation = (
            f"{len(students_below)} assessed student(s) are below the {move_on_threshold}% threshold: {names}. "
            f"Consider targeted support for these students while moving on.{unassessed_note}"
        )
        recommendation_type = 'warning'
        recommendation_action = 'proceed_with_review'
    else:
        focus_areas = ', '.join(f"'{o['objective'][:50]}'" for o in weak_objectives[:3])
        unassessed_note = (
            f" {unassessed_count} student(s) haven't taken the exit ticket yet." if unassessed_count else ""
        )
        recommendation = (
            f"{len(students_below)}/{assessed_count} assessed students are below the {move_on_threshold}% threshold. "
            f"Recommend revisiting this lesson, focusing on: {focus_areas}.{unassessed_note}"
            if focus_areas else
            f"{len(students_below)}/{assessed_count} assessed students are below the {move_on_threshold}% threshold. "
            f"Recommend revisiting this lesson.{unassessed_note}"
        )
        recommendation_type = 'danger'
        recommendation_action = 'revisit'

    # Next lesson info
    next_lesson = Lesson.objects.filter(
        unit=lesson.unit,
        order_index__gt=lesson.order_index,
        is_published=True,
    ).order_by('order_index').first()
    if not next_lesson:
        next_lesson = Lesson.objects.filter(
            unit__course=course,
            unit__order_index__gt=lesson.unit.order_index,
            is_published=True,
        ).order_by('unit__order_index', 'order_index').first()

    # ── Group students by category with targeted instruction recommendations ──
    from collections import Counter

    category_groups = []
    for cat_code, cat_meta in [
        ('UN', {'label': 'Unassessed', 'color': '#6b7280', 'bg': '#f3f4f6', 'border': '#e5e7eb'}),
        ('BE', {'label': 'Below Expectation', 'color': '#dc2626', 'bg': '#fee2e2', 'border': '#fecaca'}),
        ('AE', {'label': 'Approaching Expectation', 'color': '#d97706', 'bg': '#fef3c7', 'border': '#fde68a'}),
        ('ME', {'label': 'Meeting Expectation', 'color': '#059669', 'bg': '#d1fae5', 'border': '#a7f3d0'}),
        ('EE', {'label': 'Exceeding Expectation', 'color': '#6366f1', 'bg': '#ede9fe', 'border': '#c4b5fd'}),
    ]:
        group_students = [s for s in students_data if s['category']['code'] == cat_code]
        if not group_students:
            continue

        # Find the most common weak objectives across this group
        group_weak = Counter()
        for s in group_students:
            for wo in s.get('weak_objectives', []):
                group_weak[wo] += 1
        common_weak = [obj for obj, _ in group_weak.most_common(5)]

        # Generate targeted instruction recommendation per group
        if cat_code == 'UN':
            instruction = (
                "These students haven't taken the exit ticket yet. "
                "Until they do, we have no competency data for them. "
                "Nudge them to finish their tutor session and submit the exit ticket."
            )
        elif cat_code == 'BE':
            if common_weak:
                instruction = (
                    f"These students need intensive support. Focus your next session on: "
                    f"{', '.join(common_weak[:3])}. "
                    f"Consider one-on-one or small-group instruction on these objectives."
                )
            else:
                instruction = "These students need intensive support across most objectives."
        elif cat_code == 'AE':
            if common_weak:
                instruction = (
                    f"These students are close to meeting expectations. A brief review of: "
                    f"{', '.join(common_weak[:3])} should help them cross the threshold. "
                    f"The AI tutor will continue remediation on these objectives."
                )
            else:
                instruction = "These students are progressing well. The AI tutor will continue targeted practice."
        elif cat_code == 'ME':
            instruction = (
                "These students have met expectations. They are ready for the next lesson. "
                "Consider offering extension activities or peer tutoring opportunities."
            )
        else:  # EE
            instruction = (
                "These students exceeded expectations. Consider giving them challenge problems, "
                "leadership roles in group work, or peer tutoring responsibilities."
            )

        category_groups.append({
            'code': cat_code,
            'label': cat_meta['label'],
            'color': cat_meta['color'],
            'bg': cat_meta['bg'],
            'border': cat_meta['border'],
            'students': group_students,
            'count': len(group_students),
            'common_weak': common_weak,
            'instruction': instruction,
        })

    # Group session breakdown (G6): for every session that had >1 active
    # participant, list the participants so teachers can see which lessons
    # were completed collaboratively.
    group_sessions = []
    for s in sessions.prefetch_related('participants__student').order_by('-started_at'):
        active = s.participants.filter(is_active=True)
        if active.count() > 1:
            group_sessions.append({
                'session': s,
                'participants': [p.student for p in active],
                'is_completed': s.status == TutorSession.Status.COMPLETED,
            })

    context = {
        **request.staff_ctx,
        'lesson': lesson,
        'unit': lesson.unit,
        'course': course,
        'total_students': total_students,
        'completed_count': completed_count,
        'total_objectives': total_objectives,
        'total_terminal': 0,
        'total_enabling': total_objectives,
        'objectives_data': objectives_data,
        'students_data': students_data,
        'category_groups': category_groups,
        'avg_pct': avg_pct,
        'category_counts': category_counts,
        'students_below_count': len(students_below),
        'unassessed_count': unassessed_count,
        'assessed_count': assessed_count,
        'move_on_threshold': move_on_threshold,
        'recommendation': recommendation,
        'recommendation_type': recommendation_type,
        'recommendation_action': recommendation_action,
        'next_lesson': next_lesson,
        'group_sessions': group_sessions,
    }

    return render(request, 'dashboard/lesson_session_report.html', context)


@teacher_required
@require_POST
def process_pending_materials(request, course_id):
    """Process teaching materials — supports single, batch, or all pending.

    Routes per-material by page count (MATERIAL_JOB_PAGE_THRESHOLD = 50):
      - Small (<50 pages): existing in-process daemon-thread pipeline.
      - Large (>=50 pages): Container Apps Job dispatch. The row goes to
        'pending' (not 'pending_confirmation') because the user is already
        clicking a "Process now" button — confirmation already implicit.
    """
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.dashboard.material_tasks import process_teaching_material, process_teaching_material_fast
    from apps.dashboard.material_routing import should_dispatch_to_job
    from apps.dashboard.background_tasks import run_async
    from apps.dashboard.job_dispatch import dispatch_material_job

    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    # Determine processing mode
    mode = request.POST.get('mode', 'rich')
    process_fn = process_teaching_material if mode == 'rich' else process_teaching_material_fast

    # Determine which materials to process
    specific_ids = request.POST.get('material_ids', '').strip()
    if specific_ids:
        # Process specific materials (single or batch)
        id_list = [int(x) for x in specific_ids.split(',') if x.strip().isdigit()]
        to_process = TeachingMaterialUpload.objects.filter(
            id__in=id_list, course=course
        )
    else:
        # Process all processable materials for this course
        # Link unlinked materials first
        from apps.dashboard.models import CurriculumUpload
        material_q = Q(course=course)
        if course.curriculum_upload_id:
            material_q |= Q(curriculum_upload_id=course.curriculum_upload_id)
        TeachingMaterialUpload.objects.filter(material_q, course__isnull=True).update(course=course)

        to_process = TeachingMaterialUpload.objects.filter(
            course=course,
        ).filter(
            Q(status='pending') | Q(status='pending_confirmation') |
            Q(status='processing', chunks_created=0) | Q(status='failed')
        )

    count = to_process.count()
    if count == 0:
        messages.info(request, "No materials to process.")
        return redirect('dashboard:course_detail', course_id=course.id)

    # Split by routing — same threshold (>=50 pages → Job).
    in_process_ids = []
    job_dispatched = 0
    job_dispatch_errors = []
    for material in to_process:
        use_job, page_count = should_dispatch_to_job(material.file_path)
        # Update page count on the row if missing (older uploads pre-P2)
        if page_count and material.pages_total != page_count:
            material.pages_total = page_count
        material.status = 'pending'
        material.error_message = ''
        material.save(update_fields=['status', 'pages_total', 'error_message'])
        if use_job:
            try:
                execution_name = dispatch_material_job(material.id, mode=mode)
                material.job_execution_name = execution_name[:128]
                material.save(update_fields=['job_execution_name'])
                job_dispatched += 1
            except Exception as exc:
                logger.error(f"Job dispatch failed for material {material.id}: {exc}")
                material.status = 'failed'
                material.error_message = f"Job dispatch failed: {exc}"
                material.save(update_fields=['status', 'error_message'])
                job_dispatch_errors.append(material.title)
        else:
            in_process_ids.append(material.id)

    mode_label = "Rich (LLM Vision)" if mode == 'rich' else "Fast (Text Only)"

    if in_process_ids:
        TeachingMaterialUpload.objects.filter(id__in=in_process_ids).update(status='processing')

        def _process_materials(ids, fn):
            import django.db
            django.db.connections.close_all()
            for i, mid in enumerate(ids):
                try:
                    print(f"[ProcessMaterials] {mode_label}: material {mid} ({i+1}/{len(ids)})", flush=True)
                    fn(mid)
                    print(f"[ProcessMaterials] Done {mid}", flush=True)
                except Exception as e:
                    print(f"[ProcessMaterials] Material {mid} failed: {e}", flush=True)

        run_async(_process_materials, in_process_ids, process_fn)

    parts = []
    if in_process_ids:
        parts.append(f"{len(in_process_ids)} small file(s) processing in-app ({mode_label})")
    if job_dispatched:
        parts.append(f"{job_dispatched} large file(s) dispatched to Container Apps Job")
    if job_dispatch_errors:
        parts.append(f"{len(job_dispatch_errors)} dispatch failures: {', '.join(job_dispatch_errors[:3])}")
    messages.success(request, "; ".join(parts) if parts else f"Processing {count} material(s).")
    return redirect('dashboard:course_detail', course_id=course.id)


# ============================================================================
# Teaching Materials
# ============================================================================

@staff_required
def material_process(request, upload_id):
    """Show processing status for a teaching material upload. Handles edit via POST."""
    from apps.dashboard.models import TeachingMaterialUpload

    institution = request.staff_ctx['institution']
    if institution is not None:
        upload = get_object_or_404(
            TeachingMaterialUpload,
            Q(institution=institution) | Q(institution__isnull=True),
            id=upload_id,
        )
    else:
        upload = get_object_or_404(TeachingMaterialUpload, id=upload_id)

    # Handle edit POST
    if request.method == 'POST' and request.POST.get('action') == 'update':
        upload.title = request.POST.get('title', upload.title).strip()
        upload.material_type = request.POST.get('material_type', upload.material_type)
        upload.grade_level = request.POST.get('grade_level', upload.grade_level)
        upload.description = request.POST.get('description', upload.description).strip()
        upload.save()
        messages.success(request, "Material updated.")
        return redirect('dashboard:material_process', upload_id=upload.id)

    # Decode structured failure JSON if present (P1 onwards). Older rows
    # may carry plain-string error_message — leave error_detail=None and
    # the template renders the raw string.
    error_detail = None
    if upload.error_message and upload.error_message.startswith('{'):
        import json as _json
        try:
            error_detail = _json.loads(upload.error_message)
        except (ValueError, TypeError):
            error_detail = None

    # Cost estimate + verdict — shown on the confirm screen for
    # pending_confirmation rows so the user sees price BEFORE clicking confirm.
    cost_estimate = None
    cost_verdict_label = None
    if upload.status == 'pending_confirmation':
        from apps.dashboard.cost_estimator import (
            estimate_material_cost, cost_verdict,
            COST_WARN_THRESHOLD_USD, COST_HARD_BLOCK_USD,
        )
        try:
            cost_estimate = estimate_material_cost(
                upload.file_path,
                mode='rich',
                page_count=upload.pages_total or None,
            )
            cost_verdict_label = cost_verdict(cost_estimate['estimated_cost_usd'])
        except Exception as exc:
            logger.warning(f"cost estimate failed for upload {upload.id}: {exc}")

    context = {
        **request.staff_ctx,
        'upload': upload,
        'error_detail': error_detail,
        'cost_estimate': cost_estimate,
        'cost_verdict_label': cost_verdict_label,
        'material_types': TeachingMaterialUpload.MaterialType.choices,
        'is_super_admin': request.staff_ctx.get('role') == 'superadmin',
    }
    return render(request, 'dashboard/materials/process.html', context)


@require_POST
@teacher_required
def course_upload_material(request, course_id):
    """Upload teaching materials to a course with processing mode choice."""
    import os
    from django.conf import settings as django_settings
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.dashboard.material_tasks import process_teaching_material, process_teaching_material_fast
    from apps.dashboard.material_routing import should_dispatch_to_job, MATERIAL_JOB_PAGE_THRESHOLD
    from apps.dashboard.cost_estimator import estimate_material_cost
    from apps.dashboard.background_tasks import run_async
    from apps.dashboard.job_dispatch import dispatch_material_job

    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    uploaded_files = request.FILES.getlist('material_files') or [request.FILES.get('material_file')]
    uploaded_files = [f for f in uploaded_files if f]
    title = request.POST.get('material_title', '').strip()
    material_type = request.POST.get('material_type', 'textbook')
    processing_mode = request.POST.get('processing_mode', 'rich')
    description = request.POST.get('material_description', '').strip()

    if not uploaded_files or not title:
        messages.error(request, "File and title are required.")
        return redirect('dashboard:course_detail', course_id=course.id)

    upload_dir = os.path.join(django_settings.MEDIA_ROOT, 'material_uploads')
    os.makedirs(upload_dir, exist_ok=True)

    # Split uploads into in-process (small) and Job-bound (large) by page count.
    # Same threshold (MATERIAL_JOB_PAGE_THRESHOLD = 50) regardless of material
    # type. Job-bound rows are created with status='pending_confirmation' so the
    # cost-confirm screen can intercept them; in-process rows go straight to
    # 'pending' and dispatch to the existing background-thread pipeline.
    in_process_ids = []
    job_bound_ids = []
    for uploaded_file in uploaded_files:
        file_path = os.path.join(upload_dir, uploaded_file.name)
        with open(file_path, 'wb+') as dest:
            for chunk in uploaded_file.chunks():
                dest.write(chunk)

        file_title = f"{title} - {os.path.splitext(uploaded_file.name)[0]}" if len(uploaded_files) > 1 else title

        use_job, page_count = should_dispatch_to_job(file_path)
        # For Job-bound uploads compute the cost estimate up front so the
        # confirm screen can render it without re-opening the PDF.
        est_cost = None
        est_duration = None
        if use_job:
            est = estimate_material_cost(file_path, mode=processing_mode, page_count=page_count)
            est_cost = est['estimated_cost_usd']
            est_duration = est['estimated_duration_seconds']

        material_record = TeachingMaterialUpload.objects.create(
            institution=course.institution,
            uploaded_by=request.user,
            file_path=file_path,
            original_filename=uploaded_file.name,
            title=file_title,
            subject_name=course.title,
            grade_level=course.grade_level or '',
            material_type=material_type,
            description=description,
            course=course,
            pages_total=page_count,
            estimated_cost_usd=est_cost,
            estimated_duration_seconds=est_duration,
            status='pending_confirmation' if use_job else 'pending',
        )
        if use_job:
            job_bound_ids.append(material_record.id)
        else:
            in_process_ids.append(material_record.id)

    # In-process path — existing daemon-thread dispatch (no behavior change for small files).
    if in_process_ids:
        process_fn = process_teaching_material if processing_mode == 'rich' else process_teaching_material_fast

        def _process_materials(ids, fn):
            import django.db
            django.db.connections.close_all()
            for mid in ids:
                try:
                    print(f"[UploadMaterial] Processing {mid} mode={processing_mode}", flush=True)
                    fn(mid)
                    print(f"[UploadMaterial] Done {mid}", flush=True)
                except Exception as e:
                    print(f"[UploadMaterial] FAILED {mid}: {e}", flush=True)
                    import traceback; traceback.print_exc()

        run_async(_process_materials, in_process_ids, process_fn)

    mode_label = "Rich (LLM Vision)" if processing_mode == 'rich' else "Fast (Text Only)"
    parts = []
    if in_process_ids:
        parts.append(f"{len(in_process_ids)} small file(s) processing now in {mode_label} mode")
    if job_bound_ids:
        parts.append(
            f"{len(job_bound_ids)} large file(s) (>={MATERIAL_JOB_PAGE_THRESHOLD} pages) "
            "awaiting your confirmation — see materials list"
        )
    messages.success(request, "; ".join(parts) if parts else "Nothing uploaded.")
    return redirect('dashboard:course_detail', course_id=course.id)


@teacher_required
@require_POST
def material_confirm_processing(request, upload_id):
    """Confirm a Job-bound material upload and dispatch to Container Apps Job.

    Flips status pending_confirmation → pending and starts the Job. Mode
    (rich/fast) comes from POST body; defaults to rich. Cost guardrails:
      - estimate <= $10 : auto-confirm
      - $10 < estimate <= $50 : confirm but log warning
      - estimate > $50 : hard-block unless ?force=1 AND user is super-admin
    Persists the Job execution name on the upload row for debugging.
    """
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.dashboard.job_dispatch import dispatch_material_job
    from apps.dashboard.cost_estimator import (
        estimate_material_cost, cost_verdict,
        COST_WARN_THRESHOLD_USD, COST_HARD_BLOCK_USD,
    )

    institution = request.staff_ctx['institution']
    if institution is not None:
        upload = get_object_or_404(
            TeachingMaterialUpload,
            Q(institution=institution) | Q(institution__isnull=True),
            id=upload_id,
        )
    else:
        upload = get_object_or_404(TeachingMaterialUpload, id=upload_id)

    if upload.status not in ('pending_confirmation', 'failed', 'pending'):
        messages.error(
            request,
            f"Upload {upload.title!r} is not awaiting confirmation (status={upload.status})."
        )
        return redirect('dashboard:material_process', upload_id=upload.id)

    mode = request.POST.get('mode', 'rich')
    if mode not in ('rich', 'fast'):
        mode = 'rich'

    # Re-estimate at confirm time so the guardrail uses the latest pricing
    # / model config (estimate stored on the row may be stale if the model
    # changed between upload and confirm).
    est = estimate_material_cost(upload.file_path, mode=mode, page_count=upload.pages_total or None)
    cost_usd = est['estimated_cost_usd']
    upload.estimated_cost_usd = cost_usd
    upload.estimated_duration_seconds = est['estimated_duration_seconds']

    is_super_admin = request.staff_ctx.get('role') == 'superadmin'
    force = request.POST.get('force') == '1'

    verdict = cost_verdict(cost_usd)
    if verdict == 'red' and not (is_super_admin and force):
        messages.error(
            request,
            f"Estimated cost ${cost_usd} exceeds the hard-block threshold (${COST_HARD_BLOCK_USD}). "
            "Contact a super-admin to force-confirm."
        )
        upload.save(update_fields=['estimated_cost_usd', 'estimated_duration_seconds'])
        return redirect('dashboard:material_process', upload_id=upload.id)
    if verdict == 'yellow':
        messages.warning(
            request,
            f"Heads-up: estimated cost ${cost_usd} is above ${COST_WARN_THRESHOLD_USD} — proceeding anyway."
        )

    upload.status = 'pending'
    upload.error_message = ''
    upload.save(update_fields=['status', 'error_message', 'estimated_cost_usd', 'estimated_duration_seconds'])
    upload.add_log(
        f"Confirmed for {mode} processing (est. ${cost_usd}, "
        f"{est['estimated_duration_seconds']}s) → dispatching Container Apps Job"
    )

    try:
        execution_name = dispatch_material_job(upload.id, mode=mode)
        upload.job_execution_name = execution_name[:128]
        upload.save(update_fields=['job_execution_name'])
        messages.success(
            request,
            f"Started processing {upload.title!r} (est. ${cost_usd}) → execution {execution_name}"
        )
    except Exception as exc:
        # Dispatch failed → put it back in pending_confirmation so the user
        # can retry. Don't mark 'failed' — the job didn't even start.
        upload.status = 'pending_confirmation'
        upload.error_message = f"Job dispatch failed: {exc}"
        upload.save(update_fields=['status', 'error_message'])
        upload.add_log(f"Dispatch failed: {exc}")
        messages.error(request, f"Failed to start processing job: {exc}")

    return redirect('dashboard:material_process', upload_id=upload.id)


@teacher_required
@require_POST
def material_delete(request, material_id):
    """Delete a teaching material and its vector chunks."""
    from apps.dashboard.models import TeachingMaterialUpload

    institution = request.staff_ctx['institution']
    if institution is not None:
        material = get_object_or_404(
            TeachingMaterialUpload,
            Q(institution=institution) | Q(institution__isnull=True),
            id=material_id,
        )
    else:
        material = get_object_or_404(TeachingMaterialUpload, id=material_id)

    course = material.course
    title = material.title

    # Delete the file
    import os
    if material.file_path and os.path.exists(material.file_path):
        try:
            os.remove(material.file_path)
        except OSError:
            pass

    material.delete()
    messages.success(request, f"Deleted material: {title}")

    if course:
        return redirect('dashboard:course_detail', course_id=course.id)
    return redirect('dashboard:curriculum_list')


# ============================================================================
# Settings
# ============================================================================

@teacher_required
def settings_page(request):
    """Institution settings — general for all staff, theme + prompts for superadmins."""
    institution = request.staff_ctx['institution']
    membership = request.staff_ctx['membership']
    is_superadmin = request.user.is_staff

    if request.method == 'POST':
        action = request.POST.get('action', 'general')

        if action == 'general' and institution is not None and is_superadmin:
            institution.name = request.POST.get('name', institution.name)
            institution.timezone = request.POST.get('timezone', institution.timezone)
            institution.save()
            messages.success(request, "Settings updated.")

        elif action == 'account':
            first_name = request.POST.get('first_name', '').strip()
            last_name = request.POST.get('last_name', '').strip()
            email = request.POST.get('email', '').strip()
            new_school = (request.POST.get('school') or '').strip()
            if not email:
                messages.error(request, "Email is required.")
            elif User.objects.filter(email=email).exclude(pk=request.user.pk).exists():
                messages.error(request, "That email is already in use by another account.")
            else:
                request.user.first_name = first_name
                request.user.last_name = last_name
                request.user.email = email
                request.user.save()

                # School (institution) update — re-points the active
                # membership. New value comes from the school dropdown
                # populated from active institutions.
                if new_school and membership:
                    new_inst = (
                        Institution.objects.filter(id=new_school, is_active=True).first()
                        or Institution.objects.filter(slug=new_school, is_active=True).first()
                    )
                    if new_inst and new_inst.id != membership.institution_id:
                        membership.institution = new_inst
                        membership.save(update_fields=['institution'])

                messages.success(request, "Profile updated.")

        elif action == 'password':
            current_password = request.POST.get('current_password', '')
            new_password = request.POST.get('new_password', '')
            confirm_password = request.POST.get('confirm_password', '')
            if not request.user.check_password(current_password):
                messages.error(request, "Current password is incorrect.")
            elif len(new_password) < 6:
                messages.error(request, "New password must be at least 6 characters.")
            elif new_password != confirm_password:
                messages.error(request, "New passwords do not match.")
            else:
                request.user.set_password(new_password)
                request.user.save()
                update_session_auth_hash(request, request.user)
                messages.success(request, "Password changed successfully.")

        elif action == 'delete_account':
            if request.user.is_staff:
                messages.error(request, "Super Admin accounts cannot be self-deleted.")
            else:
                from apps.safety import DataPrivacy
                DataPrivacy.delete_user_data(request.user, keep_anonymized=True)
                request.user.delete()
                logout(request)
                return redirect('accounts:landing')

        elif action == 'competency' and is_superadmin:
            platform_config = PlatformConfig.load()
            try:
                platform_config.threshold_be_max = int(request.POST.get('threshold_be_max', 50))
                platform_config.threshold_ae_max = int(request.POST.get('threshold_ae_max', 80))
                platform_config.threshold_me_min = int(request.POST.get('threshold_me_min', 80))
                platform_config.threshold_ee_time_minutes = int(request.POST.get('threshold_ee_time', 5))
                platform_config.threshold_move_on = int(request.POST.get('threshold_move_on', 70))
                platform_config.teachers_can_edit_content = request.POST.get('teachers_can_edit') == '1'
                platform_config.save()
                messages.success(request, "Competency thresholds updated.")
            except (ValueError, TypeError):
                messages.error(request, "Invalid threshold values.")

        elif action == 'theme' and is_superadmin:
            platform_config = PlatformConfig.load()
            platform_config.platform_name = request.POST.get('platform_name', platform_config.platform_name)
            if request.FILES.get('logo'):
                platform_config.logo = request.FILES['logo']
            if request.POST.get('clear_logo') == '1':
                platform_config.logo = None
            platform_config.primary_color = request.POST.get('primary_color', platform_config.primary_color)
            platform_config.secondary_color = request.POST.get('secondary_color', platform_config.secondary_color)
            platform_config.accent_color = request.POST.get('accent_color', platform_config.accent_color)
            platform_config.save()
            messages.success(request, "Theme updated.")

        elif action == 'add_school' and is_superadmin:
            school_name = request.POST.get('school_name', '').strip()
            school_slug = request.POST.get('school_slug', '').strip()
            school_tz = request.POST.get('school_timezone', 'UTC')
            if school_name and school_slug:
                if Institution.objects.filter(slug=school_slug).exists():
                    messages.error(request, f"A school with slug '{school_slug}' already exists.")
                else:
                    Institution.objects.create(
                        name=school_name,
                        slug=school_slug,
                        timezone=school_tz,
                        is_active=True,
                    )
                    messages.success(request, f"School '{school_name}' created.")
            else:
                messages.error(request, "School name and slug are required.")

        elif action == 'toggle_user' and is_superadmin:
            user_id = request.POST.get('user_id')
            if user_id:
                target = User.objects.filter(id=user_id).first()
                if target and target != request.user and not target.is_staff:
                    target.is_active = not target.is_active
                    target.save(update_fields=['is_active'])
                    Membership.objects.filter(user=target).update(is_active=target.is_active)
                    status = "activated" if target.is_active else "deactivated"
                    messages.success(request, f"User '{target.get_full_name() or target.email}' {status}.")
                else:
                    messages.error(request, "Cannot modify this user.")

        elif action == 'delete_user' and is_superadmin:
            user_id = request.POST.get('user_id')
            if user_id:
                target = User.objects.filter(id=user_id).first()
                if target and target != request.user and not target.is_staff:
                    name = target.get_full_name() or target.email
                    target.delete()
                    messages.success(request, f"User '{name}' deleted.")
                else:
                    messages.error(request, "Cannot delete this user.")

        elif action == 'create_admin' and is_superadmin:
            admin_email = request.POST.get('admin_email', '').strip()
            admin_first = request.POST.get('admin_first_name', '').strip()
            admin_last = request.POST.get('admin_last_name', '').strip()
            admin_password = request.POST.get('admin_password', '').strip()
            if not admin_email or not admin_password:
                messages.error(request, "Email and password are required.")
            elif User.objects.filter(email=admin_email).exists():
                messages.error(request, f"A user with email '{admin_email}' already exists.")
            else:
                new_admin = User.objects.create_user(
                    username=admin_email,
                    email=admin_email,
                    password=admin_password,
                    first_name=admin_first,
                    last_name=admin_last,
                    is_staff=True,
                )
                messages.success(request, f"Super Admin '{new_admin.get_full_name() or admin_email}' created.")

        elif action == 'toggle_admin' and is_superadmin:
            user_id = request.POST.get('user_id')
            if user_id:
                target = User.objects.filter(id=user_id).first()
                if target and target != request.user:
                    target.is_staff = not target.is_staff
                    target.save(update_fields=['is_staff'])
                    if target.is_staff:
                        messages.success(request, f"'{target.get_full_name() or target.email}' promoted to Super Admin.")
                    else:
                        messages.success(request, f"'{target.get_full_name() or target.email}' demoted from Super Admin.")
                else:
                    messages.error(request, "Cannot modify your own admin status.")

        elif action == 'toggle_school' and is_superadmin:
            school_id = request.POST.get('school_id')
            if school_id:
                school = Institution.objects.filter(id=school_id).first()
                if school:
                    school.is_active = not school.is_active
                    school.save()
                    status = "activated" if school.is_active else "deactivated"
                    messages.success(request, f"School '{school.name}' {status}.")

        elif action == 'grades' and is_superadmin:
            platform_config = PlatformConfig.load()
            grades_json = request.POST.get('grades_json', '[]')
            try:
                platform_config.grades = json.loads(grades_json)
                platform_config.save()
                messages.success(request, "Grade levels updated.")
            except json.JSONDecodeError:
                messages.error(request, "Invalid data format. Please try again.")

        elif action == 'ai_model' and is_superadmin:
            from apps.llm.models import ModelConfig

            tutor_provider = request.POST.get('tutor_provider', '').strip()
            tutor_model = request.POST.get('tutor_model', '').strip()
            tutor_api_key = request.POST.get('tutor_api_key', '').strip()

            gen_provider = request.POST.get('gen_provider', '').strip()
            gen_model = request.POST.get('gen_model', '').strip()
            gen_api_key = request.POST.get('gen_api_key', '').strip()

            img_provider = request.POST.get('img_provider', '').strip()
            img_model = request.POST.get('img_model', '').strip()
            img_api_key = request.POST.get('img_api_key', '').strip()

            valid_providers = [p[0] for p in ModelConfig.Provider.choices]
            all_providers_valid = all(
                p in valid_providers for p in [tutor_provider, gen_provider, img_provider]
            )
            if not all_providers_valid:
                messages.error(request, "Invalid provider.")
            elif not tutor_model or not gen_model or not img_model:
                messages.error(request, "Model name is required for all purposes.")
            else:
                env_var_map = {
                    'anthropic': 'ANTHROPIC_API_KEY',
                    'openai': 'OPENAI_API_KEY',
                    'google': 'GOOGLE_API_KEY',
                    'azure_openai': 'AZURE_OPENAI_API_KEY',
                    'local_ollama': '',
                }
                inst = institution or Institution.objects.filter(is_active=True).first()

                # Deactivate all existing configs
                ModelConfig.objects.filter(is_active=True).update(is_active=False)

                # Tutoring config (also used for exit_tickets, skill_extraction)
                for purpose in ['tutoring', 'exit_tickets', 'skill_extraction']:
                    config = ModelConfig.objects.create(
                        institution=inst,
                        name=f"{tutor_provider.title()} - {purpose}",
                        provider=tutor_provider,
                        model_name=tutor_model,
                        api_key_env_var=env_var_map.get(tutor_provider, ''),
                        purpose=purpose,
                        is_active=True,
                    )
                    if tutor_api_key:
                        config.set_api_key(tutor_api_key)
                        config.save()

                # Generation config
                config = ModelConfig.objects.create(
                    institution=inst,
                    name=f"{gen_provider.title()} - generation",
                    provider=gen_provider,
                    model_name=gen_model,
                    api_key_env_var=env_var_map.get(gen_provider, ''),
                    purpose='generation',
                    is_active=True,
                )
                if gen_api_key:
                    config.set_api_key(gen_api_key)
                    config.save()

                # Image generation config
                # If no dedicated key provided, inherit from whichever config shares the same provider
                img_key_to_use = img_api_key
                if not img_key_to_use and img_provider == gen_provider:
                    img_key_to_use = gen_api_key
                if not img_key_to_use and img_provider == tutor_provider:
                    img_key_to_use = tutor_api_key

                config = ModelConfig.objects.create(
                    institution=inst,
                    name=f"{img_provider.title()} - image_generation",
                    provider=img_provider,
                    model_name=img_model,
                    api_key_env_var=env_var_map.get(img_provider, ''),
                    purpose='image_generation',
                    is_active=True,
                )
                if img_key_to_use:
                    config.set_api_key(img_key_to_use)
                    config.save()

                messages.success(request, f"AI models updated — Tutoring: {tutor_provider}/{tutor_model}, Generation: {gen_provider}/{gen_model}, Image: {img_provider}/{img_model}.")

        elif action == 'add_personality' and is_superadmin:
            from apps.accounts.models import TutorPersonality
            p_name = request.POST.get('personality_name', '').strip()
            p_emoji = request.POST.get('personality_emoji', '').strip()
            p_desc = request.POST.get('personality_description', '').strip()
            p_prompt = request.POST.get('personality_prompt', '').strip()
            if not p_name or not p_prompt:
                messages.error(request, "Name and prompt modifier are required.")
            elif TutorPersonality.objects.filter(name=p_name).exists():
                messages.error(request, f"A personality named '{p_name}' already exists.")
            else:
                TutorPersonality.objects.create(
                    name=p_name, emoji=p_emoji,
                    description=p_desc, system_prompt_modifier=p_prompt,
                )
                messages.success(request, f"Personality '{p_name}' created.")

        elif action == 'toggle_personality' and is_superadmin:
            from apps.accounts.models import TutorPersonality
            pid = request.POST.get('personality_id')
            p = TutorPersonality.objects.filter(id=pid).first()
            if p:
                p.is_active = not p.is_active
                p.save(update_fields=['is_active'])
                status = "activated" if p.is_active else "deactivated"
                messages.success(request, f"Personality '{p.name}' {status}.")

        elif action == 'delete_personality' and is_superadmin:
            from apps.accounts.models import TutorPersonality
            pid = request.POST.get('personality_id')
            p = TutorPersonality.objects.filter(id=pid).first()
            if p:
                name = p.name
                p.delete()
                messages.success(request, f"Personality '{name}' deleted.")

        elif action == 'prompts' and is_superadmin:
            from apps.llm.models import PromptPack
            prompt_pack = PromptPack.objects.filter(
                institution__isnull=True, is_active=True
            ).first()
            if not prompt_pack:
                prompt_pack = PromptPack.objects.create(
                    institution=None,
                    name='Default',
                    system_prompt='',
                    is_active=True,
                )
            prompt_pack.tutor_system_prompt = request.POST.get('tutor_system_prompt', '')
            prompt_pack.content_generation_prompt = request.POST.get('content_generation_prompt', '')
            prompt_pack.exit_ticket_prompt = request.POST.get('exit_ticket_prompt', '')
            prompt_pack.grading_prompt = request.POST.get('grading_prompt', '')
            prompt_pack.image_generation_prompt = request.POST.get('image_generation_prompt', '')
            prompt_pack.safety_prompt = request.POST.get('safety_prompt', '')
            prompt_pack.save()
            messages.success(request, "AI prompts updated.")

        return redirect('dashboard:settings')

    # Load prompt pack and prompt defaults for display
    prompt_pack = None
    prompt_fields = []
    platform_config = None
    if is_superadmin:
        from apps.llm.models import PromptPack
        prompt_pack = PromptPack.objects.filter(
            institution__isnull=True, is_active=True
        ).first()

        from apps.llm.prompts import get_prompt_defaults
        PROMPT_DEFAULTS = get_prompt_defaults()

        # Build structured list for template: (field_name, label, desc, default, current)
        field_meta = [
            ('tutor_system_prompt', 'Tutor System Prompt', 'The main system prompt for the conversational tutor.'),
            ('safety_prompt', 'Safety Prompt', 'Safety guidelines injected into the tutor prompt.'),
            ('content_generation_prompt', 'Content Generation Prompt', 'System prompt for AI-generated lesson content.'),
            ('exit_ticket_prompt', 'Exit Ticket Prompt', 'System prompt for exit ticket question generation.'),
            ('grading_prompt', 'Grading Prompt', 'System prompt for AI answer grading.'),
            ('image_generation_prompt', 'Image Generation Context', 'Prefix added to all image generation prompts.'),
        ]
        for fname, label, desc in field_meta:
            current = getattr(prompt_pack, fname, '') if prompt_pack else ''
            default_value = PROMPT_DEFAULTS.get(fname, '')
            prompt_fields.append({
                'name': fname,
                'label': label,
                'desc': desc,
                'default': default_value,
                'current': current or default_value,
            })

        platform_config = PlatformConfig.load()

    # AI Model config context (superadmin only) — per-purpose
    tutor_provider = 'google'
    tutor_model = 'gemini-3.1-pro-preview'
    has_tutor_db_key = False
    has_tutor_env_key = False
    gen_provider = 'google'
    gen_model = 'gemini-3.1-pro-preview'
    has_gen_db_key = False
    has_gen_env_key = False
    img_provider = 'google'
    img_model = 'gemini-3.1-flash-image-preview'
    has_img_db_key = False
    has_img_env_key = False
    provider_choices = []
    provider_defaults_json = '{}'
    img_provider_defaults_json = '{}'
    if is_superadmin:
        from apps.llm.models import ModelConfig
        tutor_config = ModelConfig.objects.filter(is_active=True, purpose='tutoring').first()
        if tutor_config:
            tutor_provider = tutor_config.provider
            tutor_model = tutor_config.model_name
            has_tutor_db_key = bool(tutor_config.api_key_encrypted)
            has_tutor_env_key = bool(os.getenv(tutor_config.api_key_env_var or '', ''))
        gen_config = ModelConfig.objects.filter(is_active=True, purpose='generation').first()
        if gen_config:
            gen_provider = gen_config.provider
            gen_model = gen_config.model_name
            has_gen_db_key = bool(gen_config.api_key_encrypted)
            has_gen_env_key = bool(os.getenv(gen_config.api_key_env_var or '', ''))
        img_config = ModelConfig.objects.filter(is_active=True, purpose='image_generation').first()
        if img_config:
            img_provider = img_config.provider
            img_model = img_config.model_name
            has_img_db_key = bool(img_config.api_key_encrypted)
            has_img_env_key = bool(os.getenv(img_config.api_key_env_var or '', ''))
        provider_choices = ModelConfig.Provider.choices
        provider_defaults_json = json.dumps({
            'anthropic': 'claude-sonnet-4-20250514',
            'openai': 'gpt-4o',
            'google': 'gemini-3.1-pro-preview',
            'azure_openai': 'gpt-4o',
            'local_ollama': 'llama3',
        })
        img_provider_defaults_json = json.dumps({
            'openai': 'gpt-image-2',
            'google': 'gemini-3.1-flash-image-preview',
        })

    # Tutor personalities (superadmin)
    personalities = []
    if is_superadmin:
        from apps.accounts.models import TutorPersonality
        personalities = TutorPersonality.objects.all()

    all_timezones = sorted(zoneinfo.available_timezones())
    all_schools = Institution.objects.exclude(slug=Institution.GLOBAL_SLUG).order_by('name') if is_superadmin else []
    # Active schools list for the user's own profile-school dropdown
    # (every staff member can change their own school regardless of
    # whether they're a superadmin).
    user_school_choices = list(
        Institution.objects.filter(is_active=True)
        .exclude(slug=Institution.GLOBAL_SLUG)
        .order_by('name')
        .values('id', 'name')
    )
    all_users = (
        User.objects.exclude(id=request.user.id)
        .filter(
            Q(is_staff=True) |
            Q(memberships__role='staff')
        )
        .distinct()
        .prefetch_related('memberships__institution')
        .order_by('-is_staff', 'last_name', 'first_name')
    ) if is_superadmin else []

    context = {
        **request.staff_ctx,
        'is_superadmin': is_superadmin,
        'prompt_pack': prompt_pack,
        'prompt_fields': prompt_fields,
        'platform_config': platform_config,
        'all_timezones': all_timezones,
        'all_schools': all_schools,
        'user_school_choices': user_school_choices,
        'all_users': all_users,
        'tutor_provider': tutor_provider,
        'tutor_model': tutor_model,
        'has_tutor_db_key': has_tutor_db_key,
        'has_tutor_env_key': has_tutor_env_key,
        'gen_provider': gen_provider,
        'gen_model': gen_model,
        'has_gen_db_key': has_gen_db_key,
        'has_gen_env_key': has_gen_env_key,
        'img_provider': img_provider,
        'img_model': img_model,
        'has_img_db_key': has_img_db_key,
        'has_img_env_key': has_img_env_key,
        'provider_choices': provider_choices,
        'provider_defaults_json': provider_defaults_json,
        'img_provider_defaults_json': img_provider_defaults_json,
        'personalities': personalities,
    }

    return render(request, 'dashboard/settings.html', context)


# ============================================================================
# Lesson Review & Content Management
# ============================================================================

@teacher_required
def lesson_detail(request, lesson_id):
    """Review lesson steps, media, and exit ticket."""
    from apps.curriculum.models import Lesson, LessonStep
    from apps.tutoring.models import ExitTicket, TutorSession

    institution = request.staff_ctx['institution']

    if institution is not None:
        lesson = get_object_or_404(
            Lesson,
            Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True),
            id=lesson_id,
        )
    else:
        lesson = get_object_or_404(Lesson, id=lesson_id)

    # Auto-fix legacy 40-min lessons
    if lesson.estimated_minutes == 40:
        lesson.estimated_minutes = 20
        lesson.save(update_fields=['estimated_minutes'])

    # Get all steps
    steps = lesson.steps.all().order_by('order_index')
    
    # Count media (steps that have media with URLs)
    media_count = 0
    for step in steps:
        if step.media and step.media.get('images'):
            for img in step.media['images']:
                if img.get('url'):
                    media_count += 1
    
    # Get exit ticket
    exit_ticket = ExitTicket.objects.filter(lesson=lesson).first()
    exit_ticket_count = 0
    exit_questions = []
    untagged_eo_count = 0
    if exit_ticket:
        exit_questions = list(exit_ticket.questions.all().order_by('order_index'))
        exit_ticket_count = len(exit_questions)
        # Pre-serialize plot_spec to a JSON string so the template can
        # drop it straight into a textarea for editing.
        for q in exit_questions:
            if q.answer_data and isinstance(q.answer_data, dict):
                spec = q.answer_data.get('plot_spec')
                if spec:
                    q.plot_spec_json = json.dumps(spec, indent=2, ensure_ascii=False)
            if not (q.enabling_objective or '').strip():
                untagged_eo_count += 1
    
    # Students who completed
    students_completed = TutorSession.objects.filter(
        lesson=lesson,
        status='completed'
    ).values('student').distinct().count()

    # Prerequisites
    from apps.tutoring.skills_models import LessonPrerequisite
    course = lesson.unit.course
    prerequisites = LessonPrerequisite.objects.filter(
        lesson=lesson, is_direct=True
    ).select_related('prerequisite')
    available_lessons = Lesson.objects.filter(
        unit__course=course, is_published=True
    ).exclude(id=lesson.id).order_by('unit__order_index', 'order_index')

    # Enabling objectives: lesson.enabling_objectives is the canonical
    # granular list (populated by _expand_to_granular_subskills during
    # generation). Fall back to lesson.metadata['teaching_steps'] then
    # to step-level enabling_objective fields for legacy lessons.
    enabling_objectives = list(lesson.enabling_objectives or [])
    if not enabling_objectives:
        enabling_objectives = list(
            (lesson.metadata or {}).get('teaching_steps', []) or []
        )
    if not enabling_objectives:
        seen = set()
        for step in steps:
            eo = getattr(step, 'enabling_objective', '') or ''
            if eo and eo not in seen:
                seen.add(eo)
                enabling_objectives.append(eo)
    # Real terminal objectives live on the parent UNIT — distinct from
    # the lesson's granular EOs. Without this the template was
    # mislabelling lesson.enabling_objectives as "Terminal Objectives".
    unit_terminal_objectives = list(
        (lesson.unit.terminal_objectives or []) if lesson.unit else []
    )
    # Keep teaching_steps in context for backward compat with any
    # downstream JS that still reads it.
    teaching_steps = enabling_objectives

    # Auto-recover orphaned 'generating' state. Daemon background threads
    # die with the gunicorn worker, so a deploy mid-regen leaves the
    # lesson stuck. Mirror the course_detail recovery: any lesson that
    # hasn't bumped updated_at in 10 min is reset to 'empty'.
    from datetime import timedelta
    if (
        lesson.content_status == 'generating'
        and lesson.updated_at
        and lesson.updated_at < timezone.now() - timedelta(minutes=10)
    ):
        lesson.content_status = 'empty'
        lesson.save(update_fields=['content_status'])

    # ?regen=1 is set by the regenerate redirect so the polling banner
    # shows immediately even if we land before the worker has had time
    # to flip content_status from 'empty' to 'generating'. Closes a
    # ~100ms race where the user sees a blank page with no feedback.
    just_kicked_regen = request.GET.get('regen') == '1'
    is_generating = (
        lesson.content_status == 'generating' or just_kicked_regen
    )

    # L4 — sibling lessons in the same unit, used by the Move-to dropdown
    # on each Terminal Objective in the edit panel. Excludes self.
    sibling_lessons = list(
        lesson.unit.lessons
        .exclude(id=lesson.id)
        .order_by('order_index', 'id')
        .values('id', 'title')
    )

    context = {
        **request.staff_ctx,
        'lesson': lesson,
        'unit': lesson.unit,
        'course': course,
        'steps': steps,
        'media_count': media_count,
        'exit_ticket': exit_ticket,
        'exit_questions': exit_questions,
        'exit_ticket_count': exit_ticket_count,
        'untagged_eo_count': untagged_eo_count,
        'students_completed': students_completed,
        'prerequisites': prerequisites,
        'available_lessons': available_lessons,
        'teaching_steps': teaching_steps,
        'enabling_objectives': enabling_objectives,
        'unit_terminal_objectives': unit_terminal_objectives,
        'sibling_lessons': sibling_lessons,
        'is_generating': is_generating,
    }

    return render(request, 'dashboard/curriculum/lesson_detail.html', context)


@teacher_required
@require_POST
def exit_question_edit(request, question_id):
    """Edit or delete a single exit ticket question via AJAX."""
    from apps.tutoring.models import ExitTicketQuestion
    import json

    institution = request.staff_ctx['institution']
    lookup = {'id': question_id}
    if institution is not None:
        lookup['exit_ticket__lesson__unit__course__institution'] = institution
    question = get_object_or_404(ExitTicketQuestion, **lookup)

    data = json.loads(request.body) if request.body else {}

    # Delete action
    if data.get('action') == 'delete':
        question.delete()
        return JsonResponse({'success': True, 'deleted': True})

    # Update fields
    for field in ['question_text', 'option_a', 'option_b', 'option_c', 'option_d',
                  'correct_answer', 'explanation', 'difficulty', 'concept_tag']:
        if field in data:
            value = data[field]
            if field == 'correct_answer':
                value = value[:1].upper()
            setattr(question, field, value)

    # Plot spec edit / remove (interactive Chart.js plot for
    # data_interpretation questions)
    if 'plot_spec' in data or data.get('remove_plot_spec'):
        from apps.tutoring.plot_spec import coerce_plot_spec
        ad = question.answer_data or {}
        if not isinstance(ad, dict):
            ad = {}
        if data.get('remove_plot_spec'):
            ad.pop('plot_spec', None)
        else:
            cleaned, err = coerce_plot_spec(data['plot_spec'])
            if err:
                return JsonResponse({'success': False, 'error': err}, status=400)
            ad['plot_spec'] = cleaned
        question.answer_data = ad

    # Free-form answer_data fields a teacher might tweak.
    # Includes fill-in-blank (text_template + blanks) so the inline
    # edit on lesson_detail / summative review saves correctly.
    ad_fields_present = any(
        k in data for k in (
            'data_description', 'model_answer', 'keywords',
            'text_template', 'blanks',
        )
    )
    if ad_fields_present:
        ad = question.answer_data or {}
        if not isinstance(ad, dict):
            ad = {}
        for k in ('data_description', 'model_answer', 'text_template'):
            if k in data:
                ad[k] = data[k]
        if 'keywords' in data and isinstance(data['keywords'], list):
            ad['keywords'] = [str(k) for k in data['keywords']]
        if 'blanks' in data and isinstance(data['blanks'], list):
            ad['blanks'] = [str(b).strip() for b in data['blanks'] if str(b).strip()]
        question.answer_data = ad

    question.save()
    return JsonResponse({'success': True})


@teacher_required
@require_POST
def lesson_prerequisite_edit(request, lesson_id):
    """Add or remove a lesson prerequisite via AJAX."""
    from apps.tutoring.skills_models import LessonPrerequisite

    institution = request.staff_ctx['institution']
    lookup = {'id': lesson_id}
    if institution is not None:
        lookup['unit__course__institution'] = institution
    lesson = get_object_or_404(Lesson, **lookup)

    data = json.loads(request.body) if request.body else {}
    action = data.get('action')
    prereq_id = data.get('prerequisite_id')

    if not prereq_id:
        return JsonResponse({'success': False, 'error': 'Missing prerequisite_id'}, status=400)

    if action == 'add':
        prereq_lesson = get_object_or_404(Lesson, id=prereq_id, unit__course=lesson.unit.course)
        LessonPrerequisite.objects.get_or_create(
            lesson=lesson,
            prerequisite=prereq_lesson,
            defaults={'strength': 1.0, 'is_direct': True},
        )
        return JsonResponse({'success': True})

    elif action == 'delete':
        LessonPrerequisite.objects.filter(
            lesson=lesson, prerequisite_id=prereq_id
        ).delete()
        return JsonResponse({'success': True})

    return JsonResponse({'success': False, 'error': 'Invalid action'}, status=400)


@teacher_required
@require_POST
def course_set_default_duration(request, course_id):
    """Bulk-update lesson.estimated_minutes for every lesson in a course
    WITHOUT regenerating any content.

    Lessons are generated at max depth (10 steps) — duration only
    drives runtime step selection by the tutor engine. Recalibrating
    the course duration is therefore a metadata change, not an LLM
    regeneration. This view exists so teachers can recalibrate
    immediately without triggering the parallel regen pipeline.
    See memory/max_depth_lesson_steps_plan.md.
    """
    from apps.curriculum.models import Course, Lesson

    institution = request.staff_ctx['institution']
    lookup = {'id': course_id}
    if institution is not None:
        lookup['institution'] = institution
    course = get_object_or_404(Course, **lookup)

    raw = (request.POST.get('lesson_duration') or '').strip()
    try:
        new_duration = int(raw)
    except ValueError:
        messages.error(request, "Pick a duration first.")
        return redirect('dashboard:course_detail', course_id=course.id)
    if not (5 <= new_duration <= 120):
        messages.error(request, f"Duration must be between 5 and 120 minutes (got {new_duration}).")
        return redirect('dashboard:course_detail', course_id=course.id)

    updated = Lesson.objects.filter(unit__course=course).update(
        estimated_minutes=new_duration,
    )

    # Course-level policy toggle — ships in the same form so the
    # teacher sets duration + access in one action. The checkbox is
    # only present in POST when ticked; absence = lockdown.
    new_allow = bool(request.POST.get('allow_student_override'))
    policy_msg = ''
    if new_allow != course.allow_student_duration_override:
        course.allow_student_duration_override = new_allow
        course.save(update_fields=['allow_student_duration_override'])
        policy_msg = (
            ' Students CAN now pick their session duration.'
            if new_allow else
            ' Students can NO LONGER change duration; everyone uses the default.'
        )

    messages.success(
        request,
        f"Default lesson duration set to {new_duration} min for {updated} lesson(s) "
        f"in '{course.title}'.{policy_msg} Active sessions keep their existing pacing; "
        f"new sessions will use the new duration. No regeneration triggered.",
    )
    return redirect('dashboard:course_detail', course_id=course.id)


@teacher_required
@require_POST
def course_regenerate_all(request, course_id):
    """Regenerate ALL generated content in a course in one click:
    lesson steps, exit-ticket question banks, and (for math
    courses) the summative-exam bank. Runs lessons 3 in parallel
    in a background thread.

    Phase 1 — steps (~2 min/lesson, parallel): triggers Layer 1+3
      arithmetic verification on the new step content.
    Phase 2 — exit tickets (~30s/lesson, parallel): triggers
      Layers 1+2+4 on the new question banks. Drops
      ExitTicketAttempt history for affected lessons.
    Phase 3 — summative bank (sampling, no LLM): rebuilds the
      per-course bank from the freshly generated lesson exit
      tickets. Math courses only.

    Student data preserved across all phases:
      - StudentLessonProgress, StudentSkillMastery, TutorSession
      - StudentCompetencyRecord (permanent mastery transcript)
      - Lesson row (PK stable; FKs survive)

    What gets wiped:
      - LessonStep rows (replaced)
      - ExitTicketQuestion rows (replaced)
      - ExitTicketAttempt rows for the regenerated lessons
      - Summative ExitTicketQuestion rows (rebuilt)

    See memory/course_regeneration_for_slow_learners.md (Phase 3)
    and memory/llm_arithmetic_defense_plan.md.
    """
    from apps.curriculum.models import Course, Lesson
    from apps.dashboard.background_tasks import run_async, generate_complete_course
    from apps.accounts.models import Institution

    # Pilot-mode gate: course-level regenerate is platform-admin only.
    # Per-lesson regenerate (lesson_regenerate view) is governed by
    # teachers_can_edit_content separately.
    if not request.staff_ctx.get('can_regenerate_courses'):
        messages.warning(
            request,
            "Course-level regenerate is restricted to platform admins during the pilot. "
            "Use per-lesson regenerate for individual lessons."
        )
        return redirect('dashboard:course_detail', course_id=course_id)

    institution = request.staff_ctx['institution']
    lookup = {'id': course_id}
    if institution is not None:
        lookup['institution'] = institution
    course = get_object_or_404(Course, **lookup)

    # Optional duration knob — lets the teacher recalibrate every
    # lesson's `estimated_minutes` BEFORE regen, without going through
    # the destructive curriculum re-parse path. Only writes if the
    # form sent a value; absent = keep each lesson's existing duration.
    new_duration_raw = (request.POST.get('lesson_duration') or '').strip()
    duration_msg = ''
    if new_duration_raw:
        try:
            new_duration = int(new_duration_raw)
            if 5 <= new_duration <= 60:
                Lesson.objects.filter(unit__course=course).update(
                    estimated_minutes=new_duration,
                )
                duration_msg = f" Lesson duration set to {new_duration} min."
        except ValueError:
            pass

    # Read scope checkboxes from the form (default: all on).
    regen_steps = bool(request.POST.get('regen_steps'))
    regen_et = bool(request.POST.get('regen_exit_tickets'))
    regen_sum = bool(request.POST.get('regen_summative'))
    if not (regen_steps or regen_et or regen_sum):
        messages.warning(
            request,
            "Pick at least one thing to regenerate "
            "(steps, exit tickets, or summative).",
        )
        return redirect('dashboard:course_detail', course_id=course.id)

    inst = institution or course.institution or Institution.get_global()
    run_async(
        generate_complete_course,
        course.id, inst.id,
        regen_steps=regen_steps,
        regen_exit_tickets=regen_et,
        regen_summative=regen_sum,
    )

    lesson_count = course.units.aggregate(n=Count('lessons'))['n'] or 0
    parts = []
    if regen_steps:
        parts.append("lesson steps")
    if regen_et:
        parts.append("exit tickets")
    if regen_sum and course.is_math:
        parts.append("summative bank")
    if len(parts) > 1:
        scope_human = ", ".join(parts[:-1]) + " + " + parts[-1]
    else:
        scope_human = parts[0] if parts else "(nothing)"
    messages.success(
        request,
        f"Regenerating {scope_human} in '{course.title}' "
        f"({lesson_count} lesson(s)).{duration_msg} "
        f"Running 10 in parallel — refresh this page to watch progress. "
        f"Student mastery + permanent competency transcript are preserved."
    )
    return redirect('dashboard:course_detail', course_id=course.id)


@teacher_required
@require_POST
def lesson_regenerate(request, lesson_id):
    """Regenerate one lesson with per-scope opt-in.

    POST flags (mirrors the course-level form):
      regen_steps          — wipe + regenerate LessonStep rows
      regen_exit_tickets   — force-replace the ExitTicketQuestion bank
                             (attempt history preserved)

    Default when neither flag is sent: regenerate everything (back-compat
    for callers that haven't been updated yet — the per-lesson ⚡ button
    on course_detail still posts without flags).

    Exit-ticket-only is the cheap-fix path: ~30s vs ~3min for steps.
    Used when the teacher wants to refresh the question bank without
    paying to rebuild the teaching script. Steps stay intact.

    Runs in a background thread so the HTTP request returns immediately.
    The course detail page polls + auto-recovers stuck lessons (10-min
    cutoff).

    Permission: gated by ``can_regenerate_courses`` so the same flag
    governs course-level AND per-lesson regeneration. Pilot teachers
    can review + edit lessons but cannot regenerate.
    """
    if not request.staff_ctx.get('can_regenerate_courses'):
        messages.warning(
            request,
            "Lesson regeneration is restricted to platform admins during the pilot.",
        )
        return redirect('dashboard:lesson_detail', lesson_id=lesson_id)

    from apps.curriculum.models import Lesson
    from apps.dashboard.background_tasks import (
        run_async,
        generate_complete_lesson,
        regenerate_lesson_exit_ticket_only,
    )
    from apps.accounts.models import Institution

    institution = request.staff_ctx['institution']

    lookup = {'id': lesson_id}
    if institution is not None:
        lookup['unit__course__institution'] = institution
    lesson = get_object_or_404(Lesson, **lookup)

    # Guard: skip if a generation is already in flight. Rapid clicks
    # used to wipe in-progress state and spawn pre-empting tasks.
    if lesson.content_status == 'generating':
        messages.info(
            request,
            f"'{lesson.title}' is already generating. Wait a minute "
            f"and refresh — clicking again won't make it faster.",
        )
        return redirect('dashboard:lesson_detail', lesson_id=lesson.id)

    # Resolve scope. The new form posts ``scope_marker=1`` so we can
    # distinguish it from legacy callers (the per-lesson ⚡ button on
    # course_detail still posts without flags). Legacy callers default
    # to "regen everything"; the new form respects the checkboxes —
    # including the "both unchecked" case, which becomes a validation
    # error rather than silently doing the full regen.
    has_scope_marker = bool(request.POST.get('scope_marker'))
    if has_scope_marker:
        regen_steps = bool(request.POST.get('regen_steps'))
        regen_et = bool(request.POST.get('regen_exit_tickets'))
    else:
        regen_steps = True
        regen_et = True
    if not (regen_steps or regen_et):
        messages.warning(
            request,
            "Pick at least one thing to regenerate (lesson steps or exit ticket).",
        )
        return redirect('dashboard:lesson_detail', lesson_id=lesson.id)

    inst = institution or lesson.unit.course.institution or Institution.get_global()

    # Exit-ticket-only path — preserves steps. Cheap (~30s).
    if regen_et and not regen_steps:
        run_async(regenerate_lesson_exit_ticket_only, lesson.id, inst.id)
        messages.success(
            request,
            f"Regenerating exit ticket for '{lesson.title}'. "
            f"Usually ~30s — the lesson page will update automatically. "
            f"Lesson steps and attempt history are preserved.",
        )
        # ?regen=1 lets the lesson_detail template force the polling
        # banner on, even if the page renders before the worker has
        # had a chance to flip content_status to 'generating'.
        return redirect(
            f"{reverse('dashboard:lesson_detail', args=[lesson.id])}?regen=1"
        )

    # Steps regen (with or without exit-ticket replace) — full pipeline.
    # Wipe steps + bump updated_at so the course-detail auto-recovery
    # treats this lesson as freshly running.
    lesson.steps.all().delete()
    lesson.content_status = 'empty'
    lesson.updated_at = timezone.now()
    lesson.save(update_fields=['content_status', 'updated_at'])

    # When exit ticket is ALSO requested, do the in-place replace AFTER
    # the steps pipeline finishes — the pipeline's _generate_exit_ticket
    # has skip-if-exists semantics, so we follow up with force_regenerate.
    # Each callee manages its own DB connection (connection.close at
    # their entry), so the closure doesn't need to.
    lesson_id_local = lesson.id
    inst_id_local = inst.id

    def _full_then_et():
        generate_complete_lesson(lesson_id_local, inst_id_local)
        regenerate_lesson_exit_ticket_only(lesson_id_local, inst_id_local)

    if regen_et:
        run_async(_full_then_et)
        scope_msg = "lesson steps + exit ticket"
    else:
        run_async(generate_complete_lesson, lesson.id, inst.id)
        scope_msg = "lesson steps (exit ticket preserved)"

    messages.success(
        request,
        f"Regenerating {scope_msg} for '{lesson.title}'. "
        f"This usually takes 1–3 minutes — the lesson page will update "
        f"automatically.",
    )
    return redirect(
        f"{reverse('dashboard:lesson_detail', args=[lesson.id])}?regen=1"
    )


@teacher_required
@require_POST
def lesson_generate_content(request, lesson_id):
    """Generate full content pipeline for a lesson asynchronously."""
    from apps.dashboard.background_tasks import run_async, generate_complete_lesson

    institution = request.staff_ctx['institution']

    lookup = {'id': lesson_id}
    if institution is not None:
        lookup['unit__course__institution'] = institution
    lesson = get_object_or_404(Lesson, **lookup)

    from apps.tutoring.models import ExitTicket

    # Guard: skip if a generation is already running. Don't write to
    # content_status here — that's how the previous code raced with
    # the running worker (worker reads `_is_cancelled` after step 1
    # and saw the view's write, then bailed out with "cancelled before
    # media"). The worker's own guard + the auto-recovery on
    # course_detail handle stuck states.
    if lesson.content_status == 'generating':
        messages.info(
            request,
            f"'{lesson.title}' is already generating. Wait a minute and refresh.",
        )
        return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)

    # Guard: skip if already has full content (steps + exit ticket).
    has_steps = lesson.steps.count() >= 3
    has_exit = ExitTicket.objects.filter(lesson=lesson, questions__isnull=False).exists()
    if has_steps and has_exit:
        messages.info(
            request,
            f"'{lesson.title}' already has {lesson.steps.count()} steps and an exit ticket. "
            f"Use the Regenerate button if you want to rebuild.",
        )
        return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)

    # Partial state from a prior failed/cancelled run? Wipe before
    # respawn so the worker rebuilds cleanly. (Without this,
    # generate_exit_ticket_for_lesson skips when an exit ticket
    # already exists, leaving the lesson with steps but 0 questions.)
    # updated_at must bump too — see the matching note in lesson_regenerate.
    lesson.steps.all().delete()
    ExitTicket.objects.filter(lesson=lesson).delete()
    lesson.content_status = 'empty'
    lesson.updated_at = timezone.now()
    lesson.save(update_fields=['content_status', 'updated_at'])

    from apps.accounts.models import Institution
    inst = institution or lesson.unit.course.institution or Institution.get_global()
    run_async(generate_complete_lesson, lesson.id, inst.id)

    messages.info(request, f"Generating content for '{lesson.title}' in the background...")
    return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)


@teacher_required
@require_POST
def lesson_publish(request, lesson_id):
    """Publish or unpublish a lesson."""
    from apps.curriculum.models import Lesson

    institution = request.staff_ctx['institution']

    if institution is not None:
        lesson = get_object_or_404(
            Lesson,
            Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True),
            id=lesson_id,
        )
    else:
        lesson = get_object_or_404(Lesson, id=lesson_id)
    
    # Gate: tier_3/tier_4 content requires teacher approval before publishing
    if not lesson.is_published and lesson.content_quality in ('tier_3', 'tier_4'):
        if not lesson.teacher_approved:
            messages.warning(
                request,
                f"This lesson has {lesson.get_content_quality_display()} content. "
                f"Please review and approve it before publishing."
            )
            return redirect('dashboard:lesson_detail', lesson_id=lesson.id)

    # Toggle publish status
    lesson.is_published = not lesson.is_published
    lesson.save()

    # When publishing a lesson, ensure the parent course is also published
    if lesson.is_published:
        course = lesson.unit.course
        if not course.is_published:
            course.is_published = True
            course.save(update_fields=['is_published'])

    status = "published" if lesson.is_published else "unpublished"
    messages.success(request, f"Lesson '{lesson.title}' {status}.")

    return redirect('dashboard:lesson_detail', lesson_id=lesson.id)


@teacher_required
def lesson_approve(request, lesson_id):
    """Approve lesson content for student access (required for tier_3/tier_4)."""
    from apps.curriculum.models import Lesson

    institution = request.staff_ctx['institution']

    if institution is not None:
        lesson = get_object_or_404(
            Lesson,
            Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True),
            id=lesson_id,
        )
    else:
        lesson = get_object_or_404(Lesson, id=lesson_id)

    lesson.teacher_approved = True
    lesson.teacher_approved_at = timezone.now()
    lesson.teacher_approved_by = request.user
    lesson.save(update_fields=['teacher_approved', 'teacher_approved_at', 'teacher_approved_by'])

    messages.success(request, f"Lesson '{lesson.title}' approved. You can now publish it.")
    return redirect('dashboard:lesson_detail', lesson_id=lesson.id)


@teacher_required
@require_POST
def lesson_group_settings(request, lesson_id):
    """Update group-session settings on a lesson:
      - allow_group_mode (bool) — single gate. When enabled, group
        sessions start immediately (no separate teacher approval).
      - max_group_size (int)
    """
    from apps.curriculum.models import Lesson

    institution = request.staff_ctx['institution']
    if institution is not None:
        lesson = get_object_or_404(
            Lesson,
            Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True),
            id=lesson_id,
        )
    else:
        lesson = get_object_or_404(Lesson, id=lesson_id)

    lesson.allow_group_mode = request.POST.get('allow_group_mode') == 'on'
    try:
        size = int(request.POST.get('max_group_size', '4') or 4)
    except (ValueError, TypeError):
        size = 4
    lesson.max_group_size = max(2, min(size, 10))
    lesson.save(update_fields=[
        'allow_group_mode',
        'max_group_size',
    ])
    messages.success(request, "Group session settings updated.")
    return redirect('dashboard:lesson_detail', lesson_id=lesson.id)


@teacher_required
@require_POST
def delete_student(request, student_id):
    """Teacher-initiated student deletion.

    A teacher (staff member) may delete any student that belongs to one
    of their institutions. Cascade-deletes the student's tutoring history
    along with the user (Membership, StudentProfile, sessions, turns,
    progress, exit ticket attempts).
    """
    from django.contrib.auth.models import User
    from apps.safety import SafetyAuditLog
    from apps.accounts.models import Membership

    institution = request.staff_ctx['institution']
    target = get_object_or_404(User, id=student_id)

    # Cannot delete yourself via this endpoint (use self-delete).
    if target.id == request.user.id:
        messages.error(request, "Use the account-deletion page to delete your own account.")
        return redirect('dashboard:student_detail', student_id=student_id)

    # Only delete students within the staff member's institution.
    teacher_inst_ids = list(
        request.user.memberships.filter(is_active=True, role='staff')
        .values_list('institution_id', flat=True)
    )
    if institution is not None:
        teacher_inst_ids.append(institution.id)
    student_in_scope = Membership.objects.filter(
        user=target,
        role='student',
        is_active=True,
        institution_id__in=teacher_inst_ids,
    ).exists()
    if not student_in_scope and not request.user.is_staff:
        messages.error(request, "You can only delete students from your school.")
        return redirect('dashboard:student_list')

    # Refuse to delete a staff member through this endpoint.
    if Membership.objects.filter(user=target, role='staff', is_active=True).exists():
        messages.error(request, "This account is a staff account, not a student.")
        return redirect('dashboard:student_list')

    username = target.username
    SafetyAuditLog.log(
        'account_deleted',
        user=request.user,
        details={
            'mode': 'teacher_deletes_student',
            'target_user_id': target.id,
            'target_username': username,
        },
        severity='warning',
        request=request,
    )
    target.delete()
    messages.success(request, f"Student '{username}' has been deleted.")
    return redirect('dashboard:student_list')


@staff_required
def staff_list(request):
    """Admin-only list of staff members for an institution.

    Superadmins see all staff; institution admins see staff for their
    selected institution. Each row links to a delete action.
    """
    from django.contrib.auth.models import User
    from apps.accounts.models import Membership

    if not request.user.is_staff:
        # Restrict to platform admins for now (consistent with staff invite).
        messages.error(request, "Admin access required.")
        return redirect('dashboard:home')

    institution = request.staff_ctx['institution']
    qs = Membership.objects.filter(role='staff').select_related(
        'user', 'institution',
    )
    if institution is not None:
        qs = qs.filter(institution=institution)

    staff_rows = []
    seen_users = set()
    for m in qs.order_by('user__username'):
        if m.user_id in seen_users:
            continue
        seen_users.add(m.user_id)
        staff_rows.append({
            'user': m.user,
            'institution': m.institution,
            'is_active': m.is_active,
            'is_self': m.user_id == request.user.id,
        })

    context = {**request.staff_ctx, 'staff_rows': staff_rows}
    return render(request, 'dashboard/staff_list.html', context)


@staff_required
@require_POST
def delete_staff(request, user_id):
    """Admin-initiated staff (teacher) deletion.

    Restricted to platform admins (request.user.is_staff). Refuses to
    delete the requester themselves.
    """
    from django.contrib.auth.models import User
    from apps.safety import SafetyAuditLog

    if not request.user.is_staff:
        messages.error(request, "Admin access required.")
        return redirect('dashboard:home')

    target = get_object_or_404(User, id=user_id)
    if target.id == request.user.id:
        messages.error(request, "Use the account-deletion page to delete your own account.")
        return redirect('dashboard:staff_list')

    username = target.username
    SafetyAuditLog.log(
        'account_deleted',
        user=request.user,
        details={
            'mode': 'admin_deletes_staff',
            'target_user_id': target.id,
            'target_username': username,
        },
        severity='warning',
        request=request,
    )
    target.delete()
    messages.success(request, f"Staff account '{username}' has been deleted.")
    return redirect('dashboard:staff_list')


# ----------------------------------------------------------------
# Admin-initiated password reset for staff accounts
#
# Two flavours:
#   show  — generate a random password, return it to the admin
#           ONCE so they can hand-deliver it (works without email).
#   email — generate a one-time reset link via Django's built-in
#           default_token_generator, email it to the staff user.
#
# Both flavours: set Membership.password_reset_required=True on
# every membership the target user has. The middleware redirects
# the user to a forced-change screen on their next request after
# login until they set a new password — at which point the flag
# clears.
# ----------------------------------------------------------------


def _generate_temp_password(length: int = 12) -> str:
    """Random password — letters + digits only, avoids ambiguous chars
    (no 0/O, 1/l/I) so the admin can read it back to the user
    over phone / SMS without confusion."""
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _flag_password_reset_required(target_user):
    """Set Membership.password_reset_required=True on every membership
    the user has. Login-side middleware reads this and redirects."""
    from apps.accounts.models import Membership
    Membership.objects.filter(user=target_user).update(
        password_reset_required=True,
    )


@staff_required
@require_POST
def staff_reset_password_show(request, user_id):
    """Admin-initiated reset that returns the new password JSON for
    one-time display. The admin copies it and shares with the user
    out-of-band (in person, phone, signal, etc.). Forces the user
    to change it on their next login.
    """
    from django.contrib.auth.models import User
    from django.http import JsonResponse
    from apps.safety import SafetyAuditLog

    if not request.user.is_staff:
        return JsonResponse({"error": "Admin access required"}, status=403)
    target = get_object_or_404(User, id=user_id)
    if target.id == request.user.id:
        return JsonResponse({"error": "Use the account-settings page to change your own password."}, status=400)

    new_password = _generate_temp_password()
    target.set_password(new_password)
    target.save(update_fields=['password'])
    _flag_password_reset_required(target)

    SafetyAuditLog.log(
        'password_reset',
        user=request.user,
        details={
            'mode': 'admin_resets_staff_show',
            'target_user_id': target.id,
            'target_username': target.username,
        },
        severity='warning',
        request=request,
    )
    logger.info(
        f"[StaffReset] admin={request.user.username} reset staff={target.username} "
        f"via show (forced-change=True)"
    )
    return JsonResponse({
        "username": target.username,
        "temporary_password": new_password,
        "must_change_on_next_login": True,
    })


@staff_required
@require_POST
def staff_reset_password_email(request, user_id):
    """Admin-initiated reset that emails the user a one-time link.
    Uses Django's built-in default_token_generator (same one
    powering PasswordResetView) so the link expires per the
    Django default (3 days).
    """
    from django.contrib.auth.models import User
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes
    from django.urls import reverse
    from django.core.mail import send_mail
    from django.conf import settings as dj_settings
    from django.http import JsonResponse
    from apps.safety import SafetyAuditLog

    if not request.user.is_staff:
        return JsonResponse({"error": "Admin access required"}, status=403)
    target = get_object_or_404(User, id=user_id)
    if target.id == request.user.id:
        return JsonResponse({"error": "Use the account-settings page to change your own password."}, status=400)
    if not target.email:
        return JsonResponse({"error": "User has no email address on file"}, status=400)

    _flag_password_reset_required(target)

    uidb64 = urlsafe_base64_encode(force_bytes(target.pk))
    token = default_token_generator.make_token(target)
    try:
        reset_path = reverse(
            'password_reset_confirm', kwargs={'uidb64': uidb64, 'token': token},
        )
    except Exception:
        # If the URL name isn't wired, fall back to the canonical Django path
        reset_path = f"/accounts/reset/{uidb64}/{token}/"
    reset_url = request.build_absolute_uri(reset_path)

    try:
        from apps.dashboard.models import PlatformConfig
        platform_name = PlatformConfig.load().platform_name or 'AI Tutor'
    except Exception:
        platform_name = 'AI Tutor'
    sender_name = request.user.get_full_name() or request.user.username

    try:
        send_mail(
            subject=f"Your {platform_name} password was reset",
            message=(
                f"Hello {target.first_name or target.username},\n\n"
                f"An administrator ({sender_name}) reset your "
                f"{platform_name} password. Use the link below to "
                f"choose a new password — the link is one-time and "
                f"expires shortly.\n\n"
                f"{reset_url}\n\n"
                f"If you didn't expect this, contact your "
                f"administrator immediately.\n\n"
                f"— {platform_name}"
            ),
            from_email=dj_settings.DEFAULT_FROM_EMAIL,
            recipient_list=[target.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.error(
            f"[StaffReset] email to {target.email} failed: {e}", exc_info=True,
        )
        return JsonResponse({
            "error": f"Email delivery failed — try the show-password option instead. ({e})",
        }, status=502)

    SafetyAuditLog.log(
        'password_reset',
        user=request.user,
        details={
            'mode': 'admin_resets_staff_email',
            'target_user_id': target.id,
            'target_username': target.username,
            'target_email': target.email,
        },
        severity='warning',
        request=request,
    )
    logger.info(
        f"[StaffReset] admin={request.user.username} emailed reset link to "
        f"staff={target.username} <{target.email}>"
    )
    return JsonResponse({
        "username": target.username,
        "email": target.email,
        "sent": True,
    })


@teacher_required
@require_POST
def course_subject_type(request, course_id):
    """Update Course.subject_type. Drives is_math + subject-specific tutor
    rules. See memory/math_tutor_fix_plan.md M8.
    """
    from apps.curriculum.models import Course

    institution = request.staff_ctx['institution']
    if institution is not None:
        course = get_object_or_404(
            Course,
            Q(institution=institution) | Q(institution__isnull=True),
            id=course_id,
        )
    else:
        course = get_object_or_404(Course, id=course_id)

    new_value = request.POST.get('subject_type', '').strip()
    valid = {choice[0] for choice in Course.SubjectType.choices}
    if new_value and new_value not in valid:
        messages.error(request, "Invalid subject type.")
    else:
        course.subject_type = new_value
        course.save(update_fields=['subject_type'])
        messages.success(request, f"Subject type set to '{new_value or 'auto-detect'}'.")
    return redirect('dashboard:course_detail', course_id=course.id)


@teacher_required
@require_POST
def exit_question_regenerate(request, question_id):
    """POST → prompt-mode rewrite of an MCQ exit-ticket question.
    Returns the candidate fields as JSON. Does NOT persist.

    For v1: MCQ only. Auto-review mode requires the exit_question
    judge that ships in Q4 — for now, only `mode=prompt` is supported.

    Body (form-encoded):
      mode: must be 'prompt' (auto_review returns 400 until Q4).
      teacher_guidance: free-form instruction (required).
    """
    from apps.tutoring.models import ExitTicketQuestion
    from django.http import JsonResponse

    institution = request.staff_ctx['institution']
    question = get_object_or_404(
        ExitTicketQuestion.objects.select_related(
            'exit_ticket__lesson__unit__course',
        ),
        id=question_id,
    )
    course = question.exit_ticket.lesson.unit.course
    if (
        course.institution is not None
        and institution is not None
        and course.institution_id != institution.id
        and not getattr(request.user, 'is_superuser', False)
    ):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    mode = (request.POST.get('mode') or 'prompt').strip().lower()
    if mode not in ('auto_review', 'prompt'):
        return JsonResponse({
            'ok': False, 'error': f'unknown mode: {mode}',
        }, status=400)

    if question.question_type != 'mcq':
        return JsonResponse({
            'ok': False,
            'error': (
                f'regen for question_type={question.question_type!r} '
                'not yet supported (v1 covers MCQ only)'
            ),
        }, status=400)

    if mode == 'auto_review':
        # Q4 wired: run the exit_question judge first; if it rejects,
        # rewrite using the recommended_fix as guidance and re-judge.
        # Single-shot rewrite (no cycle loop) — exit-Q regen is
        # deterministic enough that the cycle ensemble we use for
        # lesson steps doesn't add value.
        try:
            from apps.curriculum.content_judges.exit_question import (
                run_exit_question_judge,
            )
            from apps.llm.models import ModelConfig
            gen_config = ModelConfig.get_for('generation')
            judge_exclude = (
                (gen_config.provider or '').lower() if gen_config else None
            )
            verdict = run_exit_question_judge(
                question_text=question.question_text or '',
                option_a=question.option_a or '', option_b=question.option_b or '',
                option_c=question.option_c or '', option_d=question.option_d or '',
                correct_answer=question.correct_answer or '',
                explanation=question.explanation or '',
                lesson=question.exit_ticket.lesson,
                step_concept_tag=question.concept_tag or '',
                enabling_objective=question.enabling_objective or '',
                exclude_provider=judge_exclude,
            )
            verdict_dict = {
                'passed': verdict.passed,
                'violations': list(verdict.violations or []),
                'reasoning': verdict.reasoning or '',
                'recommended_fix': verdict.recommended_fix or '',
                'provider': verdict.provider or '',
                'model_name': verdict.model_name or '',
                'skipped': verdict.skipped,
                'skip_reason': verdict.skip_reason or '',
            }

            if verdict.skipped or (verdict.passed and not verdict.violations):
                return JsonResponse({
                    'ok': True,
                    'mode': 'auto_review',
                    'changed': False,
                    'message': (
                        f'Judge {"skipped" if verdict.skipped else "passed"} '
                        '— nothing to fix. Switch to Prompt mode to '
                        'request a specific rewrite.'
                    ),
                    'candidate': {
                        'question_text': question.question_text,
                        'option_a': question.option_a,
                        'option_b': question.option_b,
                        'option_c': question.option_c,
                        'option_d': question.option_d,
                        'correct_answer': question.correct_answer,
                        'explanation': question.explanation,
                    },
                    'judge_verdict': verdict_dict,
                })

            # Rewrite using the judge's recommended_fix as guidance.
            from apps.curriculum.content_regen import (
                run_exit_question_prompt_regen,
            )
            guidance = verdict.recommended_fix or (
                "Address these violations: "
                + ", ".join(verdict.violations or [])
            )
            result = run_exit_question_prompt_regen(
                original_question={
                    'question_text': question.question_text or '',
                    'option_a': question.option_a or '',
                    'option_b': question.option_b or '',
                    'option_c': question.option_c or '',
                    'option_d': question.option_d or '',
                    'correct_answer': question.correct_answer or '',
                    'explanation': question.explanation or '',
                },
                teacher_guidance=guidance,
                lesson=question.exit_ticket.lesson,
                step_concept_tag=question.concept_tag or '',
                enabling_objective=question.enabling_objective or '',
            )
            if result.error:
                return JsonResponse({
                    'ok': False,
                    'error': f'regen failed: {result.error}',
                    'judge_verdict': verdict_dict,
                }, status=500)

            return JsonResponse({
                'ok': True,
                'mode': 'auto_review',
                'changed': True,
                'candidate': result.as_dict(),
                'judge_verdict': verdict_dict,
                'audit': {
                    'picked_model': result.picked_model,
                    'elapsed_seconds': round(result.elapsed_seconds, 2),
                },
            })
        except Exception as exc:
            logger.error(
                f"[ExitQRegen] auto_review question={question_id} failed: "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            return JsonResponse({
                'ok': False,
                'error': f'{type(exc).__name__}: {str(exc)[:200]}',
            }, status=500)

    # mode == 'prompt'
    teacher_guidance = (request.POST.get('teacher_guidance') or '').strip()
    if not teacher_guidance:
        return JsonResponse({
            'ok': False,
            'error': 'teacher_guidance is required for Prompt mode',
        }, status=400)

    try:
        from apps.curriculum.content_regen import (
            run_exit_question_prompt_regen,
        )
        result = run_exit_question_prompt_regen(
            original_question={
                'question_text': question.question_text or '',
                'option_a': question.option_a or '',
                'option_b': question.option_b or '',
                'option_c': question.option_c or '',
                'option_d': question.option_d or '',
                'correct_answer': question.correct_answer or '',
                'explanation': question.explanation or '',
            },
            teacher_guidance=teacher_guidance,
            lesson=question.exit_ticket.lesson,
            step_concept_tag=question.concept_tag or '',
            enabling_objective=question.enabling_objective or '',
        )
    except Exception as exc:
        logger.error(
            f"[ExitQRegen] question={question_id} failed: "
            f"{type(exc).__name__}: {exc}",
            exc_info=True,
        )
        return JsonResponse({
            'ok': False,
            'error': f'{type(exc).__name__}: {str(exc)[:200]}',
        }, status=500)

    if result.error:
        return JsonResponse({
            'ok': False,
            'error': f'regen failed: {result.error}',
        }, status=500)

    return JsonResponse({
        'ok': True,
        'mode': 'prompt',
        'candidate': result.as_dict(),
        'audit': {
            'picked_model': result.picked_model,
            'elapsed_seconds': round(result.elapsed_seconds, 2),
        },
    })


@teacher_required
@require_POST
def exit_question_save_regen(request, question_id):
    """POST → persist accepted MCQ regen candidate to the question.

    Body (form-encoded): question_text, option_a, option_b, option_c,
    option_d, correct_answer (single letter), explanation. Optional
    audit_blob (JSON string) is stored on the parent ExitTicket's
    metadata so we can see manual regen history.
    """
    from apps.tutoring.models import ExitTicketQuestion
    from django.http import JsonResponse

    institution = request.staff_ctx['institution']
    question = get_object_or_404(
        ExitTicketQuestion.objects.select_related(
            'exit_ticket__lesson__unit__course',
        ),
        id=question_id,
    )
    course = question.exit_ticket.lesson.unit.course
    if (
        course.institution is not None
        and institution is not None
        and course.institution_id != institution.id
        and not getattr(request.user, 'is_superuser', False)
    ):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    qt = (request.POST.get('question_text') or '').strip()
    if not qt:
        return JsonResponse({
            'ok': False, 'error': 'question_text is required',
        }, status=400)

    correct = (request.POST.get('correct_answer') or '').strip().upper()[:1]
    if correct not in ('A', 'B', 'C', 'D'):
        return JsonResponse({
            'ok': False,
            'error': 'correct_answer must be A, B, C, or D',
        }, status=400)

    question.question_text = qt[:1500]
    question.option_a = (request.POST.get('option_a') or '').strip()[:500]
    question.option_b = (request.POST.get('option_b') or '').strip()[:500]
    question.option_c = (request.POST.get('option_c') or '').strip()[:500]
    question.option_d = (request.POST.get('option_d') or '').strip()[:500]
    question.correct_answer = correct
    question.explanation = (request.POST.get('explanation') or '').strip()[:1000]
    question.save(update_fields=[
        'question_text', 'option_a', 'option_b', 'option_c',
        'option_d', 'correct_answer', 'explanation',
    ])

    return JsonResponse({
        'ok': True,
        'question_id': question.id,
    })


@teacher_required
@require_POST
def lesson_step_regenerate(request, step_id):
    """POST → run regen on a lesson step's teacher_script. Returns the
    candidate text + audit as JSON. Does NOT persist.

    The teacher reviews the candidate inline, then either POSTs to
    lesson_step_save_regen (commit) or discards (no-op).

    Modes (POST `mode`):
      auto_review — runs the Q2 factual_step judge + run_step_regen
        ensemble (cycle cap 2, temp decay). Only meaningful when the
        lesson has KB evidence to verify against.
      prompt — single-pass rewrite driven by `teacher_guidance`.
        No judge gating — the teacher's prompt is authoritative.
    """
    from apps.curriculum.models import LessonStep
    from django.http import JsonResponse

    institution = request.staff_ctx['institution']
    step = get_object_or_404(
        LessonStep.objects.select_related('lesson__unit__course'),
        id=step_id,
    )
    # Same institution scoping as step_edit — let teachers regen
    # platform-wide course steps too (institution=None on course is OK).
    course = step.lesson.unit.course
    if (
        course.institution is not None
        and institution is not None
        and course.institution_id != institution.id
        and not getattr(request.user, 'is_superuser', False)
    ):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    mode = (request.POST.get('mode') or 'auto_review').strip().lower()
    if mode not in ('auto_review', 'prompt'):
        return JsonResponse({
            'ok': False, 'error': f'unknown mode: {mode}',
        }, status=400)

    if not (step.teacher_script or '').strip():
        return JsonResponse({
            'ok': False, 'error': 'step has no teacher_script to rewrite',
        }, status=400)

    try:
        if mode == 'auto_review':
            # First: run the factual_step judge so we have something
            # to feed run_step_regen as the source verdict.
            from apps.curriculum.content_judges.factual_step import (
                run_factual_step_judge,
            )
            from apps.curriculum.content_regen import run_step_regen
            from apps.llm.models import ModelConfig

            gen_config = ModelConfig.get_for('generation')
            judge_exclude = (
                (gen_config.provider or '').lower() if gen_config else None
            )
            verdict = run_factual_step_judge(
                step.teacher_script,
                lesson=step.lesson,
                exclude_provider=judge_exclude,
            )
            verdict_dict = {
                'passed': verdict.passed,
                'violations': list(verdict.violations or []),
                'reasoning': verdict.reasoning or '',
                'recommended_fix': verdict.recommended_fix or '',
                'provider': verdict.provider or '',
                'model_name': verdict.model_name or '',
                'skipped': verdict.skipped,
                'skip_reason': verdict.skip_reason or '',
            }

            if verdict.skipped or (verdict.passed and not verdict.violations):
                # No issues to regen against — return the original
                # text + a note. The teacher can switch to prompt mode
                # if they still want a rewrite.
                return JsonResponse({
                    'ok': True,
                    'mode': 'auto_review',
                    'changed': False,
                    'message': (
                        f'Judge {"skipped" if verdict.skipped else "passed"} '
                        '— nothing to fix. Switch to Prompt mode to '
                        'request a specific rewrite.'
                    ),
                    'candidate_text': step.teacher_script,
                    'judge_verdict': verdict_dict,
                    'audit': None,
                })

            regen_result = run_step_regen(
                step_text=step.teacher_script,
                judge_result=verdict_dict,
                lesson=step.lesson,
                step_objective=step.enabling_objective or '',
                step_concept_tag=step.concept_tag or '',
            )
            return JsonResponse({
                'ok': True,
                'mode': 'auto_review',
                'changed': regen_result.text != step.teacher_script,
                'candidate_text': regen_result.text,
                'judge_verdict': verdict_dict,
                'audit': {
                    'cycles_run': regen_result.cycles_run,
                    'clean': regen_result.clean,
                    'picked_model': regen_result.picked_model,
                    'elapsed_seconds': round(regen_result.elapsed_seconds, 2),
                    'final_violations': list(regen_result.final_violations),
                    'cycles': regen_result.audit,
                },
            })

        # mode == 'prompt'
        teacher_guidance = (request.POST.get('teacher_guidance') or '').strip()
        if not teacher_guidance:
            return JsonResponse({
                'ok': False,
                'error': 'teacher_guidance is required for Prompt mode',
            }, status=400)

        from apps.curriculum.content_regen import run_step_prompt_regen
        regen_result = run_step_prompt_regen(
            step_text=step.teacher_script,
            teacher_guidance=teacher_guidance,
            lesson=step.lesson,
            step_objective=step.enabling_objective or '',
            step_concept_tag=step.concept_tag or '',
        )
        return JsonResponse({
            'ok': True,
            'mode': 'prompt',
            'changed': regen_result.text != step.teacher_script,
            'candidate_text': regen_result.text,
            'audit': {
                'cycles_run': regen_result.cycles_run,
                'picked_model': regen_result.picked_model,
                'elapsed_seconds': round(regen_result.elapsed_seconds, 2),
                'cycles': regen_result.audit,
            },
        })
    except Exception as exc:
        logger.error(
            f"[StepRegen] step={step_id} mode={mode} failed: "
            f"{type(exc).__name__}: {exc}",
            exc_info=True,
        )
        return JsonResponse({
            'ok': False,
            'error': f'{type(exc).__name__}: {str(exc)[:200]}',
        }, status=500)


@teacher_required
@require_POST
def lesson_step_save_regen(request, step_id):
    """POST → persist the regen candidate to step.teacher_script.

    Body: candidate_text (the text the teacher reviewed and accepted),
    plus optional `audit_blob` (JSON string) from the regen call so we
    can record the audit on step.judge_outputs['regen_audit'].

    On save: also sets content_quality_status='human_edited' so the
    UI shows the "edited" badge instead of the prior auto/flagged state.
    """
    from apps.curriculum.models import LessonStep
    from django.http import JsonResponse

    institution = request.staff_ctx['institution']
    step = get_object_or_404(
        LessonStep.objects.select_related('lesson__unit__course'),
        id=step_id,
    )
    course = step.lesson.unit.course
    if (
        course.institution is not None
        and institution is not None
        and course.institution_id != institution.id
        and not getattr(request.user, 'is_superuser', False)
    ):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    candidate_text = (request.POST.get('candidate_text') or '').strip()
    if not candidate_text:
        return JsonResponse({
            'ok': False, 'error': 'candidate_text is required',
        }, status=400)

    audit_blob = request.POST.get('audit_blob') or ''
    parsed_audit = None
    if audit_blob:
        try:
            parsed_audit = json.loads(audit_blob)
        except Exception:
            parsed_audit = None

    step.teacher_script = candidate_text
    step.content_quality_status = LessonStep.ContentQualityStatus.HUMAN_EDITED
    outputs = dict(step.judge_outputs or {})
    if parsed_audit:
        # Store last manual regen audit alongside any auto regen_audit
        # so we can see both histories.
        outputs['manual_regen_audit'] = parsed_audit
    step.judge_outputs = outputs
    step.save(update_fields=[
        'teacher_script', 'content_quality_status', 'judge_outputs',
    ])

    return JsonResponse({
        'ok': True,
        'step_id': step.id,
        'content_quality_status': step.content_quality_status,
    })


@teacher_required
def step_edit(request, step_id):
    """Edit a lesson step."""
    from apps.curriculum.models import LessonStep

    institution = request.staff_ctx['institution']

    # Include platform-wide courses (institution=None) so teachers
    # whose membership is school-scoped can still edit lessons in
    # courses that aren't owned by any single school. Without this,
    # the lookup 404'd on every platform-wide course.
    if institution is not None:
        step = get_object_or_404(
            LessonStep,
            Q(lesson__unit__course__institution=institution)
            | Q(lesson__unit__course__institution__isnull=True),
            id=step_id,
        )
    else:
        step = get_object_or_404(LessonStep, id=step_id)
    
    lesson = step.lesson
    total_steps = lesson.steps.count()
    
    # Phase options for 5E model
    phases = [
        ('engage', 'Engage'),
        ('explore', 'Explore'),
        ('explain', 'Explain'),
        ('elaborate', 'Elaborate'),
        ('evaluate', 'Evaluate'),
    ]
    
    if request.method == 'POST':
        action = request.POST.get('action', '')

        # Always save image description edits if media exists
        if step.media and step.media.get('images'):
            images = step.media['images']
            descriptions_changed = False
            for i, img in enumerate(images):
                new_desc = request.POST.get(f'image_description_{i}', '').strip()
                if new_desc and new_desc != img.get('description', ''):
                    img['description'] = new_desc
                    descriptions_changed = True
            if descriptions_changed:
                step.save()

        # Handle regenerate media action
        if action == 'regenerate_media':
            image_index = int(request.POST.get('image_index', 0))
            # Teacher-selected provider: 'openai' (gpt-image-2), 'gemini',
            # or '' (use ModelConfig default with cross-provider fallback).
            model_choice = (request.POST.get('model_choice') or '').strip().lower()
            images = step.media.get('images', []) if step.media else []
            if 0 <= image_index < len(images):
                img = images[image_index]
                description = img.get('description', '')
                if description:
                    try:
                        from apps.tutoring.image_service import ImageGenerationService
                        service = ImageGenerationService(
                            lesson=lesson,
                            institution=lesson.unit.course.institution,
                        )
                        result = service.get_or_generate_image(
                            prompt=description,
                            category=img.get('type', 'diagram'),
                            model_override=model_choice or None,
                        )
                        if result and result.get('url'):
                            img['url'] = result['url']
                            img['source'] = 'generated'
                            if result.get('model'):
                                img['model'] = result['model']
                            if result.get('provider'):
                                img['provider'] = result['provider']
                            step.save()
                            label = result.get('model') or 'image'
                            messages.success(request, f"Regenerated with {label}.")
                        else:
                            err = getattr(service, 'last_error', None)
                            if err:
                                messages.warning(request, f"Image generation failed: {err}")
                            else:
                                messages.warning(request, "Image generation returned no result.")
                    except Exception as e:
                        logger.error(f"Image regeneration error: {e}")
                        messages.error(request, f"Image generation failed: {e}")
                else:
                    messages.warning(request, "No image description to generate from.")
            context = {
                **request.staff_ctx,
                'step': step,
                'lesson': lesson,
                'total_steps': total_steps,
                'phases': phases,
            }
            return render(request, 'dashboard/curriculum/step_edit.html', context)

        # Handle upload image action
        if action == 'upload_image':
            uploaded = request.FILES.get('image')
            if uploaded:
                from apps.media_library.models import MediaAsset
                institution = lesson.unit.course.institution
                asset = MediaAsset.objects.create(
                    institution=institution,
                    title=request.POST.get('image_alt', '') or uploaded.name,
                    asset_type='image',
                    file=uploaded,
                    alt_text=request.POST.get('image_alt', ''),
                    caption=request.POST.get('image_caption', ''),
                )
                # Best-effort figure_facts extraction so teacher uploads
                # also feed the runtime tutor with verified visual
                # ground truth. Non-fatal.
                try:
                    from apps.curriculum.figure_facts_extractor import (
                        extract_and_save_for_asset,
                    )
                    extract_and_save_for_asset(asset)
                except Exception:
                    pass
                if not step.media:
                    step.media = {'images': []}
                if 'images' not in step.media:
                    step.media['images'] = []
                step.media['images'].append({
                    'url': asset.file.url,
                    'alt': asset.alt_text,
                    'caption': asset.caption,
                    'description': asset.alt_text or asset.title,
                    'type': 'diagram',
                    'source': 'uploaded',
                })
                step.save()
                messages.success(request, "Image uploaded successfully.")
            else:
                messages.warning(request, "No file selected.")
            context = {
                **request.staff_ctx,
                'step': step,
                'lesson': lesson,
                'total_steps': total_steps,
                'phases': phases,
            }
            return render(request, 'dashboard/curriculum/step_edit.html', context)

        # Handle replace image action
        if action == 'replace_image':
            image_index = int(request.POST.get('image_index', 0))
            uploaded = request.FILES.get('image')
            images = step.media.get('images', []) if step.media else []
            if uploaded and 0 <= image_index < len(images):
                from apps.media_library.models import MediaAsset
                institution = lesson.unit.course.institution
                asset = MediaAsset.objects.create(
                    institution=institution,
                    title=images[image_index].get('description', '') or uploaded.name,
                    asset_type='image',
                    file=uploaded,
                    alt_text=images[image_index].get('alt', ''),
                    caption=images[image_index].get('caption', ''),
                )
                # Replacement image — re-extract facts since the visual
                # has changed. force=True overrides any stale facts.
                try:
                    from apps.curriculum.figure_facts_extractor import (
                        extract_and_save_for_asset,
                    )
                    extract_and_save_for_asset(asset, force=True)
                except Exception:
                    pass
                images[image_index]['url'] = asset.file.url
                images[image_index]['source'] = 'uploaded'
                step.save()
                messages.success(request, "Image replaced successfully.")
            else:
                messages.warning(request, "No file selected or invalid image index.")
            context = {
                **request.staff_ctx,
                'step': step,
                'lesson': lesson,
                'total_steps': total_steps,
                'phases': phases,
            }
            return render(request, 'dashboard/curriculum/step_edit.html', context)

        # Handle delete image action
        if action == 'delete_image':
            image_index = int(request.POST.get('image_index', 0))
            images = step.media.get('images', []) if step.media else []
            if 0 <= image_index < len(images):
                images.pop(image_index)
                step.save()
                messages.success(request, "Image removed.")
            else:
                messages.warning(request, "Invalid image index.")
            context = {
                **request.staff_ctx,
                'step': step,
                'lesson': lesson,
                'total_steps': total_steps,
                'phases': phases,
            }
            return render(request, 'dashboard/curriculum/step_edit.html', context)

        # Normal save — update step content
        step.phase = request.POST.get('phase', step.phase)
        step.step_type = request.POST.get('step_type', step.step_type)
        step.teacher_script = request.POST.get('teacher_script', step.teacher_script)
        step.question = request.POST.get('question', step.question)
        step.expected_answer = request.POST.get('expected_answer', step.expected_answer)
        step.answer_type = request.POST.get('answer_type', step.answer_type)

        # Parse choices (one per line)
        choices_text = request.POST.get('choices', '')
        if choices_text.strip():
            step.choices = [c.strip() for c in choices_text.split('\n') if c.strip()]
        else:
            step.choices = []

        # Parse hints (one per line) → hint_1 / hint_2 / hint_3.
        # `step.hints` is a read-only @property; assigning to it 500s.
        hints_text = request.POST.get('hints', '')
        parsed_hints = [h.strip() for h in hints_text.split('\n') if h.strip()] if hints_text.strip() else []
        step.hint_1 = parsed_hints[0] if len(parsed_hints) > 0 else ''
        step.hint_2 = parsed_hints[1] if len(parsed_hints) > 1 else ''
        step.hint_3 = parsed_hints[2] if len(parsed_hints) > 2 else ''

        step.save()

        messages.success(request, "Step updated.")
        return redirect('dashboard:lesson_detail', lesson_id=lesson.id)
    
    context = {
        **request.staff_ctx,
        'step': step,
        'lesson': lesson,
        'total_steps': total_steps,
        'phases': phases,
    }
    
    return render(request, 'dashboard/curriculum/step_edit.html', context)


@teacher_required
@require_POST
def course_generate_all(request, course_id):
    """Generate content and media for all lessons in a course."""
    from apps.dashboard.background_tasks import run_async, generate_all_content_async
    from apps.dashboard.models import CurriculumUpload
    
    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    # Guard: skip if any lesson is already generating
    from apps.curriculum.models import Lesson
    generating_count = Lesson.objects.filter(
        unit__course=course, content_status='generating'
    ).count()
    if generating_count > 0:
        messages.info(request, f"Content generation is already in progress ({generating_count} lessons generating).")
        return redirect('dashboard:course_detail', course_id=course.id)

    # Create a new processing record for progress tracking
    upload = CurriculumUpload.objects.create(
        institution=course.institution,
        created_course=course,
        status='processing',
        subject_name=course.title,
        grade_level=course.grade_level or '',
        original_filename='content_generation',
        file_path='',
        processing_log='',
        current_step=4,  # Content generation step
    )
    
    upload.add_log(f"📝 Starting content generation for {course.title}...")
    upload.save()
    
    # Start async generation - always include media
    run_async(generate_all_content_async, course_id=course.id, upload_id=upload.id, generate_media=True)
    
    return redirect('dashboard:content_progress', upload_id=upload.id)


@teacher_required
@require_POST
def course_generate_media(request, course_id):
    """Generate media for all lessons in a course."""
    from apps.dashboard.background_tasks import run_async, generate_media_async
    from apps.dashboard.models import CurriculumUpload
    
    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    # Check if force regenerate was requested
    force_regenerate = request.POST.get('force', '') == '1'

    # Create a new processing record for progress tracking
    upload = CurriculumUpload.objects.create(
        institution=course.institution,
        created_course=course,
        status='media_processing',
        subject_name=course.title,
        grade_level=course.grade_level or '',
        original_filename='media_generation',
        file_path='',
        processing_log='',
    )
    
    upload.add_log(f"🖼️ Starting media generation for {course.title}...")
    upload.add_log(f"   Force regenerate: {force_regenerate}")
    upload.save()
    
    # Start async generation
    run_async(generate_media_async, course_id=course.id, upload_id=upload.id, force_regenerate=force_regenerate)
    
    return redirect('dashboard:media_progress', upload_id=upload.id)


@teacher_required
def media_progress(request, upload_id):
    """Show media generation progress."""
    from apps.dashboard.models import CurriculumUpload
    
    institution = request.staff_ctx['institution']
    
    lookup = {'id': upload_id}
    if institution is not None:
        lookup['institution'] = institution
    upload = get_object_or_404(CurriculumUpload, **lookup)

    context = {
        **request.staff_ctx,
        'upload': upload,
        'course': upload.created_course,
    }

    return render(request, 'dashboard/curriculum/media_progress.html', context)


@teacher_required
def content_progress(request, upload_id):
    """Show content generation progress."""
    from apps.dashboard.models import CurriculumUpload

    institution = request.staff_ctx['institution']

    lookup = {'id': upload_id}
    if institution is not None:
        lookup['institution'] = institution
    upload = get_object_or_404(CurriculumUpload, **lookup)
    
    context = {
        **request.staff_ctx,
        'upload': upload,
        'course': upload.created_course,
    }
    
    return render(request, 'dashboard/curriculum/content_progress.html', context)


@teacher_required
@require_POST
def cancel_generation(request, upload_id):
    """Cancel an in-progress 'Generate All' operation."""
    from apps.dashboard.models import CurriculumUpload

    institution = request.staff_ctx['institution']
    lookup = {'id': upload_id}
    if institution is not None:
        lookup['institution'] = institution
    upload = get_object_or_404(CurriculumUpload, **lookup)

    upload.is_cancelled = True
    upload.status = 'completed'
    upload.add_log("⛔ Generation cancelled by teacher.")
    upload.save()

    # Reset any lessons still stuck in 'generating' for this course
    if upload.created_course:
        Lesson.objects.filter(
            unit__course=upload.created_course,
            content_status='generating',
        ).update(content_status='empty')

    messages.success(request, "Generation cancelled.")
    return redirect('dashboard:course_detail', course_id=upload.created_course_id)


@teacher_required
@require_POST
def cancel_lesson_generation(request, lesson_id):
    """Cancel generation for a single lesson."""
    institution = request.staff_ctx['institution']

    lookup = {'id': lesson_id}
    if institution is not None:
        lookup['unit__course__institution'] = institution
    lesson = get_object_or_404(Lesson, **lookup)

    if lesson.content_status == 'generating':
        # Set the explicit 'cancelled' sentinel — that's the ONE state
        # the worker's _is_cancelled() listens for. Status 'empty' /
        # 'pending' are normal-flow states and must NOT be read as
        # a cancel signal (that triggered the multi-spawn race).
        lesson.content_status = 'cancelled'
        lesson.updated_at = timezone.now()
        lesson.save(update_fields=['content_status', 'updated_at'])
        messages.success(request, f"Cancelled generation for '{lesson.title}'.")
    else:
        messages.info(request, f"'{lesson.title}' was not generating.")

    return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)


@teacher_required
@require_POST
def course_publish_all(request, course_id):
    """Publish all lessons in a course."""
    from apps.curriculum.models import Lesson
    
    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    # Publish all lessons that have content
    lessons = Lesson.objects.filter(unit__course=course)
    published = 0
    
    for lesson in lessons:
        if lesson.steps.count() >= 5 and not lesson.is_published:
            lesson.is_published = True
            lesson.save()
            published += 1
    
    # Publish the course
    course.is_published = True
    course.save()
    
    messages.success(request, f"Published {published} lessons and the course.")
    return redirect('dashboard:course_detail', course_id=course.id)


@teacher_required
@require_POST
def course_unpublish_all(request, course_id):
    """Unpublish all lessons in a course."""
    from apps.curriculum.models import Lesson

    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    unpublished = Lesson.objects.filter(
        unit__course=course, is_published=True
    ).update(is_published=False)

    course.is_published = False
    course.save(update_fields=['is_published'])

    messages.success(request, f"Unpublished {unpublished} lessons and the course.")
    return redirect('dashboard:course_detail', course_id=course.id)


@teacher_required
def unit_create(request, course_id):
    """Create a new unit in a course."""
    from apps.curriculum.models import Unit
    
    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        
        if title:
            # Get next order index
            max_order = course.units.aggregate(models.Max('order_index'))['order_index__max'] or -1
            
            Unit.objects.create(
                course=course,
                title=title,
                description=description,
                order_index=max_order + 1
            )
            messages.success(request, f"Unit '{title}' created.")
            return redirect('dashboard:course_detail', course_id=course.id)
        else:
            messages.error(request, "Please enter a unit title.")
    
    context = {
        **request.staff_ctx,
        'course': course,
    }
    return render(request, 'dashboard/curriculum/unit_create.html', context)


@teacher_required
def lesson_create(request, unit_id):
    """Create a new lesson in a unit."""
    from apps.curriculum.models import Unit, Lesson
    
    institution = request.staff_ctx['institution']
    if institution is not None:
        unit = get_object_or_404(Unit, id=unit_id, course__institution=institution)
    else:
        unit = get_object_or_404(Unit, id=unit_id)
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        objective = request.POST.get('objective', '').strip()
        
        if title:
            # Get next order index
            max_order = unit.lessons.aggregate(models.Max('order_index'))['order_index__max'] or -1
            
            lesson = Lesson.objects.create(
                unit=unit,
                title=title,
                objective=objective,
                order_index=max_order + 1,
                estimated_minutes=20,
            )
            messages.success(request, f"Lesson '{title}' created.")
            return redirect('dashboard:lesson_detail', lesson_id=lesson.id)
        else:
            messages.error(request, "Please enter a lesson title.")
    
    context = {
        **request.staff_ctx,
        'unit': unit,
        'course': unit.course,
    }
    return render(request, 'dashboard/curriculum/lesson_create.html', context)


@teacher_required
@require_POST
def lesson_edit_objectives(request, lesson_id):
    """Edit a lesson's enabling_objectives (rendered as "Lesson Terminal
    Objectives" in the UI). Mirrors the trim+dedup+drop-empties logic
    from curriculum_approve so server behavior matches.

    See memory/lesson_objectives_management_plan.md (L2). The previous
    decision to deprecate the EO list (commit context: pilot_session_plan)
    was reversed when teachers needed per-lesson TO management — see
    L1 commit 706b141 which restored the editable list at review time.
    """
    from apps.curriculum.models import Lesson
    institution = request.staff_ctx['institution']
    if institution is not None:
        lesson = get_object_or_404(Lesson, id=lesson_id, unit__course__institution=institution)
    else:
        lesson = get_object_or_404(Lesson, id=lesson_id)

    raw = request.POST.getlist('enabling_objectives')   # multiple inputs same name
    seen = set()
    cleaned = []
    for eo in raw:
        if not isinstance(eo, str):
            continue
        text = eo.strip()
        if not text:
            continue
        key = ' '.join(text.lower().split())
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    prior_count = len(lesson.enabling_objectives or [])
    lesson.enabling_objectives = cleaned
    lesson.save(update_fields=['enabling_objectives'])

    delta = len(cleaned) - prior_count
    if delta > 0:
        messages.success(request, f"Saved — {delta} new TO(s) added ({len(cleaned)} total).")
    elif delta < 0:
        messages.success(request, f"Saved — {-delta} TO(s) removed ({len(cleaned)} total).")
    else:
        messages.success(request, f"Saved {len(cleaned)} TO(s).")
    return redirect('dashboard:lesson_detail', lesson_id=lesson.id)


@teacher_required
@require_POST
def lesson_move_objective(request, lesson_id):
    """Move one Terminal Objective from this lesson to another lesson
    in the same unit. L4 of memory/lesson_objectives_management_plan.md.

    POST params:
      - eo_text: the exact text of the objective to move
      - target_lesson_id: the destination lesson (must be in the same unit)

    Removes from source.enabling_objectives (case-insensitive match),
    appends to target.enabling_objectives (deduped). Same-unit only —
    cross-unit moves require manual edit (different lessons live in
    different parsed contexts).
    """
    from apps.curriculum.models import Lesson
    institution = request.staff_ctx['institution']
    if institution is not None:
        source = get_object_or_404(Lesson, id=lesson_id, unit__course__institution=institution)
    else:
        source = get_object_or_404(Lesson, id=lesson_id)

    eo_text = (request.POST.get('eo_text') or '').strip()
    target_id = (request.POST.get('target_lesson_id') or '').strip()
    if not eo_text or not target_id:
        messages.error(request, "Missing objective text or target lesson.")
        return redirect('dashboard:lesson_detail', lesson_id=source.id)

    try:
        target = Lesson.objects.get(id=int(target_id), unit_id=source.unit_id)
    except (Lesson.DoesNotExist, ValueError):
        messages.error(request, "Target lesson must be in the same unit.")
        return redirect('dashboard:lesson_detail', lesson_id=source.id)

    if institution is not None and target.unit.course.institution_id != institution.id:
        messages.error(request, "Target lesson is in a different institution.")
        return redirect('dashboard:lesson_detail', lesson_id=source.id)

    # Remove from source — case-insensitive whitespace-normalized match,
    # so trivial reformatting doesn't strand the entry.
    eo_key = ' '.join(eo_text.lower().split())
    source_eos = list(source.enabling_objectives or [])
    new_source = []
    removed = False
    for eo in source_eos:
        if not isinstance(eo, str):
            continue
        if not removed and ' '.join(eo.lower().split()) == eo_key:
            removed = True
            continue
        new_source.append(eo)
    if not removed:
        messages.warning(request, f"Objective not found on this lesson — already moved?")
        return redirect('dashboard:lesson_detail', lesson_id=source.id)

    # Append to target — dedup so we don't double-add an existing entry.
    target_eos = list(target.enabling_objectives or [])
    target_keys = {' '.join(e.lower().split()) for e in target_eos if isinstance(e, str)}
    if eo_key not in target_keys:
        target_eos.append(eo_text)

    source.enabling_objectives = new_source
    source.save(update_fields=['enabling_objectives'])
    target.enabling_objectives = target_eos
    target.save(update_fields=['enabling_objectives'])

    messages.success(
        request,
        f"Moved \"{eo_text[:60]}{'…' if len(eo_text) > 60 else ''}\" "
        f"from \"{source.title}\" to \"{target.title}\".",
    )
    return redirect('dashboard:lesson_detail', lesson_id=source.id)


@teacher_required
@require_POST
def course_edit(request, course_id):
    """Edit course title, description, subject, grade levels."""
    institution = request.staff_ctx['institution']

    if institution is not None:
        course = get_object_or_404(Course, id=course_id, institution=institution)
    else:
        course = get_object_or_404(Course, id=course_id)

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    grade_level = request.POST.get('grade_level', '').strip()
    subject_code = request.POST.get('subject_code', '').strip()
    grade_levels_raw = request.POST.getlist('grade_levels')   # multi-select
    # Per-course image policy. Checkbox: present in POST = on, absent = off.
    # The reparse path doesn't include the checkbox in its hidden form,
    # so we only update tutoring_images_enabled when the canonical edit
    # form was submitted (action != 'reparse').
    images_enabled_posted = (
        request.POST.get('action') != 'reparse'
        and 'subject_code' in request.POST  # canonical edit form marker
    )
    tutoring_images_enabled = bool(request.POST.get('tutoring_images_enabled'))

    if not title:
        messages.error(request, "Course title cannot be empty.")
        return redirect('dashboard:course_detail', course_id=course.id)

    # Validate subject_code (allow empty for back-compat with reparse path
    # that doesn't supply it; required only on the explicit edit form).
    valid_subjects = {c[0] for c in Course.SubjectCode.choices}
    if subject_code and subject_code not in valid_subjects:
        messages.error(request, f"Invalid subject_code: {subject_code!r}.")
        return redirect('dashboard:course_detail', course_id=course.id)

    # Validate + normalise grade_levels
    valid_years = {c[0] for c in Course.SecondaryYear.choices}
    grade_levels = sorted({g for g in grade_levels_raw if g in valid_years})

    # Auto-derive grade_level (CharField) from grade_levels list when blank
    if not grade_level and grade_levels:
        grade_level = ",".join(grade_levels)

    course.title = title
    course.description = description
    course.grade_level = grade_level
    update_fields = ['title', 'description', 'grade_level', 'updated_at']

    # Only overwrite subject_code/grade_levels when supplied — the
    # reparse path doesn't include them in its POST and we don't want
    # to wipe stored values.
    if subject_code or 'subject_code' in request.POST:
        course.subject_code = subject_code
        update_fields.append('subject_code')
    if grade_levels or 'grade_levels' in request.POST:
        course.grade_levels = grade_levels
        update_fields.append('grade_levels')
    if images_enabled_posted:
        course.tutoring_images_enabled = tutoring_images_enabled
        update_fields.append('tutoring_images_enabled')

    course.save(update_fields=update_fields)

    # Check if re-parse was requested
    if request.POST.get('action') == 'reparse':
        from apps.dashboard.models import CurriculumUpload
        from apps.dashboard.background_tasks import run_async

        # Find the curriculum upload that created this course
        upload = None
        if course.curriculum_upload_id:
            upload = CurriculumUpload.objects.filter(id=course.curriculum_upload_id).first()
        if not upload:
            upload = CurriculumUpload.objects.filter(created_course=course).first()

        if not upload or not upload.file_path:
            messages.error(request, "No curriculum file found to re-parse. Please upload a new curriculum.")
            return redirect('dashboard:course_detail', course_id=course.id)

        # Re-parse no longer carries a duration knob. Lessons are
        # generated at max depth and the engine adapts duration at
        # runtime — see memory/max_depth_lesson_steps_plan.md.
        # Existing upload.lesson_duration_minutes (set at original
        # upload time) carries through as the default for any newly
        # created lessons; teachers change it later via the green
        # "Default lesson duration" form on the course page.
        upload.status = 'processing'
        upload.processing_log = ''
        upload.save()

        # IMPORTANT: do NOT delete units/lessons here.
        #
        # Previously this path called `course.units.all().delete()`, which
        # cascaded into LessonStep, ExitTicket, ExitTicketAttempt,
        # StudentLessonProgress, StudentSkillMastery, TutorSession, and
        # SkillPracticeLog rows. A re-parse therefore wiped the whole pilot's
        # competency history — exactly the data we are trying to preserve.
        #
        # `complete_curriculum_upload` (apps/curriculum/pipeline.py) already
        # uses `update_or_create` keyed on (course, unit_title) and
        # (unit, lesson_title). Re-parsing now upserts in place, so:
        #   - existing Lesson rows keep their PK → student progress, sessions,
        #     mastery, and exit-ticket history all survive
        #   - new lessons in the re-parsed structure are created fresh
        #   - lessons that no longer appear in the new parse are LEFT in the DB
        #     (orphans) rather than deleted — teacher can prune manually if
        #     wanted, but no data is silently destroyed
        #
        # Trade-off: a renamed lesson title creates a duplicate (the old row
        # stays under its old title, a new row is created under the new
        # title). This is the right default — losing student data to a
        # cosmetic title change would be far worse than a one-time manual
        # cleanup.

        # Re-plan lessons only (skip text extraction + vectorization — already done)
        # Auto-completes after replan: the user already explicitly chose to
        # re-parse, so we don't ask them to "approve" the result on a separate
        # page. They'd just see an empty course in between and think it failed.
        def _replan(upload_id, course_id):
            import django.db
            django.db.connections.close_all()
            try:
                from apps.dashboard.models import CurriculumUpload
                from apps.curriculum.pipeline import generate_lesson_structure, complete_curriculum_upload
                from apps.accounts.models import Institution

                up = CurriculumUpload.objects.get(id=upload_id)
                institution_id = up.institution_id or Institution.get_global().id

                up.current_step = 3
                up.add_log("📚 Re-planning lesson structure (skipping text extraction — already in KB)...")
                up.save()

                print(f"[Reparse] Step 3: Vision extraction + lesson planning for {up.subject_name}", flush=True)

                structure = generate_lesson_structure(
                    subject=up.subject_name,
                    grade_level=up.grade_level or 'S1',
                    institution_id=institution_id,
                    extracted_text='(already vectorized)',
                    file_path=up.file_path,
                )

                units_count = len(structure.get('units', []))
                lessons_count = structure.get('total_lessons', 0)
                up.add_log(f"   ✓ Found {units_count} units with {lessons_count} lessons")
                up.parsed_data = structure
                up.save()

                if lessons_count <= 0:
                    up.status = 'failed'
                    up.error_message = 'No lessons extracted'
                    up.add_log("❌ No lessons extracted — leaving course empty. Try uploading the curriculum again.")
                    up.save()
                    print(f"[Reparse] FAILED: 0 lessons extracted", flush=True)
                    return

                # Auto-complete: rebuild units/lessons immediately. Without
                # this, the course stays empty until the teacher visits the
                # curriculum_process page and clicks Approve.
                up.add_log("🛠️ Re-creating units & lessons from new structure...")
                up.save()
                complete_curriculum_upload(up.id)
                print(f"[Reparse] Done: {units_count} units, {lessons_count} lessons (auto-completed)", flush=True)

            except Exception as e:
                print(f"[Reparse] FAILED: {e}", flush=True)
                import traceback; traceback.print_exc()
                try:
                    up = CurriculumUpload.objects.get(id=upload_id)
                    up.status = 'failed'
                    up.error_message = str(e)
                    up.add_log(f"❌ Re-parse failed: {e}")
                    up.save()
                except Exception:
                    pass

        run_async(_replan, upload.id, course.id)
        messages.success(
            request,
            "Re-parsing the curriculum document. Existing lessons upsert "
            "by title (mastery + transcripts preserved). Refresh in ~1 "
            "minute to see the result.",
        )
        return redirect('dashboard:course_detail', course_id=course.id)

    messages.success(request, f"Course updated.")
    return redirect('dashboard:course_detail', course_id=course.id)


@teacher_required
@require_POST
def course_change_institution(request, course_id):
    """Move a Course from one institution to another (incl. All Schools).

    Super-admin only — moving courses across tenants is a platform-admin
    operation. Guarded against courses with generated content because the
    cascade would also need to migrate ChromaDB chunks (per-institution
    collections) and student-facing data — out of scope for v1. The user
    case is moving an empty just-created course to the right owner.

    POST params:
        target_institution_id: integer institution PK, or '' / 'platform'
            for platform-wide (institution=None).

    Allowed states:
      - Every lesson in the course must have content_status='empty' AND
        zero LessonStep rows. If any lesson has generated content, refuse.

    Side effects:
      - Course.institution updated
      - TeachingMaterialUploads linked to this course are also re-tagged
        (TeachingMaterialUpload.institution → new institution) so
        material visibility moves with the course
      - Returns to course detail; if the user's currently-selected
        institution scope no longer covers the course, falls back to
        the curriculum list
    """
    from apps.curriculum.models import Course, Lesson, LessonStep
    from apps.accounts.models import Institution

    if request.staff_ctx.get('role') != 'superadmin':
        messages.error(request, "Only super-admins can move courses across institutions.")
        return redirect('dashboard:course_detail', course_id=course_id)

    course = get_object_or_404(Course, id=course_id)

    # Guard: any lesson with non-empty content blocks the move.
    blocking_lessons = Lesson.objects.filter(unit__course=course).exclude(content_status='empty')
    blocking_count = blocking_lessons.count()
    if blocking_count == 0:
        # Belt + braces: also check for steps directly (in case a lesson
        # has steps without flipping content_status — shouldn't happen
        # but cheap to verify).
        step_count = LessonStep.objects.filter(lesson__unit__course=course).count()
        if step_count > 0:
            blocking_count = step_count
    if blocking_count:
        messages.error(
            request,
            f"Cannot move course: {blocking_count} lesson(s) have generated content. "
            f"Moving courses with content would orphan ChromaDB chunks "
            f"(per-institution collections) and student session data. "
            f"Delete the generated content (regenerate as empty) or use a "
            f"separate migration for content-bearing moves."
        )
        return redirect('dashboard:course_detail', course_id=course.id)

    raw = (request.POST.get('target_institution_id') or '').strip().lower()
    if raw in ('', 'platform', 'platform-wide', 'none', '0'):
        target = None
        target_label = 'All Schools (platform-wide)'
    else:
        try:
            target = Institution.objects.get(id=int(raw), is_active=True)
            target_label = target.name
        except (Institution.DoesNotExist, ValueError):
            messages.error(request, f"Invalid target institution: {raw!r}.")
            return redirect('dashboard:course_detail', course_id=course.id)

    # Optional collision check — same title at the target institution.
    collision = Course.objects.filter(institution=target, title=course.title).exclude(id=course.id).first()
    if collision:
        messages.warning(
            request,
            f"Heads-up: another course titled {course.title!r} already exists at "
            f"{target_label}. Both will coexist; consider renaming one for clarity."
        )

    prior_label = course.institution.name if course.institution else 'All Schools (platform-wide)'

    course.institution = target
    course.save(update_fields=['institution', 'updated_at'])

    # Re-tag any TeachingMaterialUploads tied to this course so material
    # visibility moves with the course. Materials with course=None are
    # left alone (they belong to a different scope).
    from apps.dashboard.models import TeachingMaterialUpload
    retagged = TeachingMaterialUpload.objects.filter(course=course).update(institution=target)

    msg = f"Moved {course.title!r} from {prior_label} to {target_label}."
    if retagged:
        msg += f" Re-tagged {retagged} linked teaching material(s) to the new institution."
    messages.success(request, msg)

    # If the super-admin is currently scoped to a school that's not the
    # new owner, redirect to curriculum list (course_detail would 404
    # under the institution scope filter).
    current = request.staff_ctx.get('institution')
    if current is not None and current != target:
        return redirect('dashboard:curriculum_list')
    return redirect('dashboard:course_detail', course_id=course.id)


@teacher_required
@require_POST
def course_delete(request, course_id):
    """Delete a course and all its units/lessons/steps."""
    institution = request.staff_ctx['institution']

    if institution is not None:
        course = get_object_or_404(Course, id=course_id, institution=institution)
    else:
        course = get_object_or_404(Course, id=course_id)

    title = course.title

    # Clean up teaching materials: vector chunks, files, and DB records
    from apps.dashboard.models import TeachingMaterialUpload
    materials = TeachingMaterialUpload.objects.filter(course=course)
    if materials.exists():
        try:
            from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
            from apps.accounts.models import Institution
            kb = CurriculumKnowledgeBase(institution_id=course.institution_id or Institution.get_global().id)
            collection = kb._get_collection()
            if collection:
                for mat in materials:
                    try:
                        collection.delete(where={"upload_id": mat.id})
                    except Exception as e:
                        logger.warning(f"Failed to delete vector chunks for material {mat.id}: {e}")
        except Exception as e:
            logger.warning(f"Failed to clean up vector DB for course {course_id}: {e}")

        # Delete uploaded files from disk
        import os
        for mat in materials:
            if mat.file_path and os.path.exists(mat.file_path):
                try:
                    os.remove(mat.file_path)
                except OSError as e:
                    logger.warning(f"Failed to delete file {mat.file_path}: {e}")

        materials.delete()

    course.delete()

    messages.success(request, f"Course '{title}' and its teaching materials deleted.")
    return redirect('dashboard:curriculum_list')


# ============================================================================
# Flagged Sessions (Safety)
# ============================================================================

@staff_required
def flagged_sessions(request):
    """List flagged tutoring sessions for staff review.

    Scope (Edward, 2026-05-07): SAFETY-ONLY. The flagged dashboard is
    a child-protection / classroom-safety surface — it must show only
    student messages flagged as harmful / inappropriate / manipulation
    by the LLM safety judge (apps/tutoring/judges/safety.py).

    Explicitly EXCLUDED:
      - Validator-flagged sessions (e.g. numeric_claim_contradicted) —
        teachers don't need a child-protection dashboard polluted with
        curriculum-disagreement audits. Those still surface in the
        teacher monitor / session reports.
      - off_topic flags from the legacy ContentSafetyFilter — dropped
        as a category entirely.
      - AI-output flags — unsafe tutor text never reaches the student
        because the safety judge in run_all_judges triggers regen.
    """
    from apps.tutoring.models import SessionTurn

    institution = request.staff_ctx['institution']
    status_filter = request.GET.get('status', 'unreviewed')
    # `flag_filter` query-param kept for URL-compat but ignored: the
    # only flag family rendered is safety. Existing links with
    # ?flag_type=validator|safety|all will all show the safety set.
    flag_filter = 'safety'

    # Allowed flag types — drop everything that isn't a child-protection
    # category. SessionTurn.flag_type is set by the safety judge in
    # views.respond() with one of: harmful, inappropriate, manipulation.
    SAFETY_FLAG_TYPES = ('harmful', 'inappropriate', 'manipulation')

    safety_session_ids = set(
        SessionTurn.objects
        .filter(is_flagged=True, flag_type__in=SAFETY_FLAG_TYPES)
        .values_list('session_id', flat=True)
        .distinct()
    )

    qs = TutorSession.objects.filter(
        is_flagged=True, id__in=safety_session_ids,
    )
    qs = filter_by_institution(qs, institution)
    if status_filter == 'unreviewed':
        qs = qs.filter(flag_reviewed=False)
    elif status_filter == 'reviewed':
        qs = qs.filter(flag_reviewed=True)

    qs = qs.select_related(
        'student', 'lesson', 'reviewed_by',
    ).order_by('-flagged_at', '-started_at')

    # Counts for the page header (safety-scoped now).
    total_flagged_qs = filter_by_institution(
        TutorSession.objects.filter(
            is_flagged=True, id__in=safety_session_ids,
        ),
        institution,
    )
    total_flagged = total_flagged_qs.count()
    unreviewed_count = total_flagged_qs.filter(flag_reviewed=False).count()

    # Annotate so the template marks each row as safety (no longer
    # need has_validator_flag — validator flags don't surface here).
    for s in qs:
        s.has_safety_flag = True
        s.has_validator_flag = False

    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'dashboard/flagged_sessions.html', {
        **request.staff_ctx,
        'sessions': page,
        'status_filter': status_filter,
        'flag_filter': flag_filter,
        'total_flagged': total_flagged,
        'unreviewed_count': unreviewed_count,
        # Kept in context for template compatibility; always 0 now.
        'validator_flagged_count': 0,
    })


@staff_required
def flagged_session_detail(request, session_id):
    """View transcript of a flagged session with highlighted flagged turns."""
    institution = request.staff_ctx['institution']

    qs = TutorSession.objects.filter(is_flagged=True)
    qs = filter_by_institution(qs, institution)
    session = get_object_or_404(qs.select_related('student', 'lesson', 'reviewed_by'), id=session_id)

    from apps.tutoring.models import SessionTurn
    turns = SessionTurn.objects.filter(session=session).order_by('created_at')

    return render(request, 'dashboard/flagged_session_detail.html', {
        **request.staff_ctx,
        'session': session,
        'turns': turns,
    })


@staff_required
@require_POST
def resolve_flag(request, session_id):
    """Mark a flagged session as reviewed and optionally re-approve the student."""
    institution = request.staff_ctx['institution']

    qs = TutorSession.objects.filter(is_flagged=True)
    qs = filter_by_institution(qs, institution)
    session = get_object_or_404(qs, id=session_id)

    session.flag_reviewed = True
    session.reviewed_by = request.user
    session.reviewed_at = timezone.now()
    session.save(update_fields=['flag_reviewed', 'reviewed_by', 'reviewed_at'])

    # If the student is suspended, lift the suspension (teacher has reviewed)
    try:
        from apps.accounts.models import StudentProfile
        profile = StudentProfile.objects.filter(user=session.student).first()
        if profile and profile.is_tutor_suspended:
            profile.is_tutor_suspended = False
            profile.tutor_suspended_reason = (
                f"{profile.tutor_suspended_reason}\n"
                f"Re-approved by {request.user.get_full_name() or request.user.username} "
                f"on {timezone.now().strftime('%Y-%m-%d %H:%M')}"
            )
            profile.save(update_fields=['is_tutor_suspended', 'tutor_suspended_reason'])
            messages.success(request, f"Flag resolved and {session.student.get_full_name() or session.student.username}'s tutor access restored.")
            return redirect('dashboard:flagged_session_detail', session_id=session.id)
    except Exception:
        pass

    messages.success(request, "Flag resolved.")
    return redirect('dashboard:flagged_session_detail', session_id=session.id)

@login_required
def seed_demo_school(request):
    """Run the seed_demo_school management command (superadmin only)."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Superadmin only")

    from django.core.management import call_command
    from io import StringIO

    reset = request.GET.get('reset', '') == '1'
    output = StringIO()

    try:
        args = ['--reset'] if reset else []
        call_command('seed_demo_school', *args, stdout=output, stderr=output)
        result = output.getvalue()
        messages.success(request, f"Demo school seeded successfully.")
    except Exception as e:
        result = f"Error: {e}\n{output.getvalue()}"
        messages.error(request, f"Seed failed: {e}")

    from django.http import HttpResponse
    return HttpResponse(
        f"<pre>{result}</pre><br><a href='/dashboard/'>Back to Dashboard</a>",
        content_type='text/html'
    )


@teacher_required
@require_POST
def reindex_materials(request, course_id):
    """Re-index completed materials into KB (after embedding change)."""
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.dashboard.background_tasks import run_async

    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    completed = TeachingMaterialUpload.objects.filter(
        course=course, status='completed'
    )
    count = completed.count()

    if count == 0:
        messages.info(request, "No completed materials to reindex.")
        return redirect('dashboard:course_detail', course_id=course.id)

    material_ids = list(completed.values_list('id', flat=True))

    def _reindex(ids):
        import django.db
        django.db.connections.close_all()
        from apps.dashboard.models import TeachingMaterialUpload
        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
        from apps.accounts.models import Institution

        for mid in ids:
            try:
                upload = TeachingMaterialUpload.objects.get(id=mid)
                import os
                if not os.path.exists(upload.file_path):
                    print(f"[Reindex] Skip {upload.original_filename}: file not found", flush=True)
                    continue

                inst_id = upload.institution_id or Institution.get_global().id
                kb = CurriculumKnowledgeBase(institution_id=inst_id)
                result = kb.index_teaching_material(
                    file_path=upload.file_path,
                    subject=upload.subject_name,
                    grade_level=upload.grade_level,
                    material_title=upload.title,
                    material_type=upload.material_type,
                    upload_id=upload.id,
                )
                chunks = result.get('chunks_indexed', 0)
                upload.chunks_created = chunks
                upload.save(update_fields=['chunks_created'])
                print(f"[Reindex] {upload.original_filename}: {chunks} chunks", flush=True)
            except Exception as e:
                print(f"[Reindex] FAILED {mid}: {e}", flush=True)

    run_async(_reindex, material_ids)
    messages.success(request, f"Reindexing {count} materials into knowledge base.")
    return redirect('dashboard:course_detail', course_id=course.id)


# ============================================================================
# Live Monitor & Chat History
# ============================================================================

@staff_required
def lesson_live_monitor(request, lesson_id):
    """Real-time monitoring of active tutoring sessions for a lesson."""
    from apps.curriculum.models import Lesson

    institution = request.staff_ctx['institution']
    if institution is not None:
        lesson = get_object_or_404(
            Lesson,
            Q(unit__course__institution=institution) | Q(unit__course__institution__isnull=True),
            id=lesson_id,
        )
    else:
        lesson = get_object_or_404(Lesson, id=lesson_id)

    # Exclude abandoned sessions from the live monitor — they're
    # noise (a student who started, bounced, then started fresh
    # appears 5× otherwise) and the live monitor's purpose is "what
    # is happening now or just completed". Teachers who specifically
    # want abandoned chats can find them via the student detail page.
    # Also dedupe by student: keep only the most recent session per
    # student (handles edge cases where a student has multiple
    # active or completed rows for the same lesson).
    raw_sessions = (
        TutorSession.objects
        .filter(lesson=lesson)
        .exclude(status='abandoned')
        .select_related('student')
        .prefetch_related('turns')
        .annotate(last_turn_at=Max('turns__created_at'))
        .order_by('-started_at')
    )
    seen_students = set()
    sessions = []
    for s in raw_sessions:
        if s.student_id in seen_students:
            continue
        seen_students.add(s.student_id)
        sessions.append(s)

    # Latest exit-ticket attempt per session — the engine only writes
    # exit_ticket_score to engine_state when the student passes, so pulling
    # from ExitTicketAttempt covers both pass and fail (remediation) paths.
    from apps.tutoring.models import ExitTicketAttempt
    latest_attempts = {}
    for att in (ExitTicketAttempt.objects
                .filter(exit_ticket__lesson=lesson, session__in=sessions)
                .order_by('session_id', '-completed_at')):
        latest_attempts.setdefault(att.session_id, att)

    now = timezone.now()
    IDLE_THRESHOLD_SECONDS = 5 * 60
    total_lesson_steps = lesson.steps.count()

    session_data = []
    for session in sessions:
        state = session.engine_state or {}

        # Active engagement: sum gaps between consecutive turns, clipping any
        # gap > 5 min so a student who left the tab open for days doesn't rack
        # up wall-clock minutes. Gives "time actually tutoring" not "wall clock".
        # Gaps are also floored at 0: started_lesson_at can be set retroactively
        # (or by a later code path), so it may post-date the first turn — in
        # which case the first gap would be negative and corrupt the total.
        duration = None
        turn_times = sorted(t.created_at for t in session.turns.all())
        if turn_times:
            active_seconds = 0.0
            prev = session.started_lesson_at or turn_times[0]
            for t in turn_times:
                gap = (t - prev).total_seconds()
                active_seconds += max(0, min(gap, IDLE_THRESHOLD_SECONDS))
                prev = t
            # Include time since last turn if still active
            if session.status == 'active' and not session.ended_at:
                tail = (now - prev).total_seconds()
                active_seconds += max(0, min(tail, IDLE_THRESHOLD_SECONDS))
            duration = round(active_seconds / 60, 1)

        is_idle = False
        idle_minutes = None
        if session.status == 'active' and session.last_turn_at:
            seconds_since_turn = (now - session.last_turn_at).total_seconds()
            if seconds_since_turn > IDLE_THRESHOLD_SECONDS:
                is_idle = True
                idle_minutes = round(seconds_since_turn / 60)

        is_remediation = state.get('is_remediation', False)
        current_step = state.get('current_topic_index', 0) + 1
        if total_lesson_steps:
            current_step = min(current_step, total_lesson_steps)

        attempt = latest_attempts.get(session.id)
        exit_score = attempt.score if attempt else None
        exit_passed = attempt.passed if attempt else None
        exit_total = 10  # exit tickets are fixed at 10 questions
        has_exit_review = bool(attempt)
        # Treat as completed if the session is done OR latest attempt passed
        # (covers teacher overrides that bumped a student to passing).
        is_completed = session.status == 'completed' or bool(exit_passed)

        # Group info — surface "this session has N students sharing
        # the device" so teachers watching live know it's a group
        # session, not solo. The participants list lives on the
        # session via SessionParticipant rows.
        try:
            participant_users = list(session.active_students)
        except Exception:
            participant_users = []
        is_group_session = len(participant_users) > 1
        if is_group_session:
            participant_names = [
                (u.get_full_name() or u.username) for u in participant_users
            ]
        else:
            participant_names = []

        session_data.append({
            'session': session,
            'student_name': session.student.get_full_name() or session.student.username,
            'status': session.status,
            'is_idle': is_idle,
            'idle_minutes': idle_minutes,
            'cognitive_load': state.get('cognitive_load', 0.5),
            'current_step': current_step,
            'total_steps': total_lesson_steps,
            'exchange_count': state.get('exchange_count', 0),
            'display_phase': state.get('display_phase', ''),
            'is_remediation': is_remediation,
            'duration_minutes': duration,
            'exit_score': exit_score,
            'exit_total': exit_total,
            'exit_passed': exit_passed,
            'has_exit_review': has_exit_review,
            'is_completed': is_completed,
            'covered_eos': state.get('covered_enabling_objectives', []),
            'failed_eos': state.get('exit_ticket_failed_eos', []),
            'is_group': is_group_session,
            'participant_count': len(participant_users) or 1,
            'participant_names': participant_names,
        })

    # Monitor AI: auto-issue guidance to tutors for struggling students,
    # debounced to once per 5 min per lesson so page refreshes don't spam.
    _maybe_run_monitor_ai(lesson, session_data)

    # Surface recent AI guidance so the teacher can see what the Monitor AI
    # has told the tutors (read-only banner, no form).
    from apps.tutoring.models import TeacherGuidance
    recent_ai_guidances = list(
        TeacherGuidance.objects
        .filter(session__lesson=lesson, is_from_ai=True)
        .select_related('session__student')
        .order_by('-created_at')[:5]
        .values('message', 'created_at', 'session__student__first_name',
                'session__student__last_name', 'session__student__username')
    )
    for g in recent_ai_guidances:
        first = g.pop('session__student__first_name', '') or ''
        last = g.pop('session__student__last_name', '') or ''
        username = g.pop('session__student__username', '') or ''
        full = f"{first} {last}".strip()
        g['student_name'] = full or username

    context = {
        **request.staff_ctx,
        'lesson': lesson,
        'course': lesson.unit.course,
        'sessions': session_data,
        'active_count': sum(1 for s in session_data if s['status'] == 'active' and not s['is_idle'] and not s['is_completed']),
        'idle_count': sum(1 for s in session_data if s['is_idle'] and not s['is_completed']),
        'completed_count': sum(1 for s in session_data if s['is_completed']),
        'struggling_count': sum(
            1 for s in session_data
            if s['status'] == 'active' and not s['is_idle'] and s['cognitive_load'] > 0.7
        ),
        'recent_ai_guidances': recent_ai_guidances,
    }
    return render(request, 'dashboard/lesson_monitor.html', context)


def _maybe_run_monitor_ai(lesson, session_data):
    """Debounced Monitor-AI pass: if no AI guidance has been issued for this
    lesson in the last 5 minutes, scan active sessions and inject guidance
    into the tutor for anyone with cognitive_load > 0.7."""
    from apps.tutoring.models import TutorSession, TeacherGuidance

    debounce_since = timezone.now() - timedelta(minutes=5)
    recent = TeacherGuidance.objects.filter(
        session__lesson=lesson,
        is_from_ai=True,
        created_at__gte=debounce_since,
    ).exists()
    if recent:
        return

    struggling_ids = [
        s['session'].id for s in session_data
        if s['status'] == 'active' and not s['is_idle'] and s['cognitive_load'] > 0.7
    ]
    if not struggling_ids:
        return

    sessions = TutorSession.objects.filter(id__in=struggling_ids).select_related('student')
    for session in sessions:
        state = session.engine_state or {}
        load = state.get('cognitive_load', 0.5)
        phase = state.get('display_phase', '')
        msg = (
            f"Monitor AI: this student's cognitive load is {load:.2f} (high). "
            f"Slow down, use simpler language, provide one worked example at a time, "
            f"and break the current concept into smaller steps. Be extra encouraging. "
            f"Currently in phase: {phase or 'unknown'}."
        )
        TeacherGuidance.objects.create(
            session=session,
            author=None,
            message=msg,
            is_from_ai=True,
        )


@teacher_required
@require_POST
def send_guidance(request, lesson_id):
    """Send teacher guidance to active tutor sessions."""
    from apps.tutoring.models import TutorSession, TeacherGuidance
    from apps.curriculum.models import Lesson

    institution = request.staff_ctx['institution']
    lesson = get_object_or_404(Lesson, id=lesson_id)

    session_ids = request.POST.getlist('session_ids')
    message = request.POST.get('message', '').strip()
    is_ai = request.POST.get('ai_guidance') == '1'

    if is_ai:
        # Generate AI guidance based on class patterns
        sessions = TutorSession.objects.filter(lesson=lesson, status='active')
        struggling = []
        for s in sessions:
            state = s.engine_state or {}
            if state.get('cognitive_load', 0.5) > 0.7:
                name = s.student.get_full_name() or s.student.username
                struggling.append(f"{name} (load: {state.get('cognitive_load', 0.5):.1f})")

        if struggling:
            message = (
                f"Monitor AI detected {len(struggling)} struggling student(s): "
                f"{', '.join(struggling[:5])}. "
                f"For these students: use simpler language, provide more worked examples, "
                f"and break concepts into smaller steps. Be extra encouraging."
            )
        else:
            message = "All students are progressing well. Continue at current pace."
        session_ids = ['all']

    if not message:
        messages.error(request, "Please enter a guidance message.")
        return redirect('dashboard:lesson_monitor', lesson_id=lesson.id)

    # Determine target sessions
    if 'all' in session_ids:
        target_sessions = TutorSession.objects.filter(lesson=lesson, status='active')
    else:
        target_sessions = TutorSession.objects.filter(id__in=[int(x) for x in session_ids if x.isdigit()])

    count = 0
    for session in target_sessions:
        TeacherGuidance.objects.create(
            session=session,
            author=request.user if not is_ai else None,
            message=message,
            is_from_ai=is_ai,
        )
        count += 1

    source = "Monitor AI" if is_ai else "Teacher"
    messages.success(request, f"{source} guidance sent to {count} session(s).")
    return redirect('dashboard:lesson_monitor', lesson_id=lesson.id)


@staff_required
def session_chat_history(request, session_id):
    """View the full chat history of a tutoring session."""
    from apps.tutoring.models import SessionTurn

    institution = request.staff_ctx['institution']
    qs = TutorSession.objects.all()
    qs = filter_by_institution(qs, institution)
    session = get_object_or_404(qs.select_related('student', 'lesson'), id=session_id)

    turns = SessionTurn.objects.filter(session=session).order_by('created_at')
    state = session.engine_state or {}

    # Active engagement (same calc as live monitor — clip turn-to-turn gaps > 5 min
    # so multi-day sessions don't show 4-digit minute totals). Floor at 0 because
    # started_lesson_at can post-date the first turn.
    IDLE_CAP_SECONDS = 5 * 60
    duration_minutes = None
    turn_times = list(turns.values_list('created_at', flat=True))
    if turn_times:
        active_seconds = 0.0
        prev = session.started_lesson_at or turn_times[0]
        for t in turn_times:
            gap = (t - prev).total_seconds()
            active_seconds += max(0, min(gap, IDLE_CAP_SECONDS))
            prev = t
        if session.status == 'active' and not session.ended_at:
            tail = (timezone.now() - prev).total_seconds()
            active_seconds += max(0, min(tail, IDLE_CAP_SECONDS))
        duration_minutes = round(active_seconds / 60, 1)

    context = {
        **request.staff_ctx,
        'session': session,
        'turns': turns,
        'lesson': session.lesson,
        'student_name': session.student.get_full_name() or session.student.username,
        'cognitive_load': state.get('cognitive_load', 0.5),
        'duration_minutes': duration_minutes,
        # Exchange count = engine-tracked counter, same source the
        # live monitor uses. Fall back to len(student turns) when
        # state is empty (legacy sessions).
        'exchange_count': state.get(
            'exchange_count',
            sum(1 for t in turns if t.role == 'student'),
        ),
        'exit_score': state.get('exit_ticket_score'),
        'exit_total': state.get('exit_ticket_total'),
        'covered_eos': state.get('covered_enabling_objectives', []),
        'failed_eos': state.get('exit_ticket_failed_eos', []),
    }
    return render(request, 'dashboard/session_chat_history.html', context)


@staff_required
def session_exit_review(request, session_id):
    """Review a student's exit ticket: each question with their answer vs the correct answer."""
    from apps.tutoring.models import ExitTicketAttempt, ExitTicketQuestion

    institution = request.staff_ctx['institution']
    qs = TutorSession.objects.all()
    qs = filter_by_institution(qs, institution)
    session = get_object_or_404(qs.select_related('student', 'lesson'), id=session_id)

    attempts = list(
        ExitTicketAttempt.objects
        .filter(session=session)
        .select_related('exit_ticket')
        .order_by('-completed_at')
    )
    if not attempts:
        messages.info(request, "This student hasn't completed the exit ticket yet.")
        return redirect('dashboard:lesson_monitor', lesson_id=session.lesson_id)

    latest = attempts[0]

    # Map stored answers (in presentation order) back to the question records
    # via the engine_state's selected_exit_ticket_ids (same randomized order).
    state = session.engine_state or {}
    selected_ids = state.get('selected_exit_ticket_ids', [])
    q_map = {q.id: q for q in ExitTicketQuestion.objects.filter(id__in=selected_ids)}
    ordered_questions = [q_map[qid] for qid in selected_ids if qid in q_map]

    stored_answers = latest.answers if isinstance(latest.answers, list) else []

    def _mcq_letter(q, raw):
        """Best-effort: render the student's MCQ selection as a letter (A-D)."""
        if not raw:
            return ''
        s = str(raw).strip()
        if len(s) == 1 and s.upper() in 'ABCD':
            return s.upper()
        for letter in 'ABCD':
            opt = getattr(q, f'option_{letter.lower()}', '') or ''
            if opt and opt.strip().lower() == s.lower():
                return letter
        return s

    import ast
    def _maybe_parse(raw):
        """Storage stringifies answers via str(...) — recover lists/dicts when possible."""
        if not isinstance(raw, str):
            return raw
        s = raw.strip()
        if s.startswith(('[', '{')) and s.endswith((']', '}')):
            try:
                return ast.literal_eval(s)
            except (ValueError, SyntaxError):
                return raw
        return raw

    items = []
    for i, q in enumerate(ordered_questions):
        ans = stored_answers[i] if i < len(stored_answers) else {}
        if not isinstance(ans, dict):
            ans = {}
        selected = _maybe_parse(ans.get('selected', ''))
        is_correct = ans.get('correct', False)
        q_type = ans.get('question_type') or getattr(q, 'question_type', 'mcq') or 'mcq'
        selected_letter = _mcq_letter(q, selected) if q_type == 'mcq' else ''
        ad = q.answer_data or {}

        options = []
        if q_type == 'mcq':
            for letter in ('A', 'B', 'C', 'D'):
                text = getattr(q, f'option_{letter.lower()}', '') or ''
                if text:
                    options.append({
                        'letter': letter,
                        'text': text,
                        'is_selected': letter == selected_letter,
                        'is_correct_option': letter == q.correct_answer,
                    })

        # Type-specific reconstruction so the teacher sees what the student saw
        fill_template = ''
        fill_blanks_correct = []
        fill_student_blanks = []
        matching_rows = []
        data_description = ''

        if q_type == 'fill_in_blank':
            fill_template = ad.get('text_template', '') or ''
            fill_blanks_correct = ad.get('blanks', []) or []
            # Student answer is typically a list of strings (one per blank)
            if isinstance(selected, list):
                fill_student_blanks = [str(b) for b in selected]
            elif isinstance(selected, str):
                fill_student_blanks = [selected]
        elif q_type == 'matching':
            pairs = ad.get('pairs', []) or []
            student_map = selected if isinstance(selected, dict) else {}
            for p in pairs:
                left = p.get('left', '')
                correct_right = p.get('right', '')
                student_right = student_map.get(left, '') if isinstance(student_map, dict) else ''
                matching_rows.append({
                    'left': left,
                    'student_right': student_right,
                    'correct_right': correct_right,
                    'is_match': str(student_right).strip().lower() == str(correct_right).strip().lower(),
                })
        elif q_type in ('short_answer', 'data_interpretation'):
            data_description = ad.get('data_description', '') or ''

        # Best-guess "expected answer" string for non-MCQ
        if q_type == 'fill_in_blank':
            correct_answer_display = ', '.join(fill_blanks_correct)
        elif q_type == 'matching':
            correct_answer_display = '; '.join(
                f"{r['left']} → {r['correct_right']}" for r in matching_rows
            )
        elif q_type in ('short_answer', 'data_interpretation'):
            correct_answer_display = ad.get('model_answer', '') or ''
        else:
            correct_answer_display = ''

        # Render student answer for free-text/list cases
        if q_type == 'fill_in_blank':
            selected_display = ', '.join(fill_student_blanks) or '(no answer)'
        elif q_type == 'matching':
            selected_display = '; '.join(
                f"{r['left']} → {r['student_right'] or '(blank)'}" for r in matching_rows
            )
        elif isinstance(selected, (list, dict)):
            import json as _json
            selected_display = _json.dumps(selected)
        else:
            selected_display = str(selected) if selected else ''

        items.append({
            'index': i + 1,
            'question_text': q.question_text,
            'explanation': q.explanation,
            'question_type': q_type,
            'options': options,
            'selected_letter': selected_letter,
            'selected_text': selected_display,
            'fill_template': fill_template,
            'fill_blanks_correct': fill_blanks_correct,
            'fill_student_blanks': fill_student_blanks,
            'matching_rows': matching_rows,
            'data_description': data_description,
            'correct_letter': q.correct_answer,
            'correct_answer_display': correct_answer_display,
            'is_correct': is_correct,
            'concept_tag': ans.get('concept_tag', '') or q.concept_tag,
        })

    context = {
        **request.staff_ctx,
        'session': session,
        'lesson': session.lesson,
        'student_name': session.student.get_full_name() or session.student.username,
        'attempt': latest,
        'attempt_id': latest.id,
        'all_attempts': attempts,
        'items': items,
        'score': latest.score,
        'total': len(ordered_questions) or 10,
        'passed': latest.passed,
    }
    return render(request, 'dashboard/session_exit_review.html', context)


@staff_required
@require_POST
def session_exit_review_override(request, attempt_id):
    """Teacher overrides the correct/incorrect mark on a single exit-ticket question.

    Body: {"index": <0-based question index>, "is_correct": true/false}
    Recomputes attempt.score and attempt.passed from the updated answers list.
    """
    from apps.tutoring.models import ExitTicketAttempt

    institution = request.staff_ctx['institution']
    qs = ExitTicketAttempt.objects.select_related('session__lesson__unit__course')
    if institution is not None:
        qs = qs.filter(
            Q(session__lesson__unit__course__institution=institution)
            | Q(session__lesson__unit__course__institution__isnull=True)
        )
    attempt = get_object_or_404(qs, id=attempt_id)

    try:
        data = json.loads(request.body)
        idx = int(data.get('index'))
        is_correct = bool(data.get('is_correct'))
    except (json.JSONDecodeError, TypeError, ValueError):
        return JsonResponse({'error': 'Invalid payload'}, status=400)

    answers = attempt.answers if isinstance(attempt.answers, list) else []
    if idx < 0 or idx >= len(answers):
        return JsonResponse({'error': 'Index out of range'}, status=400)

    answers[idx] = {**(answers[idx] if isinstance(answers[idx], dict) else {}),
                    'correct': is_correct,
                    'teacher_override': True}
    attempt.answers = answers
    attempt.score = sum(1 for a in answers if isinstance(a, dict) and a.get('correct'))
    attempt.passed = attempt.score >= 8
    attempt.save(update_fields=['answers', 'score', 'passed'])

    # If the override pushed a failing session over the pass line, complete it.
    session = attempt.session
    session_completed = False
    if attempt.passed and session and session.status != TutorSession.Status.COMPLETED:
        session.status = TutorSession.Status.COMPLETED
        session.ended_at = session.ended_at or timezone.now()
        session.completed_lesson_at = session.completed_lesson_at or timezone.now()
        session.mastery_achieved = True
        session.save(update_fields=['status', 'ended_at', 'completed_lesson_at', 'mastery_achieved'])
        session_completed = True

    return JsonResponse({
        'ok': True,
        'score': attempt.score,
        'passed': attempt.passed,
        'is_correct': is_correct,
        'session_completed': session_completed,
    })


# ============================================================================
# EXIT-TICKET FIGURE EDIT — gpt-image-2 prompt → raster URL
# ============================================================================
#
# Per the unified image-gen decision (memory/feedback_image_gen_unified.md),
# the SVG-template path is dead. The teacher provides a free-text prompt;
# we generate via OpenAI gpt-image-2 (with Gemini fallback) through
# ImageGenerationService and persist the URL as `answer_data.figure_url`.
# Any legacy figure_spec / figure_svg fields are stripped on first save.

@teacher_required
def exit_ticket_figure_edit(request, question_id):
    """Generate or replace a question figure via gpt-image-2.

    GET: render form (current figure preview + prompt textarea).
    POST: read prompt, generate via gpt-image-2 (model_override or default),
    persist URL as answer_data.figure_url.
    """
    from apps.tutoring.image_service import ImageGenerationService

    question = _question_for_staff(request, question_id)
    if question is None:
        raise Http404("Question not found or not yours.")

    answer_data = question.answer_data or {}
    if not isinstance(answer_data, dict):
        answer_data = {}

    # Seed prompt from existing figure_prompt OR an old description if any.
    figure_prompt = answer_data.get('figure_prompt') or answer_data.get('figure_description') or ''

    error = None
    success = None

    if request.method == 'POST':
        if request.POST.get('action') == 'remove':
            for k in ('figure_prompt', 'figure_url', 'figure_svg', 'figure_spec',
                      'figure', 'figure_source', 'figure_description'):
                answer_data.pop(k, None)
            question.answer_data = answer_data
            question.save(update_fields=['answer_data'])
            success = 'Figure removed.'
            figure_prompt = ''
        else:
            new_prompt = (request.POST.get('figure_prompt') or '').strip()
            model_choice = (request.POST.get('model_choice') or '').strip().lower()
            if not new_prompt:
                error = 'Prompt is empty.'
            else:
                # Resolve lesson + institution for the image service.
                lesson = (
                    question.exit_ticket.lesson if question.exit_ticket else None
                )
                inst = (
                    lesson.unit.course.institution
                    if lesson and lesson.unit and lesson.unit.course
                    else None
                )
                svc = ImageGenerationService(lesson=lesson, institution=inst)
                # When the question already has a figure, pass it as
                # vision context so the model EDITS the existing
                # image rather than regenerating from scratch — keeps
                # the prior figure's good parts and only changes what
                # the new prompt describes (Edward, 2026-05-07).
                current_url = (answer_data or {}).get('figure_url') or ''
                result = svc.get_or_generate_image(
                    prompt=new_prompt,
                    category='diagram',
                    model_override=model_choice or None,
                    current_image_url=current_url or None,
                )
                if result and result.get('url'):
                    # Strip ALL legacy figure fields so figure_url is the
                    # single source of truth.
                    for legacy in ('figure_svg', 'figure_spec', 'figure',
                                   'figure_source', 'figure_description'):
                        answer_data.pop(legacy, None)
                    answer_data['figure_prompt'] = new_prompt
                    answer_data['figure_url'] = result['url']
                    if result.get('model'):
                        answer_data['figure_model'] = result['model']
                    question.answer_data = answer_data
                    question.save(update_fields=['answer_data'])
                    success = f"Figure generated with {result.get('model') or 'image gen'}."
                    figure_prompt = new_prompt
                else:
                    err = getattr(svc, 'last_error', None)
                    error = f"Image generation failed: {err}" if err else "Image generation returned no result."

    return render(request, 'dashboard/exit_ticket_figure_edit.html', {
        'question': question,
        'figure_prompt': figure_prompt,
        'figure_url': answer_data.get('figure_url', ''),
        'figure_svg': answer_data.get('figure_svg', ''),  # legacy display only
        'figure_model': answer_data.get('figure_model', ''),
        'error': error,
        'success': success,
    })


@teacher_required
def exit_ticket_figure_regenerate(request, question_id):
    """Deprecated. The SVG-template path was retired in favour of
    gpt-image-2 raster generation. Use exit_ticket_figure_edit, which
    now takes a free-text prompt and runs ImageGenerationService.

    Kept as a 410 Gone so any cached front-end requests fail loudly.
    """
    return JsonResponse({
        'ok': False,
        'error': "Figure spec/SVG editing was retired. Use the prompt-based image edit on this question's figure page.",
    }, status=410)


@teacher_required
def exit_ticket_figure_delete(request, question_id):
    """Remove the figure (spec + svg + url) from a question."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)

    question = _question_for_staff(request, question_id)
    if question is None:
        return JsonResponse({'ok': False, 'error': 'not found'}, status=404)

    answer_data = question.answer_data or {}
    if not isinstance(answer_data, dict):
        answer_data = {}
    for key in ('figure_spec', 'figure_svg', 'figure_url', 'figure_source', 'figure_description'):
        answer_data.pop(key, None)
    question.answer_data = answer_data
    question.save(update_fields=['answer_data'])
    return JsonResponse({'ok': True})


def _question_for_staff(request, question_id):
    """Resolve an ExitTicketQuestion the staff member is allowed to
    edit. Honors institution scoping the same way lesson_detail does.

    Returns None when the question doesn't exist or the staff isn't
    in the owning institution (and isn't an all-schools admin).
    """
    from apps.tutoring.models import ExitTicketQuestion

    institution = request.staff_ctx.get('institution')
    qs = ExitTicketQuestion.objects.select_related(
        'exit_ticket__lesson__unit__course__institution'
    )
    if institution is not None:
        qs = qs.filter(
            Q(exit_ticket__lesson__unit__course__institution=institution)
            | Q(exit_ticket__lesson__unit__course__institution__isnull=True)
        )
    return qs.filter(id=question_id).first()


# ============================================================================
# Feedback / Bug Reports (Pilot Task 2)
# ============================================================================

@login_required
@require_POST
def feedback_submit(request):
    """Receive a bug report or feedback from any authenticated page.

    Accepts BOTH application/json (legacy clients without
    screenshots) AND multipart/form-data (new — when the user opts
    to attach a real-pixel screenshot via getDisplayMedia()). The
    multipart form looks like:

        message=<text>&kind=bug&severity=medium&page_url=...
        + screenshot=<PNG file>  (optional, only when user opted in)
    """
    from apps.dashboard.models import FeedbackReport

    # Multipart path — screenshot may be attached.
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        message = (request.POST.get('message') or '').strip()
        kind = (request.POST.get('kind') or 'bug').strip()
        severity = (request.POST.get('severity') or 'medium').strip()
        page_url = (request.POST.get('page_url') or '')[:500]
        screenshot = request.FILES.get('screenshot')
    else:
        try:
            body = json.loads(request.body or "{}")
        except (ValueError, TypeError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        message = (body.get('message') or '').strip()
        kind = (body.get('kind') or 'bug').strip()
        severity = (body.get('severity') or 'medium').strip()
        page_url = (body.get('page_url') or '')[:500]
        screenshot = None

    if not message:
        return JsonResponse({"error": "Message is required"}, status=400)
    if kind not in {c[0] for c in FeedbackReport.Kind.choices}:
        kind = FeedbackReport.Kind.BUG
    if severity not in {c[0] for c in FeedbackReport.Severity.choices}:
        severity = FeedbackReport.Severity.MEDIUM

    institution = None
    membership = (
        Membership.objects.filter(user=request.user, is_active=True)
        .select_related('institution')
        .first()
    )
    if membership:
        institution = membership.institution

    FeedbackReport.objects.create(
        user=request.user,
        institution=institution,
        kind=kind,
        severity=severity,
        message=message[:4000],
        page_url=page_url,
        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:500],
        screenshot=screenshot,
    )
    return JsonResponse({"ok": True})


@login_required
def feedback_list(request):
    """Super-admin list of feedback reports. Teachers cannot view —
    feedback often references issues with content / other teachers'
    work and isn't a per-school management tool."""
    from apps.dashboard.models import FeedbackReport

    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(
            request,
            "Feedback reports are restricted to platform admins.",
        )
        return redirect('dashboard:home')

    qs = FeedbackReport.objects.select_related('user', 'institution', 'resolved_by')

    show = request.GET.get('show', 'open')
    if show == 'open':
        qs = qs.filter(is_resolved=False)
    elif show == 'resolved':
        qs = qs.filter(is_resolved=True)

    paginator = Paginator(qs, 30)
    page = paginator.get_page(request.GET.get('page', 1))

    ctx = get_staff_context(request) or {}
    return render(request, 'dashboard/feedback/list.html', {
        **ctx,
        'reports': page,
        'show': show,
        'open_count': FeedbackReport.objects.filter(is_resolved=False).count(),
    })


@login_required
@require_POST
def feedback_resolve(request, report_id):
    """Mark a feedback report resolved (or reopen it). Super-admin only."""
    from apps.dashboard.models import FeedbackReport

    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse(
            {"error": "Feedback reports are restricted to platform admins."},
            status=403,
        )

    report = get_object_or_404(FeedbackReport, id=report_id)
    notes = (request.POST.get('notes') or '').strip()

    if report.is_resolved:
        report.is_resolved = False
        report.resolved_at = None
        report.resolved_by = None
    else:
        report.is_resolved = True
        report.resolved_at = timezone.now()
        report.resolved_by = request.user
        if notes:
            report.resolution_notes = notes
    report.save()
    return redirect('dashboard:feedback_list')


# ============================================================================
# Help / FAQ (Pilot Task 3)
# ============================================================================

def help_index(request):
    """Single-page in-app help with collapsible sections + slots for short
    instructional videos. Public — anonymous visitors land here from the
    home page's Help link. Staff-only sections show conditionally on
    role. See `memory/pilot_launch_execution.md`."""
    is_staff_user = False
    if request.user.is_authenticated:
        is_staff_user = (
            request.user.is_staff
            or Membership.objects.filter(
                user=request.user, role='staff', is_active=True,
            ).exists()
        )
    return render(request, 'help/index.html', {
        'is_staff_user': is_staff_user,
    })


# ============================================================================
# Course-level summative exams (Pilot)
# ============================================================================

@teacher_required
@require_POST
def summative_generate(request, course_id):
    """Kick off summative generation for a course in the background."""
    from apps.dashboard.background_tasks import run_async
    from apps.tutoring.summative_generator import generate_summative_for_course

    institution = request.staff_ctx['institution']
    if institution is not None:
        course = get_object_or_404(
            Course,
            Q(institution=institution) | Q(institution__isnull=True),
            id=course_id,
        )
    else:
        course = get_object_or_404(Course, id=course_id)

    def _run(cid):
        import django.db
        django.db.connections.close_all()
        try:
            from apps.curriculum.models import Course as _Course
            c = _Course.objects.get(id=cid)
            print(f"[Summative] starting generation for {c.title}", flush=True)
            result = generate_summative_for_course(c)
            print(f"[Summative] done: {result}", flush=True)
        except Exception as e:
            print(f"[Summative] FAILED: {e}", flush=True)
            import traceback; traceback.print_exc()

    run_async(_run, course.id)
    messages.success(
        request,
        f"Generating summative exam for {course.title}. Check back in 1–2 minutes.",
    )
    return redirect('dashboard:summative_review', course_id=course.id)


@teacher_required
def summative_review(request, course_id):
    """Teacher review of a course summative bank."""
    from apps.tutoring.models import ExitTicket, ExitTicketAttempt
    from apps.tutoring.summative_selection import coverage_report
    from apps.accounts.models import Membership

    institution = request.staff_ctx['institution']
    if institution is not None:
        course = get_object_or_404(
            Course,
            Q(institution=institution) | Q(institution__isnull=True),
            id=course_id,
        )
    else:
        course = get_object_or_404(Course, id=course_id)

    summative = ExitTicket.objects.filter(
        course=course,
        assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
    ).first()

    coverage = None
    questions = []
    student_scores = []
    score_stats = {
        'attempted': 0, 'passed': 0, 'not_taken': 0,
        'avg_best_pct': None, 'total_students': 0,
    }
    questions_per_attempt = 0

    if summative:
        coverage = coverage_report(summative)
        questions = list(summative.questions.order_by('order_index'))
        questions_per_attempt = (
            summative.questions_per_attempt or len(questions) or 30
        )
        passing_pct = (summative.passing_score or 70)

        # Roster: all active students in the course's institution.
        roster_qs = Membership.objects.filter(
            role='student', is_active=True,
        )
        if course.institution_id:
            roster_qs = roster_qs.filter(institution=course.institution)
        roster_qs = roster_qs.select_related('user')
        roster = list(roster_qs)

        # Index attempts per student so a single pass over them
        # populates best/latest/count for everyone.
        attempts_qs = ExitTicketAttempt.objects.filter(
            exit_ticket=summative, completed_at__isnull=False,
        ).select_related('student', 'session').order_by('student_id', 'completed_at')

        per_student = {}  # user_id → {best, best_pct, latest, latest_pct, attempts, last_at, passed, *_was_group}
        for a in attempts_qs:
            uid = a.student_id
            pct = (a.score / questions_per_attempt * 100) if questions_per_attempt else 0
            # Was this attempt's session a group session? Cheap property
            # (one count() per attempt, fine at roster scale).
            try:
                was_group = bool(a.session and a.session.is_group)
            except Exception:
                was_group = False
            entry = per_student.setdefault(uid, {
                'best_score': 0, 'best_pct': 0,
                'latest_score': 0, 'latest_pct': 0,
                'attempts': 0, 'last_at': None, 'passed_best': False,
                'best_was_group': False, 'latest_was_group': False,
                'group_attempts': 0,
            })
            entry['attempts'] += 1
            entry['latest_score'] = a.score
            entry['latest_pct'] = round(pct)
            entry['last_at'] = a.completed_at
            entry['latest_was_group'] = was_group
            if was_group:
                entry['group_attempts'] += 1
            if pct > entry['best_pct']:
                entry['best_score'] = a.score
                entry['best_pct'] = round(pct)
                entry['passed_best'] = (a.score >= (passing_pct / 100.0) * questions_per_attempt)
                entry['best_was_group'] = was_group

        # Build the student rows (include not-attempted students so
        # teachers see who hasn't started). Sort: attempted first by
        # best-score desc, then not-taken alphabetically.
        for m in roster:
            user = m.user
            entry = per_student.get(user.id)
            if entry:
                student_scores.append({
                    'user': user,
                    'has_attempt': True,
                    'best_score': entry['best_score'],
                    'best_pct': entry['best_pct'],
                    'latest_score': entry['latest_score'],
                    'latest_pct': entry['latest_pct'],
                    'attempts': entry['attempts'],
                    'last_at': entry['last_at'],
                    'passed': entry['passed_best'],
                    'best_was_group': entry['best_was_group'],
                    'latest_was_group': entry['latest_was_group'],
                    'group_attempts': entry['group_attempts'],
                })
            else:
                student_scores.append({
                    'user': user,
                    'has_attempt': False,
                    'best_score': None, 'best_pct': None,
                    'latest_score': None, 'latest_pct': None,
                    'attempts': 0, 'last_at': None,
                    'passed': False,
                    'best_was_group': False, 'latest_was_group': False,
                    'group_attempts': 0,
                })
        student_scores.sort(key=lambda r: (
            0 if r['has_attempt'] else 1,
            -(r['best_pct'] or 0),
            (r['user'].get_full_name() or r['user'].username).lower(),
        ))

        attempted = sum(1 for r in student_scores if r['has_attempt'])
        passed = sum(1 for r in student_scores if r['passed'])
        avg_pct = None
        if attempted:
            avg_pct = round(
                sum(r['best_pct'] for r in student_scores if r['has_attempt']) / attempted
            )
        score_stats = {
            'attempted': attempted,
            'passed': passed,
            'not_taken': len(student_scores) - attempted,
            'avg_best_pct': avg_pct,
            'total_students': len(student_scores),
            'passing_pct': passing_pct,
        }

    return render(request, 'dashboard/summative/review.html', {
        **request.staff_ctx,
        'course': course,
        'summative': summative,
        'coverage': coverage,
        'questions': questions,
        'student_scores': student_scores,
        'score_stats': score_stats,
        'questions_per_attempt': questions_per_attempt,
    })


@teacher_required
@require_POST
def summative_publish(request, course_id):
    """Toggle is_published on the course summative."""
    from apps.tutoring.models import ExitTicket

    institution = request.staff_ctx['institution']
    if institution is not None:
        course = get_object_or_404(
            Course,
            Q(institution=institution) | Q(institution__isnull=True),
            id=course_id,
        )
    else:
        course = get_object_or_404(Course, id=course_id)

    summative = get_object_or_404(
        ExitTicket,
        course=course,
        assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
    )
    summative.is_published = not summative.is_published
    summative.save(update_fields=['is_published'])
    state = 'published' if summative.is_published else 'unpublished'
    messages.success(request, f"Summative exam {state} for {course.title}.")
    return redirect('dashboard:summative_review', course_id=course.id)


# ============================================================================
# Class & student competency dashboards (longitudinal, by teaching objective)
# ============================================================================

@teacher_required
def class_competency(request, course_id):
    """Class competency map — the merged readiness + competency view.

    Each row is a teaching objective. Columns: baseline / latest /
    final / Δ / mastered. Plus class-wide summary stats and a
    recommendation block (folded in from the legacy class_readiness
    report). Source: ExitTicketAttempt rows (summative + per-lesson
    exit tickets).
    """
    from apps.tutoring.competency_tracker import (
        class_competency_matrix,
        collect_objective_signals_for_course,
    )

    institution = request.staff_ctx['institution']
    if institution is not None:
        course = get_object_or_404(
            Course,
            Q(institution=institution) | Q(institution__isnull=True),
            id=course_id,
        )
    else:
        course = get_object_or_404(Course, id=course_id)

    # Roster resolution priority:
    #   1. The school picker (request.staff_ctx['institution']) — if a
    #      super admin picks "Belonie Secondary", scope every metric to
    #      that school's roster, even on platform-wide courses.
    #   2. The course's own institution if it's school-scoped.
    #   3. All active students (only when picker = "All Schools" AND
    #      the course is platform-wide).
    if institution is not None:
        roster_institution_id = institution.id
    elif course.institution_id:
        roster_institution_id = course.institution_id
    else:
        roster_institution_id = None

    if roster_institution_id is not None:
        roster_ids = list(
            Membership.objects.filter(
                role='student', is_active=True,
                institution_id=roster_institution_id,
            ).values_list('user_id', flat=True)
        )
    else:
        roster_ids = list(
            Membership.objects.filter(role='student', is_active=True)
            .values_list('user_id', flat=True)
        )

    # Grade scope — Mathematics S3 should only count S3 students, not
    # the entire school's roster. Course.grade_level can be a single
    # grade ("S3") or a comma-separated set ("S1,S2"). Empty means the
    # course is grade-agnostic and we keep the full roster.
    #
    # No fallback when zero students match: if the course is for S3
    # and nobody has grade=S3 on their profile, the right answer is
    # "no students" (which surfaces the missing grade_level data
    # problem) rather than silently counting every student in the
    # school.
    course_grades = {
        g.strip() for g in (course.grade_level or '').split(',') if g.strip()
    }
    if course_grades and roster_ids:
        roster_ids = list(
            StudentProfile.objects.filter(
                user_id__in=roster_ids,
                grade_level__in=course_grades,
            ).values_list('user_id', flat=True)
        )

    matrix = class_competency_matrix(course, students=roster_ids)

    # Class readiness score — simple average of the per-lesson
    # "Average competency" column shown in the matrix
    # (avg_latest_pct = class average of each student's most recent
    # exit-ticket score on that lesson). Edward, 2026-05-07:
    # changed from a mastery-rate metric to this raw-average reading
    # because it matches what teachers literally see in the table.
    # Lessons with no attempts (avg_latest_pct=None) are excluded
    # from the average — they appear as "—" in the column and
    # don't have a value to fold in.
    objectives = matrix['objectives']
    total_students = matrix['total_students']
    pcts = [r['avg_latest_pct'] for r in objectives if r.get('avg_latest_pct') is not None]
    readiness = round(sum(pcts) / len(pcts)) if pcts else 0

    # Struggling objectives = where avg_latest < 50% (or no class signal yet)
    struggling = [
        r for r in objectives
        if r['avg_latest_pct'] is not None and r['avg_latest_pct'] < 50
    ]
    untouched = [r for r in objectives if r['avg_latest_pct'] is None]

    if not objectives:
        recommendation = "No teaching objectives yet — re-parse the curriculum to populate."
        recommendation_type = 'warning'
    elif total_students == 0:
        recommendation = "No students enrolled at this school yet."
        recommendation_type = 'warning'
    elif matrix['students_attempted'] == 0:
        recommendation = "Students haven't taken assessments yet — once they do, this fills in."
        recommendation_type = 'warning'
    elif not struggling:
        recommendation = "Strong class — every objective is averaging 50%+. Keep moving."
        recommendation_type = 'success'
    elif len(struggling) <= 3:
        names = ', '.join(f'"{r["tag"][:60]}"' for r in struggling[:3])
        recommendation = f"Revisit before moving on: {names}."
        recommendation_type = 'warning'
    else:
        recommendation = (
            f"{len(struggling)} objectives below 50%. Consider re-teaching "
            f"the weakest unit before continuing."
        )
        recommendation_type = 'danger'

    # Per-student gaps: who's weak on the most objectives?
    signals = collect_objective_signals_for_course(course, students=roster_ids)
    student_weak: dict = {}
    for (sid, tag), bucket in signals.items():
        latest = bucket['latest']
        if not latest or not latest['total']:
            continue
        pct = (latest['correct'] / latest['total']) * 100
        if pct < 50:
            student_weak.setdefault(sid, []).append({'tag': tag, 'pct': pct})

    student_gaps = []
    if student_weak:
        from django.contrib.auth.models import User as _User
        users = {u.id: u for u in _User.objects.filter(id__in=student_weak.keys())}
        for sid, weak_list in student_weak.items():
            weak_list.sort(key=lambda r: r['pct'])
            user = users.get(sid)
            if not user:
                continue
            student_gaps.append({
                'student': user,
                'weak_count': len(weak_list),
                'weak_objectives': [w['tag'][:80] for w in weak_list[:5]],
            })
        student_gaps.sort(key=lambda x: -x['weak_count'])
        student_gaps = student_gaps[:20]

    return render(request, 'dashboard/competency/class.html', {
        **request.staff_ctx,
        'course': course,
        'matrix': matrix,
        'readiness': readiness,
        'recommendation': recommendation,
        'recommendation_type': recommendation_type,
        'struggling_count': len(struggling),
        'untouched_count': len(untouched),
        'student_gaps': student_gaps,
    })


@teacher_required
def class_readiness_redirect(request, course_id):
    """Legacy URL — class readiness merged into the competency map."""
    return redirect('dashboard:class_competency', course_id=course_id)


@teacher_required
def student_competency(request, course_id, student_id):
    """Per-student per-objective table. One row per teaching objective
    showing baseline / latest / final / delta for this student."""
    from apps.tutoring.competency_tracker import student_competency_table
    from django.contrib.auth.models import User as _User

    institution = request.staff_ctx['institution']
    if institution is not None:
        course = get_object_or_404(
            Course,
            Q(institution=institution) | Q(institution__isnull=True),
            id=course_id,
        )
    else:
        course = get_object_or_404(Course, id=course_id)

    student = get_object_or_404(_User, id=student_id)
    table = student_competency_table(course, student)

    # Compute summary stats
    rows = table['objectives']
    has_baseline = sum(1 for r in rows if r['baseline_pct'] is not None)
    has_final = sum(1 for r in rows if r['final_pct'] is not None)
    mastered_latest = sum(1 for r in rows if (r['latest_pct'] or 0) >= 70)

    return render(request, 'dashboard/competency/student.html', {
        **request.staff_ctx,
        'course': course,
        'student': student,
        'rows': rows,
        'total_objectives': len(rows),
        'has_baseline': has_baseline,
        'has_final': has_final,
        'mastered_latest': mastered_latest,
    })


# ============================================================================
# Weekly assignment (Phase 2)
# ============================================================================

@teacher_required
@require_POST
def weekly_assignment_save(request, course_id):
    """Create or update a WeeklyAssignment for (course, week_start).
    Form fields:
      - week_start: ISO date string (YYYY-MM-DD), normalized to Monday on save
      - lesson_ids: comma-separated lesson IDs, OR multiple `lesson_ids` POSTs
      - notes: optional teacher message
    """
    from datetime import date as _date
    from apps.dashboard.models import WeeklyAssignment

    institution = request.staff_ctx['institution']
    if institution is not None:
        course = get_object_or_404(
            Course, Q(institution=institution) | Q(institution__isnull=True), id=course_id,
        )
    else:
        course = get_object_or_404(Course, id=course_id)

    raw_date = (request.POST.get('week_start') or '').strip()
    try:
        ws = _date.fromisoformat(raw_date)
    except ValueError:
        messages.error(request, "Invalid week date.")
        return redirect('dashboard:course_detail', course_id=course.id)

    monday = WeeklyAssignment.normalize_to_monday(ws)
    notes = (request.POST.get('notes') or '').strip()

    # Pull lesson IDs — accept either a comma-separated single field or
    # repeated `lesson_ids` form values.
    raw_ids = request.POST.getlist('lesson_ids')
    if len(raw_ids) == 1 and ',' in raw_ids[0]:
        raw_ids = [x.strip() for x in raw_ids[0].split(',') if x.strip()]
    lesson_ids = [int(x) for x in raw_ids if x.isdigit()]

    # Whitelist to lessons in this course (defense against ID-tampering).
    allowed_ids = set(
        Lesson.objects.filter(unit__course=course, id__in=lesson_ids).values_list('id', flat=True)
    )
    final_ids = [i for i in lesson_ids if i in allowed_ids]

    if request.POST.get('action') == 'delete':
        WeeklyAssignment.objects.filter(course=course, week_start=monday).delete()
        messages.success(request, f"Cleared assignment for week of {monday.isoformat()}.")
        return redirect('dashboard:course_detail', course_id=course.id)

    wa, created = WeeklyAssignment.objects.get_or_create(
        course=course,
        week_start=monday,
        defaults={'assigned_by': request.user, 'notes': notes},
    )
    if not created:
        wa.notes = notes
        wa.assigned_by = request.user
        wa.save(update_fields=['notes', 'assigned_by', 'updated_at'])
    wa.lessons.set(final_ids)

    verb = "created" if created else "updated"
    messages.success(
        request,
        f"Weekly assignment {verb} — {len(final_ids)} lesson{'s' if len(final_ids) != 1 else ''} for week of {monday.isoformat()}.",
    )
    return redirect('dashboard:course_detail', course_id=course.id)
