from django.contrib import admin
from .models import Institution, Membership, StudentProfile, TutorPersonality, PlatformTerms


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ['user', 'institution', 'role', 'is_active', 'joined_at']
    list_filter = ['role', 'is_active', 'institution']
    search_fields = ['user__username', 'user__email']
    raw_id_fields = ['user']


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'school', 'grade_level', 'tutor_personality', 'created_at']
    list_filter = ['school', 'grade_level', 'tutor_personality']
    search_fields = ['user__username', 'user__first_name', 'user__last_name']
    raw_id_fields = ['user']


@admin.register(TutorPersonality)
class TutorPersonalityAdmin(admin.ModelAdmin):
    list_display = ['emoji', 'name', 'description', 'is_active', 'sort_order']
    list_filter = ['is_active']
    list_editable = ['is_active', 'sort_order']
    search_fields = ['name']


@admin.register(PlatformTerms)
class PlatformTermsAdmin(admin.ModelAdmin):
    list_display = ['version', 'title', 'is_active', 'effective_date', 'created_at']
    list_filter = ['is_active']
    list_editable = ['is_active']
    readonly_fields = ['created_at']
    fieldsets = (
        (None, {'fields': ('version', 'title', 'summary', 'is_active', 'effective_date')}),
        ('Content', {'fields': ('body',), 'description': 'Markdown / plain text.'}),
        ('Audit', {'fields': ('created_at',)}),
    )
