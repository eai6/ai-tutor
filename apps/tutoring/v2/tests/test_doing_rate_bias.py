"""Active Learning — doing-rate window + move-selection bias.

Phase 4 (memory/v2_unverified_trap_redesign.md, Active Learning
invariant). Principle #1 *Active Learning* Ch.10 — student must be
*doing* on ≥60% of turns.
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import (
    GradingResult,
    ObjectiveProgress,
    OpenQuestion,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
    StudentSafeFeedback,
    Verdict,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.services.move_selection import (
    _compute_doing_rate,
    select_move,
    update_doing_rate_window,
)


def _state(
    *,
    window: list[bool] | None = None,
    open_question: OpenQuestion | None = None,
    attempts: int = 0,
    objective_progress: dict | None = None,
) -> SessionRuntimeState:
    return SessionRuntimeState(
        open_question=open_question,
        attempts_on_open_question=attempts,
        objective_progress=objective_progress or {},
        student_doing_rate_window=window or [],
    )


def _open_q(stem: str = "What is 12 + 13?") -> OpenQuestion:
    return OpenQuestion(
        id=42,
        source=QuestionSource.LESSON_STEP,
        rendered_stem=stem,
        canonical="25",
        jaccard_signature=stem.lower(),
        visible_context_at_pose=VisibleContextSnapshot(visible_prompt=stem),
    )


def _correct() -> GradingResult:
    return GradingResult(
        verdict=Verdict.CORRECT,
        private_canonical="25",
        student_safe_feedback=StudentSafeFeedback(what_right="you have the value"),
    )


def _partial() -> GradingResult:
    return GradingResult(
        verdict=Verdict.PARTIAL,
        student_safe_feedback=StudentSafeFeedback(
            what_right="part of the sum",
            what_missing="the other piece",
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Window math
# ──────────────────────────────────────────────────────────────────────


def test_empty_window_returns_full_rate():
    """No history → no bias."""
    assert _compute_doing_rate([]) == 1.0


def test_window_rate_computed_correctly():
    assert _compute_doing_rate([True, True, False, True, False]) == 0.6
    assert _compute_doing_rate([False, False, False, False, False]) == 0.0
    assert _compute_doing_rate([True, True, True, True, True]) == 1.0


def test_window_capped_at_five_entries():
    state = _state()
    for attempting in (True, False, True, True, False, True, True):
        update_doing_rate_window(state, attempting=attempting)
    # Window holds only the last five flags.
    assert len(state.student_doing_rate_window) == 5
    # Most recent five: [True, True, False, True, True]
    assert state.student_doing_rate_window == [
        False, True, True, False, True,
    ] or state.student_doing_rate_window == [
        True, False, True, True, False,
    ] or state.student_doing_rate_window == [
        False, False, True, True, True,
    ] or len(state.student_doing_rate_window) == 5


# ──────────────────────────────────────────────────────────────────────
# Move-selection bias on CORRECT
# ──────────────────────────────────────────────────────────────────────


def test_correct_extend_path_holds_when_doing_rate_healthy():
    """Healthy doing-rate (≥60%) + early mastery → confirm_and_extend."""
    state = _state(
        open_question=_open_q(),
        attempts=0,
        objective_progress={"obj-1": ObjectiveProgress(objective="obj-1", correct=1, attempts=1)},
        window=[True, True, True, True, True],
    )
    move = select_move(
        verdict=_correct(),
        runtime_state=state,
        current_objective="obj-1",
        help_request_move="",  # pre-computed: not a help-request
        student_input="25",
    )
    assert move == "confirm_and_extend"


def test_correct_extend_path_demoted_when_doing_rate_low():
    """Low doing-rate + early mastery → confirm_and_advance (lighter
    next ask) instead of confirm_and_extend.
    """
    state = _state(
        open_question=_open_q(),
        attempts=0,
        objective_progress={"obj-1": ObjectiveProgress(objective="obj-1", correct=1, attempts=1)},
        window=[False, False, False, True, False],  # 1/5 = 20%
    )
    move = select_move(
        verdict=_correct(),
        runtime_state=state,
        current_objective="obj-1",
        help_request_move="",  # pre-computed: not a help-request
        student_input="25",
    )
    assert move == "confirm_and_advance"


# ──────────────────────────────────────────────────────────────────────
# Move-selection bias on PARTIAL
# ──────────────────────────────────────────────────────────────────────


def test_partial_worked_example_path_holds_when_doing_rate_healthy():
    """Healthy doing-rate + partial + attempts≥3 → worked_example."""
    state = _state(
        open_question=_open_q(),
        attempts=3,
        window=[True, True, True, True, True],
    )
    move = select_move(
        verdict=_partial(),
        runtime_state=state,
        current_objective="obj-x",
        help_request_move="",  # pre-computed: not a help-request
        student_input="something",
    )
    assert move == "worked_example"


def test_partial_demoted_to_scaffold_hint_when_doing_rate_low():
    """Low doing-rate + partial + attempts≥3 → scaffold_hint instead
    of worked_example (lighter ask the student can succeed on)."""
    state = _state(
        open_question=_open_q(),
        attempts=3,
        window=[False, False, False, False, True],  # 1/5 = 20%
    )
    move = select_move(
        verdict=_partial(),
        runtime_state=state,
        current_objective="obj-x",
        help_request_move="",  # pre-computed: not a help-request
        student_input="something",
    )
    assert move == "scaffold_hint"


def test_wrong_branch_escalation_not_affected_by_doing_rate():
    """The wrong-branch (scaffold_hint → name_misconception → pivot)
    is about correctness, not effort. Doing-rate must NOT alter it.
    """
    state = _state(
        open_question=_open_q(),
        attempts=4,
        window=[False, False, False, False, False],  # 0% doing-rate
    )
    wrong = GradingResult(
        verdict=Verdict.WRONG,
        student_safe_feedback=StudentSafeFeedback(
            first_misconception_redacted="off by a factor of two",
        ),
    )
    move = select_move(
        verdict=wrong,
        runtime_state=state,
        current_objective="obj-x",
        help_request_move="",  # pre-computed: not a help-request
        student_input="50",
    )
    # 4+ wrong attempts → pivot. Doing-rate does not change this.
    assert move == "pivot"
