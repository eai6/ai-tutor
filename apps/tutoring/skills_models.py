"""
Skills-Based Learning Models

This module implements a knowledge graph and skill mastery tracking system
that enables personalized, adaptive tutoring based on science of learning principles:

- Skills: Atomic units of knowledge that can be measured and tracked
- StudentSkillMastery: Per-student skill tracking with spaced repetition
- Prerequisite relationships: Skills and lessons linked by dependencies

Key Features:
1. Embedded Spaced Repetition - Reviews happen within forward learning
2. Personalized Retrieval - Each student gets different review questions
3. Knowledge Graph - Skills linked by prerequisites
4. Adaptive Difficulty - Based on individual mastery levels
"""

import math
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Unit, Lesson, LessonStep


# =============================================================================
# SKILL MODEL - Atomic Units of Knowledge
# =============================================================================

class Skill(models.Model):
    """
    An atomic unit of knowledge/ability that can be measured and tracked.
    
    Skills are extracted from lessons and linked to questions. They form
    a knowledge graph with prerequisite relationships.
    
    Examples:
    - "identify_fault_types" - Identify normal, reverse, strike-slip faults
    - "calculate_plate_velocity" - Calculate tectonic plate movement speed
    - "explain_convection_currents" - Explain mantle convection
    """
    
    class Difficulty(models.TextChoices):
        FOUNDATIONAL = 'foundational', 'Foundational'  # Basic concepts
        INTERMEDIATE = 'intermediate', 'Intermediate'  # Applied skills
        ADVANCED = 'advanced', 'Advanced'  # Complex synthesis
    
    # Institution scope
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='skills'
    )
    
    # Identity
    code = models.CharField(
        max_length=100,
        help_text="Unique code, e.g., 'geo_identify_fault_types'"
    )
    name = models.CharField(
        max_length=200,
        help_text="Human-readable name, e.g., 'Identify types of geological faults'"
    )
    description = models.TextField(
        blank=True,
        help_text="Detailed description of what this skill entails"
    )
    
    # Curriculum context
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name='skills'
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.CASCADE,
        related_name='skills',
        null=True,
        blank=True
    )
    
    # Primary lesson that teaches this skill
    primary_lesson = models.ForeignKey(
        Lesson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='primary_skills',
        help_text="The main lesson that teaches this skill"
    )
    
    # All lessons that involve this skill (teaching or practicing)
    lessons = models.ManyToManyField(
        Lesson,
        related_name='skills',
        blank=True,
        help_text="All lessons that teach or practice this skill"
    )
    
    # Difficulty and importance
    difficulty = models.CharField(
        max_length=20,
        choices=Difficulty.choices,
        default=Difficulty.INTERMEDIATE
    )
    difficulty_score = models.FloatField(
        default=0.5,
        help_text="Numeric difficulty 0.0 (easy) to 1.0 (hard)"
    )
    importance = models.FloatField(
        default=0.5,
        help_text="How critical this skill is (0.0 to 1.0)"
    )
    
    # Prerequisites (other skills needed first)
    prerequisites = models.ManyToManyField(
        'self',
        symmetrical=False,
        related_name='unlocks',
        blank=True,
        help_text="Skills that should be mastered before this one"
    )
    
    # Bloom's Taxonomy level
    bloom_level = models.CharField(
        max_length=20,
        choices=[
            ('remember', 'Remember'),
            ('understand', 'Understand'),
            ('apply', 'Apply'),
            ('analyze', 'Analyze'),
            ('evaluate', 'Evaluate'),
            ('create', 'Create'),
        ],
        default='understand'
    )
    
    # Metadata
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text="Tags for categorization, e.g., ['plate_tectonics', 'geology']"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['institution', 'code']
        ordering = ['course', 'unit', 'name']
        verbose_name = "Skill"
        verbose_name_plural = "Skills"
    
    def __str__(self):
        return self.name
    
    def get_prerequisite_chain(self, max_depth=5):
        """Get all prerequisites recursively up to max_depth."""
        visited = set()
        chain = []
        
        def collect(skill, depth):
            if depth > max_depth or skill.id in visited:
                return
            visited.add(skill.id)
            for prereq in skill.prerequisites.all():
                chain.append(prereq)
                collect(prereq, depth + 1)
        
        collect(self, 0)
        return chain


