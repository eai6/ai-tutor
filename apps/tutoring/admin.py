"""
Tutoring app admin interface.
"""

from django.contrib import admin
from .models import (
    TutorSession, SessionTurn, StudentLessonProgress,
    ExitTicket, ExitTicketQuestion, ExitTicketAttempt
)


# ============================================================================
# Session Admin
# ============================================================================

class SessionTurnInline(admin.TabularInline):
    model = SessionTurn
    extra = 0
    readonly_fields = ['role', 'content', 'created_at', 'step', 'tokens_in', 'tokens_out']
    ordering = ['created_at']


@admin.register(TutorSession)
class TutorSessionAdmin(admin.ModelAdmin):
    list_display = ['student', 'lesson', 'status', 'mastery_achieved', 'started_at']
    list_filter = ['status', 'mastery_achieved', 'institution']
    search_fields = ['student__username', 'lesson__title']
    readonly_fields = ['started_at', 'ended_at']
    inlines = [SessionTurnInline]


@admin.register(SessionTurn)
class SessionTurnAdmin(admin.ModelAdmin):
    list_display = ['session', 'role', 'short_content', 'created_at']
    list_filter = ['role', 'session__lesson']
    search_fields = ['content']
    
    def short_content(self, obj):
        return obj.content[:60] + "..." if len(obj.content) > 60 else obj.content
    short_content.short_description = "Content"


@admin.register(StudentLessonProgress)
class StudentLessonProgressAdmin(admin.ModelAdmin):
    list_display = ['student', 'lesson', 'mastery_level', 'correct_streak', 'total_attempts']
    list_filter = ['mastery_level', 'institution']
    search_fields = ['student__username', 'lesson__title']


# ============================================================================
# Exit Ticket Admin
# ============================================================================

class ExitTicketQuestionInline(admin.TabularInline):
    model = ExitTicketQuestion
    extra = 0
    fields = ['order_index', 'question_text', 'option_a', 'option_b', 'option_c', 'option_d', 'correct_answer', 'difficulty']
    ordering = ['order_index']


@admin.register(ExitTicket)
class ExitTicketAdmin(admin.ModelAdmin):
    list_display = ['lesson', 'question_count_display', 'passing_score', 'time_limit_minutes', 'created_at']
    list_filter = ['lesson__unit__course', 'created_at']
    search_fields = ['lesson__title']
    inlines = [ExitTicketQuestionInline]
    
    def question_count_display(self, obj):
        count = obj.questions.count()
        return f"{count}/10" if count < 10 else "✓ 10"
    question_count_display.short_description = "Questions"


@admin.register(ExitTicketQuestion)
class ExitTicketQuestionAdmin(admin.ModelAdmin):
    list_display = ['exit_ticket', 'order_index', 'short_question', 'correct_answer', 'difficulty']
    list_filter = ['difficulty', 'exit_ticket__lesson__unit__course']
    search_fields = ['question_text', 'exit_ticket__lesson__title']
    ordering = ['exit_ticket', 'order_index']
    
    def short_question(self, obj):
        return obj.question_text[:60] + "..." if len(obj.question_text) > 60 else obj.question_text
    short_question.short_description = "Question"


@admin.register(ExitTicketAttempt)
class ExitTicketAttemptAdmin(admin.ModelAdmin):
    list_display = ['student', 'exit_ticket', 'score', 'passed', 'started_at', 'completed_at']
    list_filter = ['passed', 'exit_ticket__lesson__unit__course']
    search_fields = ['student__username', 'exit_ticket__lesson__title']
    readonly_fields = ['student', 'exit_ticket', 'session', 'score', 'passed', 'answers', 'started_at', 'completed_at']