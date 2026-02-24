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
from apps.curriculum.models import Lesson, LessonStep
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
    
    # Optional summary (generated at end)
    summary = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']
        verbose_name = "Tutor Session"

    def __str__(self):
        return f"{self.student.username} - {self.lesson.title} ({self.status})"


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
    correct_streak = models.PositiveIntegerField(
        default=0,
        help_text="Current streak of correct answers"
    )
    total_attempts = models.PositiveIntegerField(default=0)
    total_correct = models.PositiveIntegerField(default=0)
    best_score = models.FloatField(
        null=True,
        blank=True,
        help_text="Best quiz score as percentage"
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
    A standardized exit ticket (summative assessment) for a lesson.
    
    Each lesson should have exactly one exit ticket with 10 MCQ questions.
    Students need 8/10 (80%) to pass.
    """
    lesson = models.OneToOneField(
        Lesson,
        on_delete=models.CASCADE,
        related_name='exit_ticket'
    )
    
    passing_score = models.PositiveIntegerField(
        default=8,
        help_text="Minimum correct answers to pass (out of 10)"
    )
    
    time_limit_minutes = models.PositiveIntegerField(
        default=10,
        help_text="Time limit for exit ticket (0 = no limit)"
    )
    
    instructions = models.TextField(
        default="Answer all 10 questions. You need 8 correct to pass.",
        blank=True
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Exit Ticket"
        verbose_name_plural = "Exit Tickets"

    def __str__(self):
        return f"Exit Ticket: {self.lesson.title}"
    
    @property
    def question_count(self):
        return self.questions.count()
    
    @property
    def is_complete(self):
        """Check if exit ticket has a full question bank (30+)."""
        return self.question_count >= 30


class ExitTicketQuestion(models.Model):
    """
    A single MCQ question in an exit ticket.
    """
    class Difficulty(models.TextChoices):
        EASY = 'easy', 'Easy (recall)'
        MEDIUM = 'medium', 'Medium (apply)'
        HARD = 'hard', 'Hard (analyze)'

    exit_ticket = models.ForeignKey(
        ExitTicket,
        on_delete=models.CASCADE,
        related_name='questions'
    )
    
    question_text = models.TextField(help_text="The question stem")
    
    option_a = models.CharField(max_length=500)
    option_b = models.CharField(max_length=500)
    option_c = models.CharField(max_length=500)
    option_d = models.CharField(max_length=500)
    
    correct_answer = models.CharField(
        max_length=1,
        choices=[('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')],
        help_text="The correct option (A, B, C, or D)"
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
    Records a student's attempt at an exit ticket.
    """
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
    
    score = models.PositiveIntegerField(default=0)
    passed = models.BooleanField(default=False)
    
    # Detailed answers: {question_id: {answer: 'A', correct: True}}
    answers = models.JSONField(default=dict)
    
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.student.username} - {self.exit_ticket.lesson.title}: {self.score}/10"


# Import skills models so Django discovers them for migrations
from apps.tutoring.skills_models import (  # noqa: E402, F401
    Skill,
    LessonPrerequisite,
    StudentSkillMastery,
    SkillPracticeLog,
    StudentKnowledgeProfile,
)