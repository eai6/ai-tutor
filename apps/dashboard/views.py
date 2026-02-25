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

logger = logging.getLogger(__name__)


def get_staff_context(request):
    """Get common context for staff views."""
    membership = Membership.objects.filter(
        user=request.user,
        role__in=['staff', 'superadmin'],
        is_active=True
    ).select_related('institution').first()
    
    if not membership:
        return None
    
    return {
        'membership': membership,
        'institution': membership.institution,
        'role': membership.role,
    }


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
    
    from apps.dashboard.models import TeachingMaterialUpload
    materials = TeachingMaterialUpload.objects.filter(institution=institution)

    context = {
        **request.staff_ctx,
        'courses': course_data,
        'materials': materials,
    }

    return render(request, 'dashboard/curriculum/list.html', context)


@teacher_required
def course_detail(request, course_id):
    """View and manage a course's units and lessons."""
    institution = request.staff_ctx['institution']
    
    course = get_object_or_404(Course, id=course_id, institution=institution)
    
    units = course.units.prefetch_related('lessons', 'lessons__steps').order_by('order_index')
    
    # Get progress stats and content stats per lesson
    from apps.media_library.models import StepMedia
    from apps.tutoring.models import ExitTicket
    
    lesson_stats = {}
    for unit in units:
        for lesson in unit.lessons.all():
            # Progress stats
            progress = StudentLessonProgress.objects.filter(
                institution=institution,
                lesson=lesson
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
            }
    
    # Course-level stats
    total_lessons = sum(unit.lessons.count() for unit in units)
    lessons_with_content = sum(1 for stats in lesson_stats.values() if stats['has_content'])
    lessons_without_content = total_lessons - lessons_with_content
    total_media = sum(stats['media_count'] for stats in lesson_stats.values())
    total_media_pending = sum(stats['media_pending'] for stats in lesson_stats.values())
    
    from apps.dashboard.models import TeachingMaterialUpload
    materials = TeachingMaterialUpload.objects.filter(course=course)

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
        'materials': materials,
        'material_types': TeachingMaterialUpload.MaterialType.choices,
    }

    return render(request, 'dashboard/curriculum/course_detail.html', context)


@teacher_required
def curriculum_upload(request):
    """Upload curriculum document with optional teaching material attachment."""
    institution = request.staff_ctx['institution']

    if request.method == 'POST':
        uploaded_file = request.FILES.get('curriculum_file')
        subject_name = request.POST.get('subject_name', '').strip()
        grade_level = request.POST.get('grade_level', '')

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

        # Handle optional material attachment
        material_file = request.FILES.get('material_file')
        if material_file and request.POST.get('attach_material'):
            material_title = request.POST.get('material_title', '').strip()
            if material_title:
                from apps.dashboard.material_tasks import process_teaching_material
                from apps.dashboard.background_tasks import run_async

                mat_dir = os.path.join(settings.MEDIA_ROOT, 'material_uploads')
                os.makedirs(mat_dir, exist_ok=True)

                mat_path = os.path.join(mat_dir, material_file.name)
                with open(mat_path, 'wb+') as dest:
                    for chunk in material_file.chunks():
                        dest.write(chunk)

                material_record = TeachingMaterialUpload.objects.create(
                    institution=institution,
                    uploaded_by=request.user,
                    file_path=mat_path,
                    original_filename=material_file.name,
                    title=material_title,
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
        'grade_levels': StudentProfile.GradeLevel.choices,
        'material_types': TeachingMaterialUpload.MaterialType.choices,
    }

    return render(request, 'dashboard/curriculum/upload.html', context)


