"""
Curriculum app - Course, Unit, Lesson, and LessonStep models.

This is the "teacher-led backbone" - the structured content that
drives the tutoring sessions. Steps control the flow.

Hierarchy: Course > Unit > Lesson > LessonStep
"""

from django.db import models
from apps.accounts.models import Institution


class Course(models.Model):
    """
    Top-level curriculum container (e.g., "Grade 3 Math", "Intro to Python").
    """
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='courses'
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    grade_level = models.CharField(
        max_length=50,
        blank=True,
        help_text="e.g., 'Grade 3', 'High School', 'Adult'"
    )
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return self.title


class Unit(models.Model):
    """
    A grouping of related lessons within a course.
    """
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name='units'
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    order_index = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order_index']

    def __str__(self):
        return f"{self.course.title} > {self.title}"

    @property
    def institution(self):
        """Convenience accessor for filtering."""
        return self.course.institution


class Lesson(models.Model):
    """
    A single teaching unit with a clear objective and mastery criteria.
    """
    class MasteryRule(models.TextChoices):
        STREAK_3 = 'streak_3', '3 correct in a row'
        STREAK_5 = 'streak_5', '5 correct in a row'
        PASS_QUIZ = 'pass_quiz', 'Pass exit quiz (80%+)'
        COMPLETE_ALL = 'complete_all', 'Complete all steps'

    unit = models.ForeignKey(
        Unit,
        on_delete=models.CASCADE,
        related_name='lessons'
    )
    title = models.CharField(max_length=200)
    objective = models.TextField(
        help_text="What the student will learn/be able to do"
    )
    estimated_minutes = models.PositiveIntegerField(
        default=15,
        help_text="Estimated time to complete"
    )
    mastery_rule = models.CharField(
        max_length=20,
        choices=MasteryRule.choices,
        default=MasteryRule.STREAK_3
    )
    order_index = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order_index']

    def __str__(self):
        return self.title

    @property
    def institution(self):
        return self.unit.course.institution


class LessonStep(models.Model):
    """
    A single step in a lesson - the atomic unit of instruction.
    
    Steps can be teaching moments, worked examples, practice problems,
    quiz questions, or summaries. Each step type has slightly different
    fields that matter.
    """
    class StepType(models.TextChoices):
        TEACH = 'teach', 'Teaching'
        WORKED_EXAMPLE = 'worked_example', 'Worked Example'
        PRACTICE = 'practice', 'Practice Problem'
        QUIZ = 'quiz', 'Quiz Question'
        SUMMARY = 'summary', 'Summary'

    class AnswerType(models.TextChoices):
        NONE = 'none', 'No response needed'
        FREE_TEXT = 'free_text', 'Free Text'
        MULTIPLE_CHOICE = 'multiple_choice', 'Multiple Choice'
        SHORT_NUMERIC = 'short_numeric', 'Numeric Answer'
        TRUE_FALSE = 'true_false', 'True/False'

    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='steps'
    )
    order_index = models.PositiveIntegerField(default=0)
    step_type = models.CharField(
        max_length=20,
        choices=StepType.choices,
        default=StepType.TEACH
    )

    # Content
    teacher_script = models.TextField(
        help_text="What the AI tutor should say/explain"
    )
    question = models.TextField(
        blank=True,
        help_text="The question to ask (for practice/quiz steps)"
    )

    # Answer handling
    answer_type = models.CharField(
        max_length=20,
        choices=AnswerType.choices,
        default=AnswerType.NONE
    )
    choices = models.JSONField(
        blank=True,
        null=True,
        help_text="For MCQ: list of choices, e.g., ['A', 'B', 'C', 'D']"
    )
    expected_answer = models.TextField(
        blank=True,
        help_text="The correct answer (text or JSON for complex answers)"
    )
    rubric = models.TextField(
        blank=True,
        help_text="Grading rubric for free-text answers (used by LLM)"
    )

    # Hint ladder
    hint_1 = models.TextField(blank=True)
    hint_2 = models.TextField(blank=True)
    hint_3 = models.TextField(blank=True)

    # Attempt limits
    max_attempts = models.PositiveIntegerField(
        default=3,
        help_text="Max attempts before showing answer"
    )

    class Meta:
        ordering = ['order_index']

    def __str__(self):
        return f"{self.lesson.title} - Step {self.order_index + 1} ({self.step_type})"

    @property
    def institution(self):
        return self.lesson.unit.course.institution

    @property
    def hints(self):
        """Return list of non-empty hints."""
        return [h for h in [self.hint_1, self.hint_2, self.hint_3] if h]

    def requires_response(self):
        """Does this step need a student response?"""
        return self.answer_type != self.AnswerType.NONE
