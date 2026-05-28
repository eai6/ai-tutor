"""SessionRuntimeState — typed runtime state for the v2 engine.

Replaces the untyped 40-key ``TutorSession.engine_state`` JSON blob with
a Pydantic model persisted to ``TutorSession.runtime_state`` (separate
column, no backfill). Schema fields are additive — new fields land as
schema migrations, not free-form keys.

Source: design/refactor/refactor-implementation-plan.md §7 item 6 +
Phase 1 §2 (with the additive ``bare_answer_counts_by_objective``
field for §2.1.1 bare-answer detection).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class QuestionSource(str, Enum):
    """Cross-table provenance for posed questions (Phase 1 §4.1).

    Resolves the ``bank_question_id`` ambiguity — integer IDs collide
    across ``LessonStep`` and ``ExitTicketQuestion``.
    """

    LESSON_STEP = "lesson_step"
    EXIT_TICKET_QUESTION = "exit_ticket_question"
    INLINE_GENERATED = "inline_generated"


class QuestionRef(BaseModel):
    """Typed reference to a posed question across tables (Phase 1 §4.1).

    Backend resolves to the correct row by ``source``.
    """

    model_config = ConfigDict(frozen=True)

    source: QuestionSource
    id: int

    def composite_key(self) -> str:
        """Stable string key for ``StudentProfile.asked_questions``."""
        return f"{self.source.value}:{self.id}"


class VisibleContextSnapshot(BaseModel):
    """The student-visible context at the moment a question was posed.

    Used by the state-coherence conformance check and by resume
    artifact preservation (Phase 3 §3.4 resume test).
    """

    model_config = ConfigDict(frozen=True)

    visible_prompt: str = ""
    attached_media_ids: list[int] = Field(default_factory=list)
    recent_transcript: list[str] = Field(default_factory=list)
    mcq_option_order: list[str] = Field(default_factory=list)


class OpenQuestion(BaseModel):
    """The currently-posed assessment question awaiting a student response.

    Written at Phase B commit only — Phase A validation produces a
    PendingPose; commit converts that into the OpenQuestion record.
    """

    source: QuestionSource
    id: int
    canonical: str = ""  # private — never surfaced to StudentTutor on wrong/partial
    rendered_stem: str = ""
    visible_context_at_pose: VisibleContextSnapshot = Field(
        default_factory=VisibleContextSnapshot
    )
    posed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ObjectiveProgress(BaseModel):
    """Per-objective evidence carried across turns.

    Keyed by enabling-objective slug in
    ``SessionRuntimeState.objective_progress``.
    """

    objective: str
    attempts: int = 0
    correct: int = 0
    wrong: int = 0
    partial: int = 0
    closed: bool = False


class RemediationState(BaseModel):
    """Tracks a remediation cycle for a named misconception."""

    misconception: str = ""
    fired_at_turn: Optional[int] = None
    attempts_since: int = 0
    resolved: bool = False


class SafetyValveCounters(BaseModel):
    """Hard caps to prevent runaway sessions (Phase 2 §2.3, §7 item 3).

    pivot / close_topic should fire first under normal conditions.
    """

    turns_in_session: int = 0
    turns_on_current_objective: int = 0
    verdictless_turns: int = 0


class BareAnswerCounters(BaseModel):
    """Counters for the bare-answer signal on the math path.

    Additive field beyond §7 item 6 to support §2.1.1 bare-answer
    detection. Keyed by enabling-objective slug. Not consumed by move
    selection — exists for future tuning (e.g., bare-answer rate as a
    difficulty signal).
    """

    counts_by_objective: dict[str, int] = Field(default_factory=dict)


class ResumeMarker(BaseModel):
    """Snapshot of where to pick up on resume."""

    last_turn_id: Optional[int] = None
    last_step_index: int = 0
    last_move: str = ""


class PendingPose(BaseModel):
    """Phase-A validated tool-call output, not yet committed.

    The two-phase commit (Phase 1 §4) flows:

    1. Phase A — tool call invokes ``validate_pose(...)`` which returns
       a ``PendingPose`` (read-only check of token + derivability +
       repeat guards). No mutation of ``SessionRuntimeState`` and the
       single-use token is NOT marked consumed.
    2. Phase B — after conformance approves the candidate response,
       ``ContextManager.commit_pending_pose(pending)`` consumes the
       token, appends to the ledger, and writes ``open_question``.

    On conformance retry the PendingPose is discarded — validation runs
    from scratch. On second failure / safe-template fallback no pose
    is ever committed.
    """

    model_config = ConfigDict(frozen=True)

    question_ref: QuestionRef
    canonical: str
    rendered_stem: str
    visible_context: VisibleContextSnapshot


class SessionRuntimeState(BaseModel):
    """Typed v2 session state — persisted to ``TutorSession.runtime_state``.

    Replaces the untyped legacy ``engine_state`` blob for sessions
    routed to the v2 engine. Legacy sessions keep using ``engine_state``;
    no migration / backfill (Phase 1 §2).
    """

    model_config = ConfigDict(validate_assignment=True)

    # Currently-posed question awaiting a student response.
    open_question: Optional[OpenQuestion] = None
    attempts_on_open_question: int = 0

    # Server-side pose-tool dedup ledger (v2-prune-plan step 4). The
    # new pose_question tool appends LessonStep.id here on each call
    # and excludes already-delivered ids when picking the next slot.
    delivered_lesson_step_ids: list[int] = Field(default_factory=list)

    # Per-objective evidence carry — populated by TutorEngine after each
    # graded turn (Phase 2). Keyed by enabling-objective slug.
    objective_progress: dict[str, ObjectiveProgress] = Field(default_factory=dict)

    # Media catalog entries already shown this session (avoid re-showing
    # the same figure on consecutive turns — Phase 2 / Phase 3 MediaService).
    media_shown: list[int] = Field(default_factory=list)

    # Remediation tracking for named misconceptions (Phase 2 move table).
    remediation_state: Optional[RemediationState] = None

    # Move-table state (Phase 2 §2.3).
    current_move: str = ""
    move_history: list[str] = Field(default_factory=list)

    # Hard caps (Phase 2 §2.3, §7 item 3).
    safety_valve_counters: SafetyValveCounters = Field(
        default_factory=SafetyValveCounters
    )

    # Resume artifact preservation (Phase 3 §3.4 test).
    resume_marker: ResumeMarker = Field(default_factory=ResumeMarker)

    # Additive — bare-answer signal counters (Phase 1 §2 / §2.1.1).
    bare_answer_counts_by_objective: dict[str, int] = Field(default_factory=dict)

    # Phase 4 — difficulty rung (-2..+2) under the redesign
    # (memory/v2_unverified_trap_redesign.md Fix 2b). Mutated by the
    # difficulty-signal endpoint via the v2 single-writer path; consumed
    # by move-selection / pose tooling to pick the rung-appropriate
    # bank slot. Default 0 (default rung). Legacy ``engine_state.
    # difficulty_level`` stays untouched for in-flight legacy sessions.
    difficulty_level: int = 0

    # Phase 4 — last out-of-band system event handled by the engine
    # (e.g. ``"difficulty_change:too_easy"``). Set by the entry-point
    # adapter (the difficulty-signal endpoint, future system actors) so
    # the trace / observability dashboard records WHY a turn fired
    # without a real student utterance. Empty for normal student turns.
    last_system_event: str = ""

    # Phase 4 — Active Learning invariant. Rolling 5-turn window of
    # whether the student attempted an answer (True) vs hedged / asked
    # for help / sent meta input (False). Populated from intent
    # classifier output. Consumed by move-selection to bias toward
    # lighter cognitive lift when doing-rate drops below 60%.
    # Principle #1 *Active Learning* (Ch.10).
    student_doing_rate_window: list[bool] = Field(default_factory=list)

    # ── Per-open-question + per-objective counters (Commit D §4.2) ───
    # The engine is the single writer; the router reads them via
    # ``build_router_request`` and the router prompt references them
    # by name. Reset on open-question change.
    wrong_attempts_on_open_question: int = 0
    partial_attempts_on_open_question: int = 0
    consecutive_wrong_on_open_question: int = 0
    # Per-objective; reset when current_objective changes.
    unscaffolded_correct_on_open_question_objective: int = 0
    # Rolling list of the last ≤10 verdict values ("correct" / "partial"
    # / "wrong"), oldest first. Engine caps in the writer — Pydantic v2
    # does not enforce caps inline.
    recent_verdicts: list[str] = Field(default_factory=list)

    # Schema version for forward-compat. Bump when adding additive
    # fields; ContextManager loaders tolerate older versions.
    schema_version: int = 1

    def to_jsonable(self) -> dict:
        """Serialize for JSONField storage."""
        return self.model_dump(mode="json")

    @classmethod
    def from_jsonable(cls, data: Optional[dict]) -> "SessionRuntimeState":
        """Hydrate from JSONField storage. Tolerates empty / None."""
        if not data:
            return cls()
        return cls.model_validate(data)
