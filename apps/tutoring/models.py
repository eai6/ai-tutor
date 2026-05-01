"""
Tutoring app - Session tracking, student progress, and exit tickets.

TutorSession: A single tutoring interaction (student + lesson)
SessionTurn: Each message in the conversation
StudentLessonProgress: Tracks mastery across sessions
ExitTicket: Standardized summative assessment (10 MCQs)
ExitTicketQuestion: Individual questions in an exit ticket
ExitTicketAttempt: Records student attempts
"""

from django.db import models
from django.contrib.auth.models import User
from apps.accounts.models import Institution
from apps.curriculum.models import Lesson, LessonStep, Course
from apps.llm.models import PromptPack, ModelConfig


class TutorSession(models.Model):
    """
    A single tutoring session - one student working through one lesson.
    """
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        COMPLETED = 'completed', 'Completed'
        ABANDONED = 'abandoned', 'Abandoned'

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='tutor_sessions'
    )
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='tutor_sessions'
    )
    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='sessions'
    )
    
    # Snapshot of which prompts/model were used
    prompt_pack = models.ForeignKey(
        PromptPack,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sessions'
    )
    model_config = models.ForeignKey(
        ModelConfig,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sessions'
    )
    
    # Session state
    current_step_index = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE
    )
    mastery_achieved = models.BooleanField(default=False)
    
    # Structured engine state (for phase tracking, etc.)
    engine_state = models.JSONField(default=dict, blank=True)
    
    # Timing
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    started_lesson_at = models.DateTimeField(null=True, blank=True, help_text="When the student started the lesson content")
    completed_lesson_at = models.DateTimeField(null=True, blank=True, help_text="When the student completed the lesson content")
    
    # Optional summary (generated at end)
    summary = models.TextField(blank=True)

    # Safety flagging
    is_flagged = models.BooleanField(default=False)
    flag_reason = models.TextField(blank=True)
    flagged_at = models.DateTimeField(null=True, blank=True)
    flag_reviewed = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_flags'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # Group session approval (H3 / memory/group_lessons_v2_plan.md)
    class GroupApprovalStatus(models.TextChoices):
        NOT_REQUIRED = 'not_required', 'Not Required'
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        DENIED = 'denied', 'Denied'

    group_approval_status = models.CharField(
        max_length=20,
        choices=GroupApprovalStatus.choices,
        default=GroupApprovalStatus.NOT_REQUIRED,
    )
    group_approval_decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='group_approvals',
    )
    group_approval_decided_at = models.DateTimeField(null=True, blank=True)

    @property
    def lesson_duration_minutes(self):
        """Time spent on the lesson in minutes."""
        if self.started_lesson_at and self.completed_lesson_at:
            delta = self.completed_lesson_at - self.started_lesson_at
            return round(delta.total_seconds() / 60, 1)
        if self.started_lesson_at and self.ended_at:
            delta = self.ended_at - self.started_lesson_at
            return round(delta.total_seconds() / 60, 1)
        return None

    @property
    def active_students(self):
        """Return the queryset of active participants in this session.

        Falls back to the primary-student single-user case for legacy
        sessions where SessionParticipant rows were never created (pre-G1).
        """
        qs = User.objects.filter(
            group_sessions__session=self,
            group_sessions__is_active=True,
        )
        if qs.exists():
            return qs
        return User.objects.filter(id=self.student_id)

    @property
    def is_group(self) -> bool:
        """True when this session has more than one active participant."""
        return SessionParticipant.objects.filter(
            session=self, is_active=True,
        ).count() > 1

    class Meta:
        ordering = ['-started_at']
        verbose_name = "Tutor Session"

    def __str__(self):
        return f"{self.student.username} - {self.lesson.title} ({self.status})"


class SessionParticipant(models.Model):
    """Links a student to a TutorSession for group-lesson support.

    Every TutorSession has at least one SessionParticipant (the primary
    student). Additional participants are added when students join a group
    session on the same device. All participants get credit: one
    StudentLessonProgress update and one ExitTicketAttempt row per
    participant when the session completes.

    See memory/group_lessons_plan.md.
    """

    session = models.ForeignKey(
        TutorSession,
        on_delete=models.CASCADE,
        related_name='participants',
    )
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='group_sessions',
    )
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_primary = models.BooleanField(
        default=False,
        help_text="True for the session 'owner' (the student who started the session).",
    )

    class Meta:
        unique_together = [('session', 'student')]
        indexes = [models.Index(fields=['session', 'is_active'])]

    def __str__(self):
        return f"{self.student.username} -> session {self.session_id} (active={self.is_active})"


