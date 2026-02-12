"""
Tutoring app - Session tracking and student progress.

TutorSession: A single tutoring interaction (student + lesson)
SessionTurn: Each message in the conversation
StudentLessonProgress: Tracks mastery across sessions
"""

from django.db import models
from django.contrib.auth.models import User
from apps.accounts.models import Institution
from apps.curriculum.models import Lesson, LessonStep
from apps.llm.models import PromptPack, ModelConfig


class TutorSession(models.Model):
    """
    A single tutoring session - one student working through one lesson.
    
    Captures which prompt pack and model config were used (for reproducibility
    and debugging).
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
    
    Stores both student and tutor messages for the full transcript.
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
    
    This is the "persistent" progress - survives individual sessions.
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
