"""Django admin registration for the benchmark.

This is the *minimum* admin surface — list views with key columns + read-only
detail. The richer annotation UI (Phase 2.2) will be a custom dashboard view,
not the auto-generated admin.
"""
from django.contrib import admin

from apps.benchmark.models import (
    BenchmarkAnnotation,
    BenchmarkItem,
    BenchmarkRun,
)


@admin.register(BenchmarkItem)
class BenchmarkItemAdmin(admin.ModelAdmin):
    list_display = ('item_id', 'subject', 'lesson_id', 'stratum',
                    'annotation_count', 'created_at')
    list_filter = ('subject', 'stratum', 'created_at')
    search_fields = ('item_id',)
    readonly_fields = ('item_id', 'source_turn', 'snapshot',
                       'created_at', 'created_by')
    ordering = ('-created_at',)

    @admin.display(description='# annotations')
    def annotation_count(self, obj):
        return obj.annotations.count()


@admin.register(BenchmarkAnnotation)
class BenchmarkAnnotationAdmin(admin.ModelAdmin):
    list_display = ('item', 'system_variant', 'annotator_role',
                    'passes_display', 'failure_categories', 'updated_at')
    list_filter = ('system_variant', 'annotator_role', 'safety_concern')
    search_fields = ('item__item_id', 'rationale')
    readonly_fields = ('created_at', 'updated_at',
                       'missing_labels_display', 'extra_labels_display',
                       'passes_display')

    fieldsets = (
        ('Item & variant', {
            'fields': ('item', 'system_variant',
                       'annotator_role', 'annotator_user', 'annotator_model'),
        }),
        ('Annotation', {
            'fields': ('student_claim_correct', 'actual_labels',
                       'expected_labels', 'safety_concern', 'rationale',
                       'failure_categories'),
        }),
        ('Computed verdict', {
            'fields': ('missing_labels_display', 'extra_labels_display',
                       'passes_display'),
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(boolean=True, description='Passes')
    def passes_display(self, obj):
        return obj.passes

    @admin.display(description='Missing labels')
    def missing_labels_display(self, obj):
        return ', '.join(obj.missing_labels) or '—'

    @admin.display(description='Extra labels')
    def extra_labels_display(self, obj):
        return ', '.join(obj.extra_labels) or '—'


@admin.register(BenchmarkRun)
class BenchmarkRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'system_variant', 'annotator_role',
                    'passed', 'total_items', 'pass_rate_display',
                    'created_at')
    list_filter = ('system_variant', 'annotator_role', 'created_at')
    readonly_fields = ('created_at', 'metrics')
    ordering = ('-created_at',)

    @admin.display(description='Pass rate')
    def pass_rate_display(self, obj):
        return f"{obj.pass_rate:.3f}"
