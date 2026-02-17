"""
Safety admin interface.
"""

from django.contrib import admin
from .models import SafetyAuditLog, ConsentRecord


@admin.register(SafetyAuditLog)
class SafetyAuditLogAdmin(admin.ModelAdmin):
    list_display = ['timestamp', 'event_type', 'severity', 'user_hash', 'ip_address']
    list_filter = ['event_type', 'severity', 'timestamp']
    search_fields = ['user_hash', 'ip_address', 'details']
    readonly_fields = ['timestamp', 'event_type', 'user_id', 'user_hash', 
                       'session_id', 'details', 'ip_address', 'user_agent', 'severity']
    ordering = ['-timestamp']
    
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ConsentRecord)
class ConsentRecordAdmin(admin.ModelAdmin):
    list_display = ['user', 'consent_type', 'given', 'given_at', 'withdrawn_at']
    list_filter = ['consent_type', 'given']
    search_fields = ['user__username', 'user__email', 'parent_email']
    readonly_fields = ['created_at', 'updated_at']
