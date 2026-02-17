"""
Teacher & Management Dashboard Views

Provides:
- Dashboard overview with key metrics
- Student progress tracking
- Curriculum management (upload & auto-generate)
- Class/course management
"""

import json
from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Count, Avg, Q, F
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST

from apps.accounts.models import Institution, Membership, StudentProfile
from apps.curriculum.models import Course, Unit, Lesson
from apps.tutoring.models import TutorSession, StudentLessonProgress
from django.contrib.auth.models import User


def get_teacher_context(request):
    """Get common context for teacher views."""
    membership = Membership.objects.filter(
        user=request.user,
        role__in=['admin', 'teacher', 'editor'],
        is_active=True
    ).select_related('institution').first()
    
    if not membership:
        return None
    
    return {
        'membership': membership,
        'institution': membership.institution,
        'role': membership.role,
    }


def teacher_required(view_func):
    """Decorator to require teacher/admin role."""
    @login_required
    def wrapper(request, *args, **kwargs):
        ctx = get_teacher_context(request)
        if not ctx:
            messages.error(request, "You don't have teacher access.")
            return redirect('tutoring:catalog')
        request.teacher_ctx = ctx
        return view_func(request, *args, **kwargs)
    return wrapper


# ============================================================================
# Dashboard Home
# ============================================================================

