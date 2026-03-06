"""
Accounts app - Institution and Membership models.

Multi-tenancy pattern: Every record in the system is tied to an Institution.
Users can belong to multiple institutions with different roles.
"""

from django.db import models
from django.contrib.auth.models import User


class Institution(models.Model):
    """
    Represents a school. Each Institution record is a distinct school
    managed by the platform. Staff and students belong to specific schools.
    """
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, help_text="URL-friendly identifier")
    timezone = models.CharField(max_length=50, default='UTC')
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    GLOBAL_SLUG = 'global'

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']

    @classmethod
    def get_global(cls):
        """Get or create the Global institution for platform-wide content.

        Used as a fallback when content is uploaded in "All Schools" mode
        (institution=None) but downstream operations (media saving, skill
        extraction) require a non-null institution FK.
        """
        institution, _ = cls.objects.get_or_create(
            slug=cls.GLOBAL_SLUG,
            defaults={'name': 'Global (All Schools)', 'is_active': True},
        )
        return institution


class Membership(models.Model):
    """
    Links users to institutions with a specific role.
    A user can have different roles in different institutions.
    """
    class Role(models.TextChoices):
        STAFF = 'staff', 'Staff (Teacher/Admin)'
        STUDENT = 'student', 'Student'

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.STUDENT
    )
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # A user can only have one role per institution
        unique_together = ['user', 'institution']
        ordering = ['institution', 'user']

    def __str__(self):
        return f"{self.user.username} - {self.institution.name} ({self.role})"

    @property
    def is_staff(self):
        """Returns True if user has staff role."""
        return self.role == self.Role.STAFF


class TutorPersonality(models.Model):
    """Configurable tutor personality that modifies the AI tutor's tone."""
    name = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=200, blank=True)
    emoji = models.CharField(max_length=10, default='')
    system_prompt_modifier = models.TextField()
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.emoji} {self.name}" if self.emoji else self.name


class StudentProfile(models.Model):
    """
    Extended profile for students with school and grade information.
    Used for personalization and progress tracking.
    """
    class GradeLevel(models.TextChoices):
        S1 = 'S1', 'Secondary 1'
        S2 = 'S2', 'Secondary 2'
        S3 = 'S3', 'Secondary 3'
        S4 = 'S4', 'Secondary 4'
        S5 = 'S5', 'Secondary 5'

    # Fallback defaults (used when PlatformConfig has no entries)
    DEFAULT_SCHOOL_CHOICES = [
        ('anse_boileau', 'Anse Boileau Secondary'),
        ('anse_royale', 'Anse Royale Secondary'),
        ('belonie', 'Belonie Secondary'),
        ('beau_vallon', 'Beau Vallon Secondary'),
        ('english_river', 'English River Secondary'),
        ('la_digue', 'La Digue Secondary'),
        ('mont_fleuri', 'Mont Fleuri Secondary'),
        ('perseverance', 'Perseverance Secondary'),
        ('pointe_larue', 'Pointe Larue Secondary'),
        ('plaisance', 'Plaisance Secondary'),
        ('praslin', 'Praslin Secondary'),
        ('other', 'Other'),
    ]
    SCHOOL_CHOICES = DEFAULT_SCHOOL_CHOICES  # backward compat alias

    DEFAULT_GRADE_CHOICES = list(GradeLevel.choices)
    
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='student_profile'
    )
    school = models.CharField(
        max_length=50,
        choices=SCHOOL_CHOICES,
        blank=True,
        help_text="Student's school"
    )
    grade_level = models.CharField(
        max_length=5,
        choices=GradeLevel.choices,
        blank=True,
        help_text="Current grade level"
    )
    tutor_personality = models.ForeignKey(
        'TutorPersonality', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='students',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.get_school_display()} ({self.grade_level})"
    
    def get_school_display_name(self):
        """Return the full school name."""
        for code, name in PlatformConfig.get_school_choices():
            if code == self.school:
                return name
        return self.school


class PlatformConfig(models.Model):
    """
    Singleton model for platform-wide configuration (branding, grades, etc.).
    Superadmins can edit these via the Settings page.
    """
    platform_name = models.CharField(max_length=255, default='AI Tutor')

    # Branding (platform-wide)
    logo = models.ImageField(upload_to='platform_logos/', blank=True, null=True)
    primary_color = models.CharField(max_length=7, default='#E8590C')
    secondary_color = models.CharField(max_length=7, default='#4ECDC4')
    accent_color = models.CharField(max_length=7, default='#FFE66D')

    schools = models.JSONField(default=list)   # [{"code": "...", "name": "..."}]
    grades = models.JSONField(default=list)    # [{"code": "...", "name": "..."}]
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1  # Enforce singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def get_school_choices(cls):
        """Return school choices. Prefers Institution records, then JSON config, then defaults."""
        schools = Institution.objects.filter(is_active=True).exclude(slug=Institution.GLOBAL_SLUG).order_by('name')
        if schools.exists():
            return [(str(inst.id), inst.name) for inst in schools]
        obj = cls.load()
        if obj.schools:
            return [(s['code'], s['name']) for s in obj.schools]
        return StudentProfile.DEFAULT_SCHOOL_CHOICES

    @classmethod
    def get_grade_choices(cls):
        obj = cls.load()
        if obj.grades:
            return [(g['code'], g['name']) for g in obj.grades]
        return StudentProfile.DEFAULT_GRADE_CHOICES

    class Meta:
        verbose_name = 'Platform Configuration'

    def __str__(self):
        return 'Platform Configuration'


class StaffInvitation(models.Model):
    """
    Invitation for staff (teachers/admins) to join an institution.
    Staff cannot self-register - they must be invited.
    """
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='invitations'
    )
    email = models.EmailField(blank=True, default='')
    role = models.CharField(
        max_length=20,
        choices=Membership.Role.choices,
        default=Membership.Role.STAFF
    )
    token = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sent_invitations'
    )
    
    is_used = models.BooleanField(default=False)
    registered_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invitation'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.email} - {self.role} @ {self.institution.name}"
    
    def get_role_display(self):
        """Return human-readable role name."""
        return dict(Membership.Role.choices).get(self.role, self.role)
