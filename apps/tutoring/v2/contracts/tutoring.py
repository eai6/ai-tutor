"""Service-call contracts: TutoringContext, GradingRequest, GradingResult,
ProfileUpdate.

Phase 1 ships these as the data-shape boundary; Phase 2 fills in the
services that consume / produce them.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.tutoring.v2.contracts.runtime_state import (
    OpenQuestion,
    SessionRuntimeState,
)


class Verdict(str, Enum):
    """Grader verdict — first-class ``unverified`` is intentional.

    Per refactor-analysis §3, ``unverified`` means "we don't know" — it
    is NOT an error, and conformance has explicit rules for it.
    """

    CORRECT = "correct"
    WRONG = "wrong"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"


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


class ProfileUpdate(BaseModel):
    """End-of-session output from StudentProfiler (Phase 3 §3.1)."""

    profile_summary_text: str = ""
    asked_questions_delta: dict[str, dict] = Field(default_factory=dict)


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


# The 13 science-of-learning principle names (from design/science-
# principles.md Table 1). The router emits ``principle_emphasis`` from
# this closed set; Pydantic validates membership so a hallucinated
# principle name surfaces as a ValidationError instead of silently
# steering the move LLM.
ALLOWED_PRINCIPLES: tuple[str, ...] = (
    "Active Learning",
    "Direct Instruction",
    "Deliberate Practice",
    "Mastery Learning",
    "Cognitive Load",
    "Automaticity",
    "Layering",
    "Non-Interference",
    "Spaced Repetition",
    "Interleaving",
    "Testing Effect",
    "Targeted Remediation",
    "Gamification",
)


class RouterRequest(BaseModel):
    """Inputs to ``MoveRouter.route()``.

    Built from ``TutoringContext`` + ``GradingResult`` + the runtime
    state at one site (``RouterRequest.from_context``) so callers don't
    re-derive the snapshot at each call site. Frozen — services are
    stateless and receive immutable snapshots.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # ── conversation surface ─────────────────────────────────────────
    last_n_turns: list[dict] = Field(default_factory=list)
    student_input: str = ""

    # ── grader output (None on opening / transitional / help-request) ─
    grader_verdict: Optional[Verdict] = None
    grader_reason_code: Optional[str] = None
    student_safe_feedback: StudentSafeFeedback = Field(
        default_factory=StudentSafeFeedback
    )

    # ── student / lesson context ─────────────────────────────────────
    profile_summary: str = ""
    objective: str = ""
    lesson_title: str = ""
    lesson_subject: str = ""
    lesson_step_teacher_script: str = ""
    lesson_step_worked_example: str = ""
    media_catalog_summary: str = ""
    is_final_step: bool = True

    # ── runtime state slices ─────────────────────────────────────────
    move_history: list[str] = Field(default_factory=list)
    objective_correct: int = 0
    objective_wrong: int = 0
    objective_partial: int = 0
    objective_unverified: int = 0
    objective_attempts: int = 0
    turns_in_session: int = 0
    turns_on_current_objective: int = 0
    verdictless_turns: int = 0
    unverified_run_length: int = 0
    attempts_on_open_question: int = 0
    open_question_stem: str = ""
    open_question_has_pending: bool = False

    # ── tool surface availability ────────────────────────────────────
    pose_tool_available: bool = True


class RouterDecision(BaseModel):
    """Output of ``MoveRouter.route()``.

    The router decides WHAT to do; the move LLM (StudentTutor) decides
    HOW to say it. ``focus_note`` is what-to-address this turn (1-2
    sentences, ≤50 tokens / 250 chars). ``principle_emphasis`` is the
    1-3 names from ``ALLOWED_PRINCIPLES`` to surface in the move prompt.
    ``rationale`` is for the v2 observability trace — a single sentence
    the auditor can grep on.
    """

    chosen_move: RouterMove
    principle_emphasis: list[str] = Field(default_factory=list, max_length=3)
    focus_note: str = Field(default="", max_length=250)
    rationale: str = Field(default="", max_length=400)

    @field_validator("principle_emphasis")
    @classmethod
    def _validate_principle_names(cls, v: list[str]) -> list[str]:
        allowed = set(ALLOWED_PRINCIPLES)
        bad = [name for name in v if name not in allowed]
        if bad:
            raise ValueError(
                f"principle_emphasis contains unknown principle(s): "
                f"{bad!r}; must be a subset of {ALLOWED_PRINCIPLES!r}"
            )
        return v
