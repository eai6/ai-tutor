"""
Accounts app - Institution and Membership models.

Multi-tenancy pattern: Every record in the system is tied to an Institution.
Users can belong to multiple institutions with different roles.
"""

from django.conf import settings as django_settings
from django.db import models
from django.contrib.auth.models import User


class Institution(models.Model):
    """
    Represents a school. Each Institution record is a distinct school
    managed by the platform. Staff and students belong to specific schools.
    """
    class SessionMode(models.TextChoices):
        SHARED_DEVICE = 'shared_device', 'Shared device (paired sessions)'
        INDIVIDUAL = 'individual', 'Individual accounts & devices'

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, help_text="URL-friendly identifier")
    timezone = models.CharField(max_length=50, default='UTC')
    is_active = models.BooleanField(default=True)
    # Default UI language for students/staff at this school. The
    # LocaleResolverMiddleware uses this when an authenticated user
    # has no `StudentProfile.preferred_locale` override AND is not
    # currently in a Course-scoped view (in which case `Course.locale`
    # wins). See memory/multi_locale_architecture_research.md.
    default_locale = models.CharField(
        max_length=10,
        default='en-us',
        choices=django_settings.LANGUAGES,
        help_text=(
            "Default UI language for users of this institution. "
            "Falls back to settings.LANGUAGE_CODE when unset."
        ),
    )
    session_mode = models.CharField(
        max_length=20,
        choices=SessionMode.choices,
        default=SessionMode.SHARED_DEVICE,
        help_text=(
            "How students take lessons in this school. "
            "'shared_device' lets paired students share a device — "
            "students can only add a pre-approved groupmate to their "
            "session. 'individual' hides the add-student button entirely."
        ),
    )

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

    # Set to True when an admin resets the user's password. Cleared
    # the moment the user successfully changes it. The login flow
    # checks this flag and redirects to a "set new password" screen
    # if any of the user's memberships have it true. Per-membership
    # rather than per-user because admin reset is an institution-
    # scoped action — but in practice we set it on every membership
    # the target user has, so any session of theirs forces the
    # change.
    password_reset_required = models.BooleanField(default=False)

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


