from django.contrib import admin
from .models import CurriculumUpload, TeacherClass, TeachingMaterialUpload


@admin.register(CurriculumUpload)
class CurriculumUploadAdmin(admin.ModelAdmin):
    list_display = ['subject_name', 'institution', 'status', 'lessons_created', 'created_at']
    list_filter = ['status', 'institution', 'created_at']
    search_fields = ['subject_name']
    readonly_fields = ['processing_log', 'created_at', 'updated_at', 'completed_at']


@admin.register(TeachingMaterialUpload)
class TeachingMaterialUploadAdmin(admin.ModelAdmin):
    list_display = ['title', 'subject_name', 'material_type', 'institution', 'status', 'chunks_created', 'created_at']
    list_filter = ['status', 'material_type', 'institution', 'created_at']
    search_fields = ['title', 'subject_name']
    readonly_fields = ['processing_log', 'created_at', 'updated_at', 'completed_at']


@admin.register(TeacherClass)
class TeacherClassAdmin(admin.ModelAdmin):
    list_display = ['name', 'grade_level', 'teacher', 'institution', 'is_active']
    list_filter = ['grade_level', 'institution', 'is_active']
    search_fields = ['name']
    filter_horizontal = ['students', 'courses']
