"""Content-quality benchmark models (Q5 of the content quality plan).

Captures every teacher edit to a generated piece of content (lesson
step text, exit-ticket MCQ, image). The benchmark serves three goals:

  1. **Quality measurement** — quantify how often teachers correct
     each piece of content, sliced by judge verdict at gen time.
  2. **Baseline benchmark** — frozen edit set becomes the test
     fixture for prompt-tuning A/B (run new prompt → check if it
     produces content that survives edits).
  3. **Prompt iteration** — high-frequency tags surface where the
     generation prompts (or judges) need tightening.

Mirrors `apps/benchmark/` shape but for STATIC generated content
rather than live tutor turns. Lives as a module under the curriculum
app rather than a separate Django app — single content type, single
admin scope, no need for app-level isolation yet (Rule of Three).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models

from ai_tutor.apps.curriculum.models import Lesson


class ContentEditTag(models.TextChoices):
    """Controlled vocabulary for tagging WHY a teacher edited a piece
    of generated content.

    Maps loosely to the judge violation codes but at a HUMAN level —
    a teacher tagging FACTUAL_INCORRECT might be addressing what the
    factual_step judge would call STEP_FACT_CONTRADICTED, or what the
    figure_alignment judge would call FIGURE_FACTUAL_ERROR. The tag
    surface is intentionally smaller than the judge surface so
    cross-cutting tags work across content types.

    Adding a tag here = update the autopopulate.py mapping table too.
    """
    FACTUAL_INCORRECT = 'FACTUAL_INCORRECT', 'Factual error'
    MISLEADING_IMAGE = 'MISLEADING_IMAGE', 'Misleading image'
    WRONG_ANSWER_KEY = 'WRONG_ANSWER_KEY', 'Wrong answer key'
    AMBIGUOUS_QUESTION = 'AMBIGUOUS_QUESTION', 'Ambiguous question'
    OFF_TOPIC = 'OFF_TOPIC', 'Off-topic / off-objective'
    WRONG_GRADE_LEVEL = 'WRONG_GRADE_LEVEL', 'Wrong grade level / readability'
    POOR_PEDAGOGY = 'POOR_PEDAGOGY', 'Poor pedagogy'
    CULTURAL_MISFIT = 'CULTURAL_MISFIT', 'Cultural / contextual misfit'
    FORMAT_ISSUE = 'FORMAT_ISSUE', 'Formatting / readability issue'
    OTHER = 'OTHER', 'Other'


class ContentEditEvent(models.Model):
    """One teacher edit to a generated content piece.

    Created by the capture hooks in dashboard views (step_edit,
    exit_question_edit, *_save_regen, regenerate_media) when the
    teacher submits a change. before_payload + after_payload are
    SHALLOW snapshots of the relevant fields — enough to render a
    diff but not the entire model.

    `suggested_tags` are auto-derived by autopopulate.derive_suggested_tags
    at create time. `error_tags` is set by the teacher (later) via the
    admin detail page; defaults to the suggested_tags so a teacher
    who doesn't review still gets reasonable benchmark labels.
    """

    class ContentType(models.TextChoices):
        STEP = 'step', 'Lesson Step'
        EXIT_QUESTION = 'exit_question', 'Exit-Ticket Question'
        IMAGE = 'image', 'Media Image'

    class Source(models.TextChoices):
        # How the edit happened. Useful for slicing benchmark metrics
        # by "what driver of edits do we see most?"
        MANUAL_EDIT = 'manual_edit', 'Manual edit (teacher typed)'
        AI_REGEN_AUTO = 'ai_regen_auto', 'AI regen (auto-review mode)'
        AI_REGEN_PROMPT = 'ai_regen_prompt', 'AI regen (prompt mode)'
        IMAGE_REGEN = 'image_regen', 'Image regenerated'

    content_type = models.CharField(
        max_length=20,
        choices=ContentType.choices,
        help_text="Type of content edited"
    )
    content_id = models.PositiveIntegerField(
        help_text="PK of LessonStep / ExitTicketQuestion / MediaAsset"
    )
    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='content_edit_events',
        help_text="Parent lesson — kept on SET_NULL so events survive course deletion."
    )

    # Frozen snapshots of the relevant fields, pre and post edit.
    # Shape varies by content_type:
    #   step:           {'teacher_script': str, 'question': str|None, ...}
    #   exit_question:  {'question_text': str, 'option_a-d': str, 'correct_answer': str, ...}
    #   image:          {'url': str, 'description': str|None, 'caption': str|None}
    # Always wide enough to render a diff; never the entire model.
    before_payload = models.JSONField(
        default=dict, blank=True,
        help_text="Frozen state pre-edit (for diff + replay)"
    )
    after_payload = models.JSONField(
        default=dict, blank=True,
        help_text="Frozen state post-edit"
    )

    # Auto-derived suggestions (autopopulate.derive_suggested_tags at
    # create time) vs the teacher's confirmed tags. Both are lists of
    # ContentEditTag.value strings.
    suggested_tags = models.JSONField(
        default=list, blank=True,
        help_text="Auto-derived tag suggestions from diff + judge_outputs"
    )
    error_tags = models.JSONField(
        default=list, blank=True,
        help_text="Teacher-confirmed tags. Defaults to suggested_tags."
    )

    teacher_notes = models.TextField(
        blank=True, default='',
        help_text="Free-form note the teacher left about the edit."
    )

    # Snapshot of judge_outputs on the edited row at the moment of
    # edit — lets the benchmark answer "did the AI judge predict this
    # edit?" without re-querying the row's current state (which may
    # have been re-edited).
    judge_outputs_at_edit = models.JSONField(
        default=dict, blank=True,
        help_text="judge_outputs snapshot at edit time (judge_predicts_edit analysis)"
    )

    source = models.CharField(
        max_length=30,
        choices=Source.choices,
        default=Source.MANUAL_EDIT,
        help_text="How this edit was triggered"
    )

    edited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='content_edit_events',
        help_text="Teacher who made the edit (null when system-driven)"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['content_type', 'content_id']),
            models.Index(fields=['lesson', '-created_at']),
            models.Index(fields=['-created_at']),
        ]
        verbose_name = "Content Edit Event"
        verbose_name_plural = "Content Edit Events"

    def __str__(self):
        return (
            f"{self.get_content_type_display()} #{self.content_id} "
            f"edited at {self.created_at:%Y-%m-%d %H:%M}"
        )

    @property
    def tags_human(self) -> list[str]:
        """Render error_tags using the human-readable labels from
        ContentEditTag.choices for display."""
        labels = dict(ContentEditTag.choices)
        return [labels.get(t, t) for t in (self.error_tags or [])]
