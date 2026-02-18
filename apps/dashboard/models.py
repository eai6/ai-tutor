"""
Dashboard Models - For tracking curriculum uploads and processing.
"""

from django.db import models
from django.contrib.auth.models import User
from apps.accounts.models import Institution


class CurriculumUpload(models.Model):
    """Track curriculum document uploads for auto-generation."""
    
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        REVIEW = 'review', 'Review'  # Waiting for teacher approval
        MEDIA_PROCESSING = 'media_processing', 'Media Processing'  # Generating images
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
    
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='curriculum_uploads'
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='curriculum_uploads'
    )
    
    file_path = models.CharField(max_length=500)
    original_filename = models.CharField(max_length=255, blank=True)
    subject_name = models.CharField(max_length=100)
    grade_level = models.CharField(max_length=10, blank=True)
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    error_message = models.TextField(blank=True)
    
    # Processing state
    current_step = models.IntegerField(default=0)  # Track which step we're on
    parsed_data = models.JSONField(null=True, blank=True)  # Store parsed curriculum for review
    extracted_text_length = models.IntegerField(default=0)
    
    # Results
    created_course = models.ForeignKey(
        'curriculum.Course',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    units_created = models.IntegerField(default=0)
    lessons_created = models.IntegerField(default=0)
    steps_created = models.IntegerField(default=0)  # Lesson steps generated
    
    # Processing log
    processing_log = models.TextField(blank=True)
    teacher_feedback = models.TextField(blank=True)  # Store teacher feedback
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.subject_name} - {self.status} ({self.created_at.date()})"
    
    def add_log(self, message):
        """Add a message to the processing log."""
        from django.utils import timezone
        timestamp = timezone.now().strftime('%H:%M:%S')
        self.processing_log += f"[{timestamp}] {message}\n"
        self.save(update_fields=['processing_log'])


class TeacherClass(models.Model):
    """Optional: Group students into classes for easier management."""
    
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='teacher_classes'
    )
    name = models.CharField(max_length=100)
    grade_level = models.CharField(max_length=10, blank=True)
    teacher = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='teaching_classes'
    )
    students = models.ManyToManyField(
        User,
        related_name='enrolled_classes',
        blank=True
    )
    
    # Assigned courses
    courses = models.ManyToManyField(
        'curriculum.Course',
        related_name='assigned_classes',
        blank=True
    )
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['grade_level', 'name']
        verbose_name_plural = 'Teacher Classes'
    
    def __str__(self):
        return f"{self.name} ({self.grade_level})"