class SessionTurn(models.Model):
    """
    A single message in the tutoring conversation.
    """
    class Role(models.TextChoices):
        SYSTEM = 'system', 'System'
        TUTOR = 'tutor', 'Tutor'
        STUDENT = 'student', 'Student'

    session = models.ForeignKey(
        TutorSession,
        on_delete=models.CASCADE,
        related_name='turns'
    )
    role = models.CharField(
        max_length=10,
        choices=Role.choices
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Link to the step this turn relates to (if applicable)
    step = models.ForeignKey(
        LessonStep,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='turns'
    )
    
    # Usage tracking (optional)
    tokens_in = models.PositiveIntegerField(null=True, blank=True)
    tokens_out = models.PositiveIntegerField(null=True, blank=True)
    
    # Flexible metadata (hints used, attempts, grading result, etc.)
    metadata = models.JSONField(default=dict, blank=True)

    # Mobile / offline provenance (memory/mobile_rn_plan.md). Set by the
    # /api/v1/sessions/<id>/sync/ endpoint when the client uploads turns
    # generated by an on-device LLM while the device was offline.
    generated_offline = models.BooleanField(default=False)
    offline_model_id = models.CharField(
        max_length=100, blank=True,
        help_text='MobileInferenceModel.id used to generate this turn (if offline).',
    )
    client_generated_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Wall-clock time on the client when the turn was authored. '
                  'Distinct from created_at (server-side ingest time).',
    )

    # Safety flagging
    is_flagged = models.BooleanField(default=False)
    flag_type = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = "Session Turn"

    def __str__(self):
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"[{self.role}] {preview}"


class StudentLessonProgress(models.Model):
    """
    Tracks a student's overall progress on a lesson across multiple sessions.
    """
    class MasteryLevel(models.TextChoices):
        NOT_STARTED = 'not_started', 'Not Started'
        IN_PROGRESS = 'in_progress', 'In Progress'
        MASTERED = 'mastered', 'Mastered'

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='student_progress'
    )
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='lesson_progress'
    )
    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='student_progress'
    )
    
    mastery_level = models.CharField(
        max_length=20,
        choices=MasteryLevel.choices,
        default=MasteryLevel.NOT_STARTED
    )
    # NOTE (C6): correct_streak / total_attempts / total_correct were removed
    # in 2026-04-25. They were dead fields never updated by any code path.
    # Use ExitTicketAttempt + StudentLessonProgress.attempts_count instead.
    best_score = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Best exit-ticket score as a fraction in [0.0, 1.0]. "
            "Populated on every exit ticket submission. Monotonic (never demoted)."
        ),
    )

    attempts_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of exit ticket attempts across all sessions for this lesson.",
    )
    last_attempt_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the student most recently submitted the exit ticket.",
    )
    last_completion_session = models.ForeignKey(
        'TutorSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='completed_progress_records',
        help_text="The session that earned mastery (or the most recent attempt session).",
    )
    last_completion_was_group = models.BooleanField(
        default=False,
        help_text=(
            "True when the last attempt session had multiple active "
            "participants (group lesson). Filterable for teacher reports."
        ),
    )

    last_session_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['student', 'lesson']
        ordering = ['-updated_at']
        verbose_name = "Student Lesson Progress"
        verbose_name_plural = "Student Lesson Progress"

    def __str__(self):
        return f"{self.student.username} - {self.lesson.title} ({self.mastery_level})"


# ============================================================================
# Exit Ticket Models (Standardized Summative Assessment)
# ============================================================================

