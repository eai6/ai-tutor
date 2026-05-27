"""Safety-floor unit tests — design/tasks/move-router-implementation-plan.md §5.2.

Per-floor unit tests + ordering. Pure functions: no LLM, no DB.
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import (
    ObjectiveProgress,
    PosedQuestionLedgerEntry,
    QuestionSource,
    RouterDecision,
    SafetyValveCounters,
    SessionRuntimeState,
)
from apps.tutoring.v2.services.safety_floors import (
    MAX_TURNS_PER_OBJECTIVE,
    MAX_TURNS_PER_SESSION,
    MAX_VERDICTLESS_RUN,
    apply_safety_floors,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _state(
    *,
    move_history=None,
    objective_progress=None,
    counters=None,
    unverified_run_length=0,
) -> SessionRuntimeState:
    state = SessionRuntimeState()
    if move_history:
        state.move_history = list(move_history)
    if objective_progress:
        state.objective_progress = dict(objective_progress)
    if counters:
        state.safety_valve_counters = counters
    state.unverified_run_length = unverified_run_length
    return state


def _decision(move: str = "scaffold_hint") -> RouterDecision:
    return RouterDecision(
        chosen_move=move,
        principle_emphasis=["Active Learning"],
        focus_note="",
        rationale="",
    )


def _apply(decision, state, **kw):
    return apply_safety_floors(
        decision=decision,
        runtime_state=state,
        student_input=kw.pop("student_input", ""),
        profile_summary=kw.pop("profile_summary", ""),
        pose_tool_available=kw.pop("pose_tool_available", True),
        current_objective=kw.pop("current_objective", "obj1"),
    )


# ──────────────────────────────────────────────────────────────────────
# Floor #1 — turn caps
# ──────────────────────────────────────────────────────────────────────


def test_floor_turn_caps_session_overrides_to_close():
    state = _state(
        counters=SafetyValveCounters(turns_in_session=MAX_TURNS_PER_SESSION),
    )
    outcome = _apply(_decision("scaffold_hint"), state)
    assert outcome.final_move == "close_topic"
    assert "turn_caps_session" in outcome.override_floors


def test_floor_turn_caps_objective_overrides_to_close():
    state = _state(
        counters=SafetyValveCounters(
            turns_on_current_objective=MAX_TURNS_PER_OBJECTIVE,
        ),
    )
    outcome = _apply(_decision("scaffold_hint"), state)
    assert outcome.final_move == "close_topic"
    assert "turn_caps_objective" in outcome.override_floors


def test_floor_turn_caps_verdictless_overrides_to_close():
    state = _state(
        counters=SafetyValveCounters(verdictless_turns=MAX_VERDICTLESS_RUN),
    )
    outcome = _apply(_decision("explain"), state)
    assert outcome.final_move == "close_topic"
    assert "turn_caps_verdictless" in outcome.override_floors


def test_floor_turn_caps_does_not_fire_below_threshold():
    state = _state(
        counters=SafetyValveCounters(
            turns_in_session=MAX_TURNS_PER_SESSION - 1,
            turns_on_current_objective=MAX_TURNS_PER_OBJECTIVE - 1,
            verdictless_turns=MAX_VERDICTLESS_RUN - 1,
        ),
    )
    outcome = _apply(_decision("scaffold_hint"), state)
    assert outcome.final_move == "scaffold_hint"
    assert outcome.override_floors == ()


# ──────────────────────────────────────────────────────────────────────
# Floor #2 — objective evidence saturation
# ──────────────────────────────────────────────────────────────────────


def test_floor_objective_evidence_overrides_router_to_close():
    state = _state(
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=3, correct=2, wrong=1,
            ),
        },
    )
    outcome = _apply(_decision("confirm_and_extend"), state)
    assert outcome.final_move == "close_topic"
    assert "objective_evidence_saturated" in outcome.override_floors


def test_floor_objective_evidence_does_not_fire_when_router_already_close():
    state = _state(
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=3, correct=2, wrong=1,
            ),
        },
    )
    outcome = _apply(_decision("close_topic"), state)
    assert outcome.final_move == "close_topic"
    # Router already agreed — no override fires for floor #2.
    assert "objective_evidence_saturated" not in outcome.override_floors


def test_floor_objective_evidence_does_not_fire_below_threshold():
    state = _state(
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=2, correct=2,
            ),
        },
    )
    outcome = _apply(_decision("confirm_and_advance"), state)
    # attempts < _OBJECTIVE_MIN_ATTEMPTS (3) → no force-close.
    assert outcome.final_move == "confirm_and_advance"


def test_floor_objective_evidence_skips_closed_progress():
    state = _state(
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=4, correct=3, closed=True,
            ),
        },
    )
    outcome = _apply(_decision("scaffold_hint"), state)
    assert outcome.final_move == "scaffold_hint"


# ──────────────────────────────────────────────────────────────────────
# Floor #3 — name_misconception repeat
# ──────────────────────────────────────────────────────────────────────


def test_floor_misconception_repeat_overrides_to_pivot():
    state = _state(
        move_history=["name_misconception", "scaffold_hint", "name_misconception"],
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=4, correct=0, wrong=4,
            ),
        },
    )
    outcome = _apply(_decision("name_misconception"), state)
    assert outcome.final_move == "pivot"
    assert "misconception_not_resolving" in outcome.override_floors


def test_floor_misconception_does_not_fire_on_first_emission():
    state = _state(
        move_history=["scaffold_hint", "explain"],
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=2, correct=0,
            ),
        },
    )
    outcome = _apply(_decision("name_misconception"), state)
    assert outcome.final_move == "name_misconception"


def test_floor_misconception_does_not_fire_when_correct_seen_since():
    state = _state(
        move_history=["name_misconception", "scaffold_hint"],
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=3, correct=1, wrong=2,
            ),
        },
    )
    outcome = _apply(_decision("name_misconception"), state)
    # The pattern broke — at least one correct on the objective.
    assert outcome.final_move == "name_misconception"


def test_floor_misconception_does_not_fire_when_router_picked_something_else():
    state = _state(
        move_history=["name_misconception", "scaffold_hint", "name_misconception"],
    )
    outcome = _apply(_decision("scaffold_hint"), state)
    assert outcome.final_move == "scaffold_hint"


# ──────────────────────────────────────────────────────────────────────
# Floor #4 — pose ledger saturated
# ──────────────────────────────────────────────────────────────────────


def test_floor_pose_saturated_overrides_to_close_when_some_correct():
    state = _state(
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=2, correct=1, wrong=1,
            ),
        },
    )
    outcome = _apply(_decision("scaffold_hint"), state, pose_tool_available=False)
    assert outcome.final_move == "close_topic"
    assert "pose_ledger_saturated" in outcome.override_floors


def test_floor_pose_saturated_overrides_to_pivot_when_zero_correct():
    state = _state(
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=2, correct=0, wrong=2,
            ),
        },
    )
    outcome = _apply(_decision("scaffold_hint"), state, pose_tool_available=False)
    assert outcome.final_move == "pivot"
    assert "pose_ledger_saturated" in outcome.override_floors


def test_floor_pose_saturated_does_not_fire_when_tool_available():
    state = _state()
    outcome = _apply(_decision("scaffold_hint"), state, pose_tool_available=True)
    assert outcome.final_move == "scaffold_hint"


def test_floor_pose_saturated_does_not_override_close_topic():
    state = _state()
    outcome = _apply(_decision("close_topic"), state, pose_tool_available=False)
    assert outcome.final_move == "close_topic"
    assert "pose_ledger_saturated" not in outcome.override_floors


# ──────────────────────────────────────────────────────────────────────
# Floor #5 — help-request regex backstop
# ──────────────────────────────────────────────────────────────────────


def test_floor_help_regex_overrides_to_explain():
    outcome = _apply(
        _decision("scaffold_hint"),
        _state(),
        student_input="what is condensation",
    )
    assert outcome.final_move == "explain"
    assert "help_request_regex_backstop" in outcome.override_floors


def test_floor_help_regex_picks_worked_example_for_struggling_profile():
    outcome = _apply(
        _decision("scaffold_hint"),
        _state(),
        student_input="i don't understand percentages",
        profile_summary="The student has been struggling with fractions.",
    )
    assert outcome.final_move == "worked_example"
    assert "help_request_regex_backstop" in outcome.override_floors


def test_floor_help_regex_does_not_fire_when_router_chose_explain():
    outcome = _apply(
        _decision("explain"),
        _state(),
        student_input="i don't understand",
    )
    assert outcome.final_move == "explain"
    assert "help_request_regex_backstop" not in outcome.override_floors


def test_floor_help_regex_does_not_fire_when_router_chose_worked_example():
    outcome = _apply(
        _decision("worked_example"),
        _state(),
        student_input="show me how",
    )
    assert outcome.final_move == "worked_example"
    assert "help_request_regex_backstop" not in outcome.override_floors


def test_floor_help_regex_does_not_fire_on_attempting_input():
    outcome = _apply(
        _decision("confirm_and_advance"),
        _state(),
        student_input="12",
    )
    assert outcome.final_move == "confirm_and_advance"


def test_floor_help_regex_catches_im_lost():
    outcome = _apply(
        _decision("scaffold_hint"),
        _state(),
        student_input="I'm lost",
    )
    assert outcome.final_move == "explain"


def test_floor_help_regex_catches_teach_me():
    outcome = _apply(
        _decision("scaffold_hint"),
        _state(),
        student_input="teach me",
    )
    assert outcome.final_move == "explain"


# ──────────────────────────────────────────────────────────────────────
# Ordering
# ──────────────────────────────────────────────────────────────────────


def test_floor_ordering_turn_caps_wins_over_help_regex():
    """When the turn-cap floor fires first → close_topic; the help regex
    floor should see close_topic and not re-override (it only flips
    non-explain/non-worked moves, and close_topic isn't in that set).
    """
    state = _state(
        counters=SafetyValveCounters(
            turns_on_current_objective=MAX_TURNS_PER_OBJECTIVE,
        ),
    )
    outcome = _apply(
        _decision("scaffold_hint"),
        state,
        student_input="explain it",
    )
    assert outcome.final_move == "close_topic"
    assert "turn_caps_objective" in outcome.override_floors
    # Help regex doesn't flip close_topic.
    assert "help_request_regex_backstop" not in outcome.override_floors


def test_floor_ordering_evidence_then_pose_saturated_collapse():
    """Objective evidence saturated AND pose tool unavailable — floor #2
    closes first; floor #4 sees close_topic and stays out."""
    state = _state(
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=3, correct=3,
            ),
        },
    )
    outcome = _apply(
        _decision("scaffold_hint"),
        state,
        pose_tool_available=False,
    )
    assert outcome.final_move == "close_topic"
    assert outcome.override_floors[0] == "objective_evidence_saturated"


def test_floor_ordering_help_regex_can_fire_after_misconception_pivot():
    """When floor #3 flips name_misconception→pivot, the student input is
    also a help request → floor #5 then flips pivot→explain."""
    state = _state(
        move_history=["name_misconception", "scaffold_hint", "name_misconception"],
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=4, correct=0, wrong=4,
            ),
        },
    )
    outcome = _apply(
        _decision("name_misconception"),
        state,
        student_input="explain it to me",
    )
    assert "misconception_not_resolving" in outcome.override_floors
    assert "help_request_regex_backstop" in outcome.override_floors
    assert outcome.final_move == "explain"


# ──────────────────────────────────────────────────────────────────────
# No-op happy path
# ──────────────────────────────────────────────────────────────────────


def test_floors_pass_through_when_nothing_trips():
    state = _state(
        objective_progress={
            "obj1": ObjectiveProgress(
                objective="obj1", attempts=1, correct=1,
            ),
        },
    )
    outcome = _apply(_decision("confirm_and_advance"), state)
    assert outcome.final_move == "confirm_and_advance"
    assert outcome.override_floors == ()
    assert outcome.overridden is False
