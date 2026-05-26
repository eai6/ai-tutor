"""Typed Pydantic contracts for the v2 engine.

These are the data shapes services pass to each other. Each service
receives a frozen snapshot — never live mutable state. See
``apps.tutoring.v2.contracts.runtime_state.SessionRuntimeState``.
"""

from apps.tutoring.v2.contracts.runtime_state import (
    BareAnswerCounters,
    ObjectiveProgress,
    OpenQuestion,
    PendingPose,
    PosedQuestionLedgerEntry,
    QuestionRef,
    QuestionSource,
    RemediationState,
    ResumeMarker,
    SafetyValveCounters,
    SessionRuntimeState,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.contracts.tutoring import (
    GradingRequest,
    GradingResult,
    ProfileUpdate,
    TutoringContext,
    Verdict,
)

__all__ = [
    "BareAnswerCounters",
    "GradingRequest",
    "GradingResult",
    "ObjectiveProgress",
    "OpenQuestion",
    "PendingPose",
    "PosedQuestionLedgerEntry",
    "ProfileUpdate",
    "QuestionRef",
    "QuestionSource",
    "RemediationState",
    "ResumeMarker",
    "SafetyValveCounters",
    "SessionRuntimeState",
    "TutoringContext",
    "Verdict",
    "VisibleContextSnapshot",
]
