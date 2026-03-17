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
        related_name='courses',
        null=True,
        blank=True,
        help_text="Null = platform-wide course visible to all schools"
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

    MATH_KEYWORDS = ('math', 'maths', 'mathematics', 'algebra', 'geometry', 'calculus')

    @property
    def is_math(self):
        return any(kw in (self.title or '').lower() for kw in self.MATH_KEYWORDS)

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
    grade_level = models.CharField(
        max_length=50,
        blank=True,
        help_text="Target grade level(s), e.g. 'S1', 'S1,S2'. Empty = visible to all grades in the course."
    )

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

    class ContentStatus(models.TextChoices):
        EMPTY = 'empty', 'Empty'
        GENERATING = 'generating', 'Generating'
        READY = 'ready', 'Ready'
        FAILED = 'failed', 'Failed'

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
    content_status = models.CharField(
        max_length=20,
        choices=ContentStatus.choices,
        default=ContentStatus.EMPTY,
        help_text="Status of generated content for this lesson"
    )

    # Flexible metadata (key concepts, skills, image suggestions, etc.)
    metadata = models.JSONField(default=dict, blank=True)
    
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
    
    Enhanced with:
    - Media content (images, diagrams, videos)
    - Educational materials (vocabulary, examples)
    - Curriculum context (from knowledge base)
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
    
    # =========================================================================
    # MEDIA CONTENT
    # =========================================================================
    media = models.JSONField(
        blank=True,
        null=True,
        help_text="""Media content for this step. Structure:
        {
            "images": [
                {
                    "url": "/media/lessons/...",
                    "alt": "Description of image",
                    "caption": "Figure 1: ...",
                    "type": "diagram|photo|illustration|chart",
                    "source": "generated|library|curriculum"
                }
            ],
            "videos": [
                {
                    "url": "https://...",
                    "title": "Video title",
                    "duration_seconds": 120,
                    "start_time": 0
                }
            ],
            "audio": [
                {
                    "url": "/media/audio/...",
                    "title": "Pronunciation guide"
                }
            ]
        }
        """
    )
    
    # =========================================================================
    # EDUCATIONAL MATERIALS
    # =========================================================================
    educational_content = models.JSONField(
        blank=True,
        null=True,
        help_text="""Educational materials for this step. Structure:
        {
            "key_vocabulary": [
                {"term": "...", "definition": "...", "example": "..."}
            ],
            "worked_example": {
                "problem": "...",
                "steps": [
                    {"step": 1, "action": "...", "explanation": "..."}
                ],
                "final_answer": "..."
            },
            "formulas": [
                {"name": "...", "formula": "...", "variables": {...}}
            ],
            "key_points": ["point 1", "point 2"],
            "common_mistakes": ["mistake 1", "mistake 2"],
            "real_world_connections": ["connection 1"],
            "seychelles_context": "Local example or connection"
        }
        """
    )
    
    # =========================================================================
    # CURRICULUM CONTEXT (from Knowledge Base)
    # =========================================================================
    curriculum_context = models.JSONField(
        blank=True,
        null=True,
        help_text="""Curriculum context from knowledge base. Structure:
        {
            "teaching_strategies": ["strategy 1", "strategy 2"],
            "learning_objectives": ["objective 1"],
            "assessment_criteria": ["criteria 1"],
            "prerequisite_knowledge": ["prereq 1"],
            "cross_curricular_links": ["link 1"],
            "differentiation": {
                "support": "For struggling students...",
                "extension": "For advanced students..."
            },
            "resources_from_curriculum": ["resource 1"]
        }
        """
    )
    
    # =========================================================================
    # PHASE/PEDAGOGY
    # =========================================================================
    phase = models.CharField(
        max_length=20,
        blank=True,
        help_text="Pedagogical phase: engage, explore, explain, practice, evaluate"
    )
    concept_tag = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text="Groups steps by concept — all steps teaching/practicing the same concept share a tag"
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
    
    # =========================================================================
    # HELPER METHODS
    # =========================================================================
    
    def get_images(self):
        """Get list of images for this step."""
        if self.media and isinstance(self.media, dict):
            return self.media.get('images', [])
        return []
    
    def get_primary_image(self):
        """Get the first/main image for this step."""
        images = self.get_images()
        return images[0] if images else None
    
    def get_vocabulary(self):
        """Get key vocabulary for this step."""
        if self.educational_content and isinstance(self.educational_content, dict):
            return self.educational_content.get('key_vocabulary', [])
        return []
    
    def get_worked_example(self):
        """Get worked example if this is a worked_example step."""
        if self.educational_content and isinstance(self.educational_content, dict):
            return self.educational_content.get('worked_example')
        return None
    
    def get_teaching_strategies(self):
        """Get teaching strategies from curriculum context."""
        if self.curriculum_context and isinstance(self.curriculum_context, dict):
            return self.curriculum_context.get('teaching_strategies', [])
        return []
    
    def get_seychelles_context(self):
        """Get local Seychelles context/examples."""
        if self.educational_content and isinstance(self.educational_content, dict):
            return self.educational_content.get('seychelles_context', '')
        return ''
    
    def has_media(self):
        """Check if step has any media content."""
        if not self.media:
            return False
        return bool(
            self.media.get('images') or 
            self.media.get('videos') or 
            self.media.get('audio')
        )
