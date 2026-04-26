"""Read-only resource endpoints — courses, lessons, sessions, progress."""

from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from apps.api.mixins import InstitutionScopedMixin
from apps.api.permissions import IsInstitutionMember
from apps.api.serializers.curriculum import (
    CourseSerializer, LessonSerializer, LessonStepSerializer,
)
from apps.api.serializers.tutoring import (
    TutorSessionSerializer, StudentLessonProgressSerializer,
)
from apps.curriculum.models import Course, Lesson, LessonStep
from apps.tutoring.models import TutorSession, StudentLessonProgress


class CourseList(InstitutionScopedMixin, generics.ListAPIView):
    """GET /api/v1/courses/ — courses visible to the user."""
    permission_classes = [IsAuthenticated, IsInstitutionMember]
    serializer_class = CourseSerializer
    queryset = Course.objects.filter(is_published=True).order_by('title')
    institution_field = 'institution'


class LessonList(InstitutionScopedMixin, generics.ListAPIView):
    """GET /api/v1/lessons/ — lessons across visible courses.
    Filters: ?course=<id>, ?unit=<id>.
    """
    permission_classes = [IsAuthenticated, IsInstitutionMember]
    serializer_class = LessonSerializer
    institution_field = 'unit__course__institution'
    queryset = Lesson.objects.filter(is_published=True).select_related(
        'unit', 'unit__course',
    ).order_by('unit__order_index', 'order_index')

    def get_queryset(self):
        qs = super().get_queryset()
        course = self.request.query_params.get('course')
        unit = self.request.query_params.get('unit')
        if course:
            qs = qs.filter(unit__course_id=course)
        if unit:
            qs = qs.filter(unit_id=unit)
        return qs


class LessonDetail(InstitutionScopedMixin, generics.RetrieveAPIView):
    """GET /api/v1/lessons/<id>/."""
    permission_classes = [IsAuthenticated, IsInstitutionMember]
    serializer_class = LessonSerializer
    institution_field = 'unit__course__institution'
    queryset = Lesson.objects.filter(is_published=True).select_related(
        'unit', 'unit__course',
    )


class LessonStepList(InstitutionScopedMixin, generics.ListAPIView):
    """GET /api/v1/lessons/<lesson_id>/steps/."""
    permission_classes = [IsAuthenticated, IsInstitutionMember]
    serializer_class = LessonStepSerializer
    institution_field = 'lesson__unit__course__institution'
    queryset = LessonStep.objects.all().order_by('order_index')

    def get_queryset(self):
        return super().get_queryset().filter(lesson_id=self.kwargs['lesson_id'])


class SessionDetail(generics.RetrieveAPIView):
    """GET /api/v1/sessions/<id>/ — full session payload."""
    permission_classes = [IsAuthenticated, IsInstitutionMember]
    serializer_class = TutorSessionSerializer

    def get_queryset(self):
        from django.db.models import Q
        user = self.request.user
        # Either the primary student or an active participant.
        return TutorSession.objects.filter(
            Q(student=user) | Q(participants__student=user, participants__is_active=True),
        ).distinct().prefetch_related('participants__student').select_related('lesson')


class ProgressList(generics.ListAPIView):
    """GET /api/v1/progress/ — student's lesson progress across all
    institutions they belong to."""
    permission_classes = [IsAuthenticated, IsInstitutionMember]
    serializer_class = StudentLessonProgressSerializer

    def get_queryset(self):
        return StudentLessonProgress.objects.filter(
            student=self.request.user,
        ).select_related('lesson', 'lesson__unit', 'lesson__unit__course').order_by('-updated_at')
