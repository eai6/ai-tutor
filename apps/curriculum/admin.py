from django.contrib import admin
from django.utils.html import format_html
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
    """
    model = LessonStep
    extra = 1
    fields = [
        'order_index', 'phase', 'step_type', 'teacher_script', 
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
    Standalone admin for steps - allows detailed editing of all fields
    including media, educational content, and curriculum context.
    """
    list_display = ['__str__', 'phase', 'step_type', 'answer_type', 'has_media_display', 'order_index']
    list_filter = ['step_type', 'answer_type', 'phase', 'lesson__unit__course__institution']
    search_fields = ['teacher_script', 'question', 'lesson__title']
    
    fieldsets = (
        ('Lesson & Order', {
            'fields': ('lesson', 'order_index', 'phase')
        }),
        ('Step Content', {
            'fields': ('step_type', 'teacher_script'),
            'description': 'The main teaching content for this step.'
        }),
        ('Question & Answer', {
            'fields': ('question', 'answer_type', 'choices', 'expected_answer', 'rubric'),
            'classes': ('collapse',),
            'description': 'For practice and quiz steps.'
        }),
        ('Hints', {
            'fields': ('hint_1', 'hint_2', 'hint_3', 'max_attempts'),
            'classes': ('collapse',),
        }),
        ('📸 Media Content', {
            'fields': ('media',),
            'classes': ('collapse',),
            'description': '''JSON structure for media:
            {
                "images": [{"url": "...", "alt": "...", "caption": "...", "type": "diagram|photo"}],
                "videos": [{"url": "...", "title": "...", "duration_seconds": 120}]
            }'''
        }),
        ('📚 Educational Content', {
            'fields': ('educational_content',),
            'classes': ('collapse',),
            'description': '''JSON structure:
            {
                "key_vocabulary": [{"term": "...", "definition": "...", "example": "..."}],
                "worked_example": {"problem": "...", "steps": [...], "final_answer": "..."},
                "key_points": ["..."],
                "common_mistakes": ["..."],
                "seychelles_context": "Local example"
            }'''
        }),
        ('🎓 Curriculum Context', {
            'fields': ('curriculum_context',),
            'classes': ('collapse',),
            'description': '''JSON structure from knowledge base:
            {
                "teaching_strategies": ["..."],
                "learning_objectives": ["..."],
                "differentiation": {"support": "...", "extension": "..."}
            }'''
        }),
    )
    
    def has_media_display(self, obj):
        """Show if step has media content."""
        if obj.has_media():
            return format_html('<span style="color: green;">✓ Yes</span>')
        return format_html('<span style="color: gray;">-</span>')
    has_media_display.short_description = 'Media'
    
    class Media:
        css = {
            'all': ('admin/css/forms.css',)
        }
        js = ('admin/js/vendor/jquery/jquery.min.js',)
