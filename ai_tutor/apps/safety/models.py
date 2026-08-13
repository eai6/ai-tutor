"""
Safety app models.
"""

from django.db import models


class SafetyAuditLog(models.Model):
    """
    Audit log for safety-related events.
    
    Tracks:
    - Content flags
    - Rate limit hits
    - Age verification
    - Data access
    """
    
    class EventType(models.TextChoices):
        CONTENT_FLAGGED = 'content_flagged', 'Content Flagged'
        RATE_LIMITED = 'rate_limited', 'Rate Limited'
        AGE_CHECK = 'age_check', 'Age Verification'
        DATA_EXPORT = 'data_export', 'Data Export'
        DATA_DELETE = 'data_delete', 'Data Deletion'
        LOGIN_ATTEMPT = 'login_attempt', 'Login Attempt'
        CONSENT_GIVEN = 'consent_given', 'Consent Given'
        CONSENT_WITHDRAWN = 'consent_withdrawn', 'Consent Withdrawn'
        DATA_CLEANUP = 'data_cleanup', 'Data Cleanup'
    
    timestamp = models.DateTimeField(auto_now_add=True)
    event_type = models.CharField(max_length=30, choices=EventType.choices)
    user_id = models.IntegerField(null=True, blank=True)
    user_hash = models.CharField(max_length=16, blank=True)
    session_id = models.IntegerField(null=True, blank=True)
    
    details = models.JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    
    severity = models.CharField(
        max_length=10,
        choices=[
            ('info', 'Info'),
            ('warning', 'Warning'),
            ('critical', 'Critical'),
        ],
        default='info'
    )
    
    class Meta:
        app_label = 'safety'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['event_type', 'timestamp']),
            models.Index(fields=['user_hash', 'timestamp']),
            models.Index(fields=['severity', 'timestamp']),
        ]
    
    def __str__(self):
        return f"{self.event_type} - {self.timestamp}"


class ConsentRecord(models.Model):
    """
    Track user consent for GDPR compliance.
    """
    
    class ConsentType(models.TextChoices):
        DATA_PROCESSING = 'data_processing', 'Data Processing'
        AI_TUTORING = 'ai_tutoring', 'AI Tutoring'
        ANALYTICS = 'analytics', 'Analytics'
        PARENTAL = 'parental', 'Parental Consent'
    
    user = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='consent_records'
    )
    consent_type = models.CharField(max_length=20, choices=ConsentType.choices)
    given = models.BooleanField(default=False)
    given_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    
    # For parental consent
    parent_email = models.EmailField(blank=True)
    parent_name = models.CharField(max_length=100, blank=True)
    
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        app_label = 'safety'
        unique_together = ['user', 'consent_type']
    
    def __str__(self):
        status = "given" if self.given else "not given"
        return f"{self.user.username} - {self.consent_type}: {status}"
