"""Service-call contracts: TutoringContext, GradingRequest, GradingResult.

Phase 1 ships these as the data-shape boundary; Phase 2 fills in the
services that consume / produce them.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.tutoring.v2.contracts.runtime_state import (
    OpenQuestion,
    SessionRuntimeState,
)


class Verdict(str, Enum):
    """Grader verdict — strict ternary.

    Per v2-prune-plan §4.1 the grader MUST return one of
    CORRECT, PARTIAL, or WRONG for every gradable student turn.
    The router determines whether grading happens at all.
    """

    CORRECT = "correct"
    WRONG = "wrong"
    PARTIAL = "partial"


class StudentSafeFeedback(BaseModel):
    """Redacted feedback shape passed to StudentTutor on wrong/partial.

    The plumbing-level invariant (Phase 2 §2.2): the move prompt
    template has NO slot named ``canonical_answer`` for these moves.
    The canonical lives only on the parallel ``private_canonical``
    field of GradingResult and never reaches StudentTutor on
    non-correct verdicts.
    """

    what_right: str = ""
    what_missing: str = ""
    first_misconception_redacted: str = ""


class GradingRequest(BaseModel):
    """Inputs to StudentGrader.grade_student_response()."""

    model_config = ConfigDict(frozen=True)

    open_question: OpenQuestion
    student_input: str
    is_math: bool = False
    # Optional KB chunks for non-math grounded adjudication (Phase 2).
    kb_chunks: list[str] = Field(default_factory=list)


class GradingResult(BaseModel):
    """Output of StudentGrader.grade_student_response().

    Shape extends refactor-analysis §3 by one additive boolean
    ``bare_answer`` (Phase 2 §2.1.1). The flag is consumed by the
    selected move's *prompt*, not by move *selection* — move selection
    sees only ``verdict``.

    ``reason_code`` is the Two-LLM grader's structured diagnostic
    (design/tasks/two-llm-grader-implementation-plan.md §2.3 / §2.4):
    arithmetic_failed, conclusion_inconsistent_with_canonical,
    meta_input, state_inconsistent, grader_extraction_failed. The
    move layer can branch deterministically on this code; the human-
    readable ``reasoning`` string is still authoritative for logs.
    """

    verdict: Verdict
    private_canonical: str = ""  # never passed to StudentTutor on wrong/partial
    student_safe_feedback: StudentSafeFeedback = Field(
        default_factory=StudentSafeFeedback
    )
    student_value: str = ""
    reasoning: str = ""
    citation: str = ""
    bare_answer: bool = False
    reason_code: Optional[
        Literal[
            # math grader codes
            "arithmetic_failed",
            "conclusion_inconsistent_with_canonical",
            # non-math grader codes
            "self_reported_guess",
            "known_misconception",
            "denies_canonical",
            "off_topic",
            # shared codes
            "meta_input",
            "state_inconsistent",
            "grader_extraction_failed",
        ]
    ] = None


class TutoringContext(BaseModel):
    """Frozen snapshot of session context handed to a service call.

    Services are stateless — they receive frozen snapshots, not live
    state. ContextManager owns the read/write boundary against
    SessionRuntimeState (Phase 2 §2.7).
    """

    model_config = ConfigDict(frozen=True)

    session_id: int
    student_id: int
    institution_id: int
    lesson_id: int
    locale: str = "en"
    grade_level: str = ""
    institution_name: str = ""
    tutor_persona: str = ""
    client_kind: Literal["web", "mobile"] = "web"
    full_transcript: list[dict] = Field(default_factory=list)
    runtime_state: SessionRuntimeState
    profile_summary: str = ""  # last-persisted snapshot (R2)
    current_objective: str = ""
    # Lesson-level metadata surfaced into the shared preamble so the
    # LLM has a concrete subject + title to anchor on, instead of
    # falling back to a training-data prior ("S3 maths" on a geography
    # lesson). Default-empty for backwards compatibility with the
    # Phase 1 placeholder routing tests.
    lesson_title: str = ""
    lesson_subject: str = ""
    # Per-step pedagogy anchors lifted from the current LessonStep.
    # Surfaced so (a) the explain / worked_example move prompts can
    # ground their generation in lesson-authored content and (b) the
    # safe-terminal templates can deliver each move's pedagogy minimum
    # when the LLM-authored response fails conformance twice. Subject-
    # agnostic — empty for any step without these fields populated.
    current_step_teacher_script: str = ""
    current_step_worked_example: str = ""
    # True when the active step is the final step of the lesson.
    # Used by close_topic to phrase the transition correctly: more
    # steps remaining → "let's move on to <next>"; final step → "you're
    # ready for the exit ticket". Default True is the safe fallback for
    # contexts that don't know (mid-session legacy / mocked tests) —
    # treating a session as "final" never leaks half-done lessons.
    is_final_step: bool = True


# ──────────────────────────────────────────────────────────────────────
# Move Router — design/tasks/move-router-implementation-plan.md
# ──────────────────────────────────────────────────────────────────────


# Closed set of move names the router may pick. Mirrors the post-cutover
# move table in `apps/tutoring/v2/services/move_prompts.py` — every move
# except the deleted ``pose_question``. The router can never emit
# ``pose_question`` because there is no remaining case where "ask with
# no framing" is the right pedagogical move; the other 8 moves all end
# in a tool-posed question.
RouterMove = Literal[
    "confirm_and_advance",
    "confirm_and_extend",
    "scaffold_hint",
    "name_misconception",
    "worked_example",
    "explain",
    "pivot",
    "close_topic",
]


RouterCase = Literal[
    "answer_attempt",
    "help_request",
    "opening_turn",
    "forced_close",
]


class RouterRequest(BaseModel):
    """Inputs to ``MoveRouter.route()``.

    Built from ``TutoringContext`` + the runtime state at one site
    (``build_router_request``) so callers don't re-derive the snapshot
    at each call site. Frozen — services are stateless and receive
    immutable snapshots.

    Note (post-prune Commit D): the router runs FIRST on every turn,
    BEFORE the grader. The grader's ``verdict`` is therefore not in
    the request — the router's job is to decide whether the grader
    runs at all (``verdict_needed`` on its output) and what move to
    pick for each possible grader outcome on answer-attempt turns.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # ── conversation surface ─────────────────────────────────────────
    last_n_turns: list[dict] = Field(default_factory=list)
    student_input: str = ""

    # ── student / lesson context ─────────────────────────────────────
    profile_summary: str = ""
    objective: str = ""
    lesson_title: str = ""
    lesson_subject: str = ""
    lesson_step_teacher_script: str = ""
    lesson_step_worked_example: str = ""
    media_catalog_summary: str = ""
    is_final_step: bool = True

    # ── runtime state slices (legacy — additive, kept until commit G) ──
    move_history: list[str] = Field(default_factory=list)
    objective_correct: int = 0
    objective_wrong: int = 0
    objective_partial: int = 0
    objective_attempts: int = 0
    turns_in_session: int = 0
    turns_on_current_objective: int = 0
    verdictless_turns: int = 0
    attempts_on_open_question: int = 0
    open_question_stem: str = ""
    open_question_has_pending: bool = False

    # ── NEW counter fields (Commit D §4.2) — named, engine-supplied ──
    # The router prompt references these by name and MUST NOT re-derive
    # them from the transcript. Engine is the sole writer.
    wrong_attempts_on_open_question: int = 0
    partial_attempts_on_open_question: int = 0
    consecutive_wrong_on_open_question: int = 0
    objective_turn_count: int = 0
    prior_answer_attempts_on_objective: int = 0
    correct_on_objective: int = 0
    unscaffolded_correct_on_objective: int = 0
    # Last ≤10 verdict values ("correct" / "partial" / "wrong"), oldest first.
    recent_verdicts: list[str] = Field(default_factory=list)

    # ── tool surface availability ────────────────────────────────────
    pose_tool_available: bool = True