# =============================================================================
# LESSON PREREQUISITES - Explicit Lesson Dependencies
# =============================================================================

class LessonPrerequisite(models.Model):
    """
    Explicit prerequisite relationship between lessons.
    
    This defines the knowledge graph at the lesson level, which helps
    determine which previous lessons to draw review questions from.
    """
    
    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='prerequisites'
    )
    prerequisite = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='required_for'
    )
    
    # How strongly this prerequisite is required
    strength = models.FloatField(
        default=1.0,
        help_text="1.0 = essential, 0.5 = helpful, 0.0 = loosely related"
    )
    
    # Is this a direct/key prerequisite?
    is_direct = models.BooleanField(
        default=True,
        help_text="True if this is an immediate prerequisite, False if transitive"
    )
    
    class Meta:
        unique_together = ['lesson', 'prerequisite']
        verbose_name = "Lesson Prerequisite"
    
    def __str__(self):
        return f"{self.lesson.title} requires {self.prerequisite.title}"


# =============================================================================
# STUDENT SKILL MASTERY - Per-Student Skill Tracking
# =============================================================================

class StudentSkillMastery(models.Model):
    """
    Tracks a student's mastery of a specific skill over time.
    
    Implements spaced repetition scheduling using a modified SM-2 algorithm.
    This is the core of personalized learning - each student has their own
    mastery record for each skill.
    
    Key fields:
    - mastery_level: Current mastery (0.0 to 1.0)
    - next_review_due: When this skill should be reviewed
    - ease_factor: How easily the student learns this (affects intervals)
    """
    
    class MasteryState(models.TextChoices):
        NOT_STARTED = 'not_started', 'Not Started'
        LEARNING = 'learning', 'Learning'
        REVIEWING = 'reviewing', 'Reviewing'
        MASTERED = 'mastered', 'Mastered'
    
    # Core relationships
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='skill_mastery'
    )
    skill = models.ForeignKey(
        Skill,
        on_delete=models.CASCADE,
        related_name='student_mastery'
    )
    
    # Mastery tracking
    mastery_level = models.FloatField(
        default=0.0,
        help_text="Current mastery level (0.0 to 1.0)"
    )
    state = models.CharField(
        max_length=20,
        choices=MasteryState.choices,
        default=MasteryState.NOT_STARTED
    )
    
    # Spaced repetition fields (SM-2 algorithm)
    last_practiced = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this skill was last practiced"
    )
    next_review_due = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this skill should be reviewed"
    )
    repetition_count = models.IntegerField(
        default=0,
        help_text="Number of successful reviews"
    )
    ease_factor = models.FloatField(
        default=2.5,
        help_text="SM-2 ease factor (1.3 to 3.0)"
    )
    interval_days = models.IntegerField(
        default=1,
        help_text="Current review interval in days"
    )
    
    # Performance tracking
    total_attempts = models.IntegerField(default=0)
    correct_attempts = models.IntegerField(default=0)
    current_streak = models.IntegerField(
        default=0,
        help_text="Current streak of correct answers"
    )
    best_streak = models.IntegerField(default=0)
    
    # Learning history
    first_learned = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the student first learned this skill"
    )
    last_correct = models.DateTimeField(
        null=True,
        blank=True
    )
    last_incorrect = models.DateTimeField(
        null=True,
        blank=True
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['student', 'skill']
        ordering = ['-updated_at']
        verbose_name = "Student Skill Mastery"
        verbose_name_plural = "Student Skill Mastery Records"
    
    def __str__(self):
        return f"{self.student.username} - {self.skill.name} ({self.mastery_level:.0%})"
    
    @property
    def accuracy(self):
        """Calculate accuracy percentage."""
        if self.total_attempts == 0:
            return 0.0
        return self.correct_attempts / self.total_attempts
    
    def calculate_retention(self):
        """
        Estimate current retention based on time since last practice.
        Uses exponential decay: R = e^(-t/S) where S is stability.
        
        Returns a value between 0.0 and 1.0 representing estimated retention.
        """
        if not self.last_practiced:
            return 0.0
        
        days_since = (timezone.now() - self.last_practiced).total_seconds() / 86400
        
        # Stability increases with ease factor and interval
        stability = self.interval_days * (self.ease_factor / 2.5)
        stability = max(stability, 0.5)  # Minimum stability
        
        # Exponential decay
        retention = math.exp(-days_since / stability)
        
        return min(1.0, max(0.0, retention))
    
    def is_due_for_review(self):
        """Check if this skill is due for review."""
        if not self.next_review_due:
            return False
        return timezone.now() >= self.next_review_due
    
    def days_until_review(self):
        """Get days until next review (negative if overdue)."""
        if not self.next_review_due:
            return None
        delta = self.next_review_due - timezone.now()
        return delta.days
    
    def record_attempt(self, was_correct: bool, quality: int = None):
        """
        Record a practice attempt and update spaced repetition schedule.
        
        Args:
            was_correct: Whether the answer was correct
            quality: Optional quality rating 0-5 (for SM-2). If not provided,
                    will be inferred from was_correct.
        
        SM-2 Algorithm:
        - Quality 0-2: Reset repetitions (forgotten)
        - Quality 3: Correct but hard
        - Quality 4: Correct with hesitation  
        - Quality 5: Perfect recall
        """
        now = timezone.now()
        self.total_attempts += 1
        self.last_practiced = now
        
        # Infer quality if not provided
        if quality is None:
            quality = 4 if was_correct else 1
        
        if was_correct:
            self.correct_attempts += 1
            self.current_streak += 1
            self.best_streak = max(self.best_streak, self.current_streak)
            self.last_correct = now
            
            if not self.first_learned:
                self.first_learned = now
            
            # Update state
            if self.mastery_level >= 0.8:
                self.state = self.MasteryState.MASTERED
            elif self.repetition_count > 0:
                self.state = self.MasteryState.REVIEWING
            else:
                self.state = self.MasteryState.LEARNING
            
            # SM-2: Update ease factor
            # EF' = EF + (0.1 - (5-q) * (0.08 + (5-q) * 0.02))
            self.ease_factor = max(
                1.3,
                self.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
            )
            
            # SM-2: Calculate next interval
            self.repetition_count += 1
            if self.repetition_count == 1:
                self.interval_days = 1
            elif self.repetition_count == 2:
                self.interval_days = 3
            else:
                self.interval_days = int(self.interval_days * self.ease_factor)
            
            # Cap interval at 180 days
            self.interval_days = min(self.interval_days, 180)
            
            # Update mastery level (increases with successful reviews)
            mastery_boost = 0.1 * (quality / 5)
            self.mastery_level = min(1.0, self.mastery_level + mastery_boost)
            
        else:
            self.current_streak = 0
            self.last_incorrect = now
            
            # SM-2: Reset on failure
            self.repetition_count = 0
            self.interval_days = 1
            
            # Decrease ease factor (makes future intervals shorter)
            self.ease_factor = max(1.3, self.ease_factor - 0.2)
            
            # Decrease mastery level
            self.mastery_level = max(0.0, self.mastery_level - 0.15)
            
            # Update state
            if self.mastery_level < 0.3:
                self.state = self.MasteryState.LEARNING
            else:
                self.state = self.MasteryState.REVIEWING
        
        # Schedule next review
        self.next_review_due = now + timedelta(days=self.interval_days)
        
        self.save()
        return self
    
    def get_review_priority(self, for_lesson: Lesson = None):
        """
        Calculate review priority score for this skill.
        Higher score = higher priority for review.
        
        Factors:
        - Is it due/overdue for review?
        - Is it a prerequisite for the current lesson?
        - Is retention dropping?
        - How important is the skill?
        """
        score = 0.0
        
        # Factor 1: Overdue for review (max 50 points)
        if self.next_review_due:
            days_overdue = (timezone.now() - self.next_review_due).days
            if days_overdue > 0:
                score += min(50, 10 + days_overdue * 5)
        
        # Factor 2: Prerequisite for current lesson (30 points)
        if for_lesson and self.skill.primary_lesson:
            prereqs = LessonPrerequisite.objects.filter(
                lesson=for_lesson,
                prerequisite=self.skill.primary_lesson
            )
            if prereqs.exists():
                score += 30 * prereqs.first().strength
        
        # Factor 3: Low retention (max 30 points)
        retention = self.calculate_retention()
        if retention < 0.8:
            score += (1 - retention) * 30
        
        # Factor 4: Needs consolidation (few repetitions)
        if 0 < self.repetition_count < 3:
            score += 15
        
        # Factor 5: Skill importance
        score += self.skill.importance * 10
        
        return score


# =============================================================================
# SKILL PRACTICE LOG - Detailed Practice History
# =============================================================================

class SkillPracticeLog(models.Model):
    """
    Detailed log of each skill practice attempt.
    
    Used for analytics and understanding learning patterns.
    """
    
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='skill_practice_logs'
    )
    skill = models.ForeignKey(
        Skill,
        on_delete=models.CASCADE,
        related_name='practice_logs'
    )
    mastery_record = models.ForeignKey(
        StudentSkillMastery,
        on_delete=models.CASCADE,
        related_name='practice_logs'
    )
    
    # Context
    session = models.ForeignKey(
        'tutoring.TutorSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='skill_practice_logs'
    )
    lesson_step = models.ForeignKey(
        LessonStep,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    # Practice type
    practice_type = models.CharField(
        max_length=20,
        choices=[
            ('initial', 'Initial Learning'),
            ('retrieval', 'Retrieval Practice'),
            ('interleaved', 'Interleaved Practice'),
            ('review', 'Spaced Review'),
            ('remediation', 'Targeted Remediation'),
        ],
        default='initial'
    )
    
    # Result
    was_correct = models.BooleanField()
    quality = models.IntegerField(
        null=True,
        blank=True,
        help_text="Quality rating 0-5"
    )
    time_taken_seconds = models.IntegerField(
        null=True,
        blank=True
    )
    hints_used = models.IntegerField(default=0)
    
    # Mastery snapshot at time of practice
    mastery_before = models.FloatField()
    mastery_after = models.FloatField()
    retention_estimate = models.FloatField(
        null=True,
        blank=True,
        help_text="Estimated retention at time of practice"
    )
    
    # Timestamp
    practiced_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-practiced_at']
        verbose_name = "Skill Practice Log"
    
    def __str__(self):
        result = "✓" if self.was_correct else "✗"
        return f"{self.student.username} - {self.skill.code} {result}"


# =============================================================================
# STUDENT KNOWLEDGE PROFILE - Aggregated Student State
# =============================================================================

class StudentKnowledgeProfile(models.Model):
    """
    Aggregated knowledge profile for a student in a course.
    
    This provides a high-level view of a student's knowledge state,
    useful for personalization and progress tracking.
    """
    
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='knowledge_profiles'
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name='student_profiles'
    )
    
    # Aggregated stats
    total_skills = models.IntegerField(default=0)
    mastered_skills = models.IntegerField(default=0)
    learning_skills = models.IntegerField(default=0)
    
    # Average mastery
    average_mastery = models.FloatField(default=0.0)
    average_retention = models.FloatField(default=0.0)
    
    # Engagement
    total_practice_time_minutes = models.IntegerField(default=0)
    total_sessions = models.IntegerField(default=0)
    current_streak_days = models.IntegerField(default=0)
    longest_streak_days = models.IntegerField(default=0)
    last_activity = models.DateTimeField(null=True, blank=True)
    
    # XP and gamification
    total_xp = models.IntegerField(default=0)
    level = models.IntegerField(default=1)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['student', 'course']
        verbose_name = "Student Knowledge Profile"
    
    def __str__(self):
        return f"{self.student.username} - {self.course.title} ({self.average_mastery:.0%})"
    
    def recalculate(self):
        """Recalculate aggregated stats from skill mastery records."""
        from django.db.models import Avg, Count, Sum
        
        skills = Skill.objects.filter(course=self.course)
        self.total_skills = skills.count()
        
        mastery_records = StudentSkillMastery.objects.filter(
            student=self.student,
            skill__in=skills
        )
        
        stats = mastery_records.aggregate(
            avg_mastery=Avg('mastery_level'),
            mastered=Count('id', filter=models.Q(mastery_level__gte=0.8)),
            learning=Count('id', filter=models.Q(mastery_level__gt=0, mastery_level__lt=0.8)),
        )
        
        self.average_mastery = stats['avg_mastery'] or 0.0
        self.mastered_skills = stats['mastered'] or 0
        self.learning_skills = stats['learning'] or 0
        
        # Calculate average retention
        total_retention = 0
        count = 0
        for record in mastery_records:
            if record.last_practiced:
                total_retention += record.calculate_retention()
                count += 1
        
        self.average_retention = total_retention / count if count > 0 else 0.0
        
        self.save()
        return self
    
    def add_xp(self, amount: int, reason: str = ""):
        """Add XP and check for level up."""
        self.total_xp += amount

        # Simple leveling: 1000 XP per level
        new_level = (self.total_xp // 1000) + 1
        leveled_up = new_level > self.level
        self.level = new_level

        self.save()
        return leveled_up


# =============================================================================
# ACHIEVEMENTS & BADGES
# =============================================================================

class Achievement(models.Model):
    """Defines an earnable achievement / badge."""

    class Category(models.TextChoices):
        MILESTONE = 'milestone', 'Milestone'
        STREAK = 'streak', 'Streak'
        MASTERY = 'mastery', 'Mastery'
        SPECIAL = 'special', 'Special'

    class TriggerType(models.TextChoices):
        FIRST_LESSON = 'first_lesson', 'First Lesson Completed'
        LESSONS_COMPLETED = 'lessons_completed', 'Lessons Completed'
        STREAK_DAYS = 'streak_days', 'Streak Days'
        XP_THRESHOLD = 'xp_threshold', 'XP Threshold'
        LEVEL_REACHED = 'level_reached', 'Level Reached'
        PERFECT_SCORE = 'perfect_score', 'Perfect Exit-Ticket Score'
        EXIT_TICKET_PASS = 'exit_ticket_pass', 'Exit Ticket Passed'

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255)
    emoji = models.CharField(max_length=10, default='')
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.MILESTONE)
    trigger_type = models.CharField(max_length=30, choices=TriggerType.choices)
    trigger_value = models.IntegerField(default=0)
    xp_reward = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.emoji} {self.name}" if self.emoji else self.name


class StudentAchievement(models.Model):
    """Records when a student earns an achievement."""
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='achievements')
    achievement = models.ForeignKey(Achievement, on_delete=models.CASCADE, related_name='earned_by')
    earned_at = models.DateTimeField(auto_now_add=True)
    context = models.JSONField(default=dict, blank=True)
    is_seen = models.BooleanField(default=False)

    class Meta:
        unique_together = ['student', 'achievement']
        ordering = ['-earned_at']

    def __str__(self):
        return f"{self.student.username} — {self.achievement.name}"
