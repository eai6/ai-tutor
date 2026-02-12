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
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ModelConfig)
class ModelConfigAdmin(admin.ModelAdmin):
    list_display = ['name', 'institution', 'provider', 'model_name', 'is_active']
    list_filter = ['provider', 'is_active', 'institution']
    search_fields = ['name', 'model_name']
    readonly_fields = ['created_at', 'updated_at']
