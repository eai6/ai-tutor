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
        related_name='curriculum_uploads',
        null=True,
        blank=True,
        help_text="Null = platform-wide curriculum upload"
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
    grade_level = models.CharField(max_length=20, blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    error_message = models.TextField(blank=True)
    is_cancelled = models.BooleanField(default=False)

    # Processing state
    current_step = models.IntegerField(default=0)  # Track which step we're on
    parsed_data = models.JSONField(null=True, blank=True)  # Store parsed curriculum for review
    extracted_text_length = models.IntegerField(default=0)
    
    # Results
    created_course = models.ForeignKey(
        'curriculum.Course',
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    units_created = models.IntegerField(default=0)
    lessons_created = models.IntegerField(default=0)
    steps_created = models.IntegerField(default=0)  # Lesson steps generated
    
    # Lesson configuration
    lesson_duration_minutes = models.PositiveIntegerField(
        default=20,
        help_text="Target lesson duration in minutes (controls steps and EOs per lesson)"
    )

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


class TeachingMaterialUpload(models.Model):
    """Track teaching material uploads (textbooks, references, worksheets)."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    class MaterialType(models.TextChoices):
        TEXTBOOK = 'textbook', 'Textbook'
        REFERENCE = 'reference', 'Reference'
        WORKSHEET = 'worksheet', 'Worksheet'
        NOTES = 'notes', 'Notes'
        QUESTION_BANK = 'question_bank', 'Question Bank / Exam Paper'
        OTHER = 'other', 'Other'

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='teaching_material_uploads',
        null=True,
        blank=True,
        help_text="Null = platform-wide teaching material"
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='teaching_material_uploads'
    )
    course = models.ForeignKey(
        'curriculum.Course',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='teaching_materials'
    )
    curriculum_upload = models.ForeignKey(
        'CurriculumUpload',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='teaching_materials'
    )

    file_path = models.CharField(max_length=500)
    original_filename = models.CharField(max_length=255)
    title = models.CharField(max_length=255)
    subject_name = models.CharField(max_length=100)
    grade_level = models.CharField(max_length=20, blank=True)
    material_type = models.CharField(
        max_length=20,
        choices=MaterialType.choices,
        default=MaterialType.TEXTBOOK
    )
    description = models.TextField(blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    error_message = models.TextField(blank=True)
    extracted_text_length = models.IntegerField(default=0)
    chunks_created = models.IntegerField(default=0)
    figures_extracted = models.IntegerField(default=0, db_default=0)
    processing_log = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.material_type}) - {self.status}"

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
    grade_level = models.CharField(max_length=20, blank=True)
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

class FeedbackReport(models.Model):
    """In-app bug report / feedback submitted via the floating help
    button. Visible to superadmins on `/dashboard/feedback/`. See
    `memory/pilot_launch_execution.md` (Task 2)."""

    class Kind(models.TextChoices):
        BUG = 'bug', 'Bug'
        FEEDBACK = 'feedback', 'Feedback'
        IDEA = 'idea', 'Idea / suggestion'

    class Severity(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High — pilot blocker'

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='feedback_reports',
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='feedback_reports',
    )
    kind = models.CharField(max_length=15, choices=Kind.choices, default=Kind.BUG)
    severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.MEDIUM,
    )
    message = models.TextField(help_text="What happened / what's wrong / what would help.")
    page_url = models.CharField(max_length=500, blank=True, default='')
    user_agent = models.CharField(max_length=500, blank=True, default='')
    # Optional screen capture attached at submit time. The picker on
    # the feedback widget defaults to checked but the student can opt
    # out — we never capture without the explicit click that triggers
    # browser permission. See _includes/feedback_button.html.
    screenshot = models.ImageField(
        upload_to='feedback_screenshots/',
        null=True, blank=True,
        help_text="Optional real-pixel screenshot attached by the user when submitting.",
    )

    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='resolved_feedback_reports',
    )
    resolution_notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        who = self.user.username if self.user else '(anon)'
        return f"[{self.kind}] {who}: {self.message[:60]}"


# ============================================================================
# Weekly lesson assignment (Phase 2 of email/assignment work)
# ============================================================================
#
# A teacher assigns one or more lessons in a course to "this week" and
# students see them in a "This week's lessons" panel + receive them in
# the Monday digest email. Per pilot decision a Course = a class
# (e.g. S3 Math), so the assignment is keyed (course, week_start)
# and applies to every student in that course's institution+grade.

class WeeklyAssignment(models.Model):
    """One row per (course, week). Teachers attach lessons + an
    optional note; students see this in their weekly panel and
    Monday digest."""

    course = models.ForeignKey(
        'curriculum.Course',
        on_delete=models.CASCADE,
        related_name='weekly_assignments',
    )
    # Monday of the assignment week (date, no time). Always normalize
    # to ISO week's Monday on save so duplicate weeks can't be
    # created via off-by-one dates.
    week_start = models.DateField(
        help_text="Monday of the week this assignment covers.",
    )
    lessons = models.ManyToManyField(
        'curriculum.Lesson',
        related_name='weekly_assignments',
        blank=True,
        help_text="Lessons due this week.",
    )
    notes = models.TextField(
        blank=True, default='',
        help_text="Optional teacher message — shown to students with the assignment.",
    )
    assigned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='weekly_assignments_made',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-week_start']
        unique_together = [['course', 'week_start']]
        verbose_name = "Weekly assignment"
        verbose_name_plural = "Weekly assignments"

    def __str__(self):
        return f"{self.course.title} — week of {self.week_start.isoformat()}"

    @staticmethod
    def normalize_to_monday(d):
        """Snap any date to the Monday of its ISO week."""
        from datetime import timedelta
        return d - timedelta(days=d.weekday())

    def save(self, *args, **kwargs):
        # Normalize week_start so two teachers can't accidentally
        # create overlapping assignments by picking different days.
        if self.week_start:
            self.week_start = self.normalize_to_monday(self.week_start)
        super().save(*args, **kwargs)

    @property
    def week_end(self):
        """Sunday of the same week (inclusive)."""
        from datetime import timedelta
        return self.week_start + timedelta(days=6)

    @classmethod
    def current_week_start(cls):
        """Monday of THIS week (in server-local date — Seychelles UTC+4
        is close enough that the date boundary lines up)."""
        from django.utils import timezone as _tz
        return cls.normalize_to_monday(_tz.localdate())

    @classmethod
    def for_student_this_week(cls, student):
        """Return WeeklyAssignment rows assigned to courses the student
        has access to, where week_start == this week's Monday.

        Course-access rule mirrors the existing tutoring/views catalog:
        institution match (with platform-wide fallback) + grade match.
        """
        from apps.accounts.models import Membership, StudentProfile
        from django.db.models import Q

        membership = Membership.objects.filter(
            user=student, is_active=True
        ).first()
        if not membership:
            return cls.objects.none()
        institution = membership.institution
        profile = StudentProfile.objects.filter(user=student).first()
        grade = profile.grade_level if profile else ''

        course_filter = Q(course__institution=institution) | Q(course__institution__isnull=True)
        if grade:
            course_filter &= Q(course__grade_level=grade) | Q(course__grade_level='')
        return cls.objects.filter(
            course_filter,
            week_start=cls.current_week_start(),
        ).select_related('course').prefetch_related('lessons')