class StudentGroup(models.Model):
    """A teacher-formed pair (or small group) of students who share a
    device for paired tutoring sessions.

    Pilot constraint (Seychelles 2026-05-11): groups are formed by the
    teacher (typically from baseline-assessment scores) and frozen for
    the pilot. Students can only add a groupmate to their session — no
    arbitrary username/password adds. See
    `memory/pilot_launch_execution.md`.

    A student should belong to at most one *active* group at a time.
    Enforcement happens in the teacher dashboard (adding a student to a
    new group removes them from any prior active group).
    """
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='student_groups',
    )
    name = models.CharField(
        max_length=100,
        help_text="Teacher-readable label, e.g., 'Pair A' or 'Group 1'.",
    )
    students = models.ManyToManyField(
        User,
        related_name='student_groups',
        blank=True,
        help_text="Students in this group/pair.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="When False, the group is archived and ignored by the session-add gate.",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_student_groups',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['institution', 'name']
        unique_together = [['institution', 'name']]

    def __str__(self):
        return f"{self.name} ({self.institution.name})"

    @property
    def member_count(self):
        return self.students.count()

    @classmethod
    def get_active_group_for(cls, user):
        """Return the user's active group (or None)."""
        return cls.objects.filter(students=user, is_active=True).first()

    @classmethod
    def groupmates_of(cls, user):
        """Return a queryset of users who share an active group with `user`,
        excluding `user` themself. Used to populate the 'add a groupmate'
        picker in the chat UI."""
        group = cls.get_active_group_for(user)
        if not group:
            return User.objects.none()
        return group.students.exclude(pk=user.pk)


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
    student_id = models.CharField(
        max_length=50,
        blank=True,
        default='',
        help_text="Unique student ID from school management system (e.g., STU-2024-001)"
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
    # Optional per-student UI language override. When set, takes
    # precedence over Institution.default_locale for all non-course
    # views (catalog, dashboard, accounts pages). Inside a Course
    # chat session, the course's locale still wins regardless.
    # Nullable + blank because most students never set it — Django
    # admin renders a blank "select language" entry that means
    # "use my school's default".
    preferred_locale = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        choices=django_settings.LANGUAGES,
        help_text=(
            "Student's preferred UI language. Blank = follow the "
            "school's default. Course-scoped views (chat tutor) "
            "always render in the course's locale, regardless of "
            "this preference."
        ),
    )

    # Safety suspension — student locked out of tutor until teacher reviews
    is_tutor_suspended = models.BooleanField(
        default=False,
        help_text="Locked out of AI tutor due to safety violations. Teacher must review and re-approve."
    )
    tutor_suspended_at = models.DateTimeField(null=True, blank=True)
    tutor_suspended_reason = models.TextField(
        blank=True,
        help_text="Reason for suspension (auto-filled from safety flags)"
    )

    # Cross-course skills snapshot (denormalized from competency tracker
    # so the tutor and recommendation engine can read without an
    # aggregator query). Shape:
    #   {
    #     "<course_id>": {
    #         "<teaching_objective>": {"pct": 65.0, "level": "developing",
    #                                    "source": "baseline", "attempts": 1},
    #         ...
    #     },
    #     ...
    #   }
    # Updated on every summative submit + every per-lesson exit-ticket
    # attempt that has a result blob.
    skills_snapshot = models.JSONField(default=dict, blank=True)
    skills_snapshot_updated_at = models.DateTimeField(null=True, blank=True)

    # Platform terms acceptance — version of `PlatformTerms` the student
    # most recently accepted. When `PlatformTerms.active().version` is
    # higher than this, the next login routes through `/terms/accept/`.
    terms_accepted_version = models.PositiveIntegerField(default=0)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

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

    # Competency category thresholds (configurable by super admin)
    threshold_be_max = models.IntegerField(
        default=50,
        help_text="Below Expectation (BE): 0% to this value (exclusive). Default: 50%"
    )
    threshold_ae_max = models.IntegerField(
        default=80,
        help_text="Approaching Expectation (AE): BE threshold to this value (exclusive). Default: 80%"
    )
    threshold_me_min = models.IntegerField(
        default=80,
        help_text="Meeting Expectation (ME): this value to 100% (exclusive). Default: 80%"
    )
    threshold_ee_time_minutes = models.IntegerField(
        default=5,
        help_text="Exceeding Expectation (EE): 100% objectives + exit ticket completed under this many minutes. Default: 5"
    )
    threshold_move_on = models.IntegerField(
        default=70,
        help_text="Recommend teacher to move on when ALL students achieve at least this % of objectives. Default: 70%"
    )

    # Role-based access. Three independent gates so the pilot can
    # let teachers edit + publish lesson content while still keeping
    # destructive curriculum operations (upload, course regen) on
    # super-admin only.
    teachers_can_edit_content = models.BooleanField(
        default=False,
        help_text="If False, teachers can only view content and reports — they cannot edit, publish, or delete lesson content. Super admins always have full access."
    )
    teachers_can_upload_curriculum = models.BooleanField(
        default=False,
        help_text="If False, teachers cannot upload new curriculum (PDF/DOCX → course generation). Super admin only by default — uploads are destructive and rebuild the whole course."
    )
    teachers_can_regenerate_courses = models.BooleanField(
        default=False,
        help_text="If False, teachers cannot trigger course-level regenerate (rebuilds every lesson + exit ticket in the course). Per-lesson regenerate is gated separately by teachers_can_edit_content."
    )

    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1  # Enforce singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def categorize_student(self, pct_achieved: float, exit_time_minutes: float = None) -> dict:
        """Categorize a student based on % of enabling objectives achieved.

        Returns dict with 'code', 'label', 'color', 'description'.
        """
        if pct_achieved >= 100 and exit_time_minutes is not None and exit_time_minutes <= self.threshold_ee_time_minutes:
            return {'code': 'EE', 'label': 'Exceeding Expectation', 'color': '#6366f1', 'description': 'All objectives met, completed quickly'}
        if pct_achieved >= self.threshold_me_min:
            return {'code': 'ME', 'label': 'Meeting Expectation', 'color': '#059669', 'description': 'Most objectives achieved'}
        if pct_achieved >= self.threshold_be_max:
            return {'code': 'AE', 'label': 'Approaching Expectation', 'color': '#d97706', 'description': 'Making progress, some objectives remain'}
        return {'code': 'BE', 'label': 'Below Expectation', 'color': '#dc2626', 'description': 'Significant gaps in understanding'}

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


class PlatformTerms(models.Model):
    """Versioned platform terms / consent text shown at sign-up and on
    re-acceptance when a new version is published.

    Exactly one row should have `is_active=True` at a time. Existing
    users whose `terms_accepted_version` is below the active version
    are routed through `/terms/accept/` on next login.
    """
    version = models.PositiveIntegerField(unique=True)
    title = models.CharField(max_length=200, default="AI Tutor — Terms")
    summary = models.CharField(
        max_length=300, blank=True,
        help_text="One-line summary shown beside the checkbox at sign-up.",
    )
    body = models.TextField(help_text="Full terms text (Markdown / plain HTML).")
    is_active = models.BooleanField(default=False)
    effective_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-version']
        verbose_name = "Platform terms"
        verbose_name_plural = "Platform terms"

    def __str__(self):
        return f"v{self.version} {'(active)' if self.is_active else ''}"

    @classmethod
    def active(cls):
        """Return the currently active terms row, or None."""
        return cls.objects.filter(is_active=True).order_by('-version').first()

    @classmethod
    def active_version(cls) -> int:
        active = cls.active()
        return active.version if active else 0


# ============================================================================
# Email verification (role-agnostic — works for students and staff)
# ============================================================================

class UserEmailStatus(models.Model):
    """Tracks email verification + digest opt-out for any user (student
    or staff). One row per User, auto-created on demand via the
    `for_user` classmethod.

    Soft gate: an unverified user can still log in and use the site;
    they just see a banner asking them to verify and won't receive
    the weekly digest.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='email_status',
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    # Weekly digest opt-out (CAN-SPAM / GDPR). Default opted-IN.
    opt_out = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User email status"
        verbose_name_plural = "User email statuses"

    def __str__(self):
        suffix = 'verified' if self.verified_at else 'unverified'
        return f"{self.user.username} — {suffix}"

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    @classmethod
    def for_user(cls, user):
        """Idempotent get-or-create. Use everywhere instead of direct .get()."""
        obj, _ = cls.objects.get_or_create(user=user)
        return obj


class EmailVerificationToken(models.Model):
    """One-time token sent to verify a user's email at sign-up (or on
    resend). Tokens expire after 7 days; consumed tokens have
    `used_at` set so they can't be replayed.
    """
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='email_verification_tokens',
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        state = 'used' if self.used_at else ('expired' if self.is_expired else 'live')
        return f"{self.user.username} ({state})"

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone as _tz
        return self.expires_at < _tz.now()

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and not self.is_expired

    @classmethod
    def issue(cls, user, ttl_days: int = 7):
        """Mint a fresh token for `user`. Invalidates any prior live
        tokens (so resend always supersedes). Returns the new token row.
        """
        import secrets
        from datetime import timedelta
        from django.utils import timezone as _tz
        cls.objects.filter(user=user, used_at__isnull=True).update(used_at=_tz.now())
        return cls.objects.create(
            user=user,
            token=secrets.token_urlsafe(32),
            expires_at=_tz.now() + timedelta(days=ttl_days),
        )

    def consume(self):
        """Mark this token as used + flip the user's email_status to
        verified. Idempotent — re-consuming is a no-op."""
        from django.utils import timezone as _tz
        if self.used_at is not None:
            return
        self.used_at = _tz.now()
        self.save(update_fields=['used_at'])
        status = UserEmailStatus.for_user(self.user)
        if status.verified_at is None:
            status.verified_at = _tz.now()
            status.save(update_fields=['verified_at'])
