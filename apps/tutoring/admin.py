from django.contrib import admin
from .models import TutorSession, SessionTurn, StudentLessonProgress


class SessionTurnInline(admin.TabularInline):
    model = SessionTurn
    extra = 0
    readonly_fields = ['role', 'content', 'created_at', 'step', 'tokens_in', 'tokens_out']
    can_delete = False
    
    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TutorSession)
class TutorSessionAdmin(admin.ModelAdmin):
    list_display = ['student', 'lesson', 'status', 'mastery_achieved', 'started_at']
    list_filter = ['status', 'mastery_achieved', 'institution']
    search_fields = ['student__username', 'lesson__title']
    readonly_fields = ['started_at', 'ended_at']
    raw_id_fields = ['student', 'lesson', 'prompt_pack', 'model_config']
    inlines = [SessionTurnInline]


@admin.register(SessionTurn)
class SessionTurnAdmin(admin.ModelAdmin):
    list_display = ['session', 'role', 'created_at', 'tokens_in', 'tokens_out']
    list_filter = ['role', 'session__institution']
    readonly_fields = ['created_at']
    raw_id_fields = ['session', 'step']


@admin.register(StudentLessonProgress)
class StudentLessonProgressAdmin(admin.ModelAdmin):
    list_display = ['student', 'lesson', 'mastery_level', 'correct_streak', 'updated_at']
    list_filter = ['mastery_level', 'institution']
    search_fields = ['student__username', 'lesson__title']
    readonly_fields = ['created_at', 'updated_at']
    raw_id_fields = ['student', 'lesson']
