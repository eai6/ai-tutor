from django.contrib import admin
from .models import Course, Unit, Lesson, LessonStep


class UnitInline(admin.TabularInline):
    model = Unit
    extra = 1
    fields = ['title', 'order_index']


class LessonInline(admin.TabularInline):
    model = Lesson
    extra = 1
    fields = ['title', 'order_index', 'is_published']


class LessonStepInline(admin.StackedInline):
    """
    Stacked inline for lesson steps - shows more fields per step.
    Use TabularInline if you prefer a compact view.
    """
    model = LessonStep
    extra = 1
    fields = [
        'order_index', 'step_type', 'teacher_script', 
        'question', 'answer_type', 'choices', 'expected_answer',
        'hint_1', 'hint_2', 'hint_3', 'max_attempts'
    ]


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ['title', 'institution', 'grade_level', 'is_published', 'updated_at']
    list_filter = ['is_published', 'institution', 'grade_level']
    search_fields = ['title', 'description']
    inlines = [UnitInline]


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ['title', 'course', 'order_index']
    list_filter = ['course__institution', 'course']
    search_fields = ['title']
    inlines = [LessonInline]


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ['title', 'unit', 'mastery_rule', 'is_published', 'estimated_minutes']
    list_filter = ['is_published', 'mastery_rule', 'unit__course__institution']
    search_fields = ['title', 'objective']
    inlines = [LessonStepInline]
    
    fieldsets = (
        (None, {
            'fields': ('unit', 'title', 'objective')
        }),
        ('Settings', {
            'fields': ('order_index', 'estimated_minutes', 'mastery_rule', 'is_published')
        }),
    )


@admin.register(LessonStep)
class LessonStepAdmin(admin.ModelAdmin):
    """
    Standalone admin for steps - useful for detailed editing.
    Most editing will happen via Lesson inline.
    """
    list_display = ['__str__', 'step_type', 'answer_type', 'order_index']
    list_filter = ['step_type', 'answer_type', 'lesson__unit__course__institution']
    search_fields = ['teacher_script', 'question']
