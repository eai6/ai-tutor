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
from django.db.models import Count, Avg, Q, F
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

        return {
            'membership': None,
            'institution': institution,
            'role': 'superadmin',
            'all_schools': all_schools,
            'is_aggregated': institution is None,
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

    return {
        'membership': membership,
        'institution': institution,
        'role': 'staff',
        'all_schools': staff_schools if len(staff_schools) > 1 else [],
        'is_aggregated': False,
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

    avg_mastery = 0
    if progress_stats['total'] > 0:
        avg_mastery = round((progress_stats['mastered'] / progress_stats['total']) * 100)

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
    ).select_related('student', 'lesson').order_by('-started_at')[:10]

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
    institution = request.staff_ctx['institution']
    
    # Get students with their progress
    students = filter_by_institution(
        Membership.objects.filter(role='student', is_active=True),
        institution
    ).select_related('user').order_by('user__last_name', 'user__first_name')

    # Enrich with progress data
    student_data = []
    for membership in students:
        user = membership.user

        # Get progress stats
        progress = filter_by_institution(
            StudentLessonProgress.objects.filter(student=user),
            institution
        ).aggregate(
            total=Count('id'),
            mastered=Count('id', filter=Q(mastery_level='mastered')),
        )

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

    # Get all sessions
    sessions = filter_by_institution(
        TutorSession.objects.filter(student=student),
        institution
    ).select_related('lesson').order_by('-started_at')[:20]

    # Stats
    stats = {
        'total_sessions': filter_by_institution(TutorSession.objects.filter(student=student), institution).count(),
        'completed_sessions': filter_by_institution(TutorSession.objects.filter(student=student, status='completed'), institution).count(),
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
        **request.staff_ctx,
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
    
    units = course.units.prefetch_related('lessons', 'lessons__steps').order_by('order_index')
    
    # Get progress stats and content stats per lesson
    from apps.media_library.models import StepMedia
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
    is_generating = any(s['content_status'] == 'generating' for s in lesson_stats.values())

    from apps.dashboard.models import TeachingMaterialUpload, CurriculumUpload
    materials = TeachingMaterialUpload.objects.filter(course=course)

    # Find the most recent processing upload for the stop button
    active_upload = None
    if is_generating:
        active_upload = CurriculumUpload.objects.filter(
            created_course=course, status='processing',
        ).order_by('-created_at').first()

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
        'material_types': TeachingMaterialUpload.MaterialType.choices,
        'is_platform_wide': is_platform_wide,
        'course_read_only': course_read_only,
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
        grade_level = ','.join(grade_levels)

        if not uploaded_file:
            messages.error(request, "Please upload a curriculum file.")
            return redirect('dashboard:curriculum_upload')

        if not subject_name:
            messages.error(request, "Please enter a subject name.")
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

        upload_record = CurriculumUpload.objects.create(
            institution=institution,
            uploaded_by=request.user,
            file_path=file_path,
            subject_name=subject_name,
            grade_level=grade_level,
            status='pending'
        )

        # Handle optional material attachments (multi-file)
        material_files = request.FILES.getlist('material_files')
        if material_files and request.POST.get('attach_material'):
            material_title = request.POST.get('material_title', '').strip()
            if material_title:
                from apps.dashboard.material_tasks import process_teaching_material
                from apps.dashboard.background_tasks import run_async

                mat_dir = os.path.join(settings.MEDIA_ROOT, 'material_uploads')
                os.makedirs(mat_dir, exist_ok=True)

                for material_file in material_files:
                    mat_path = os.path.join(mat_dir, material_file.name)
                    with open(mat_path, 'wb+') as dest:
                        for chunk in material_file.chunks():
                            dest.write(chunk)

                    # For multiple files, append filename stem to title
                    if len(material_files) > 1:
                        stem = os.path.splitext(material_file.name)[0]
                        file_title = f"{material_title} - {stem}"
                    else:
                        file_title = material_title

                    material_record = TeachingMaterialUpload.objects.create(
                        institution=institution,
                        uploaded_by=request.user,
                        file_path=mat_path,
                        original_filename=material_file.name,
                        title=file_title,
                        subject_name=subject_name,
                        grade_level=grade_level,
                        material_type=request.POST.get('material_type', 'textbook'),
                        description=request.POST.get('material_description', '').strip(),
                        curriculum_upload=upload_record,
                    )

                    run_async(process_teaching_material, material_record.id)

        messages.success(request, "Curriculum uploaded! Processing will begin shortly.")
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
    institution = request.staff_ctx['institution']
    
    from apps.dashboard.models import CurriculumUpload
    
    lookup = {'id': upload_id}
    if institution is not None:
        lookup['institution'] = institution
    upload = get_object_or_404(CurriculumUpload, **lookup)

    # Prepare context based on status
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


@teacher_required
def curriculum_generate(request, upload_id):
    """API endpoint to start curriculum generation."""
    institution = request.staff_ctx['institution']
    
    from apps.dashboard.models import CurriculumUpload
    from apps.dashboard.tasks import process_curriculum_upload
    
    lookup = {'id': upload_id}
    if institution is not None:
        lookup['institution'] = institution
    upload = get_object_or_404(CurriculumUpload, **lookup)

    if upload.status != 'pending':
        return JsonResponse({'error': 'Already processing'}, status=400)
    
    # Start processing (sync for now, async with Celery later)
    try:
        upload.status = 'processing'
        upload.save()
        
        result = process_curriculum_upload(upload.id)
        
        return JsonResponse({
            'status': 'success',
            'success': True,
            'message': 'Processing started',
            'review_required': result.get('status') == 'review',
            'course_id': result.get('course_id'),
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
                        'estimated_minutes': 40,
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

    lookup = {'id': upload_id}
    if institution is not None:
        lookup['institution'] = institution
    upload = get_object_or_404(CurriculumUpload, **lookup)

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
            
            if 'math' in detected_subject.lower():
                curriculum = parse_mathematics_curriculum(text, grade_level)
            else:
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
    
    context = {
        **request.staff_ctx,
        'students_by_grade': students_by_grade,
    }
    
    return render(request, 'dashboard/classes/list.html', context)


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
    
    context = {
        **request.staff_ctx,
        'days': days,
        'sessions_by_day': list(sessions_by_day),
        'top_students': top_students,
        'lessons': lessons,
    }
    
    return render(request, 'dashboard/reports/overview.html', context)


# ============================================================================
# Teaching Materials
# ============================================================================

@staff_required
def material_process(request, upload_id):
    """Show processing status for a teaching material upload."""
    from apps.dashboard.models import TeachingMaterialUpload

    institution = request.staff_ctx['institution']
    lookup = {'id': upload_id}
    if institution is not None:
        lookup['institution'] = institution
    upload = get_object_or_404(TeachingMaterialUpload, **lookup)

    context = {
        **request.staff_ctx,
        'upload': upload,
    }
    return render(request, 'dashboard/materials/process.html', context)


@require_POST
@teacher_required
def course_upload_material(request, course_id):
    """Upload a teaching material directly to a course."""
    import os
    from django.conf import settings as django_settings
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.dashboard.material_tasks import process_teaching_material
    from apps.dashboard.background_tasks import run_async

    institution = request.staff_ctx['institution']
    course = get_scoped_object_or_404(Course, institution, id=course_id)

    uploaded_file = request.FILES.get('material_file')
    title = request.POST.get('material_title', '').strip()
    material_type = request.POST.get('material_type', 'textbook')
    description = request.POST.get('material_description', '').strip()

    if not uploaded_file or not title:
        messages.error(request, "File and title are required.")
        return redirect('dashboard:course_detail', course_id=course.id)

    upload_dir = os.path.join(django_settings.MEDIA_ROOT, 'material_uploads')
    os.makedirs(upload_dir, exist_ok=True)

    file_path = os.path.join(upload_dir, uploaded_file.name)
    with open(file_path, 'wb+') as dest:
        for chunk in uploaded_file.chunks():
            dest.write(chunk)

    material_record = TeachingMaterialUpload.objects.create(
        institution=course.institution,
        uploaded_by=request.user,
        file_path=file_path,
        original_filename=uploaded_file.name,
        title=title,
        subject_name=course.title,
        grade_level=course.grade_level or '',
        material_type=material_type,
        description=description,
        course=course,
    )

    run_async(process_teaching_material, material_record.id)

    messages.success(request, f"'{title}' uploaded! Processing started.")
    return redirect('dashboard:course_detail', course_id=course.id)


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

    all_timezones = sorted(zoneinfo.available_timezones())
    all_schools = Institution.objects.all().order_by('name') if is_superadmin else []
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
    from apps.media_library.models import StepMedia
    
    institution = request.staff_ctx['institution']
    
    lookup = {'id': lesson_id}
    if institution is not None:
        lookup['unit__course__institution'] = institution
    lesson = get_object_or_404(Lesson, **lookup)
    
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
        exit_questions = exit_ticket.questions.all().order_by('order_index')
        exit_ticket_count = exit_questions.count()
    
    # Students who completed
    students_completed = TutorSession.objects.filter(
        lesson=lesson,
        status='completed'
    ).values('student').distinct().count()
    
    context = {
        **request.staff_ctx,
        'lesson': lesson,
        'unit': lesson.unit,
        'course': lesson.unit.course,
        'steps': steps,
        'media_count': media_count,
        'exit_ticket': exit_ticket,
        'exit_questions': exit_questions,
        'exit_ticket_count': exit_ticket_count,
        'students_completed': students_completed,
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
    question.save()
    return JsonResponse({'success': True})


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
        generator = LessonContentGenerator(institution_id=institution.id)
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
                        institution=institution or lesson.unit.course.institution
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

        # Step 3: Generate exit tickets
        exit_questions = 0
        try:
            from apps.dashboard.background_tasks import generate_exit_ticket_for_lesson
            exit_questions = generate_exit_ticket_for_lesson(lesson, institution)
        except Exception as e:
            logger.warning(f"Exit ticket generation for {lesson.title}: {e}")

        # Step 4: Extract skills
        skills_extracted = 0
        try:
            from apps.tutoring.skill_extraction import SkillExtractionService
            skill_service = SkillExtractionService(institution_id=institution.id)
            skills = skill_service.extract_skills_for_lesson(lesson)
            skills_extracted = len(skills)
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

    # Guard: skip if already generating or has content
    if lesson.content_status == 'generating':
        messages.info(request, f"'{lesson.title}' is already being generated.")
        return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)

    if lesson.steps.count() >= 5:
        messages.info(request, f"'{lesson.title}' already has {lesson.steps.count()} steps.")
        return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)

    # Mark as generating and kick off in background
    lesson.content_status = 'generating'
    lesson.save(update_fields=['content_status'])

    institution_id = (institution or lesson.unit.course.institution).id
    run_async(generate_complete_lesson, lesson.id, institution_id)

    messages.info(request, f"Generating content for '{lesson.title}' in the background...")
    return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)


@teacher_required
@require_POST
def lesson_publish(request, lesson_id):
    """Publish or unpublish a lesson."""
    from apps.curriculum.models import Lesson
    
    institution = request.staff_ctx['institution']
    
    lookup = {'id': lesson_id}
    if institution is not None:
        lookup['unit__course__institution'] = institution
    lesson = get_object_or_404(Lesson, **lookup)
    
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
            # Re-render the same page (don't redirect to lesson detail)
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
                estimated_minutes=40,
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
            kb = CurriculumKnowledgeBase(institution_id=course.institution_id)
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