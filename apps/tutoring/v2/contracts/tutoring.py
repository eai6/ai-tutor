"""Service-call contracts: TutoringContext, GradingRequest, GradingResult,
ProfileUpdate.

Phase 1 ships these as the data-shape boundary; Phase 2 fills in the
services that consume / produce them.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

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


class ProfileUpdate(BaseModel):
    """End-of-session output from StudentProfiler (Phase 3 §3.1)."""

    profile_summary_text: str = ""
    asked_questions_delta: dict[str, dict] = Field(default_factory=dict)
