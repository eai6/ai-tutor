"""Serializers for Course / Unit / Lesson / LessonStep."""

from rest_framework import serializers

from ai_tutor.apps.curriculum.models import Course, Unit, Lesson, LessonStep


class CourseSerializer(serializers.ModelSerializer):
    institution_id = serializers.IntegerField(read_only=True, allow_null=True)
    is_math = serializers.BooleanField(read_only=True)

    class Meta:
        model = Course
        fields = [
            'id', 'title', 'description', 'grade_level', 'subject_type',
            'is_math', 'institution_id', 'is_published',
        ]


class UnitSerializer(serializers.ModelSerializer):
    course_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Unit
        fields = ['id', 'course_id', 'title', 'description', 'order_index', 'grade_level']


class LessonSerializer(serializers.ModelSerializer):
    unit_id = serializers.IntegerField(read_only=True)
    unit_title = serializers.CharField(source='unit.title', read_only=True)
    course_id = serializers.IntegerField(source='unit.course_id', read_only=True)
    course_title = serializers.CharField(source='unit.course.title', read_only=True)

    class Meta:
        model = Lesson
        fields = [
            'id', 'unit_id', 'unit_title', 'course_id', 'course_title',
            'title', 'objective', 'estimated_minutes', 'order_index',
            'is_published', 'enabling_objectives', 'content_status',
            'content_quality', 'allow_group_mode', 'max_group_size',
            'group_requires_approval',
        ]


class LessonStepSerializer(serializers.ModelSerializer):
    """Full step payload, used by the offline pack."""

    class Meta:
        model = LessonStep
        fields = [
            'id', 'order_index', 'step_type', 'phase', 'concept_tag',
            'enabling_objective', 'teacher_script', 'question',
            'answer_type', 'choices', 'expected_answer', 'rubric',
            'hint_1', 'hint_2', 'hint_3', 'max_attempts',
            'media', 'educational_content', 'curriculum_context',
        ]
