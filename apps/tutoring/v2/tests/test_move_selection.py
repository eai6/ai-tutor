"""Move-state-machine fixture tests — Phase 2 §Tests.

One fixture per move from analysis §4 demonstrating the trigger
conditions and the engine's deterministic selection.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.contracts import (
    GradingResult,
    ObjectiveProgress,
    PosedQuestionLedgerEntry,
    QuestionSource,
    RemediationState,
    SafetyValveCounters,
    SessionRuntimeState,
    StudentSafeFeedback,
    Verdict,
)
from apps.tutoring.v2.services.move_selection import ALLOWED_MOVES, select_move


def _state(**overrides) -> SessionRuntimeState:
    return SessionRuntimeState(**overrides)


def _verdict(kind: Verdict, **kw) -> GradingResult:
    return GradingResult(verdict=kind, **kw)


# ──────────────────────────────────────────────────────────────────────
# pose_question — no verdict, open question waiting / fresh session
# ──────────────────────────────────────────────────────────────────────


def test_pose_question_no_open_no_verdict():
    """No verdict + no open question + objective not flagged just-opened
    → pose_question (default open invitation)."""
    move = select_move(
        verdict=None,
        runtime_state=_state(),
        objective_just_opened=False,
    )
    assert move == "pose_question"


def test_pose_question_unverified_first_run():
    """First unverified turn → pose_question (keep floor open)."""
    move = select_move(
        verdict=_verdict(Verdict.UNVERIFIED),
        runtime_state=_state(unverified_run_length=0),
    )
    assert move == "pose_question"


# ──────────────────────────────────────────────────────────────────────
# confirm_and_advance / confirm_and_extend — verdict=correct
# ──────────────────────────────────────────────────────────────────────


def test_confirm_and_advance_default_correct():
    """Correct verdict with 2+ attempts → confirm_and_advance."""
    state = _state(attempts_on_open_question=2)
    move = select_move(verdict=_verdict(Verdict.CORRECT), runtime_state=state)
    assert move == "confirm_and_advance"


def test_confirm_and_extend_early_mastery():
    """Correct verdict on first attempt with prior progress → extend."""
    state = _state(
        attempts_on_open_question=1,
        objective_progress={
            "obj": ObjectiveProgress(objective="obj", attempts=1, correct=1),
        },
    )
    move = select_move(
        verdict=_verdict(Verdict.CORRECT),
        runtime_state=state,
        current_objective="obj",
    )
    assert move == "confirm_and_extend"


# ──────────────────────────────────────────────────────────────────────
# scaffold_hint — verdict=wrong attempts 1-2
# ──────────────────────────────────────────────────────────────────────


def test_scaffold_hint_first_wrong():
    """First wrong attempt → scaffold_hint."""
    state = _state(attempts_on_open_question=1)
    assert select_move(verdict=_verdict(Verdict.WRONG), runtime_state=state) == "scaffold_hint"


def test_scaffold_hint_second_wrong():
    """Second wrong attempt → scaffold_hint (faded)."""
    state = _state(attempts_on_open_question=2)
    assert select_move(verdict=_verdict(Verdict.WRONG), runtime_state=state) == "scaffold_hint"


def test_scaffold_hint_partial():
    """Partial verdict early → scaffold_hint."""
    state = _state(attempts_on_open_question=1)
    assert select_move(verdict=_verdict(Verdict.PARTIAL), runtime_state=state) == "scaffold_hint"


# ──────────────────────────────────────────────────────────────────────
# name_misconception — third wrong attempt
# ──────────────────────────────────────────────────────────────────────


def test_name_misconception_third_wrong():
    """Third wrong attempt on the same item → name_misconception."""
    state = _state(attempts_on_open_question=3)
    assert select_move(verdict=_verdict(Verdict.WRONG), runtime_state=state) == "name_misconception"


# ──────────────────────────────────────────────────────────────────────
# pivot — 4+ wrong OR name_misconception + next-attempt wrong
# ──────────────────────────────────────────────────────────────────────


def test_pivot_four_wrong():
    """4+ wrong attempts on the same item → pivot."""
    state = _state(attempts_on_open_question=4)
    assert select_move(verdict=_verdict(Verdict.WRONG), runtime_state=state) == "pivot"


def test_pivot_after_name_misconception():
    """name_misconception fired previously + next-attempt still wrong → pivot."""
    state = _state(
        attempts_on_open_question=1,
        remediation_state=RemediationState(
            misconception="area/perimeter", fired_at_turn=3, resolved=False,
        ),
        move_history=["name_misconception"],
    )
    assert select_move(verdict=_verdict(Verdict.WRONG), runtime_state=state) == "pivot"


# ──────────────────────────────────────────────────────────────────────
# worked_example — partial after 3 attempts OR profile suggests struggle on open
# ──────────────────────────────────────────────────────────────────────


def test_worked_example_partial_stuck():
    """Partial verdict on attempt ≥3 → worked_example."""
    state = _state(attempts_on_open_question=3)
    assert select_move(verdict=_verdict(Verdict.PARTIAL), runtime_state=state) == "worked_example"


def test_worked_example_objective_opening_with_struggle_profile():
    """Objective just opened + profile shows struggle → worked_example."""
    move = select_move(
        verdict=None,
        runtime_state=_state(),
        profile_summary="Student has struggled with prior lessons on this topic.",
        objective_just_opened=True,
    )
    assert move == "worked_example"


# ──────────────────────────────────────────────────────────────────────
# explain — objective just opened (no struggle profile) OR unverified streak
# ──────────────────────────────────────────────────────────────────────


def test_explain_on_objective_open_no_profile():
    """Fresh objective + no struggle profile → explain."""
    assert select_move(
        verdict=None, runtime_state=_state(), objective_just_opened=True,
    ) == "explain"


def test_explain_unverified_streak():
    """3 consecutive unverified verdicts → explain (frame the concept)."""
    state = _state(unverified_run_length=3)
    assert select_move(verdict=_verdict(Verdict.UNVERIFIED), runtime_state=state) == "explain"


# ──────────────────────────────────────────────────────────────────────
# close_topic — objective evidence sufficient
# ──────────────────────────────────────────────────────────────────────


def test_close_topic_when_evidence_sufficient():
    """Objective with ≥2 correct AND ratio ≥0.5 → close_topic."""
    state = _state(
        objective_progress={
            "obj": ObjectiveProgress(
                objective="obj", attempts=3, correct=2, wrong=1,
            ),
        },
    )
    # Should close even when the latest verdict is wrong — close
    # short-circuits everything before verdict-driven branches.
    move = select_move(
        verdict=_verdict(Verdict.WRONG),
        runtime_state=state,
        current_objective="obj",
    )
    assert move == "close_topic"


def test_close_topic_skipped_when_already_closed():
    """Already-closed objective → does NOT re-fire close_topic.

    The exact next move falls out of the verdict branch (here:
    confirm_and_extend because correct on early attempt with prior
    correct count). The invariant under test is "not close_topic".
    """
    state = _state(
        objective_progress={
            "obj": ObjectiveProgress(
                objective="obj", attempts=3, correct=2, wrong=1, closed=True,
            ),
        },
    )
    move = select_move(
        verdict=_verdict(Verdict.CORRECT),
        runtime_state=state,
        current_objective="obj",
    )
    assert move != "close_topic"


# ──────────────────────────────────────────────────────────────────────
# All moves listed once — guards against accidental removal from
# ALLOWED_MOVES.
# ──────────────────────────────────────────────────────────────────────


def test_allowed_moves_complete():
    """ALLOWED_MOVES contains exactly the 9 §4 moves."""
    assert set(ALLOWED_MOVES) == {
        "pose_question",
        "confirm_and_advance",
        "confirm_and_extend",
        "scaffold_hint",
        "name_misconception",
        "worked_example",
        "explain",
        "pivot",
        "close_topic",
    }


# ──────────────────────────────────────────────────────────────────────
# bare_answer is NOT a move-selection input (§2.1.1 invariant)
# ──────────────────────────────────────────────────────────────────────


def test_bare_answer_does_not_affect_move_selection():
    """verdict.bare_answer being True must NOT change the selected move."""
    state = _state(attempts_on_open_question=1)
    move_bare = select_move(
        verdict=_verdict(Verdict.WRONG, bare_answer=True),
        runtime_state=state,
    )
    move_full = select_move(
        verdict=_verdict(Verdict.WRONG, bare_answer=False),
        runtime_state=state,
    )
    assert move_bare == move_full == "scaffold_hint"
