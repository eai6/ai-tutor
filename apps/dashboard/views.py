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

        validator_count = _validator_flagged_count(institution)

        return {
            'membership': None,
            'institution': institution,
            'role': 'superadmin',
            'all_schools': all_schools,
            'is_aggregated': institution is None,
            'unreviewed_flag_count': flag_qs.count() + validator_count,
            'can_edit_content': True,  # Superadmin always has full access
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

    flag_qs = TutorSession.objects.filter(
        is_flagged=True, flag_reviewed=False, institution=institution
    )
    validator_count = _validator_flagged_count(institution)

    from apps.accounts.models import PlatformConfig
    config = PlatformConfig.load()

    return {
        'membership': membership,
        'institution': institution,
        'role': 'staff',
        'all_schools': staff_schools if len(staff_schools) > 1 else [],
        'is_aggregated': False,
        'unreviewed_flag_count': flag_qs.count() + validator_count,
        'can_edit_content': config.teachers_can_edit_content,
    }


def _validator_flagged_count(institution) -> int:
    """Count of sessions with at least one validator hard-fail turn.
    Used in the nav badge alongside safety-flag count.
    """
    from apps.tutoring.models import SessionTurn
    from apps.tutoring.validator import ISSUE_NUMERIC_CLAIM_CONTRADICTED

    session_ids = (
        SessionTurn.objects
        .filter(metadata__icontains=ISSUE_NUMERIC_CLAIM_CONTRADICTED)
        .values_list('session_id', flat=True)
        .distinct()
    )
    qs = TutorSession.objects.filter(id__in=set(session_ids))
    if institution is not None:
        qs = qs.filter(institution=institution)
    return qs.count()


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

    avg_mastery = 0
    denominator = total_available_lessons * max(total_students, 1)
    if denominator > 0:
        avg_mastery = round((progress_stats['mastered'] / denominator) * 100)

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

    # Course progress
    courses = filter_by_institution(
        Course.objects.all(), institution
    ).annotate(
        lesson_count=Count('units__lessons'),
        mastered_count=Count(
            'units__lessons__student_progress',
            filter=Q(units__lessons__student_progress__mastery_level='mastered')
        )
    )

    course_progress = []
    for course in courses:
        if course.lesson_count > 0:
            progress_pct = round((course.mastered_count / (course.lesson_count * max(total_students, 1))) * 100)
        else:
            progress_pct = 0
        course_progress.append({
            'course': course,
            'progress': min(progress_pct, 100),
        })

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
        'course_progress': course_progress,
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
    
    # Get total published lessons per course for true denominator
    course_lesson_counts = {}
    courses_qs = filter_by_institution(
        Course.objects.filter(is_published=True), institution
    ).prefetch_related('units__lessons')
    for course in courses_qs:
        count = 0
        for unit in course.units.all():
            count += unit.lessons.filter(is_published=True).count()
        if count > 0:
            course_lesson_counts[course.id] = {'course': course, 'count': count}

    # Group progress by course (with rich competency breakdown — C4)
    from apps.tutoring.competency import best_attempt, per_concept_breakdown
    courses_progress = {}
    for p in progress_list:
        course = p.lesson.unit.course
        if course.id not in courses_progress:
            total_in_course = course_lesson_counts.get(course.id, {}).get('count', 0)
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

    competency_data = []
    for cid, cp in courses_progress.items():
        course = cp['course']
        eo_skills = Skill.objects.filter(course=course, is_enabling_objective=True)
        total_eo = eo_skills.count()
        if total_eo == 0:
            continue

        achieved = StudentSkillMastery.objects.filter(
            student=student, skill__in=eo_skills,
            mastery_level__gte=config.threshold_me_min / 100.0,
        ).count()
        pct = round(achieved / total_eo * 100) if total_eo else 0
        category = config.categorize_student(pct)

        # Per-lesson breakdown
        lesson_competencies = []
        for unit in course.units.all().order_by('order_index'):
            for lesson in unit.lessons.filter(is_published=True).order_by('order_index'):
                lesson_eos = eo_skills.filter(primary_lesson=lesson)
                lesson_total = lesson_eos.count()
                if lesson_total == 0:
                    continue
                lesson_achieved = StudentSkillMastery.objects.filter(
                    student=student, skill__in=lesson_eos,
                    mastery_level__gte=config.threshold_me_min / 100.0,
                ).count()
                lesson_pct = round(lesson_achieved / lesson_total * 100) if lesson_total else 0
                lesson_cat = config.categorize_student(lesson_pct)
                lesson_competencies.append({
                    'lesson': lesson,
                    'achieved': lesson_achieved,
                    'total': lesson_total,
                    'pct': lesson_pct,
                    'category': lesson_cat,
                })

        competency_data.append({
            'course': course,
            'achieved': achieved,
            'total': total_eo,
            'pct': pct,
            'category': category,
            'lessons': lesson_competencies,
        })

    context = {
        **request.staff_ctx,
        'student': student,
        'profile': getattr(student, 'student_profile', None),
        'stats': stats,
        'sessions': sessions,
        'courses_progress': courses_progress.values(),
        'competency_data': competency_data,
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
            has_content = steps_count >= 5  # Lessons typically have 8-12 steps
            
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
        'is_platform_wide': is_platform_wide,
        'course_read_only': course_read_only,
        'course_tier_label': course_tier_label,
        'course_tier_color': course_tier_color,
        'course_tier_bg': course_tier_bg,
    }

    return render(request, 'dashboard/curriculum/course_detail.html', context)


@teacher_required
def curriculum_upload(request):
    """Upload curriculum document with optional teaching material attachment."""
    institution = request.staff_ctx['institution']
    is_superadmin = request.user.is_staff

    if institution is None and not is_superadmin:
        messages.warning(request, "Please select a specific school before uploading curriculum.")
        return redirect('dashboard:curriculum_list')

    if request.method == 'POST':
        uploaded_file = request.FILES.get('curriculum_file')
        subject_name = request.POST.get('subject_name', '').strip()
        grade_levels = request.POST.getlist('grade_level')
        grade_level = ','.join(grade_levels) if grade_levels else ''

        if not uploaded_file:
            messages.error(request, "Please upload a curriculum file.")
            return redirect('dashboard:curriculum_upload')

        if not subject_name:
            messages.error(request, "Please enter a subject name.")
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

    context = {
        **request.staff_ctx,
        'grade_levels': PlatformConfig.get_grade_choices(),
        'material_types': TeachingMaterialUpload.MaterialType.choices,
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
                
                lesson, l_created = Lesson.objects.update_or_create(
                    unit=unit,
                    title=lesson_title,
                    defaults={
                        'objective': lesson_data.get('objective', ''),
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
    """List and manage classes/groups."""
    institution = request.staff_ctx['institution']
    
    # For now, show students grouped by grade
    students_by_grade = {}
    
    memberships = filter_by_institution(
        Membership.objects.filter(role='student', is_active=True),
        institution
    ).select_related('user', 'user__student_profile')
    
    for m in memberships:
        profile = getattr(m.user, 'student_profile', None)
        grade = profile.grade_level if profile else 'Unknown'
        
        if grade not in students_by_grade:
            students_by_grade[grade] = []
        students_by_grade[grade].append(m.user)
    
    # Build next-grade map for promote buttons in template
    grade_order = ['S1', 'S2', 'S3', 'S4', 'S5']
    next_grade_map = {}
    for i, g in enumerate(grade_order):
        next_grade_map[g] = grade_order[i + 1] if i < len(grade_order) - 1 else 'Graduate'

    context = {
        **request.staff_ctx,
        'students_by_grade': students_by_grade,
        'next_grade_map': next_grade_map,
    }

    return render(request, 'dashboard/classes/list.html', context)


@teacher_required
@require_POST
def promote_students(request):
    """Bulk promote students to the next grade level."""
    student_ids = request.POST.getlist('student_ids')
    from_grade = request.POST.get('from_grade', '')

    GRADE_ORDER = ['S1', 'S2', 'S3', 'S4', 'S5']

    if from_grade not in GRADE_ORDER:
        messages.error(request, f"Invalid grade: {from_grade}")
        return redirect('dashboard:class_list')

    idx = GRADE_ORDER.index(from_grade)

    if not student_ids:
        messages.warning(request, "No students selected.")
        return redirect('dashboard:class_list')

    if idx >= len(GRADE_ORDER) - 1:
        # S5 graduation: mark as graduated (empty grade)
        updated = StudentProfile.objects.filter(
            user_id__in=student_ids, grade_level=from_grade
        ).update(grade_level='')
        # Deactivate memberships
        Membership.objects.filter(
            user_id__in=student_ids, role='student', is_active=True
        ).update(is_active=False)
        messages.success(request, f"Graduated {updated} student(s) from {from_grade}.")
    else:
        next_grade = GRADE_ORDER[idx + 1]
        updated = StudentProfile.objects.filter(
            user_id__in=student_ids, grade_level=from_grade
        ).update(grade_level=next_grade)
        messages.success(request, f"Promoted {updated} student(s) from {from_grade} to {next_grade}.")

    return redirect('dashboard:class_list')


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
    from apps.tutoring.skills_models import Skill, StudentSkillMastery
    from apps.tutoring.models import TutorSession, ExitTicketAttempt
    from django.db.models import Avg, Q
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

    # ── Enabling Objectives: read directly from lesson steps (curriculum-aligned) ──
    # The enabling objectives ARE the competencies — no separate skill model needed
    from apps.curriculum.models import LessonStep
    teaching_steps = (lesson.metadata or {}).get('teaching_steps', [])
    if not teaching_steps:
        # Collect from step enabling_objective fields
        seen = set()
        for step in LessonStep.objects.filter(lesson=lesson).order_by('order_index'):
            eo = step.enabling_objective or ''
            if eo and eo not in seen:
                seen.add(eo)
                teaching_steps.append(eo)
    # Fallback to lesson.enabling_objectives (TO chunks)
    if not teaching_steps:
        teaching_steps = lesson.enabling_objectives or []

    total_objectives = len(teaching_steps)

    # ── Per-objective competency (C4: exit-ticket only, single source of truth) ──
    # For each EO, count students whose BEST exit ticket attempt answered
    # at least one question tagged with this EO's concept_tag correctly.
    # Legacy fallbacks to engine_state.covered_enabling_objectives and
    # StudentSkillMastery have been removed per memory/lesson_competency_plan.md.
    from apps.tutoring.competency import best_attempt
    objectives_data = []
    for eo_text in teaching_steps:
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
                if ans.get('concept_tag', '') == eo_text and ans.get('correct'):
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

        # Count how many EOs this student achieved
        achieved_count = sum(1 for o in objectives_data if any(
            True for s2 in [sid] if o['achieved'] > 0
            # This is per-objective, need per-student check
        ))
        # Recalculate per student properly
        achieved_count = 0
        weak_objectives = []
        for eo_text in teaching_steps:
            eo_achieved = False
            # Same logic as above for this specific student
            attempt = ExitTicketAttempt.objects.filter(
                exit_ticket__lesson=lesson, student_id=sid
            ).order_by('-completed_at').first()
            if attempt and attempt.answers:
                for ans in (attempt.answers if isinstance(attempt.answers, list) else []):
                    if isinstance(ans, dict) and ans.get('concept_tag', '') == eo_text and ans.get('correct'):
                        eo_achieved = True
                        break
            if not eo_achieved:
                student_session = sessions.filter(student_id=sid, status='completed').first()
                if student_session:
                    state = student_session.engine_state or {}
                    covered_eos = state.get('covered_enabling_objectives', [])
                    if eo_text in covered_eos:
                        eo_achieved = True
            if not eo_achieved:
                mastery = StudentSkillMastery.objects.filter(
                    skill__enabling_objective_text__icontains=eo_text[:50],
                    skill__is_enabling_objective=True,
                    student_id=sid,
                    mastery_level__gte=mastery_threshold,
                ).first()
                if mastery:
                    eo_achieved = True

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
            'exit_score': f"{exit_attempt.score}/10" if exit_attempt else '—',
            'exit_passed': exit_attempt.passed if exit_attempt else None,
            'exit_time': f"{exit_time_minutes:.0f} min" if exit_time_minutes else '—',
            'session_status': session.status if session else 'not_started',
            'weak_objectives': weak_objectives,
            'has_exit_attempt': exit_attempt is not None,
        })

    students_data.sort(key=lambda s: s['pct'])

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
    """Process teaching materials — supports single, batch, or all pending."""
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.dashboard.material_tasks import process_teaching_material, process_teaching_material_fast
    from apps.dashboard.background_tasks import run_async

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
            Q(status='pending') | Q(status='processing', chunks_created=0) | Q(status='failed')
        )

    count = to_process.count()
    if count == 0:
        messages.info(request, "No materials to process.")
        return redirect('dashboard:course_detail', course_id=course.id)

    # Mark as processing and start background thread
    material_ids = list(to_process.values_list('id', flat=True))
    to_process.update(status='processing')

    mode_label = "Rich (LLM Vision)" if mode == 'rich' else "Fast (Text Only)"

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
                try:
                    TeachingMaterialUpload.objects.filter(id=mid).update(status='failed')
                except Exception:
                    pass

    run_async(_process_materials, material_ids, process_fn)

    messages.success(request, f"Processing {count} material(s) in {mode_label} mode.")
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

    context = {
        **request.staff_ctx,
        'upload': upload,
        'material_types': TeachingMaterialUpload.MaterialType.choices,
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
    from apps.dashboard.background_tasks import run_async

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

    material_ids = []
    for uploaded_file in uploaded_files:
        file_path = os.path.join(upload_dir, uploaded_file.name)
        with open(file_path, 'wb+') as dest:
            for chunk in uploaded_file.chunks():
                dest.write(chunk)

        file_title = f"{title} - {os.path.splitext(uploaded_file.name)[0]}" if len(uploaded_files) > 1 else title

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
        )
        material_ids.append(material_record.id)

    # Process in background — sequentially to avoid resource issues
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

    run_async(_process_materials, material_ids, process_fn)

    mode_label = "Rich (LLM Vision)" if processing_mode == 'rich' else "Fast (Text Only)"
    messages.success(request, f"{len(material_ids)} file(s) uploaded! Processing in {mode_label} mode.")
    return redirect('dashboard:course_detail', course_id=course.id)


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
            if not email:
                messages.error(request, "Email is required.")
            elif User.objects.filter(email=email).exclude(pk=request.user.pk).exists():
                messages.error(request, "That email is already in use by another account.")
            else:
                request.user.first_name = first_name
                request.user.last_name = last_name
                request.user.email = email
                request.user.save()
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
            'google': 'gemini-3.1-flash-image-preview',
        })

    # Tutor personalities (superadmin)
    personalities = []
    if is_superadmin:
        from apps.accounts.models import TutorPersonality
        personalities = TutorPersonality.objects.all()

    all_timezones = sorted(zoneinfo.available_timezones())
    all_schools = Institution.objects.exclude(slug=Institution.GLOBAL_SLUG).order_by('name') if is_superadmin else []
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

    # Enabling objectives: try metadata first, then collect from steps
    teaching_steps = (lesson.metadata or {}).get('teaching_steps', [])
    if not teaching_steps:
        # Collect unique EOs from step enabling_objective fields
        seen = set()
        for step in steps:
            eo = getattr(step, 'enabling_objective', '') or ''
            if eo and eo not in seen:
                seen.add(eo)
                teaching_steps.append(eo)

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
        'students_completed': students_completed,
        'prerequisites': prerequisites,
        'available_lessons': available_lessons,
        'teaching_steps': teaching_steps,
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

    # Free-form answer_data fields a teacher might tweak
    if 'data_description' in data or 'model_answer' in data or 'keywords' in data:
        ad = question.answer_data or {}
        if not isinstance(ad, dict):
            ad = {}
        for k in ('data_description', 'model_answer'):
            if k in data:
                ad[k] = data[k]
        if 'keywords' in data and isinstance(data['keywords'], list):
            ad['keywords'] = [str(k) for k in data['keywords']]
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
def lesson_regenerate(request, lesson_id):
    """Regenerate full pipeline: steps, media, exit tickets, and skills for a lesson."""
    from apps.curriculum.models import Lesson
    from apps.curriculum.content_generator import LessonContentGenerator
    from apps.tutoring.models import ExitTicket

    institution = request.staff_ctx['institution']

    lookup = {'id': lesson_id}
    if institution is not None:
        lookup['unit__course__institution'] = institution
    lesson = get_object_or_404(Lesson, **lookup)

    # Mark as generating
    lesson.content_status = 'generating'
    lesson.save(update_fields=['content_status'])

    try:
        # Delete existing content
        lesson.steps.all().delete()
        ExitTicket.objects.filter(lesson=lesson).delete()

        # Step 1: Generate lesson steps
        from apps.accounts.models import Institution
        inst = institution or lesson.unit.course.institution or Institution.get_global()
        generator = LessonContentGenerator(institution_id=inst.id)
        result = generator.generate_for_lesson(lesson, save_to_db=True)

        if not result.get('success'):
            lesson.content_status = 'failed'
            lesson.save(update_fields=['content_status'])
            messages.warning(request, result.get('error', 'Could not regenerate'))
            return redirect('dashboard:lesson_detail', lesson_id=lesson.id)

        steps_generated = result.get('steps_generated', 0)

        # Step 2: Generate media
        media_generated = 0
        try:
            from apps.tutoring.image_service import ImageGenerationService

            for step in lesson.steps.all():
                if not step.media:
                    continue
                images = step.media.get('images', [])
                media_updated = False
                for img in images:
                    if img.get('url'):
                        continue
                    description = img.get('description', '')
                    if not description:
                        continue
                    service = ImageGenerationService(
                        lesson=lesson,
                        institution=inst
                    )
                    img_result = service.get_or_generate_image(
                        prompt=description,
                        category=img.get('type', 'diagram')
                    )
                    if img_result and img_result.get('url'):
                        img['url'] = img_result['url']
                        img['source'] = 'generated'
                        media_updated = True
                        media_generated += 1
                if media_updated:
                    step.save()
        except Exception as e:
            logger.warning(f"Media generation for {lesson.title}: {e}")

        # Step 3: Exit tickets — already generated by content generator in Step 1
        # (generate_for_lesson calls _generate_exit_ticket which uses the new
        # multi-format prompt with EO-linked concept_tags)
        exit_questions = ExitTicket.objects.filter(lesson=lesson).first()
        exit_questions = exit_questions.questions.count() if exit_questions else 0

        # Step 4: Extract skills
        skills_extracted = 0
        try:
            from apps.tutoring.skill_extraction import SkillExtractionService
            skill_service = SkillExtractionService(institution_id=inst.id)
            skills = skill_service.extract_skills_for_lesson(lesson)
            skills_extracted = len(skills)
            # Also update course-level prerequisites (uses skill graph, no LLM)
            skill_service.detect_course_prerequisites(lesson.unit.course)
        except Exception as e:
            logger.warning(f"Skill extraction for {lesson.title}: {e}")

        # Mark as ready
        lesson.content_status = 'ready'
        lesson.save(update_fields=['content_status'])

        messages.success(
            request,
            f"Regenerated {steps_generated} steps, {media_generated} images, {exit_questions} exit questions, and {skills_extracted} skills for '{lesson.title}'"
        )

    except Exception as e:
        import traceback
        logger.error(f"Regeneration error: {traceback.format_exc()}")
        lesson.content_status = 'failed'
        lesson.save(update_fields=['content_status'])
        messages.error(request, f"Error: {str(e)}")

    return redirect('dashboard:lesson_detail', lesson_id=lesson.id)


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

    # Guard: skip if already has content
    if lesson.steps.count() >= 5:
        messages.info(request, f"'{lesson.title}' already has {lesson.steps.count()} steps.")
        return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)

    # Reset stuck 'generating' status so the background task can proceed
    if lesson.content_status == 'generating':
        lesson.content_status = 'pending'
        lesson.save(update_fields=['content_status'])

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
def step_edit(request, step_id):
    """Edit a lesson step."""
    from apps.curriculum.models import LessonStep
    
    institution = request.staff_ctx['institution']
    
    lookup = {'id': step_id}
    if institution is not None:
        lookup['lesson__unit__course__institution'] = institution
    step = get_object_or_404(LessonStep, **lookup)
    
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
                            generate_only=True,
                        )
                        if result and result.get('url'):
                            img['url'] = result['url']
                            img['source'] = 'generated'
                            step.save()
                            messages.success(request, "Image regenerated successfully.")
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

        # Parse hints (one per line)
        hints_text = request.POST.get('hints', '')
        if hints_text.strip():
            step.hints = [h.strip() for h in hints_text.split('\n') if h.strip()]
        else:
            step.hints = []

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
        lesson.content_status = 'empty'
        lesson.save(update_fields=['content_status'])
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
def course_edit(request, course_id):
    """Edit course title, description, and grade level."""
    institution = request.staff_ctx['institution']

    if institution is not None:
        course = get_object_or_404(Course, id=course_id, institution=institution)
    else:
        course = get_object_or_404(Course, id=course_id)

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    grade_level = request.POST.get('grade_level', '').strip()

    if not title:
        messages.error(request, "Course title cannot be empty.")
        return redirect('dashboard:course_detail', course_id=course.id)

    course.title = title
    course.description = description
    course.grade_level = grade_level
    course.save(update_fields=['title', 'description', 'grade_level', 'updated_at'])

    # Check if re-parse was requested
    if request.POST.get('action') == 'reparse':
        new_duration = int(request.POST.get('lesson_duration', 20))
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

        # Update the upload's duration setting
        upload.lesson_duration_minutes = new_duration
        upload.status = 'processing'
        upload.processing_log = ''
        upload.save()

        # Delete existing units/lessons (they'll be recreated)
        course.units.all().delete()

        # Re-plan lessons only (skip text extraction + vectorization — already done)
        # Auto-completes after replan: the user already explicitly chose to
        # re-parse, so we don't ask them to "approve" the result on a separate
        # page. They'd just see an empty course in between and think it failed.
        def _replan(upload_id, course_id, duration):
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
                up.add_log(f"   Target duration: {duration} minutes per lesson")
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

        run_async(_replan, upload.id, course.id, new_duration)
        messages.success(request, f"Re-planning lessons with {new_duration}-minute target. Vision extraction will analyze the PDF pages — refresh in ~1 minute to see the new units.")
        return redirect('dashboard:course_detail', course_id=course.id)

    messages.success(request, f"Course updated.")
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

    Two flag families now surface here (V4):
      - Safety flags: SessionTurn-level from the safety filter (sets
        TutorSession.is_flagged + flagged_at).
      - Validator flags: turns whose metadata.validator_issues contains
        a hard issue (numeric_claim_contradicted). Surface alongside so
        teachers can audit pedagogy + facts in one place.
    """
    from apps.tutoring.models import SessionTurn
    from apps.tutoring.validator import ISSUE_NUMERIC_CLAIM_CONTRADICTED

    institution = request.staff_ctx['institution']
    status_filter = request.GET.get('status', 'unreviewed')
    flag_filter = request.GET.get('flag_type', 'all')

    safety_qs = TutorSession.objects.filter(is_flagged=True)
    safety_qs = filter_by_institution(safety_qs, institution)
    if status_filter == 'unreviewed':
        safety_qs = safety_qs.filter(flag_reviewed=False)
    elif status_filter == 'reviewed':
        safety_qs = safety_qs.filter(flag_reviewed=True)

    # Sessions with at least one validator hard-fail turn.
    # __icontains on the JSONField avoids the JSON-array containment lookup
    # which isn't supported on SQLite (test DB).  The issue string is
    # unique enough that substring match is safe.
    validator_session_ids = set(
        SessionTurn.objects
        .filter(metadata__icontains=ISSUE_NUMERIC_CLAIM_CONTRADICTED)
        .values_list('session_id', flat=True)
        .distinct()
    )
    validator_qs = filter_by_institution(
        TutorSession.objects.filter(id__in=validator_session_ids),
        institution,
    )

    if flag_filter == 'safety':
        qs = safety_qs
    elif flag_filter == 'validator':
        qs = validator_qs
    else:
        # union
        ids = set(safety_qs.values_list('id', flat=True)) | set(
            validator_qs.values_list('id', flat=True)
        )
        qs = filter_by_institution(
            TutorSession.objects.filter(id__in=ids), institution,
        )

    qs = qs.select_related(
        'student', 'lesson', 'reviewed_by',
    ).order_by('-flagged_at', '-started_at')

    # Counts for the page header
    total_flagged = filter_by_institution(
        TutorSession.objects.filter(is_flagged=True), institution
    ).count()
    unreviewed_count = filter_by_institution(
        TutorSession.objects.filter(is_flagged=True, flag_reviewed=False), institution
    ).count()
    validator_flagged_count = validator_qs.count()

    # Annotate each session with its flag categories so the template
    # can render the right badges.
    safety_id_set = set(safety_qs.values_list('id', flat=True))
    for s in qs:
        s.has_safety_flag = s.id in safety_id_set
        s.has_validator_flag = s.id in validator_session_ids

    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'dashboard/flagged_sessions.html', {
        **request.staff_ctx,
        'sessions': page,
        'status_filter': status_filter,
        'flag_filter': flag_filter,
        'total_flagged': total_flagged,
        'unreviewed_count': unreviewed_count,
        'validator_flagged_count': validator_flagged_count,
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

    sessions = (
        TutorSession.objects
        .filter(lesson=lesson)
        .select_related('student')
        .prefetch_related('turns')
        .annotate(last_turn_at=Max('turns__created_at'))
        .order_by('-started_at')
    )

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
        duration = None
        turn_times = sorted(t.created_at for t in session.turns.all())
        if turn_times:
            active_seconds = 0.0
            prev = session.started_lesson_at or turn_times[0]
            for t in turn_times:
                active_seconds += min((t - prev).total_seconds(), IDLE_THRESHOLD_SECONDS)
                prev = t
            # Include time since last turn if still active
            if session.status == 'active' and not session.ended_at:
                active_seconds += min((now - prev).total_seconds(), IDLE_THRESHOLD_SECONDS)
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
    # so multi-day sessions don't show 4-digit minute totals).
    IDLE_CAP_SECONDS = 5 * 60
    duration_minutes = None
    turn_times = list(turns.values_list('created_at', flat=True))
    if turn_times:
        active_seconds = 0.0
        prev = session.started_lesson_at or turn_times[0]
        for t in turn_times:
            active_seconds += min((t - prev).total_seconds(), IDLE_CAP_SECONDS)
            prev = t
        if session.status == 'active' and not session.ended_at:
            active_seconds += min((timezone.now() - prev).total_seconds(), IDLE_CAP_SECONDS)
        duration_minutes = round(active_seconds / 60, 1)

    context = {
        **request.staff_ctx,
        'session': session,
        'turns': turns,
        'lesson': session.lesson,
        'student_name': session.student.get_full_name() or session.student.username,
        'cognitive_load': state.get('cognitive_load', 0.5),
        'duration_minutes': duration_minutes,
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
# EXIT-TICKET FIGURE EDIT (Stage 2 of figure_spec rework)
# ============================================================================

@teacher_required
def exit_ticket_figure_edit(request, question_id):
    """Edit an exit-ticket question's figure_spec.

    GET: render the edit form (preview + raw JSON spec textarea).
    POST: validate the new spec, render to SVG, persist both.

    Routes through the unified template renderer so any of the catalog
    kinds (charts, geometry, angles, coords, stats, geography) can be
    edited.
    """
    import json
    from apps.tutoring.models import ExitTicketQuestion
    from apps.curriculum.figure_templates import render_template

    question = _question_for_staff(request, question_id)
    if question is None:
        raise Http404("Question not found or not yours.")

    answer_data = question.answer_data or {}
    if not isinstance(answer_data, dict):
        answer_data = {}

    error = None
    success = None
    current_spec = answer_data.get('figure') or answer_data.get('figure_spec') or {}
    spec_text = json.dumps(current_spec, indent=2) if current_spec else ''

    if request.method == 'POST':
        raw = (request.POST.get('figure_spec') or '').strip()
        if not raw:
            error = 'Spec is empty.'
        else:
            try:
                spec = json.loads(raw)
            except json.JSONDecodeError as e:
                error = f'Invalid JSON: {e.msg} (line {e.lineno})'
                spec = None
            if spec is not None and isinstance(spec, dict):
                svg = render_template(spec)
                if not svg:
                    kind = spec.get('kind') or spec.get('type') or '(unset)'
                    error = (
                        f'Could not render figure (kind="{kind}"). '
                        f'Check that the kind is in the catalog and the spec is valid.'
                    )
                else:
                    answer_data['figure_spec'] = spec
                    answer_data['figure_svg'] = svg
                    # Strip any stale raster fields so the SVG wins.
                    for legacy in ('figure_url', 'figure_source', 'figure_description'):
                        answer_data.pop(legacy, None)
                    question.answer_data = answer_data
                    question.save(update_fields=['answer_data'])
                    success = 'Figure rendered.'
                    spec_text = json.dumps(spec, indent=2)

    return render(request, 'dashboard/exit_ticket_figure_edit.html', {
        'question': question,
        'spec_text': spec_text,
        'figure_svg': answer_data.get('figure_svg', ''),
        'figure_url': '',  # raster figures retired
        'error': error,
        'success': success,
    })


@teacher_required
def exit_ticket_figure_regenerate(request, question_id):
    """Regenerate the figure_spec via LLM from a teacher's prompt.

    POST: read `prompt` from form, send the current spec + the
    teacher's correction prompt to the LLM, parse a new spec, render
    it, return the result for preview (does not save until the
    teacher submits the main form).
    """
    import json
    from apps.tutoring.models import ExitTicketQuestion
    from apps.curriculum.figure_templates import render_template, list_kinds
    from apps.llm.prompts import get_prompt_or_default

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)

    question = _question_for_staff(request, question_id)
    if question is None:
        return JsonResponse({'ok': False, 'error': 'not found'}, status=404)

    teacher_prompt = (request.POST.get('prompt') or '').strip()
    if not teacher_prompt:
        return JsonResponse({'ok': False, 'error': 'prompt required'}, status=400)

    answer_data = question.answer_data or {}
    if not isinstance(answer_data, dict):
        answer_data = {}
    current_spec = answer_data.get('figure_spec', {})

    institution_id = (
        question.exit_ticket.lesson.unit.course.institution_id
        if question.exit_ticket and question.exit_ticket.lesson else None
    )

    from apps.llm.client import get_llm_client_for_purpose
    from apps.llm.json_utils import parse_llm_json

    try:
        llm_client = get_llm_client_for_purpose('exit_ticket_generation', institution_id)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'LLM client unavailable: {e}'}, status=500)

    sys_prompt = (
        "You are an assistant that produces structured chart specs in JSON. "
        "Output ONLY a JSON object — no prose, no code fence."
    )
    valid_kinds = ', '.join(list_kinds())
    user_prompt = (
        "Update this figure_spec based on the teacher's instruction.\n\n"
        f"QUESTION: {question.question_text}\n"
        f"CURRENT SPEC:\n{json.dumps(current_spec, indent=2) if current_spec else '(none)'}\n\n"
        f"TEACHER INSTRUCTION:\n{teacher_prompt}\n\n"
        "Output a JSON object with a `kind` field set to ONE of these "
        f"catalog kinds: {valid_kinds}.\n"
        "Each kind has its own fields — set what makes sense for the question.\n"
        "Examples:\n"
        "  {\"kind\":\"bar\", \"title\":\"...\", \"labels\":[...], \"datasets\":[{\"data\":[...]}]}\n"
        "  {\"kind\":\"pie\", \"title\":\"...\", \"labels\":[...], \"datasets\":[{\"data\":[...]}]}\n"
        "  {\"kind\":\"triangle\", \"type\":\"right\", \"sides\":[3,4,5], \"units\":\"cm\"}\n"
        "  {\"kind\":\"angle\", \"degrees\":48}\n"
        "  {\"kind\":\"point_angles\", \"angles\":[80,100,90,90], \"labels\":[...]}\n"
        "Numbers must be plain numbers (no commas, no units). "
        "Do NOT wrap your output in markdown code fences."
    )

    try:
        response = llm_client.generate(
            [{'role': 'user', 'content': user_prompt}],
            system_prompt=sys_prompt,
            max_tokens=2000,
        )
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'LLM call failed: {e}'}, status=500)

    try:
        new_spec = parse_llm_json(response.content)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Could not parse LLM JSON: {e}'}, status=500)

    if not isinstance(new_spec, dict):
        return JsonResponse({'ok': False, 'error': 'LLM produced non-object spec'}, status=500)

    from apps.curriculum.figure_templates import render_template
    svg = render_template(new_spec)
    if not svg:
        kind = new_spec.get('kind') or new_spec.get('type') or '(unset)'
        return JsonResponse({
            'ok': False,
            'error': f'Could not render figure (kind="{kind}"). Spec must use a catalog kind.',
        }, status=500)

    return JsonResponse({
        'ok': True,
        'spec': new_spec,
        'spec_text': json.dumps(new_spec, indent=2),
        'svg': svg,
    })


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
    """Receive a bug report or feedback from any authenticated page."""
    from apps.dashboard.models import FeedbackReport

    try:
        body = json.loads(request.body or "{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    message = (body.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "Message is required"}, status=400)

    kind = (body.get("kind") or "bug").strip()
    if kind not in {c[0] for c in FeedbackReport.Kind.choices}:
        kind = FeedbackReport.Kind.BUG

    severity = (body.get("severity") or "medium").strip()
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
        page_url=(body.get("page_url") or "")[:500],
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:500],
    )
    return JsonResponse({"ok": True})


@login_required
def feedback_list(request):
    """Superadmin list of feedback reports. Staff see only their school's reports."""
    from apps.dashboard.models import FeedbackReport

    if not (request.user.is_staff or request.user.is_superuser):
        ctx = get_staff_context(request)
        if not ctx:
            messages.error(request, "Staff access required.")
            return redirect('dashboard:home')
        institution = ctx['institution']
    else:
        institution = None

    qs = FeedbackReport.objects.select_related('user', 'institution', 'resolved_by')
    if institution is not None:
        qs = qs.filter(institution=institution)

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
        'open_count': FeedbackReport.objects.filter(is_resolved=False).count() if institution is None
                      else FeedbackReport.objects.filter(is_resolved=False, institution=institution).count(),
    })


