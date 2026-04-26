"""Serializers for TutorSession + SessionTurn + StudentLessonProgress
+ ExitTicket + ExitTicketAttempt + SessionParticipant."""

from rest_framework import serializers

from apps.tutoring.models import (
    TutorSession,
    SessionTurn,
    SessionParticipant,
    StudentLessonProgress,
    ExitTicket,
    ExitTicketQuestion,
    ExitTicketAttempt,
)


class SessionTurnSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionTurn
        fields = [
            'id', 'role', 'content', 'metadata', 'created_at',
            'generated_offline', 'offline_model_id', 'client_generated_at',
            'is_flagged', 'flag_type',
        ]
        read_only_fields = ['id', 'created_at']


class SessionParticipantSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source='student_id', read_only=True)
    username = serializers.CharField(source='student.username', read_only=True)

    class Meta:
        model = SessionParticipant
        fields = ['id', 'user_id', 'username', 'is_primary', 'is_active', 'joined_at', 'left_at']


class TutorSessionSerializer(serializers.ModelSerializer):
    participants = SessionParticipantSerializer(many=True, read_only=True)
    is_group = serializers.BooleanField(read_only=True)
    primary_student_id = serializers.IntegerField(source='student_id', read_only=True)

    class Meta:
        model = TutorSession
        fields = [
            'id', 'lesson_id', 'institution_id', 'primary_student_id',
            'status', 'mastery_achieved', 'engine_state', 'summary',
            'started_at', 'ended_at', 'started_lesson_at', 'completed_lesson_at',
            'is_flagged', 'is_group', 'participants',
            'group_approval_status', 'group_approval_decided_at',
        ]
        read_only_fields = fields  # client never POSTs the session shape directly


class StudentLessonProgressSerializer(serializers.ModelSerializer):
    lesson_title = serializers.CharField(source='lesson.title', read_only=True)
    course_id = serializers.IntegerField(source='lesson.unit.course_id', read_only=True)

    class Meta:
        model = StudentLessonProgress
        fields = [
            'id', 'lesson_id', 'lesson_title', 'course_id',
            'mastery_level', 'best_score', 'attempts_count',
            'last_attempt_at', 'last_session_at',
            'last_completion_session_id', 'last_completion_was_group',
        ]


class ExitTicketQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExitTicketQuestion
        fields = [
            'id', 'question_type', 'question_text',
            'option_a', 'option_b', 'option_c', 'option_d',
            'correct_answer', 'answer_data', 'explanation',
            'concept_tag', 'difficulty', 'order_index',
        ]


class ExitTicketSerializer(serializers.ModelSerializer):
    questions = ExitTicketQuestionSerializer(many=True, read_only=True)

    class Meta:
        model = ExitTicket
        fields = ['id', 'lesson_id', 'passing_score', 'time_limit_minutes', 'instructions', 'questions']


class ExitTicketAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExitTicketAttempt
        fields = [
            'id', 'exit_ticket_id', 'student_id', 'session_id',
            'score', 'passed', 'answers', 'started_at', 'completed_at',
        ]