@teacher_required
def curriculum_process(request, upload_id):
    """Process uploaded curriculum and show progress."""
    institution = request.staff_ctx['institution']
    
    from apps.dashboard.models import CurriculumUpload
    
    upload = get_object_or_404(
        CurriculumUpload,
        id=upload_id,
        institution=institution
    )
    
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
    
    upload = get_object_or_404(
        CurriculumUpload,
        id=upload_id,
        institution=institution
    )
    
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
        
        # Get generation options
        generate_steps = data.get('generate_steps', True)
        generate_media = data.get('generate_media', True)
        generate_exit_tickets = data.get('generate_exit_tickets', True)
        
        # Update status to processing
        upload.status = 'processing'
        upload.current_step = 3
        upload.add_log("💾 Creating curriculum records...")
        upload.save()
        
        # Create or update course
        subject = upload.subject_name
        grade = upload.grade_level or 'S1'
        course_title = f"{subject} {grade}"
        
        course, created = Course.objects.update_or_create(
            institution=institution,
            title=course_title,
            defaults={
                'description': f"{subject} curriculum for {grade}",
                'grade_level': grade,
                'is_published': False,
            }
        )
        
        upload.created_course = course
        upload.add_log(f"   {'Created' if created else 'Updated'} course: {course.title}")
        
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
        
        # Start async content generation if requested
        lessons_in_course = Lesson.objects.filter(unit__course=course).count()
        
        if generate_steps and lessons_in_course > 0:
            upload.current_step = 4
            upload.add_log(f"📝 Starting background content generation for {lessons_in_course} lessons...")
            upload.add_log(f"   This will continue in the background. Refresh to see progress.")
            upload.save()
            
            # Start async generation
            from apps.dashboard.background_tasks import run_async, generate_all_content_async
            run_async(
                generate_all_content_async, 
                course_id=course.id, 
                upload_id=upload.id,
                generate_media=generate_media
            )
            
            # Return immediately - generation continues in background
            return JsonResponse({
                'success': True,
                'status': 'processing',
                'message': f'Content generation started for {lessons_in_course} lessons',
                'course_id': course.id,
                'units_created': units_created,
                'lessons_created': lessons_created,
            })
        else:
            # No content generation requested - mark complete
            upload.status = 'completed'
            upload.steps_created = 0
            upload.completed_at = timezone.now()
            upload.add_log(f"✅ Complete! Course '{course.title}' created (no content generation).")
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
    
    upload = get_object_or_404(
        CurriculumUpload,
        id=upload_id,
        institution=institution
    )
    
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
                institution=institution,
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
    upload = get_object_or_404(
        TeachingMaterialUpload,
        id=upload_id,
        institution=institution,
    )

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
    course = get_object_or_404(Course, id=course_id, institution=institution)

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
        institution=institution,
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
    is_superadmin = membership.role == 'superadmin'

    if request.method == 'POST':
        action = request.POST.get('action', 'general')

        if action == 'general':
            institution.name = request.POST.get('name', institution.name)
            institution.timezone = request.POST.get('timezone', institution.timezone)
            institution.save()
            messages.success(request, "Settings updated.")

        elif action == 'theme' and is_superadmin:
            if request.FILES.get('logo'):
                institution.logo = request.FILES['logo']
            if request.POST.get('clear_logo') == '1':
                institution.logo = None
            institution.primary_color = request.POST.get('primary_color', institution.primary_color)
            institution.secondary_color = request.POST.get('secondary_color', institution.secondary_color)
            institution.accent_color = request.POST.get('accent_color', institution.accent_color)
            institution.custom_css = request.POST.get('custom_css', '')
            institution.save()
            messages.success(request, "Theme updated.")

        elif action == 'prompts' and is_superadmin:
            from apps.llm.models import PromptPack
            prompt_pack = PromptPack.objects.filter(
                institution=institution, is_active=True
            ).first()
            if not prompt_pack:
                prompt_pack = PromptPack.objects.create(
                    institution=institution,
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

    # Load prompt pack for display
    prompt_pack = None
    if is_superadmin:
        from apps.llm.models import PromptPack
        prompt_pack = PromptPack.objects.filter(
            institution=institution, is_active=True
        ).first()

    context = {
        **request.staff_ctx,
        'is_superadmin': is_superadmin,
        'prompt_pack': prompt_pack,
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
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution
    )
    
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


def generate_exit_ticket_for_lesson(lesson, institution) -> int:
    """
    Generate exit ticket MCQs for a lesson.
    Returns the number of questions generated.
    """
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client
    import json
    
    # Get LLM config
    config = ModelConfig.objects.filter(is_active=True).first()
    if not config:
        logger.error("No active LLM model configured for exit ticket generation")
        return 0
    
    # Build prompt for exit questions
    prompt = f"""Generate 10 multiple choice exit ticket questions for this lesson.

Lesson: {lesson.title}
Objective: {lesson.objective}
Subject: {lesson.unit.course.title}

Generate questions that test understanding of the key concepts. Each question should have:
- A clear question
- 4 answer choices (A, B, C, D)
- The correct answer letter (just the letter: A, B, C, or D)
- Brief explanation

Return as JSON array:
[
  {{
    "question": "What is...",
    "option_a": "First option",
    "option_b": "Second option", 
    "option_c": "Third option",
    "option_d": "Fourth option",
    "correct_answer": "A",
    "explanation": "Brief explanation of why A is correct"
  }}
]

Return ONLY the JSON array, no other text."""

    try:
        client = get_llm_client(config)
        
        system_prompt = "You are an expert teacher creating assessment questions. Return ONLY valid JSON, no other text."
        messages = [{"role": "user", "content": prompt}]
        
        response = client.generate(messages, system_prompt)
        response_text = response.content.strip()
        
        logger.info(f"Exit ticket raw response length: {len(response_text)}")
        
        # Handle markdown code blocks
        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0]
        elif '```' in response_text:
            parts = response_text.split('```')
            if len(parts) >= 2:
                response_text = parts[1]
        
        response_text = response_text.strip()
        
        logger.info(f"Exit ticket cleaned response: {response_text[:200]}...")
        
        questions_data = json.loads(response_text)
        
        if not questions_data or not isinstance(questions_data, list):
            logger.warning(f"Invalid exit ticket response for {lesson.title}: not a list")
            return 0
        
        logger.info(f"Parsed {len(questions_data)} questions for {lesson.title}")
        
        # Delete existing exit ticket and questions
        ExitTicket.objects.filter(lesson=lesson).delete()
        
        # Create new exit ticket
        exit_ticket = ExitTicket.objects.create(
            lesson=lesson,
            passing_score=8,
            time_limit_minutes=15,
            instructions="Answer all 10 questions. You need 8 correct to pass."
        )
        
        # Create questions
        questions_created = 0
        for i, q in enumerate(questions_data[:10]):  # Limit to 10
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
                    order_index=i
                )
                questions_created += 1
            except Exception as e:
                logger.warning(f"Failed to create question {i}: {e}")
        
        logger.info(f"Created {questions_created} exit questions for {lesson.title}")
        return questions_created
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error for exit ticket: {e}")
        logger.error(f"Response was: {response_text[:500] if 'response_text' in dir() else 'N/A'}")
        return 0
    except Exception as e:
        logger.error(f"Exit ticket generation failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 0


@teacher_required
@require_POST
def lesson_regenerate(request, lesson_id):
    """Regenerate content, media, and exit questions for a lesson."""
    from apps.curriculum.models import Lesson
    from apps.curriculum.content_generator import LessonContentGenerator
    from apps.tutoring.models import ExitTicket
    
    institution = request.staff_ctx['institution']
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution
    )
    
    try:
        # Delete existing steps
        lesson.steps.all().delete()
        
        # Delete existing exit ticket
        ExitTicket.objects.filter(lesson=lesson).delete()
        
        # Generate new content
        generator = LessonContentGenerator(institution_id=institution.id)
        result = generator.generate_for_lesson(lesson, save_to_db=True)
        
        if result.get('success'):
            steps_generated = result.get('steps_generated', 0)
            
            # Generate media for this lesson
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
                            institution=institution
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
            
            # Generate exit questions
            exit_questions = 0
            try:
                exit_questions = generate_exit_ticket_for_lesson(lesson, institution)
            except Exception as e:
                logger.warning(f"Exit ticket generation for {lesson.title}: {e}")
            
            messages.success(
                request, 
                f"Regenerated {steps_generated} steps, {media_generated} images, and {exit_questions} exit questions for '{lesson.title}'"
            )
        else:
            messages.warning(request, result.get('error', 'Could not regenerate'))
            
    except Exception as e:
        import traceback
        logger.error(f"Regeneration error: {traceback.format_exc()}")
        messages.error(request, f"Error: {str(e)}")
    
    return redirect('dashboard:lesson_detail', lesson_id=lesson.id)