@login_required
@require_POST
def feedback_resolve(request, report_id):
    """Mark a feedback report resolved (or reopen it)."""
    from apps.dashboard.models import FeedbackReport

    if not (request.user.is_staff or request.user.is_superuser):
        ctx = get_staff_context(request)
        if not ctx:
            return JsonResponse({"error": "Staff only"}, status=403)
        institution = ctx['institution']
        qs = FeedbackReport.objects.all()
        if institution is not None:
            qs = qs.filter(institution=institution)
    else:
        qs = FeedbackReport.objects.all()

    report = get_object_or_404(qs, id=report_id)
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

@login_required
def help_index(request):
    """Single-page in-app help with collapsible sections + slots for short
    instructional videos. Same page for students and teachers; sections
    show conditionally on role. See `memory/pilot_launch_execution.md`."""
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
    from apps.tutoring.models import ExitTicket
    from apps.tutoring.summative_selection import coverage_report

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
    if summative:
        coverage = coverage_report(summative)
        questions = list(summative.questions.order_by('order_index'))

    return render(request, 'dashboard/summative/review.html', {
        **request.staff_ctx,
        'course': course,
        'summative': summative,
        'coverage': coverage,
        'questions': questions,
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

    # Roster — students at this institution.
    if course.institution_id:
        roster_ids = list(
            Membership.objects.filter(
                role='student', is_active=True,
                institution_id=course.institution_id,
            ).values_list('user_id', flat=True)
        )
    else:
        roster_ids = list(
            Membership.objects.filter(role='student', is_active=True)
            .values_list('user_id', flat=True)
        )

    matrix = class_competency_matrix(course, students=roster_ids)

    # Class readiness score — average of "% of class mastered (latest ≥70)"
    # across every objective, expressed 0-100. Falls back to 0 if no data.
    objectives = matrix['objectives']
    total_students = matrix['total_students']
    if objectives and total_students:
        readiness = sum(
            (r['mastered_latest'] / total_students) * 100 for r in objectives
        ) / len(objectives)
    else:
        readiness = 0
    readiness = round(readiness)

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
