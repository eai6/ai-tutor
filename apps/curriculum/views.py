"""
Curriculum Views - Web endpoints for browsing curriculum content.
"""

from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson
from apps.tutoring.models import StudentLessonProgress


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
