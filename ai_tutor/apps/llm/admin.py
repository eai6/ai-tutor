from django import forms
from django.contrib import admin
from .models import PromptPack, ModelConfig


@admin.register(PromptPack)
class PromptPackAdmin(admin.ModelAdmin):
    list_display = ['name', 'institution', 'version', 'is_active', 'updated_at']
    list_filter = ['is_active', 'institution']
    search_fields = ['name']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        (None, {
            'fields': ('institution', 'name', 'version', 'is_active')
        }),
        ('Prompts', {
            'fields': ('system_prompt', 'teaching_style_prompt', 'safety_prompt', 'format_rules_prompt'),
            'classes': ('wide',)
        }),
        ('Extended Prompts', {
            'fields': ('tutor_system_prompt', 'content_generation_prompt', 'exit_ticket_prompt', 'grading_prompt', 'image_generation_prompt'),
            'classes': ('collapse', 'wide'),
            'description': 'Override system prompts for specific consumers. Leave empty to use built-in defaults.',
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


class ModelConfigAdminForm(forms.ModelForm):
    """Turns model_name into a grouped picker instead of a free-text box.

    Typing a tag by hand has no feedback loop on a headless box: a typo, an
    unpulled tag, or a tag with no exact MODEL_PROFILES entry all surface the
    same way — as a broken student turn, later, with no monitor attached. The
    options come from what is actually pulled and profiled on this machine.

    Choices are rebuilt per request rather than declared on the field, because
    what is pulled changes without a migration.
    """

    class Meta:
        model = ModelConfig
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from ai_tutor.apps.llm.model_catalog import available_choices

        current = (self.instance.model_name or '') if self.instance else ''
        groups = available_choices(include=current)
        if not groups:
            # Ollama down and no rows to learn from — leave the text input
            # rather than presenting an empty select the admin cannot escape.
            return
        self.fields['model_name'] = forms.ChoiceField(
            choices=groups,
            required=self.fields['model_name'].required,
            label=self.fields['model_name'].label,
            help_text=(
                "Local models are listed only when they are pulled AND have an "
                "exact profile. An unprofiled local tag is sized for the cloud "
                "(num_ctx=24192) and will not fit this device."
            ),
        )


@admin.register(ModelConfig)
class ModelConfigAdmin(admin.ModelAdmin):
    form = ModelConfigAdminForm
    list_display = ['name', 'institution', 'provider', 'model_name', 'is_active']
    list_filter = ['provider', 'is_active', 'institution']
    search_fields = ['name', 'model_name']
    readonly_fields = ['created_at', 'updated_at']
