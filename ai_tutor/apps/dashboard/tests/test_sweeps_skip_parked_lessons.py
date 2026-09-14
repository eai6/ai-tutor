"""Course-wide sweeps must not spend on parked lessons.

"Does it include the relegated lesson? cuz that is a waste." — it did. Every
course-wide sweep selected `Lesson.objects.filter(unit__course=course)` with
no retired filter, so a replace that parked a unit's worth of lessons left
them being image-generated, content-generated and judged on every subsequent
catch-up run. Real spend on work no student can reach.
"""

from unittest.mock import patch

import pytest

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.dashboard.background_tasks import generate_media_for_lessons


@pytest.fixture
def course(db):
    school = Institution.objects.create(name='Belonie', slug='belonie')
    course = Course.objects.create(title='Mathematics S3', institution=school,
                                   subject_code='mathematics', grade_level='S3')
    unit = Unit.objects.create(course=course, title='U1', order_index=0)

    def _lesson(title, retired=False):
        from django.utils import timezone
        lesson = Lesson.objects.create(
            unit=unit, title=title, objective='o', order_index=0,
            retired_at=timezone.now() if retired else None)
        LessonStep.objects.create(
            lesson=lesson, order_index=0, teacher_script='c',
            media={'images': [
                {'description': f'a diagram for {title}', 'type': 'diagram'}]},
        )
        return lesson

    _lesson('Live Lesson')
    _lesson('Parked Lesson', retired=True)
    return course


def test_image_generation_skips_parked_lessons(course):
    """The reported waste. Each parked lesson still holds steps with empty
    image URLs, so an unfiltered sweep pays to generate every one."""
    seen = []

    class _FakeService:
        def __init__(self, lesson=None, institution=None):
            seen.append(lesson.title)

        def get_or_generate_image(self, **kwargs):
            return {'url': 'http://example/i.png'}

    with patch('ai_tutor.apps.tutoring.image_service.ImageGenerationService',
               _FakeService):
        generate_media_for_lessons(course.id)

    assert seen == ['Live Lesson'], f'parked lesson was processed: {seen}'


def test_media_generation_stops_when_cancelled(db):
    """Image generation is the slowest and most expensive sweep and had no
    way to stop it — content generation has had a Stop button all along. The
    flag is set by another request in another process, so the worker has to
    RE-READ it; trusting the row it already holds would never see the change.
    """
    from unittest.mock import patch

    from ai_tutor.apps.dashboard.background_tasks import generate_media_async
    from ai_tutor.apps.dashboard.models import CurriculumUpload

    school = Institution.objects.create(name='B', slug='b')
    c = Course.objects.create(title='C', institution=school)
    unit = Unit.objects.create(course=c, title='U', order_index=0)
    for i in range(3):
        lesson = Lesson.objects.create(unit=unit, title=f'L{i}', objective='o',
                                       order_index=i)
        LessonStep.objects.create(
            lesson=lesson, order_index=0, teacher_script='c',
            media={'images': [{'description': f'd{i}', 'type': 'diagram'}]})

    upload = CurriculumUpload.objects.create(
        institution=school, created_course=c, status='media_processing',
        subject_name=c.title, original_filename='m', file_path='')

    seen = []

    class _FakeService:
        def __init__(self, lesson=None, institution=None):
            seen.append(lesson.title)

        def get_or_generate_image(self, **kwargs):
            # A teacher presses Stop while the first lesson is generating.
            CurriculumUpload.objects.filter(id=upload.id).update(
                is_cancelled=True)
            return {'url': 'http://example/i.png'}

    with patch('ai_tutor.apps.tutoring.image_service.ImageGenerationService',
               _FakeService):
        generate_media_async(course_id=c.id, upload_id=upload.id)

    assert seen == ['L0'], f'kept going after cancel: {seen}'
    assert '⛔' in CurriculumUpload.objects.get(id=upload.id).processing_log
