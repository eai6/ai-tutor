"""Deterministic safety floors for the LLM Move Router.

Companion to ``apps/tutoring/v2/services/move_router.py``.

Five ordered floors. Each is a pure function over the
``RouterDecision``, the ``SessionRuntimeState``, and the latest student
input; each may override ``chosen_move``. Floors are applied
sequentially — a later floor sees the move emitted by the earlier
floor — and each override emits a ``router.floor_override`` span so the
v2 observability dashboard can surface trip rates per floor.

Floors are SAFETY FLOORS, not pedagogical flow control. They catch the
highest-cost shapes (turn caps, evidence saturation, repeat-without-
progress, pose-tool saturation, help-request misroute) and let the
router LLM decide everything else.

Plan §2.3. Pure functions, no LLM, no DB.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    RouterDecision,
    SessionRuntimeState,
    Verdict,
)

logger = logging.getLogger(__name__)


# Plan §2.3 row #1 — turn caps (mirrors the legacy ``_safety_valve_override``).
MAX_TURNS_PER_SESSION = 40
MAX_TURNS_PER_OBJECTIVE = 12
MAX_VERDICTLESS_RUN = 6

# Plan §2.3 row #2 — objective evidence saturation threshold.
_OBJECTIVE_MIN_CORRECT = 2
_OBJECTIVE_MIN_ATTEMPTS = 3
_OBJECTIVE_MIN_RATIO = 0.66

# Plan §2.3 row #3 — name_misconception repeat window.
_NAME_MISC_REPEAT_WINDOW = 3  # last N moves to inspect

# Plan §2.3 row #5 — help-request regex backstop.
_HELP_REQUEST_RE = re.compile(
    r"\b("
    r"i\s+don'?t\s+(understand|get|know\s+how)"
    r"|what\s+(is|does)"
    r"|explain"
    r"|show\s+me"
    r"|teach\s+me"
    r"|i'?m\s+(lost|stuck)"
    r")\b",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FloorOutcome:
    """Result of applying the safety-floor chain.

    ``final_move`` is the move the engine should execute.
    ``override_floors`` lists the floor names that fired (in order)
    so the trace can audit a multi-override turn.
    """

    final_move: str
    override_floors: tuple[str, ...] = ()

    @property
    def overridden(self) -> bool:
        return bool(self.override_floors)


def apply_safety_floors(
    *,
    decision: RouterDecision,
    runtime_state: SessionRuntimeState,
    student_input: str,
    profile_summary: str = "",
    pose_tool_available: bool = True,
    current_objective: str = "",
) -> FloorOutcome:
    """Apply the 5 ordered safety floors. Returns the final move.

    Each floor that fires emits a ``router.floor_override`` audit span
    with the from-move, to-move, and floor name.
    """
    current_move: str = decision.chosen_move
    fired: list[str] = []

    # Floor #1 — turn caps.
    new_move, floor_name = _floor_turn_caps(current_move, runtime_state)
    if floor_name is not None:
        _emit_override_span(
            from_move=current_move, to_move=new_move, floor=floor_name,
        )
        fired.append(floor_name)
        current_move = new_move

    # Floor #2 — objective evidence saturation.
    new_move, floor_name = _floor_objective_evidence(
        current_move, runtime_state, current_objective=current_objective,
    )
    if floor_name is not None:
        _emit_override_span(
            from_move=current_move, to_move=new_move, floor=floor_name,
        )
        fired.append(floor_name)
        current_move = new_move

    # Floor #3 — name_misconception repeat.
    new_move, floor_name = _floor_misconception_repeat(
        current_move, runtime_state, current_objective=current_objective,
    )
    if floor_name is not None:
        _emit_override_span(
            from_move=current_move, to_move=new_move, floor=floor_name,
        )
        fired.append(floor_name)
        current_move = new_move

    # Floor #4 — pose ledger saturated (pose tool unavailable for this turn).
    new_move, floor_name = _floor_pose_saturated(
        current_move, runtime_state,
        pose_tool_available=pose_tool_available,
        current_objective=current_objective,
    )
    if floor_name is not None:
        _emit_override_span(
            from_move=current_move, to_move=new_move, floor=floor_name,
        )
        fired.append(floor_name)
        current_move = new_move

    # Floor #5 — help-request regex backstop.
    new_move, floor_name = _floor_help_request_regex(
        current_move,
        student_input=student_input,
        profile_summary=profile_summary,
    )
    if floor_name is not None:
        _emit_override_span(
            from_move=current_move, to_move=new_move, floor=floor_name,
        )
        fired.append(floor_name)
        current_move = new_move

    return FloorOutcome(
        final_move=current_move,
        override_floors=tuple(fired),
    )


# ──────────────────────────────────────────────────────────────────────
# Floor #1 — turn caps
# ──────────────────────────────────────────────────────────────────────


def _floor_turn_caps(
    current_move: str,
    runtime_state: SessionRuntimeState,
) -> tuple[str, Optional[str]]:
    """Force close_topic when any turn cap has saturated."""
    counters = runtime_state.safety_valve_counters
    if counters.turns_in_session >= MAX_TURNS_PER_SESSION:
        return "close_topic", "turn_caps_session"
    if counters.turns_on_current_objective >= MAX_TURNS_PER_OBJECTIVE:
        return "close_topic", "turn_caps_objective"
    if counters.verdictless_turns >= MAX_VERDICTLESS_RUN:
        return "close_topic", "turn_caps_verdictless"
    return current_move, None


# ──────────────────────────────────────────────────────────────────────
# Floor #2 — objective evidence saturation
# ──────────────────────────────────────────────────────────────────────


def _floor_objective_evidence(
    current_move: str,
    runtime_state: SessionRuntimeState,
    *,
    current_objective: str = "",
) -> tuple[str, Optional[str]]:
    """Force close_topic when the objective has enough correct evidence.

    The atomic ``current_step_index`` advance happens automatically in
    ``TutorEngine.respond`` after the turn finalises (the engine calls
    ``_advance_step_if_possible`` whenever ``selected_move ==
    'close_topic'``). The floor only has to set the move; the engine's
    existing post-turn pass advances the step in the same turn.
    """
    if current_move == "close_topic":
        # Router already agreed — no override needed.
        return current_move, None

    progress = _resolve_active_progress(runtime_state, current_objective)
    if progress is None:
        return current_move, None
    if progress.closed:
        return current_move, None
    if progress.attempts < _OBJECTIVE_MIN_ATTEMPTS:
        return current_move, None
    if progress.correct < _OBJECTIVE_MIN_CORRECT:
        return current_move, None
    ratio = progress.correct / max(1, progress.attempts)
    if ratio < _OBJECTIVE_MIN_RATIO:
        return current_move, None
    return "close_topic", "objective_evidence_saturated"


def _resolve_active_progress(
    runtime_state: SessionRuntimeState,
    current_objective: str,
):
    """Look up the ObjectiveProgress for the active objective.

    Prefers the explicit ``current_objective`` key (the canonical
    lookup the legacy ``select_move`` used). Falls back to the
    most-attempted unclosed entry when the key is absent — happens in
    tests that don't thread an objective name.
    """
    key = (current_objective or "_").strip() or "_"
    if key in runtime_state.objective_progress:
        return runtime_state.objective_progress[key]
    candidates = [
        p for p in runtime_state.objective_progress.values()
        if not p.closed
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.attempts)


# ──────────────────────────────────────────────────────────────────────
# Floor #3 — name_misconception repeat block
# ──────────────────────────────────────────────────────────────────────


def _floor_misconception_repeat(
    current_move: str,
    runtime_state: SessionRuntimeState,
    *,
    current_objective: str = "",
) -> tuple[str, Optional[str]]:
    """Override name_misconception → pivot when the misconception isn't resolving.

    Trigger: router picked ``name_misconception`` AND ``name_misconception``
    fired earlier in the last ``_NAME_MISC_REPEAT_WINDOW`` moves
    without a verdict change (which would have broken the pattern).
    The "verdict change broke the pattern" check is approximated by
    ``unverified_run_length`` and the per-objective progress —
    specifically: if the objective has accumulated a CORRECT since the
    last name_misconception, we treat the pattern as resolved.
    """
    if current_move != "name_misconception":
        return current_move, None
    recent = list(runtime_state.move_history or [])[-_NAME_MISC_REPEAT_WINDOW:]
    if "name_misconception" not in recent:
        return current_move, None
    # If the active objective has gained a correct since name_misconception
    # last fired, the verdict pattern broke — don't override.
    progress = _resolve_active_progress(runtime_state, current_objective)
    if progress is not None and progress.correct > 0:
        return current_move, None
    return "pivot", "misconception_not_resolving"


# ──────────────────────────────────────────────────────────────────────
# Floor #4 — pose ledger saturation
# ──────────────────────────────────────────────────────────────────────


_POSE_CAPABLE_MOVES_FOR_FLOOR = frozenset({
    "confirm_and_advance",
    "confirm_and_extend",
    "scaffold_hint",
    "name_misconception",
    "worked_example",
    "explain",
    "pivot",
})


def _floor_pose_saturated(
    current_move: str,
    runtime_state: SessionRuntimeState,
    *,
    pose_tool_available: bool,
    current_objective: str = "",
) -> tuple[str, Optional[str]]:
    """Force a terminal transition when no un-posed slots remain.

    Plan §2.3 row #4: if the router picked any non-terminal move AND
    the pose tool is unavailable, the LLM has no way to ask a
    verifiable question this turn. Override to close_topic when at
    least one correct exists on the active objective; otherwise pivot.
    """
    if pose_tool_available:
        return current_move, None
    if current_move == "close_topic":
        # Already terminal — no override.
        return current_move, None
    if current_move not in _POSE_CAPABLE_MOVES_FOR_FLOOR:
        return current_move, None

    # Decide the override target based on objective evidence.
    progress = _resolve_active_progress(runtime_state, current_objective)
    has_correct = bool(progress and progress.correct >= 1)
    target = "close_topic" if has_correct else "pivot"
    return target, "pose_ledger_saturated"


# ──────────────────────────────────────────────────────────────────────
# Floor #5 — help-request regex backstop
# ──────────────────────────────────────────────────────────────────────


def _floor_help_request_regex(
    current_move: str,
    *,
    student_input: str,
    profile_summary: str,
) -> tuple[str, Optional[str]]:
    """Belt-to-the-router's-braces help-request backstop.

    Plan §2.3 row #5: when the latest student turn matches a high-
    confidence help-request pattern AND the router did NOT already
    pick ``explain`` / ``worked_example``, override to ``explain``
    (or ``worked_example`` when the profile mentions struggle). Catches
    the GEO-S5 P1-3 shape where the standalone Haiku intent classifier
    failed soft.
    """
    if current_move in ("explain", "worked_example"):
        return current_move, None
    # ``close_topic`` is terminal — once a higher floor has force-closed
    # the topic, the help regex must not re-open it. Plan §5.2 ordering
    # test pins this contract.
    if current_move == "close_topic":
        return current_move, None
    text = (student_input or "").strip()
    if not text:
        return current_move, None
    if not _HELP_REQUEST_RE.search(text):
        return current_move, None
    target = (
        "worked_example"
        if profile_summary and "struggl" in profile_summary.lower()
        else "explain"
    )
    return target, "help_request_regex_backstop"


# ──────────────────────────────────────────────────────────────────────
# Observability
# ──────────────────────────────────────────────────────────────────────


def _emit_override_span(*, from_move: str, to_move: str, floor: str) -> None:
    """Record one floor override on the audit trace."""
    with emit_span("audit", "router.floor_override") as span:
        if span is not None:
            span["payload"] = {
                "from_move": from_move,
                "to_move": to_move,
                "floor": floor,
            }