@teacher_required
@require_POST
def lesson_generate_content(request, lesson_id):
    """Generate content and media for a lesson that doesn't have any."""
    from apps.curriculum.models import Lesson
    from apps.curriculum.content_generator import LessonContentGenerator
    
    institution = request.staff_ctx['institution']
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution
    )
    
    # Check if already has content
    if lesson.steps.count() >= 5:
        messages.info(request, f"'{lesson.title}' already has {lesson.steps.count()} steps.")
        return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)
    
    try:
        generator = LessonContentGenerator(institution_id=institution.id)
        result = generator.generate_for_lesson(lesson, save_to_db=True)
        
        if result.get('success'):
            steps_generated = result.get('steps_generated', 0)
            
            # Also generate media for this lesson
            media_generated = 0
            try:
                from apps.tutoring.image_service import ImageGenerationService
                
                for step in lesson.steps.all():
                    if not step.media:
                        continue
                    
                    images = step.media.get('images', [])
                    media_updated = False
                    
                    for img in images:
                        if img.get('url'):  # Already has URL
                            continue
                        
                        description = img.get('description', '')
                        if not description:
                            continue
                        
                        service = ImageGenerationService(
                            lesson=lesson,
                            institution=institution
                        )
                        
                        # Always generate fresh images (don't use potentially mismatched existing ones)
                        img_result = service.get_or_generate_image(
                            prompt=description,
                            category=img.get('type', 'diagram'),
                            generate_only=True  # Always generate new
                        )
                        
                        if img_result and img_result.get('url'):
                            img['url'] = img_result['url']
                            img['source'] = 'generated' if img_result.get('generated') else 'library'
                            media_updated = True
                            media_generated += 1
                    
                    if media_updated:
                        step.save()
                        
            except Exception as e:
                logger.warning(f"Media generation for {lesson.title}: {e}")
            
            # Generate exit questions
            exit_questions = 0
            try:
                exit_questions = generate_exit_ticket_for_lesson(lesson, institution)
            except Exception as e:
                logger.warning(f"Exit ticket generation for {lesson.title}: {e}")
            
            messages.success(
                request, 
                f"Generated {steps_generated} steps, {media_generated} images, and {exit_questions} exit questions for '{lesson.title}'"
            )
        else:
            messages.warning(request, f"Could not generate: {result.get('error', 'Unknown error')}")
            
    except Exception as e:
        import traceback
        logger.error(f"Content generation error: {traceback.format_exc()}")
        messages.error(request, f"Error: {str(e)}")
    
    return redirect('dashboard:course_detail', course_id=lesson.unit.course.id)