class ExitTicket(models.Model):
    """
    A standardized assessment built from a question bank.

    Two flavours, controlled by `assessment_type`:
      - 'exit_ticket' (lesson-level): bank of ~30, 10 served per attempt.
      - 'summative'    (course-level): bank of ~90, 30 served per attempt,
        stratified to cover every TO + EO across the course.

    Exactly one of `lesson` or `course` is set. See
    memory/summative_assessments_plan.md.
    """
    class AssessmentType(models.TextChoices):
        EXIT_TICKET = 'exit_ticket', 'Exit Ticket (lesson-level)'
        SUMMATIVE = 'summative', 'Summative Exam (course-level)'

    assessment_type = models.CharField(
        max_length=20,
        choices=AssessmentType.choices,
        default=AssessmentType.EXIT_TICKET,
    )

    lesson = models.OneToOneField(
        Lesson,
        on_delete=models.CASCADE,
        related_name='exit_ticket',
        null=True, blank=True,
        help_text="Set for assessment_type='exit_ticket'.",
    )
    course = models.OneToOneField(
        Course,
        on_delete=models.CASCADE,
        related_name='summative_exam',
        null=True, blank=True,
        help_text="Set for assessment_type='summative'.",
    )

    passing_score = models.PositiveIntegerField(
        default=8,
        help_text="Minimum correct answers to pass."
    )

    time_limit_minutes = models.PositiveIntegerField(
        default=10,
        help_text="Time limit in minutes (0 = no limit)."
    )

    question_bank_size = models.PositiveIntegerField(
        default=30,
        help_text="Target size of the question bank. 30 for exit tickets, 90 for summatives.",
    )
    questions_per_attempt = models.PositiveIntegerField(
        default=10,
        help_text="How many questions a student sees per attempt. 10 for exit tickets, 30 for summatives.",
    )

    is_published = models.BooleanField(
        default=False,
        help_text="Summatives only — when False, students can't take the exam.",
    )

    instructions = models.TextField(
        default="Answer all questions. You need to pass the threshold to complete.",
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Exit Ticket / Summative"
        verbose_name_plural = "Exit Tickets & Summatives"
        constraints = [
            models.CheckConstraint(
                name='exitticket_lesson_xor_course',
                condition=(
                    models.Q(lesson__isnull=False, course__isnull=True)
                    | models.Q(lesson__isnull=True, course__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        if self.assessment_type == self.AssessmentType.SUMMATIVE and self.course_id:
            return f"Summative: {self.course.title}"
        if self.lesson_id:
            return f"Exit Ticket: {self.lesson.title}"
        return f"ExitTicket #{self.pk}"

    @property
    def question_count(self):
        return self.questions.count()

    @property
    def is_complete(self):
        """Check if the question bank is fully populated."""
        return self.question_count >= self.question_bank_size

    @property
    def is_summative(self):
        return self.assessment_type == self.AssessmentType.SUMMATIVE

    @property
    def parent_label(self):
        """Human-readable label for what this assessment is attached to."""
        if self.is_summative and self.course_id:
            return self.course.title
        if self.lesson_id:
            return self.lesson.title
        return "(unattached)"


class ExitTicketQuestion(models.Model):
    """
    A question in an exit ticket — supports multiple formats (MCQ, fill-in-blank,
    matching, short answer, data interpretation).
    """
    class Difficulty(models.TextChoices):
        EASY = 'easy', 'Easy (recall)'
        MEDIUM = 'medium', 'Medium (apply)'
        HARD = 'hard', 'Hard (analyze)'

    class QuestionType(models.TextChoices):
        MCQ = 'mcq', 'Multiple Choice'
        FILL_IN_BLANK = 'fill_in_blank', 'Fill in the Blank'
        MATCHING = 'matching', 'Matching'
        SHORT_ANSWER = 'short_answer', 'Short Answer'
        DATA_INTERPRETATION = 'data_interpretation', 'Data Interpretation'

    exit_ticket = models.ForeignKey(
        ExitTicket,
        on_delete=models.CASCADE,
        related_name='questions'
    )

    question_type = models.CharField(
        max_length=25,
        choices=QuestionType.choices,
        default=QuestionType.MCQ,
        help_text="Question format type"
    )

    question_text = models.TextField(help_text="The question stem")

    # MCQ fields (kept for backward compatibility)
    option_a = models.CharField(max_length=500, blank=True, default='')
    option_b = models.CharField(max_length=500, blank=True, default='')
    option_c = models.CharField(max_length=500, blank=True, default='')
    option_d = models.CharField(max_length=500, blank=True, default='')

    correct_answer = models.CharField(
        max_length=1,
        choices=[('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')],
        blank=True,
        default='',
        help_text="The correct option for MCQ (A, B, C, or D)"
    )

    # Non-MCQ answer data
    answer_data = models.JSONField(
        null=True,
        blank=True,
        help_text="""Type-specific answer data:
        fill_in_blank: {"text_template": "The ___ is...", "blanks": ["GNP"], "accept_alternatives": [["gross national product"]]}
        matching: {"pairs": [{"left": "A", "right": "1"}], "distractor_rights": ["X"]}
        short_answer: {"model_answer": "...", "keywords": ["k1", "k2"], "min_keywords": 2}
        data_interpretation: {"data_description": "...", "model_answer": "...", "keywords": [...]}
        """
    )

    explanation = models.TextField(
        blank=True,
        help_text="Explanation of the correct answer"
    )

    concept_tag = models.CharField(
        max_length=200,
        blank=True,
        help_text="The concept/objective this question assesses"
    )

    difficulty = models.CharField(
        max_length=10,
        choices=Difficulty.choices,
        default=Difficulty.MEDIUM
    )

    order_index = models.PositiveIntegerField(
        default=0,
        help_text="Order in the exit ticket (0-9)"
    )

    image = models.ImageField(
        upload_to='exit_tickets/',
        blank=True,
        null=True,
        help_text="Optional image for the question"
    )

    # Layer 4 — parametric template source (when non-null, this row
    # was rendered from a ParametricQuestionTemplate at content-gen
    # time). Kept for retake re-rendering and teacher-review
    # visibility. See apps/curriculum/parametric_renderer.py.
    template_data = models.JSONField(
        null=True, blank=True,
        help_text=(
            "If set, this question was generated from a parametric "
            "template. The dict has the shape of "
            "ParametricQuestionTemplate (template_text, parameters, "
            "answer_formula, etc.) and is used to re-render "
            "alternative versions on retake."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order_index']
        verbose_name = "Exit Ticket Question"

    def __str__(self):
        return f"Q{self.order_index + 1}: {self.question_text[:50]}..."
    
    def to_dict(self):
        """Convert to dictionary for API/frontend."""
        return {
            'id': self.id,
            'question': self.question_text,
            'options': [
                f"A) {self.option_a}",
                f"B) {self.option_b}",
                f"C) {self.option_c}",
                f"D) {self.option_d}",
            ],
            'correct': self.correct_answer,
            'explanation': self.explanation,
            'difficulty': self.difficulty,
            'order': self.order_index,
            'image_url': self.image.url if self.image else None,
        }


class ExitTicketAttempt(models.Model):
    """
    Records a student's attempt at an exit ticket OR a course summative.

    `purpose` distinguishes baseline (first take) vs final (post-course)
    attempts on summatives, which drives the longitudinal competency
    tracking surfaced on the dashboard. Per-lesson exit-ticket attempts
    default to 'practice'.
    """
    class Purpose(models.TextChoices):
        PRACTICE = 'practice', 'Practice / lesson exit ticket'
        BASELINE = 'baseline', 'Baseline (pre-course summative)'
        FINAL = 'final', 'Final (post-course summative)'
        RETAKE = 'retake', 'Retake'
        DIAGNOSTIC = 'diagnostic', 'Diagnostic (pre-lesson assessment)'

    exit_ticket = models.ForeignKey(
        ExitTicket,
        on_delete=models.CASCADE,
        related_name='attempts'
    )
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='exit_ticket_attempts'
    )
    session = models.ForeignKey(
        TutorSession,
        on_delete=models.CASCADE,
        related_name='exit_ticket_attempts',
        null=True,
        blank=True
    )

    purpose = models.CharField(
        max_length=15,
        choices=Purpose.choices,
        default=Purpose.PRACTICE,
        help_text=(
            "What this attempt counts as. Baseline + Final on a summative "
            "drive the dashboard's longitudinal competency view."
        ),
    )

    score = models.PositiveIntegerField(default=0)
    passed = models.BooleanField(default=False)

    # Detailed answers: {question_id: {answer: 'A', correct: True}}
    answers = models.JSONField(default=dict)

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        if self.exit_ticket.lesson_id:
            label = self.exit_ticket.lesson.title
        elif self.exit_ticket.course_id:
            label = f"{self.exit_ticket.course.title} (summative)"
        else:
            label = f"ExitTicket #{self.exit_ticket_id}"
        return f"{self.student.username} - {label}: {self.score} [{self.purpose}]"


class TeacherGuidance(models.Model):
    """Guidance from teacher or Monitor AI injected into a tutor session."""
    session = models.ForeignKey(TutorSession, on_delete=models.CASCADE, related_name='guidances')
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, help_text="Teacher who sent this, or null for Monitor AI")
    message = models.TextField(help_text="Guidance instruction for the tutor engine")
    is_from_ai = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, help_text="Whether this guidance is currently applied")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        source = "Monitor AI" if self.is_from_ai else (self.author.get_full_name() if self.author else "Teacher")
        return f"{source}: {self.message[:50]}"


# Import skills models so Django discovers them for migrations
from apps.tutoring.skills_models import (  # noqa: E402, F401
    Skill,
    LessonPrerequisite,
    StudentSkillMastery,
    SkillPracticeLog,
    StudentKnowledgeProfile,
)


class LessonPackVersion(models.Model):
    """Immutable, versioned snapshot of a lesson's offline pack.

    Generated server-side and downloaded by the React Native app before
    going offline. Each pack contains the full lesson + steps + exit
    ticket + per-concept remediation variants + a JSON state-machine
    policy the app's runner consumes. See memory/mobile_rn_plan.md.
    """

    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='pack_versions',
    )
    version = models.PositiveIntegerField()
    policy_json = models.JSONField(
        default=dict,
        help_text='State-machine policy for the on-device runner.',
    )
    content_snapshot = models.JSONField(
        default=dict,
        help_text='Frozen lesson + steps + exit ticket questions at this version.',
    )
    media_manifest = models.JSONField(
        default=list, blank=True,
        help_text='List of media URLs referenced by the pack (for client to download).',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pack_versions_created',
    )

    class Meta:
        unique_together = [('lesson', 'version')]
        ordering = ['-created_at']
        indexes = [models.Index(fields=['lesson', '-version'])]

    def __str__(self):
        return f"{self.lesson.title} v{self.version}"

    @classmethod
    def latest_for(cls, lesson):
        return cls.objects.filter(lesson=lesson).order_by('-version').first()