@teacher_required
def dashboard_home(request):
    """Main teacher dashboard with overview metrics."""
    institution = request.teacher_ctx['institution']
    
    # Date ranges
    today = timezone.now().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    
    # Get all students in institution
    student_memberships = Membership.objects.filter(
        institution=institution,
        role='student',
        is_active=True
    ).select_related('user')
    
    student_ids = list(student_memberships.values_list('user_id', flat=True))
    total_students = len(student_ids)
    
    # Active students (had session in last 7 days)
    active_students = TutorSession.objects.filter(
        institution=institution,
        student_id__in=student_ids,
        started_at__date__gte=week_ago
    ).values('student').distinct().count()
    
    # Sessions stats
    total_sessions = TutorSession.objects.filter(
        institution=institution,
        started_at__date__gte=month_ago
    ).count()
    
    completed_sessions = TutorSession.objects.filter(
        institution=institution,
        status='completed',
        started_at__date__gte=month_ago
    ).count()
    
    mastery_sessions = TutorSession.objects.filter(
        institution=institution,
        status='completed',
        mastery_achieved=True,
        started_at__date__gte=month_ago
    ).count()
    
    # Progress stats
    progress_stats = StudentLessonProgress.objects.filter(
        institution=institution
    ).aggregate(
        total=Count('id'),
        mastered=Count('id', filter=Q(mastery_level='mastered')),
        in_progress=Count('id', filter=Q(mastery_level='in_progress')),
    )
    
    avg_mastery = 0
    if progress_stats['total'] > 0:
        avg_mastery = round((progress_stats['mastered'] / progress_stats['total']) * 100)
    
    # Students at risk (started but no activity in 7 days)
    at_risk_students = TutorSession.objects.filter(
        institution=institution,
        student_id__in=student_ids,
    ).exclude(
        started_at__date__gte=week_ago
    ).values('student').distinct().count()
    
    # Recent activity
    recent_sessions = TutorSession.objects.filter(
        institution=institution
    ).select_related('student', 'lesson').order_by('-started_at')[:10]
    
    # Course progress
    courses = Course.objects.filter(
        institution=institution
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
        count = TutorSession.objects.filter(
            institution=institution,
            started_at__date=date
        ).count()
        activity_data.append({
            'date': date.strftime('%b %d'),
            'sessions': count
        })
    
    context = {
        **request.teacher_ctx,
        'total_students': total_students,
        'active_students': active_students,
        'total_sessions': total_sessions,
        'completed_sessions': completed_sessions,
        'mastery_sessions': mastery_sessions,
        'avg_mastery': avg_mastery,
        'at_risk_count': at_risk_students,
        'recent_sessions': recent_sessions,
        'course_progress': course_progress,
        'activity_data': json.dumps(activity_data),
        'progress_stats': progress_stats,
    }
    
    return render(request, 'dashboard/home.html', context)


# ============================================================================
# Student Management
# ============================================================================

@teacher_required
def student_list(request):
    """List all students with progress summary."""
    institution = request.teacher_ctx['institution']
    
    # Get students with their progress
    students = Membership.objects.filter(
        institution=institution,
        role='student',
        is_active=True
    ).select_related('user').order_by('user__last_name', 'user__first_name')
    
    # Enrich with progress data
    student_data = []
    for membership in students:
        user = membership.user
        
        # Get progress stats
        progress = StudentLessonProgress.objects.filter(
            institution=institution,
            student=user
        ).aggregate(
            total=Count('id'),
            mastered=Count('id', filter=Q(mastery_level='mastered')),
        )
        
        # Get recent session
        last_session = TutorSession.objects.filter(
            institution=institution,
            student=user
        ).order_by('-started_at').first()
        
        # Get profile
        profile = getattr(user, 'student_profile', None)
        
        student_data.append({
            'user': user,
            'profile': profile,
            'lessons_mastered': progress['mastered'] or 0,
            'lessons_total': progress['total'] or 0,
            'last_active': last_session.started_at if last_session else None,
            'mastery_pct': round((progress['mastered'] / progress['total']) * 100) if progress['total'] else 0,
        })
    
    # Pagination
    paginator = Paginator(student_data, 20)
    page = request.GET.get('page', 1)
    students_page = paginator.get_page(page)
    
    context = {
        **request.teacher_ctx,
        'students': students_page,
        'total_count': len(student_data),
    }
    
    return render(request, 'dashboard/students/list.html', context)


@teacher_required
def student_detail(request, student_id):
    """Detailed view of a student's progress."""
    institution = request.teacher_ctx['institution']
    
    student = get_object_or_404(User, id=student_id)
    
    # Verify student belongs to this institution
    membership = Membership.objects.filter(
        user=student,
        institution=institution,
        role='student'
    ).first()
    
    if not membership:
        messages.error(request, "Student not found.")
        return redirect('dashboard:student_list')
    
    # Get all progress
    progress_list = StudentLessonProgress.objects.filter(
        institution=institution,
        student=student
    ).select_related('lesson', 'lesson__unit', 'lesson__unit__course').order_by(
        'lesson__unit__course__name',
        'lesson__unit__order_index',
        'lesson__order_index'
    )
    
    # Get all sessions
    sessions = TutorSession.objects.filter(
        institution=institution,
        student=student
    ).select_related('lesson').order_by('-started_at')[:20]
    
    # Stats
    stats = {
        'total_sessions': TutorSession.objects.filter(institution=institution, student=student).count(),
        'completed_sessions': TutorSession.objects.filter(institution=institution, student=student, status='completed').count(),
        'mastered_lessons': progress_list.filter(mastery_level='mastered').count(),
        'in_progress_lessons': progress_list.filter(mastery_level='in_progress').count(),
    }
    
    # Group progress by course
    courses_progress = {}
    for p in progress_list:
        course = p.lesson.unit.course
        if course.id not in courses_progress:
            courses_progress[course.id] = {
                'course': course,
                'lessons': [],
                'mastered': 0,
                'total': 0,
            }
        courses_progress[course.id]['lessons'].append(p)
        courses_progress[course.id]['total'] += 1
        if p.mastery_level == 'mastered':
            courses_progress[course.id]['mastered'] += 1
    
    context = {
        **request.teacher_ctx,
        'student': student,
        'profile': getattr(student, 'student_profile', None),
        'stats': stats,
        'sessions': sessions,
        'courses_progress': courses_progress.values(),
    }
    
    return render(request, 'dashboard/students/detail.html', context)


# ============================================================================
# Curriculum Management
# ============================================================================

@teacher_required
def curriculum_list(request):
    """List all courses grouped by grade level."""
    institution = request.teacher_ctx['institution']
    
    courses = Course.objects.filter(
        institution=institution
    ).prefetch_related('units__lessons').order_by('grade_level', 'title')
    
    # Enrich with stats
    course_data = []
    for course in courses:
        total_lessons = Lesson.objects.filter(unit__course=course).count()
        published_lessons = Lesson.objects.filter(unit__course=course, is_published=True).count()
        
        course_data.append({
            'course': course,
            'unit_count': course.units.count(),
            'total_lessons': total_lessons,
            'published_lessons': published_lessons,
        })
    
    context = {
        **request.teacher_ctx,
        'courses': course_data,
    }
    
    return render(request, 'dashboard/curriculum/list.html', context)


@teacher_required
def course_detail(request, course_id):
    """View and manage a course's units and lessons."""
    institution = request.teacher_ctx['institution']
    
    course = get_object_or_404(Course, id=course_id, institution=institution)
    
    units = course.units.prefetch_related('lessons').order_by('order_index')
    
    # Get progress stats per lesson
    lesson_stats = {}
    for unit in units:
        for lesson in unit.lessons.all():
            progress = StudentLessonProgress.objects.filter(
                institution=institution,
                lesson=lesson
            ).aggregate(
                total=Count('id'),
                mastered=Count('id', filter=Q(mastery_level='mastered')),
            )
            lesson_stats[lesson.id] = {
                'students_started': progress['total'] or 0,
                'students_mastered': progress['mastered'] or 0,
            }
    
    context = {
        **request.teacher_ctx,
        'course': course,
        'units': units,
        'lesson_stats': lesson_stats,
    }
    
    return render(request, 'dashboard/curriculum/course_detail.html', context)


@teacher_required
def curriculum_upload(request):
    """Upload curriculum document to auto-generate course structure."""
    institution = request.teacher_ctx['institution']
    
    if request.method == 'POST':
        # Handle file upload
        uploaded_file = request.FILES.get('curriculum_file')
        subject_name = request.POST.get('subject_name', '').strip()
        grade_level = request.POST.get('grade_level', '')
        
        if not uploaded_file:
            messages.error(request, "Please upload a curriculum file.")
            return redirect('dashboard:curriculum_upload')
        
        if not subject_name:
            messages.error(request, "Please enter a subject name.")
            return redirect('dashboard:curriculum_upload')
        
        # Save file temporarily
        import os
        from django.conf import settings
        
        upload_dir = os.path.join(settings.MEDIA_ROOT, 'curriculum_uploads')
        os.makedirs(upload_dir, exist_ok=True)
        
        file_path = os.path.join(upload_dir, uploaded_file.name)
        with open(file_path, 'wb+') as dest:
            for chunk in uploaded_file.chunks():
                dest.write(chunk)
        
        # Create processing task (in background ideally)
        # For now, redirect to processing page
        from apps.dashboard.models import CurriculumUpload
        
        upload_record = CurriculumUpload.objects.create(
            institution=institution,
            uploaded_by=request.user,
            file_path=file_path,
            subject_name=subject_name,
            grade_level=grade_level,
            status='pending'
        )
        
        messages.success(request, f"Curriculum uploaded! Processing will begin shortly.")
        return redirect('dashboard:curriculum_process', upload_id=upload_record.id)
    
    # GET - show upload form
    context = {
        **request.teacher_ctx,
        'grade_levels': StudentProfile.GradeLevel.choices,
    }
    
    return render(request, 'dashboard/curriculum/upload.html', context)


@teacher_required
def curriculum_process(request, upload_id):
    """Process uploaded curriculum and show progress."""
    institution = request.teacher_ctx['institution']
    
    from apps.dashboard.models import CurriculumUpload
    
    upload = get_object_or_404(
        CurriculumUpload,
        id=upload_id,
        institution=institution
    )
    
    context = {
        **request.teacher_ctx,
        'upload': upload,
    }
    
    return render(request, 'dashboard/curriculum/process.html', context)


@teacher_required
def curriculum_generate(request, upload_id):
    """API endpoint to start curriculum generation."""
    institution = request.teacher_ctx['institution']
    
    from apps.dashboard.models import CurriculumUpload
    from apps.dashboard.tasks import process_curriculum_upload
    
    upload = get_object_or_404(
        CurriculumUpload,
        id=upload_id,
        institution=institution
    )
    
    if upload.status != 'pending':
        return JsonResponse({'error': 'Already processing'}, status=400)
    
    # Start processing (sync for now, async with Celery later)
    try:
        upload.status = 'processing'
        upload.save()
        
        result = process_curriculum_upload(upload.id)
        
        return JsonResponse({
            'status': 'success',
            'message': 'Curriculum generated successfully',
            'course_id': result.get('course_id'),
        })
    except Exception as e:
        upload.status = 'failed'
        upload.error_message = str(e)
        upload.save()
        return JsonResponse({'error': str(e)}, status=500)


# ============================================================================
# Class Management
# ============================================================================

@teacher_required  
def class_list(request):
    """List and manage classes/groups."""
    institution = request.teacher_ctx['institution']
    
    # For now, show students grouped by grade
    students_by_grade = {}
    
    memberships = Membership.objects.filter(
        institution=institution,
        role='student',
        is_active=True
    ).select_related('user', 'user__student_profile')
    
    for m in memberships:
        profile = getattr(m.user, 'student_profile', None)
        grade = profile.grade_level if profile else 'Unknown'
        
        if grade not in students_by_grade:
            students_by_grade[grade] = []
        students_by_grade[grade].append(m.user)
    
    context = {
        **request.teacher_ctx,
        'students_by_grade': students_by_grade,
    }
    
    return render(request, 'dashboard/classes/list.html', context)


# ============================================================================
# Reports
# ============================================================================

@teacher_required
def reports_overview(request):
    """Generate reports on student progress."""
    institution = request.teacher_ctx['institution']
    
    # Get date range from request
    days = int(request.GET.get('days', 30))
    start_date = timezone.now().date() - timedelta(days=days)
    
    # Sessions by day
    sessions_by_day = TutorSession.objects.filter(
        institution=institution,
        started_at__date__gte=start_date
    ).annotate(
        date=TruncDate('started_at')
    ).values('date').annotate(
        count=Count('id'),
        completed=Count('id', filter=Q(status='completed')),
        mastered=Count('id', filter=Q(mastery_achieved=True))
    ).order_by('date')
    
    # Top performing students
    top_students = StudentLessonProgress.objects.filter(
        institution=institution,
        mastery_level='mastered'
    ).values('student__first_name', 'student__last_name', 'student__id').annotate(
        mastered_count=Count('id')
    ).order_by('-mastered_count')[:10]
    
    # Lessons completion rate
    lessons = Lesson.objects.filter(
        unit__course__institution=institution,
        is_published=True
    ).annotate(
        attempts=Count('sessions'),
        completions=Count('sessions', filter=Q(sessions__mastery_achieved=True))
    ).order_by('-attempts')[:20]
    
    context = {
        **request.teacher_ctx,
        'days': days,
        'sessions_by_day': list(sessions_by_day),
        'top_students': top_students,
        'lessons': lessons,
    }
    
    return render(request, 'dashboard/reports/overview.html', context)


# ============================================================================
# Settings
# ============================================================================

@teacher_required
def settings_page(request):
    """Institution settings."""
    institution = request.teacher_ctx['institution']
    
    if request.method == 'POST':
        # Update institution settings
        institution.name = request.POST.get('name', institution.name)
        institution.timezone = request.POST.get('timezone', institution.timezone)
        institution.save()
        messages.success(request, "Settings updated.")
        return redirect('dashboard:settings')
    
    context = {
        **request.teacher_ctx,
    }
    
    return render(request, 'dashboard/settings.html', context)
