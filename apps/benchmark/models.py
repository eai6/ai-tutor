"""Benchmark data model.

Two tables:

- ``BenchmarkItem`` — frozen snapshot of one tutor turn at sampling time.
  Carries the conversation history, student turn, production tutor
  response, and pipeline trace (from ``SessionTurn.metadata`` +
  ``SessionTurn.judge_outputs``). Never re-derived.

- ``BenchmarkAnnotation`` — per-(item, system_variant, annotator) labels.
  One ``BenchmarkItem`` can have many annotations: one per system variant
  (e.g. ``production_v1``, ``stripped_single_llm``, ``full_orchestrator``)
  AND one per annotator (Edward + LLM-judge cross-check). Computed verdict
  is derived from set equality between actual_labels and expected_labels.

See ``memory/eval_benchmark_v2_simplified.md`` for the locked spec.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.benchmark.labels import (
    ALL_LABELS,
    ACTION_LABELS,
    ISSUE_LABELS,
    FAILURE_CATEGORIES,
)
from apps.benchmark.pedagogy import choices_for as _pedagogy_choices


class BenchmarkItem(models.Model):
    """One frozen tutor turn from a production session.

    ``snapshot`` carries the full item per the v2 schema:
        {
          "item": {item_id, subject, lesson_id, lesson_objective,
                   conversation_history[], student_turn},
          "production": {tutor_response, pipeline_trace}
        }

    Stored as JSON so re-running the same item through a modified tutor
    only updates the production block (computed) and the verdict; the
    item block is immutable per the v2 plan's iteration-loop spec.
    """

    class Subject(models.TextChoices):
        MATH = 'math', 'Math'
        GEOGRAPHY = 'geography', 'Geography'
        SCIENCE = 'science', 'Science'
        OTHER = 'other', 'Other'

    item_id = models.CharField(
        max_length=80, unique=True,
        help_text="Stable benchmark item identifier, e.g. 'MATH_S18_T456'.",
    )
    source_turn = models.ForeignKey(
        'tutoring.SessionTurn',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='benchmark_items',
        help_text="The tutor SessionTurn this item snapshots. Nullable to "
                  "preserve the snapshot even if the source session is deleted.",
    )
    subject = models.CharField(
        max_length=20, choices=Subject.choices, default=Subject.MATH,
    )
    lesson_id = models.IntegerField(
        help_text="Lesson PK at sampling time (lesson may be deleted later).",
    )
    snapshot = models.JSONField(
        help_text="Frozen item per memory/eval_benchmark_v2_simplified.md: "
                  "{item: {...}, production: {...}}.",
    )

    # Stratification + sampling metadata
    stratum = models.CharField(
        max_length=40, blank=True,
        help_text="Stratification bucket from the sampling command, e.g. "
                  "'wrong_answer', 'validator_flagged', 'step_transition'.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='benchmark_items_created',
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Benchmark Item"
        indexes = [
            models.Index(fields=['subject', 'created_at']),
            models.Index(fields=['stratum']),
        ]

    def __str__(self):
        return f"{self.item_id} ({self.subject})"


class BenchmarkAnnotation(models.Model):
    """One annotation of a benchmark item by one annotator for one system variant.

    Multiple annotations per item are expected: Edward + LLM-judge
    cross-check (two annotators), or production_v1 + stripped + orchestrator
    (three system variants). The verdict is computed from set equality
    between ``actual_labels`` and ``expected_labels`` plus ``safety_concern``.
    """

    class SystemVariant(models.TextChoices):
        PRODUCTION_V1 = 'production_v1', 'Production (current)'
        STRIPPED = 'stripped', 'Stripped single-LLM (baseline)'
        ORCHESTRATOR = 'orchestrator', 'Full orchestrator'
        OTHER = 'other', 'Other'

    class Annotator(models.TextChoices):
        HUMAN = 'human', 'Human (Edward)'
        LLM_JUDGE = 'llm_judge', 'LLM judge (cross-check)'

    item = models.ForeignKey(
        BenchmarkItem, on_delete=models.CASCADE, related_name='annotations',
    )
    annotator_role = models.CharField(
        max_length=20, choices=Annotator.choices,
        help_text="Who produced this annotation: a human or the LLM-judge.",
    )
    annotator_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='benchmark_annotations',
        help_text="Human annotator (NULL for LLM-judge annotations).",
    )
    annotator_model = models.CharField(
        max_length=80, blank=True,
        help_text="Model identifier for LLM-judge annotations "
                  "(e.g. 'gemini-2.5-pro', 'gpt-5'). Empty for human.",
    )

    system_variant = models.CharField(
        max_length=40, choices=SystemVariant.choices,
        default=SystemVariant.PRODUCTION_V1,
        help_text="Which tutor system produced the response being annotated.",
    )

    # Annotation content (Edward authors / LLM-judge produces)
    student_claim_correct = models.BooleanField(
        null=True, blank=True,
        help_text="Ground truth: was the student's claim/answer actually "
                  "correct? Tri-state — null when no claim to evaluate.",
    )
    actual_labels = models.JSONField(
        default=list,
        help_text="Multi-select labels describing what the production "
                  "response actually did/contained. Subset of "
                  "apps.benchmark.labels.ALL_LABELS.",
    )
    expected_labels = models.JSONField(
        default=list,
        help_text="What a good response should be labeled with. Should "
                  "contain only ACTION_LABELS, never ISSUE_LABELS.",
    )
    safety_concern = models.BooleanField(
        default=False,
        help_text="Binary safety flag — separate from labels, never "
                  "auto-populated.",
    )
    rationale = models.TextField(
        blank=True,
        help_text="Free-text justification for the expected_labels choice.",
    )
    failure_categories = models.JSONField(
        default=list, blank=True,
        help_text="List of cluster tags from "
                  "apps.benchmark.labels.FAILURE_CATEGORIES. Multiple may "
                  "apply to one failure (e.g. an item with both "
                  "arithmetic_in_tutor AND incoherent_setup). Empty list "
                  "when the item passes.",
    )

    # Provenance + audit
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Benchmark Annotation"
        constraints = [
            # One annotation per (item, system_variant, annotator) tuple —
            # re-annotating the same triple updates the existing row via
            # update_or_create semantics in the (forthcoming) view code.
            models.UniqueConstraint(
                fields=['item', 'system_variant', 'annotator_role', 'annotator_user', 'annotator_model'],
                name='unique_item_variant_annotator',
            ),
        ]
        indexes = [
            models.Index(fields=['system_variant', 'annotator_role']),
        ]

    def __str__(self):
        who = self.annotator_user_id or self.annotator_model or self.annotator_role
        return f"{self.item.item_id} / {self.system_variant} / {who}"

    # -----------------------------------------------------------------
    # Computed verdict (cheap; recompute on save vs caching is fine)
    # -----------------------------------------------------------------

    @property
    def actual_set(self) -> frozenset[str]:
        return frozenset(self.actual_labels or [])

    @property
    def expected_set(self) -> frozenset[str]:
        return frozenset(self.expected_labels or [])

    @property
    def missing_labels(self) -> list[str]:
        """expected - actual: things the response failed to do."""
        return sorted(self.expected_set - self.actual_set)

    @property
    def extra_labels(self) -> list[str]:
        """actual - expected: usually surfaces issue labels."""
        return sorted(self.actual_set - self.expected_set)

    @property
    def passes(self) -> bool:
        """Computed pass: actual labels match expected AND no safety concern."""
        if self.safety_concern:
            return False
        return self.actual_set == self.expected_set


class BenchmarkRun(models.Model):
    """One scoring computation over a slice of ``BenchmarkAnnotation`` rows.

    Versioned so we can compare runs across iterations of the tutor
    pipeline. Each prompt or judge change → re-run scoring → compare
    pass rate against the prior run. The ``metrics`` JSONField stores
    the full per-slice breakdown (subject, eval_layer, failure_category,
    history-aware vs not, etc.) — kept here rather than in normalized
    rows because the slice dimensions evolve with the rubric.

    See ``apps/benchmark/scoring.py::compute_metrics`` for the schema.
    """

    system_variant = models.CharField(
        max_length=40,
        choices=BenchmarkAnnotation.SystemVariant.choices,
        default=BenchmarkAnnotation.SystemVariant.PRODUCTION_V1,
        help_text="Which tutor system's annotations were scored.",
    )
    annotator_role = models.CharField(
        max_length=20,
        choices=BenchmarkAnnotation.Annotator.choices,
        default=BenchmarkAnnotation.Annotator.HUMAN,
        help_text="Whose annotations were scored: human or LLM-judge.",
    )

    total_items = models.PositiveIntegerField(default=0)
    passed = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    metrics = models.JSONField(
        default=dict,
        help_text="Full per-slice breakdown. See "
                  "apps/benchmark/scoring.py::compute_metrics for the schema.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='benchmark_runs',
    )
    notes = models.TextField(
        blank=True,
        help_text="Free-form context for this run (e.g. \"after coherence "
                  "judge history window shipped\").",
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Benchmark Run"
        indexes = [
            models.Index(fields=['system_variant', '-created_at']),
            models.Index(fields=['annotator_role']),
        ]

    def __str__(self):
        return (
            f"Run #{self.id} ({self.system_variant}/{self.annotator_role}) "
            f"{self.passed}/{self.total_items}"
        )

    @property
    def pass_rate(self) -> float:
        if not self.total_items:
            return 0.0
        return self.passed / self.total_items


# ─── Session-level pedagogical evaluation ──────────────────────────────
#
# Separate from BenchmarkItem/BenchmarkAnnotation above, which annotate ONE
# TURN against our 30-label internal rubric. These two annotate a WHOLE
# SESSION against the eight-dimension taxonomy of Maurya et al. (NAACL 2025).
# Different unit, different rubric, different question — so different tables
# rather than overloading the existing ones.
#
# Plan: memory/session_eval_framework_plan.md

class SessionEvalItem(models.Model):
    """One production session, redacted and safety-screened, awaiting judgement.

    The transcript is FROZEN at sampling time and stored redacted. It is never
    re-derived from the live session, for two reasons: the source rows can
    change or be deleted, and re-deriving would re-expose the un-redacted text
    that the whole pipeline exists to keep away from an annotator.
    """

    class Status(models.TextChoices):
        PENDING_REVIEW = 'pending_review', 'Awaiting child-protection review'
        APPROVED = 'approved', 'Approved for annotation'
        REJECTED = 'rejected', 'Rejected — not safe to annotate'

    item_id = models.CharField(
        max_length=64, unique=True,
        help_text="Stable identifier, e.g. 'SESS_MATH_1014'.",
    )
    source_session = models.ForeignKey(
        'tutoring.TutorSession', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='eval_items',
        help_text='Nullable so a deleted session does not take the evaluation '
                  'record with it — the frozen transcript remains valid.',
    )
    # Salted hash of the session id. Exports carry this and never the real id,
    # so a released dataset cannot be joined back to a student.
    session_key = models.CharField(max_length=32, db_index=True)

    subject = models.CharField(max_length=32, blank=True, default='')
    lesson_id = models.IntegerField(null=True, blank=True)
    engine = models.CharField(
        max_length=16, blank=True, default='',
        help_text="Which tutoring engine produced it ('v1' / 'simple'). The v1 "
                  'engine interpolated the student name into its prompt, so '
                  'this materially changes redaction risk.',
    )
    outcome = models.CharField(
        max_length=32, blank=True, default='',
        help_text='passed_exit_ticket / failed_exit_ticket / no_exit_ticket.',
    )
    turn_count = models.PositiveIntegerField(default=0)

    transcript = models.JSONField(
        default=list,
        help_text='REDACTED transcript: [{turn, role, content}].',
    )
    redaction_report = models.JSONField(
        default=dict,
        help_text='What was replaced and what the residual scan found. Kept so '
                  'a reviewer can see WHY an item was cleared, not just that '
                  'it was.',
    )

    status = models.CharField(
        max_length=20, choices=Status.choices,
        default=Status.PENDING_REVIEW, db_index=True,
    )
    reject_reason = models.TextField(blank=True, default='')
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    stratum = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'subject'])]

    def __str__(self):
        return f'{self.item_id} ({self.status})'

    @property
    def is_annotatable(self) -> bool:
        """Only an approved item may be shown to an annotator."""
        return self.status == self.Status.APPROVED


class SessionEvalAnnotation(models.Model):
    """One annotator's eight-dimension verdict on one session."""

    class Annotator(models.TextChoices):
        HUMAN = 'human', 'Human'
        LLM_JUDGE = 'llm_judge', 'LLM judge'

    item = models.ForeignKey(
        SessionEvalItem, on_delete=models.CASCADE, related_name='annotations',
    )
    annotator_role = models.CharField(
        max_length=16, choices=Annotator.choices, default=Annotator.HUMAN,
    )
    annotator_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='session_eval_annotations',
    )
    annotator_model = models.CharField(max_length=64, blank=True, default='')

    # One field per dimension. Choices come from pedagogy.py so the form, the
    # database and the scorer cannot disagree about the taxonomy.
    mistake_identification = models.CharField(
        max_length=20, blank=True, default='',
        choices=_pedagogy_choices('mistake_identification'))
    mistake_location = models.CharField(
        max_length=20, blank=True, default='',
        choices=_pedagogy_choices('mistake_location'))
    revealing_answer = models.CharField(
        max_length=20, blank=True, default='',
        choices=_pedagogy_choices('revealing_answer'))
    providing_guidance = models.CharField(
        max_length=20, blank=True, default='',
        choices=_pedagogy_choices('providing_guidance'))
    actionability = models.CharField(
        max_length=20, blank=True, default='',
        choices=_pedagogy_choices('actionability'))
    coherence = models.CharField(
        max_length=20, blank=True, default='',
        choices=_pedagogy_choices('coherence'))
    tutor_tone = models.CharField(
        max_length=20, blank=True, default='',
        choices=_pedagogy_choices('tutor_tone'))
    human_likeness = models.CharField(
        max_length=20, blank=True, default='',
        choices=_pedagogy_choices('human_likeness'))

    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'annotator_role', 'annotator_user', 'annotator_model'],
                name='uniq_session_eval_annotation',
            ),
        ]

    def __str__(self):
        who = self.annotator_user or self.annotator_model or self.annotator_role
        return f'{self.item.item_id} by {who}'

    def values(self) -> dict:
        from apps.benchmark import pedagogy as P
        return {k: getattr(self, k, '') for k in P.DIMENSION_KEYS}

    @property
    def complete(self) -> bool:
        """Every dimension answered. An unanswered dimension is not a 'No'."""
        return all(bool(v) for v in self.values().values())

    @property
    def passes(self) -> bool:
        from apps.benchmark import pedagogy as P
        return P.session_passes(self.values())
