"""
Curriculum app - Course, Unit, Lesson, and LessonStep models.

This is the "teacher-led backbone" - the structured content that
drives the tutoring sessions. Steps control the flow.

Hierarchy: Course > Unit > Lesson > LessonStep
"""

from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
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
    curriculum_upload = models.ForeignKey(
        'dashboard.CurriculumUpload',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_courses',
        help_text="The curriculum upload that created this course"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    grade_level = models.CharField(
        max_length=50,
        blank=True,
        help_text="e.g., 'Grade 3', 'High School', 'Adult'"
    )
    is_published = models.BooleanField(default=False)

    # Locale of this course's curriculum content. Drives the tutor's
    # response language (via the per-turn locale block in the system
    # prompt) and the UI rendering inside chat sessions (via the
    # LocaleResolverMiddleware "course wins in-session" branch). Use
    # hyphenated lowercase to match Django's LANGUAGE_CODE convention
    # — e.g. 'en-us', 'pt-mz'. New non-English curricula land by setting
    # this field on the imported Course; the rest of the platform is
    # locale-agnostic. See memory/multi_locale_architecture_research.md.
    #
    # `choices` is wired to settings.LANGUAGES so the admin + any form
    # rendering this field shows country-forward labels (see auto-
    # memory/feedback_locale_picker_country_forward.md).
    locale = models.CharField(
        max_length=10,
        default='en-us',
        choices=settings.LANGUAGES,
        help_text=(
            "Course curriculum language. Hyphenated lowercase per Django "
            "LANGUAGE_CODE (e.g. 'en-us', 'pt-mz'). Drives tutor response "
            "language + UI activation during chat sessions."
        ),
    )

    # Subject classification (M8 / memory/math_tutor_fix_plan.md). Replaces
    # the fragile MATH_KEYWORDS title heuristic. is_math now prefers this
    # field when set, falling back to the keyword check for legacy rows
    # that haven't been classified yet.
    class SubjectType(models.TextChoices):
        MATH = 'math', 'Math'
        SCIENCE = 'science', 'Science'
        HUMANITIES = 'humanities', 'Humanities'
        LANGUAGE = 'language', 'Language'
        OTHER = 'other', 'Other'

    subject_type = models.CharField(
        max_length=20,
        choices=SubjectType.choices,
        blank=True,
        default='',
        help_text=(
            "Canonical subject classification. Drives is_math and "
            "subject-specific tutor rules. Empty falls back to the legacy "
            "MATH_KEYWORDS title heuristic."
        ),
    )

    # Finer subject identifier for material-sharing matches. SubjectType
    # is too coarse — both Geography and History collapse to HUMANITIES,
    # which means the global-KB merge can't tell them apart. SubjectCode
    # is the canonical "what subject is this course teaching" used to
    # match platform-wide courses to per-school courses for material
    # inheritance. See memory/curriculum_material_sharing_plan.md.
    class SubjectCode(models.TextChoices):
        MATHEMATICS = 'mathematics', 'Mathematics'
        GEOGRAPHY = 'geography', 'Geography'
        PHYSICS = 'physics', 'Physics'
        CHEMISTRY = 'chemistry', 'Chemistry'
        BIOLOGY = 'biology', 'Biology'
        ENGLISH = 'english', 'English'
        FRENCH = 'french', 'French'
        HISTORY = 'history', 'History'
        COMPUTER_SCIENCE = 'computer_science', 'Computer Science'
        OTHER = 'other', 'Other'

    subject_code = models.CharField(
        max_length=32,
        choices=SubjectCode.choices,
        blank=True,
        default='',
        help_text=(
            "Canonical subject for cross-institution material sharing. "
            "Required for new courses; legacy rows backfilled via "
            "`python manage.py backfill_course_subjects`."
        ),
    )

    # Normalised grade list — multi-grade allowed. Drives the global-KB
    # merge: a school's S3 course inherits chunks from any platform-wide
    # course whose grade_levels include 'S3'. The legacy `grade_level`
    # CharField stays for display + back-compat; backfill parses it once.
    class SecondaryYear(models.TextChoices):
        S1 = 'S1', 'S1'
        S2 = 'S2', 'S2'
        S3 = 'S3', 'S3'
        S4 = 'S4', 'S4'
        S5 = 'S5', 'S5'
        S6 = 'S6', 'S6'

    grade_levels = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Normalised list of secondary years (e.g. ['S3'] or "
            "['S1','S2','S3','S4','S5']). Used for material-sharing "
            "matches between school and platform-wide courses."
        ),
    )

    # Teacher policy: if False, students cannot pick their session
    # duration. The chat-page picker is hidden and any
    # `target_minutes` sent from the client is ignored — sessions
    # always use the teacher-configured `lesson.estimated_minutes`.
    # Default True preserves the agency that the picker shipped with;
    # teachers opt INTO control rather than out. See
    # memory/max_depth_lesson_steps_plan.md.
    allow_student_duration_override = models.BooleanField(
        default=True,
        help_text=(
            "Whether students may pick their session duration via the "
            "chat-page picker. When False, sessions always use the "
            "teacher-configured lesson.estimated_minutes."
        ),
    )

    # Per-course image policy. When False, the tutor:
    #   - does NOT see the figure catalog in its system prompt
    #   - cannot reference figures or emit |||MEDIA:N||| signals
    #   - does NOT attach step images to its multimodal context
    # Default True preserves the existing behaviour for every
    # course; teachers opt OUT for courses where images aren't
    # appropriate (e.g. text-only language drills, exam practice
    # without diagrams).
    tutoring_images_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Whether the AI tutor may show or reference images during "
            "tutoring sessions. Off = pure text. Default on."
        ),
    )

    # 2026-05-18 — teachers opt OUT of LessonPrerequisite gating for
    # courses where students should be free to jump around (review
    # courses, exam-prep collections, demo content). When False,
    # check_lesson_prerequisites short-circuits to "met" and the
    # catalog renders every lesson as unlocked. Default on — preserves
    # existing curriculum behavior.
    prerequisites_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Whether lessons in this course enforce LessonPrerequisite "
            "gating. Off = all lessons unlocked regardless of prior "
            "mastery. Default on."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title']

    MATH_KEYWORDS = (
        'math', 'maths', 'mathematics',
        'algebra', 'geometry', 'calculus', 'trigonometry',
        'arithmetic', 'fraction', 'fractions', 'decimal', 'decimals',
        'percentage', 'percentages',
        'probability', 'statistics',
    )

    @property
    def is_math(self):
        if self.subject_type:
            return self.subject_type == self.SubjectType.MATH
        # Legacy fallback for unclassified courses.
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
    terminal_objectives = models.JSONField(
        default=list,
        blank=True,
        help_text="Unit-level terminal objectives — what students must achieve by end of unit"
    )
    enabling_objectives = models.JSONField(
        default=list,
        blank=True,
        help_text="Unit-level enabling objectives — granular teaching steps from the syllabus"
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
    # NOTE (C6): MasteryRule enum + Lesson.mastery_rule field removed
    # 2026-04-25. Mastery is exit-ticket-driven (see
    # memory/lesson_competency_plan.md). Lesson.mastery_rule was a dead
    # field never consulted by tutor logic.

    class ContentStatus(models.TextChoices):
        EMPTY = 'empty', 'Empty'
        GENERATING = 'generating', 'Generating'
        READY = 'ready', 'Ready'
        # Layer 3 — content generated but with unresolved arithmetic
        # warnings. Lesson is still usable; teacher should review the
        # 🧮 Arithmetic Verification panel on the lesson detail page.
        READY_WITH_WARNINGS = 'ready_with_warnings', 'Ready with warnings'
        FAILED = 'failed', 'Failed'

    class ContentQuality(models.TextChoices):
        TIER_1 = 'tier_1', 'Tier 1 - Fully Resourced'
        TIER_2 = 'tier_2', 'Tier 2 - Syllabus + Exam'
        TIER_3 = 'tier_3', 'Tier 3 - Syllabus Only'
        TIER_4 = 'tier_4', 'Tier 4 - Framework Only'

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
        default=20,
        help_text="Estimated time to complete"
    )
    order_index = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=False)
    content_status = models.CharField(
        max_length=20,
        choices=ContentStatus.choices,
        default=ContentStatus.EMPTY,
        help_text="Status of generated content for this lesson"
    )

    # Enabling objectives — the specific teaching steps for this lesson
    enabling_objectives = models.JSONField(
        default=list,
        blank=True,
        help_text="List of enabling objectives this lesson covers"
    )

    # Content quality tier (P1.4)
    content_quality = models.CharField(
        max_length=10,
        choices=ContentQuality.choices,
        default=ContentQuality.TIER_3,
        help_text="Content quality tier based on available source materials"
    )
    teacher_approved = models.BooleanField(
        default=False,
        help_text="Teacher has reviewed and approved this content (required for tier_3/tier_4)"
    )
    teacher_approved_at = models.DateTimeField(null=True, blank=True)
    teacher_approved_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='approved_lessons',
    )

    # Flexible metadata (key concepts, skills, image suggestions, etc.)
    metadata = models.JSONField(default=dict, blank=True)

    # Group lessons (G1). When True, students can start this lesson as a
    # group — multiple students share one device and answer collectively.
    # See memory/group_lessons_plan.md.
    allow_group_mode = models.BooleanField(
        default=True,
        help_text="Whether students may complete this lesson together as a group.",
    )
    max_group_size = models.PositiveIntegerField(
        default=4,
        help_text="Maximum number of students allowed in a group session for this lesson.",
    )
    group_requires_approval = models.BooleanField(
        default=False,
        help_text=(
            "When True, group sessions for this lesson are blocked until a "
            "teacher approves them. Prevents students adding many friends "
            "just to skip the lesson. See memory/group_lessons_v2_plan.md."
        ),
    )

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

    class Priority(models.IntegerChoices):
        REQUIRED = 1, 'Required'      # Always included regardless of duration
        CORE = 2, 'Core'              # Included by default; first to drop on tight time
        ENRICHMENT = 3, 'Enrichment'  # Extra depth; dropped on short sessions

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
    # Priority drives runtime step selection by the tutor engine.
    # Lessons are generated at max depth (10 steps); the engine picks
    # a subset based on the session's target duration without needing
    # any LLM regeneration. Required steps stay; core drop first on
    # tight time; enrichment drops earliest. See
    # memory/max_depth_lesson_steps_plan.md.
    priority = models.PositiveSmallIntegerField(
        choices=Priority.choices,
        default=Priority.REQUIRED,
        help_text=(
            "Step priority for runtime step selection. 1=required (always "
            "included), 2=core (included by default), 3=enrichment "
            "(dropped on short sessions). Legacy steps default to 1 so "
            "existing lessons render unchanged."
        ),
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
    enabling_objective = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text="The specific enabling objective this step addresses"
    )

    # =========================================================================
    # CONTENT-QUALITY JUDGE OUTPUTS
    # =========================================================================
    # Populated by apps/curriculum/content_judges/ after generation.
    # Keyed by judge name (e.g. 'factual_step'); each value carries
    # passed / violations / reasoning / recommended_fix / provider /
    # model_name / skipped / skip_reason. Mirrors SessionTurn.judge_outputs.
    # Read by lesson detail UI to show quality badges; consumed by the
    # Q2 regen ensemble to decide whether to rewrite.
    # Also stores `regen_audit` (when content_regen ran): list of cycle
    # records with model_used, temperature, judge_violations, candidate
    # text preview, picked. See apps/curriculum/content_regen/.
    judge_outputs = models.JSONField(
        default=dict,
        blank=True,
        help_text="Content-judge verdicts keyed by judge name (factual_step, ...)"
    )

    # Quality state machine driven by the Q2 content_regen flow.
    # - unreviewed: judges haven't run (legacy / pre-Q1 steps)
    # - auto_ok:    judges passed (possibly after regen)
    # - auto_flagged: judges still failed after the regen cap was hit
    #                 → step is saved BUT surfaces a "needs human review"
    #                 badge so a teacher addresses it. NO SILENT FAILURE.
    # - human_approved: teacher reviewed and accepted as-is (Q3)
    # - human_edited:   teacher edited the content (Q3)
    class ContentQualityStatus(models.TextChoices):
        UNREVIEWED = 'unreviewed', 'Unreviewed'
        AUTO_OK = 'auto_ok', 'Auto-approved by judges'
        AUTO_FLAGGED = 'auto_flagged', 'Needs human review'
        HUMAN_APPROVED = 'human_approved', 'Human-approved'
        HUMAN_EDITED = 'human_edited', 'Human-edited'

    content_quality_status = models.CharField(
        max_length=20,
        choices=ContentQualityStatus.choices,
        default=ContentQualityStatus.UNREVIEWED,
        help_text=(
            "Quality gate state. 'auto_flagged' means judges + regen "
            "couldn't fix the content — surfaces a 'Needs Human Review' "
            "badge in the lesson detail UI. See "
            "memory/content_quality_review_plan.md."
        ),
    )

    # Per-item human-review audit trail (task #215, 2026-05-18). Set
    # when a teacher uses the "Mark as Reviewed" button on a flagged
    # step. Used to show who approved + when in the quality dashboard
    # and to filter out approved items from "unreviewed" counts.
    reviewed_by = models.ForeignKey(
        'auth.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        help_text="Teacher who marked this step as reviewed.",
    )
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the step was marked as reviewed by a human.",
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


