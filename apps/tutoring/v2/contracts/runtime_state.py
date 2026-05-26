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

from datetime import datetime
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
    PRE_POSE_TOKEN = "pre_pose_token"


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
    jaccard_signature: str = ""
    visible_context_at_pose: VisibleContextSnapshot = Field(
        default_factory=VisibleContextSnapshot
    )
    posed_at: datetime = Field(default_factory=datetime.utcnow)


class PosedQuestionLedgerEntry(BaseModel):
    """An entry in the in-session posed-question ledger.

    Drives ``in_session_repeat_guard()`` (Phase 1 §4.3 / Phase 2 §2.1.1).
    """

    source: QuestionSource
    id: int
    jaccard_signature: str
    posed_at: datetime = Field(default_factory=datetime.utcnow)


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
    unverified: int = 0
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
    jaccard_signature: str
    visible_context: VisibleContextSnapshot
    token: Optional[str] = None  # set when this pose came from a pre_pose_token


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

    # In-session repeat avoidance — Jaccard signatures + source/id.
    posed_question_ledger: list[PosedQuestionLedgerEntry] = Field(default_factory=list)

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

    # Tracks consecutive grader 'unverified' verdicts so conformance
    # can escalate (Phase 2 §2.4).
    unverified_run_length: int = 0

    # Hard caps (Phase 2 §2.3, §7 item 3).
    safety_valve_counters: SafetyValveCounters = Field(
        default_factory=SafetyValveCounters
    )

    # Resume artifact preservation (Phase 3 §3.4 test).
    resume_marker: ResumeMarker = Field(default_factory=ResumeMarker)

    # Additive — bare-answer signal counters (Phase 1 §2 / §2.1.1).
    bare_answer_counts_by_objective: dict[str, int] = Field(default_factory=dict)

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