@teacher_required
@require_POST
def lesson_publish(request, lesson_id):
    """Publish or unpublish a lesson."""
    from apps.curriculum.models import Lesson
    
    institution = request.staff_ctx['institution']
    
    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
        unit__course__institution=institution
    )
    
    # Toggle publish status
    lesson.is_published = not lesson.is_published
    lesson.save()
    
    status = "published" if lesson.is_published else "unpublished"
    messages.success(request, f"Lesson '{lesson.title}' {status}.")
    
    return redirect('dashboard:lesson_detail', lesson_id=lesson.id)


@teacher_required
def step_edit(request, step_id):
    """Edit a lesson step."""
    from apps.curriculum.models import LessonStep
    
    institution = request.staff_ctx['institution']
    
    step = get_object_or_404(
        LessonStep,
        id=step_id,
        lesson__unit__course__institution=institution
    )
    
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
        # Update step content
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
    course = get_object_or_404(Course, id=course_id, institution=institution)
    
    # Create a new processing record for progress tracking
    upload = CurriculumUpload.objects.create(
        institution=institution,
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
    course = get_object_or_404(Course, id=course_id, institution=institution)
    
    # Check if force regenerate was requested
    force_regenerate = request.POST.get('force', '') == '1'
    
    # Create a new processing record for progress tracking
    upload = CurriculumUpload.objects.create(
        institution=institution,
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
    
    upload = get_object_or_404(
        CurriculumUpload,
        id=upload_id,
        institution=institution
    )
    
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
    
    upload = get_object_or_404(
        CurriculumUpload,
        id=upload_id,
        institution=institution
    )
    
    context = {
        **request.staff_ctx,
        'upload': upload,
        'course': upload.created_course,
    }
    
    return render(request, 'dashboard/curriculum/content_progress.html', context)


@teacher_required
@require_POST
def course_publish_all(request, course_id):
    """Publish all lessons in a course."""
    from apps.curriculum.models import Lesson
    
    institution = request.staff_ctx['institution']
    course = get_object_or_404(Course, id=course_id, institution=institution)
    
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
    course = get_object_or_404(Course, id=course_id, institution=institution)
    
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
    unit = get_object_or_404(Unit, id=unit_id, course__institution=institution)
    
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