class SeychellesContext(models.Model):
    """
    Structured local context library for Seychelles.

    Stores factual data about the Seychelles (economic, geographic, trade, etc.)
    that is injected into LLM prompts during content generation and tutoring.
    Admin-editable so data can be updated without code changes.
    """
    class Category(models.TextChoices):
        ECONOMIC = 'economic', 'Economic'
        GEOGRAPHIC = 'geographic', 'Geographic'
        TRADE = 'trade', 'Trade'
        CLIMATE = 'climate', 'Climate'
        POPULATION = 'population', 'Population'
        SUSTAINABLE_DEV = 'sustainable_dev', 'Sustainable Development'
        OS_MAP = 'os_map', 'OS Map Data'
        INDUSTRY = 'industry', 'Industry'

    category = models.CharField(max_length=30, choices=Category.choices)
    title = models.CharField(max_length=200)
    content = models.TextField(help_text="The factual content or data point")
    subject_tags = models.JSONField(
        default=list,
        blank=True,
        help_text="Subjects this is relevant to, e.g. ['geography', 'economics']"
    )
    grade_levels = models.JSONField(
        default=list,
        blank=True,
        help_text="Applicable grade levels, e.g. ['S1', 'S2', 'S3']"
    )
    source = models.CharField(
        max_length=200,
        blank=True,
        help_text="Data source or attribution"
    )
    is_active = models.BooleanField(default=True)
    locale = models.CharField(
        max_length=10,
        default='en-us',
        db_index=True,
        help_text="Course locale this fact applies to (e.g. 'en-us', 'pt-mz'). "
                  "Only injected into prompts for matching-locale content, so "
                  "Seychelles facts don't leak into Mozambique courses.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'title']
        verbose_name_plural = 'Seychelles context entries'

    def __str__(self):
        return f"[{self.get_category_display()}] {self.title} ({self.locale})"

    @classmethod
    def for_locale(cls, locale, *, active_only=True):
        """Active context entries scoped to a course locale (default en-us).

        The local-context library is country-specific: an en-us (Seychelles)
        course must not be grounded in pt-mz facts and vice-versa. Entries
        are tagged with the locale they describe; nothing is returned for a
        locale with no curated facts yet (e.g. pt-mz until seeded)."""
        qs = cls.objects.filter(locale=(locale or 'en-us').lower())
        if active_only:
            qs = qs.filter(is_active=True)
        return qs


# ─── Curriculum knowledge base — pgvector replacement for ChromaDB ─────
# Each row is a chunk (curriculum text, exam question, figure caption,
# teaching material excerpt) with a sentence-transformers embedding
# stored in a pgvector ``vector(384)`` column on Postgres (TEXT on
# SQLite). See memory/pgvector_migration_plan.md for the full design.
import hashlib  # noqa: E402

from apps.curriculum.vector_field import VectorField  # noqa: E402


class CurriculumChunk(models.Model):
    """One semantically-searchable curriculum chunk.

    Backs ``CurriculumKnowledgeBase`` on Postgres + pgvector. Replaces
    the per-institution ChromaDB collections that lived under
    ``media/vectordb/institution_<N>/``.

    Embedding model: sentence-transformers ``all-MiniLM-L6-v2`` (384 d).
    Distance metric: cosine (HNSW index built with ``vector_cosine_ops``
    in the migration). Same model + metric as the pre-migration
    ChromaDB, so existing vectors carry over without re-embedding.

    Multi-tenancy: every chunk has ``institution_id``. ``0`` is the
    normalised "platform-wide" bucket (matches the
    ``GLOBAL_INSTITUTION_ID = 0`` convention used by the previous
    ChromaDB path layout). The KB layer's two-tier query
    (``query_with_global_fallback``) unions per-institution + global
    hits, weights, and dedupes by content hash.
    """

    # ── Content + embedding ──────────────────────────────────────────
    content = models.TextField(
        help_text="The chunk text. Used as the source for embeddings + as the retrieved evidence string."
    )
    embedding = VectorField(
        dimensions=384,
        help_text="sentence-transformers all-MiniLM-L6-v2 vector. ``text`` column on SQLite (no vector ops)."
    )
    # SHA-256 of content. Combined with institution_id forms the dedup
    # key — re-indexing the same source file replaces (rather than
    # duplicates) existing chunks.
    content_hash = models.CharField(max_length=64, db_index=True)

    # ── Scoping ──────────────────────────────────────────────────────
    institution_id = models.IntegerField(
        db_index=True,
        help_text="0 = platform-wide (normalised). Otherwise an accounts.Institution PK. Not a FK — kept loose so chunks survive institution deletes."
    )

    # ── Common chunk metadata (set on every chunk) ───────────────────
    subject = models.CharField(max_length=100, blank=True, default='')
    grade_level = models.CharField(max_length=64, blank=True, default='')
    section = models.CharField(max_length=512, blank=True, default='')
    chunk_type = models.CharField(
        max_length=64, blank=True, default='',
        help_text="content / objective / teaching_strategy / assessment / general / exam_question / marking_scheme / figure_description"
    )
    source_file = models.CharField(max_length=512, blank=True, default='')
    upload_id = models.IntegerField(
        null=True, blank=True, db_index=True,
        help_text="dashboard.TeachingMaterialUpload or curriculum.CurriculumUpload PK. Loose link."
    )

    # ── Teaching-material extras ─────────────────────────────────────
    source_type = models.CharField(
        max_length=64, blank=True, default='',
        help_text="teaching_material / question_bank / curriculum"
    )
    material_type = models.CharField(
        max_length=64, blank=True, default='',
        help_text="textbook / worksheet / question_bank / reference / notes / other"
    )
    material_title = models.CharField(max_length=512, blank=True, default='')

    # ── Question-bank extras ─────────────────────────────────────────
    question_number = models.IntegerField(null=True, blank=True)
    question_type = models.CharField(max_length=32, blank=True, default='')
    has_answers = models.BooleanField(default=False)
    year = models.CharField(max_length=16, blank=True, default='')
    paper_number = models.CharField(max_length=64, blank=True, default='')

    # ── Figure extras ────────────────────────────────────────────────
    figure_type = models.CharField(max_length=64, blank=True, default='')
    figure_page = models.IntegerField(null=True, blank=True)
    figure_number = models.CharField(max_length=64, blank=True, default='')
    figure_image_url = models.URLField(max_length=1024, blank=True, default='')

    # ── Timestamps ───────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Re-indexing the same content for the same institution
            # updates instead of inserting. Aligned with ChromaDB's
            # upsert-by-id semantics in the previous implementation.
            models.UniqueConstraint(
                fields=['institution_id', 'content_hash'],
                name='curriculum_chunk_unique_per_institution',
            ),
        ]
        indexes = [
            models.Index(fields=['institution_id', 'chunk_type'],
                         name='cchunk_inst_type_idx'),
            models.Index(fields=['institution_id', 'subject', 'grade_level'],
                         name='cchunk_inst_subj_grade_idx'),
            # HNSW index on ``embedding`` is added in a follow-up RunSQL
            # operation in the migration — Django's index DSL can't
            # express pgvector's ``USING hnsw (... vector_cosine_ops)``
            # form, and the index must be skipped on SQLite anyway.
        ]

    def __str__(self):
        return (
            f"CurriculumChunk(inst={self.institution_id} "
            f"type={self.chunk_type!r} len={len(self.content)})"
        )

    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