class RouterDecision(BaseModel):
    """Output of ``MoveRouter.route()``.

    Post-prune Commit D §4.2: the router is the single source of truth
    for both case classification AND move selection. Shape is
    case-conditional:

    - Non-answer-attempt (help_request / opening_turn / forced_close):
      ``{case, move, verdict_needed: false, reason}``. The grader does
      not run; the engine calls the tutor directly with the move.

    - Answer-attempt: ``{case: "answer_attempt", verdict_needed: true,
      moves_by_verdict: {correct, partial, wrong}, reason}``. The
      engine grades after the router returns and looks up the matching
      row — no engine-side mapping table.

    ``reason`` (≤400 chars) is threaded into the tutor LLM's user
    prompt as a single-sentence steering hint and stamped on the
    ``router.decision`` trace span.
    """

    case: RouterCase
    verdict_needed: bool
    move: Optional[RouterMove] = None
    moves_by_verdict: Optional[dict[str, RouterMove]] = None
    reason: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _validate_case_shape(self) -> "RouterDecision":
        if self.verdict_needed:
            if self.case != "answer_attempt":
                raise ValueError(
                    f"verdict_needed=True requires case='answer_attempt'; "
                    f"got {self.case!r}"
                )
            if self.move is not None:
                raise ValueError(
                    "verdict_needed=True requires move=None — "
                    "use moves_by_verdict to enumerate per-verdict moves"
                )
            if not self.moves_by_verdict:
                raise ValueError(
                    "verdict_needed=True requires moves_by_verdict "
                    "with keys 'correct', 'partial', 'wrong'"
                )
            required = {"correct", "partial", "wrong"}
            keys = set(self.moves_by_verdict.keys())
            if keys != required:
                raise ValueError(
                    f"moves_by_verdict keys must be exactly {required!r}; "
                    f"got {keys!r}"
                )
        else:
            if self.case == "answer_attempt":
                raise ValueError(
                    "case='answer_attempt' requires verdict_needed=True"
                )
            if self.move is None:
                raise ValueError(
                    "verdict_needed=False requires a non-None `move`"
                )
            if self.moves_by_verdict is not None:
                raise ValueError(
                    "verdict_needed=False forbids moves_by_verdict "
                    "(use `move` instead)"
                )
        return self