class ContentPackVersion(models.Model):
    """A built offline content pack for one institution.

    Server-side bookkeeping only — the device records what it imported in
    ``apps.desktop.DeviceState``. Deliberately NOT the same thing as
    ``tutoring.LessonPackVersion``, which is the per-lesson JSON bundle for
    the React Native client: different scope, consumer, and lifecycle.

    See memory/desktop_offline_app_plan.md and apps/desktop/packs.py.
    """

    institution = models.ForeignKey(
        'accounts.Institution', on_delete=models.CASCADE,
        related_name='content_pack_versions',
    )
    version = models.PositiveIntegerField(
        help_text="Monotonic per institution. Devices refuse to go backwards.")
    built_at = models.DateTimeField(auto_now_add=True)
    schema_rev = models.CharField(
        max_length=40,
        help_text="Fingerprint of the migration graph this pack was built "
                  "against. An importer on a different schema refuses.",
    )
    checksum = models.CharField(max_length=64, help_text="sha256 of the archive.")
    size_bytes = models.BigIntegerField(default=0)
    lesson_count = models.PositiveIntegerField(default=0)
    chunk_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [('institution', 'version')]
        ordering = ['-version']

    def __str__(self) -> str:
        return f'{self.institution:.30} pack v{self.version}'


# ─── Content-quality benchmark models (Q5) ─────────────────────────────
# Lives in a separate module for clarity but imported here so Django
# discovers it via the curriculum app's models package.
from apps.curriculum.quality_models import (  # noqa: E402, F401
    ContentEditEvent,
    ContentEditTag,
)
