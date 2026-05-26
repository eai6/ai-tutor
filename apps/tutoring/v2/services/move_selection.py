"""Pure-function move selection — Phase 2 §2.3.

Move selection is NOT an LLM call. Inputs per analysis §4 + Phase 2 §2.3:
  - verdict.kind (correct | wrong | partial | unverified)
  - attempts_on_open_question
  - objective_progress
  - unverified_run_length
  - current_move
  - move_history
  - profile_summary  (free-text only — NOT skills_snapshot, NOT
                      StudentSkillMastery rows)

verdict.bare_answer is **explicitly NOT** a move-selection input — it
biases the selected move's prompt content (Phase 2 §2.1.1). Move
selection sees only verdict.kind.

The 9 moves per analysis §4:
  pose_question, confirm_and_advance, confirm_and_extend,
  scaffold_hint, name_misconception, worked_example, explain,
  pivot, close_topic.

Safety valves (§7 item 3) fire OUTSIDE this module — they live in
``TutorEngine``. Per the analysis: "pivot / close_topic should fire
first under normal conditions".
"""

from __future__ import annotations

from typing import Optional

from apps.tutoring.v2.contracts import (
    GradingResult,
    ObjectiveProgress,
    SessionRuntimeState,
    Verdict,
)


ALLOWED_MOVES = (
    "pose_question",
    "confirm_and_advance",
    "confirm_and_extend",
    "scaffold_hint",
    "name_misconception",
    "worked_example",
    "explain",
    "pivot",
    "close_topic",
)


# Per analysis §4, objective evidence is sufficient when the student
# has demonstrated mastery on this objective. Conservative default:
#   - ≥ 2 correct verdicts on this objective AND
#   - correct/(attempts) ratio ≥ 50%
# Sub-decision per §7 item 3 — tune from pilot data.
_OBJECTIVE_MIN_CORRECT = 2
_OBJECTIVE_MIN_RATIO = 0.5


def select_move(
    *,
    verdict: Optional[GradingResult],
    runtime_state: SessionRuntimeState,
    profile_summary: str = "",
    objective_just_opened: bool = False,
    current_objective: str = "",
) -> str:
    """Deterministic move pick. Returns one of ``ALLOWED_MOVES``.

    Args:
      verdict: grader output for the just-graded student input, or
        ``None`` on transitional / opening turns.
      runtime_state: the loaded ``SessionRuntimeState``.
      profile_summary: free-text qualitative recall. Read but NOT used
        as a primary discriminator in MVP — present so future tuning
        can lean on it without changing the contract.
      objective_just_opened: True on the very first turn of a new
        objective (TutorEngine bookkeeping).
      current_objective: enabling-objective slug for ``objective_progress``
        lookup.
    """
    attempts = runtime_state.attempts_on_open_question
    counters = runtime_state.safety_valve_counters
    remediation = runtime_state.remediation_state
    move_history = list(runtime_state.move_history or [])

    # ── Safety-valve-adjacent close conditions (the engine's safety
    # valves are checked separately, but `close_topic` is also the
    # right move when objective evidence has saturated — fire it
    # eagerly rather than wait for the outer cap.)
    obj_progress = runtime_state.objective_progress.get(current_objective)
    if _objective_evidence_sufficient(obj_progress):
        return "close_topic"

    # ── No verdict this turn (opening / transitional / free-chat) ──
    if verdict is None:
        # First turn on a new objective → frame the concept first.
        if objective_just_opened:
            # If the student profile suggests prior struggle, lead with
            # a worked example instead of a bare explanation.
            if profile_summary and "struggl" in profile_summary.lower():
                return "worked_example"
            return "explain"
        # No open question and nothing graded → invite engagement.
        if runtime_state.open_question is None:
            return "pose_question"
        # Still awaiting an answer on the open question and we have no
        # verdict to react to — keep the floor with the student.
        return "pose_question"

    kind = verdict.verdict

    # ── verdict=correct branch ──
    if kind == Verdict.CORRECT:
        # If this correct closes out the objective, the
        # `_objective_evidence_sufficient` check above will have
        # returned `close_topic` already. So here we are still inside
        # the objective. Default to confirm_and_advance; switch to
        # confirm_and_extend on early mastery (first correct after
        # zero/one attempt — student likely had the idea already).
        if attempts <= 1 and obj_progress and obj_progress.correct >= 1:
            return "confirm_and_extend"
        return "confirm_and_advance"

    # ── verdict=wrong branch ──
    if kind == Verdict.WRONG:
        # `pivot` fires when:
        #  (a) 4+ wrong attempts on the same item, or
        #  (b) name_misconception fired previously on this item AND the
        #      very next attempt is still wrong.
        if attempts >= 4:
            return "pivot"
        if (
            remediation is not None
            and not remediation.resolved
            and _just_after(move_history, "name_misconception")
        ):
            return "pivot"
        # `name_misconception` on the 3rd wrong attempt.
        if attempts >= 3:
            return "name_misconception"
        # Default scaffolding on attempts 1–2.
        return "scaffold_hint"

    # ── verdict=partial branch ──
    if kind == Verdict.PARTIAL:
        # Scaffold the missing piece; if the student has been stuck a
        # while on this item, escalate to worked_example.
        if attempts >= 3:
            return "worked_example"
        return "scaffold_hint"

    # ── verdict=unverified branch ──
    if kind == Verdict.UNVERIFIED:
        # Three consecutive unverified turns → explain and reset.
        if runtime_state.unverified_run_length >= 3:
            return "explain"
        # When an open_question is still in flight, scaffold back to
        # it rather than pose a fresh one — the open Q stays the
        # focus, and ``scaffold_hint``'s prompt surfaces uncertainty
        # by design (it cites ``what_right`` / ``what_missing`` from
        # student_safe_feedback, which the conformance classifier
        # reads as a hedged stance).
        if runtime_state.open_question is not None:
            return "scaffold_hint"
        # No open question (free-chat / topic-divergence) → probe
        # via a fresh tool-posed question. The tutor's pose_question
        # move prompt is verdict-aware and surfaces uncertainty in
        # the lead_in on unverified turns.
        return "pose_question"

    # Defensive default.
    return "pose_question"


def _objective_evidence_sufficient(
    progress: Optional[ObjectiveProgress],
) -> bool:
    """Decide whether the current objective has enough evidence to close."""
    if progress is None:
        return False
    if progress.closed:
        # Already closed — don't double-fire close_topic.
        return False
    if progress.attempts <= 0:
        return False
    if progress.correct < _OBJECTIVE_MIN_CORRECT:
        return False
    ratio = progress.correct / max(1, progress.attempts)
    return ratio >= _OBJECTIVE_MIN_RATIO


def _just_after(move_history: list[str], target_move: str) -> bool:
    """True when the immediately-previous move equals ``target_move``."""
    if not move_history:
        return False
    return move_history[-1] == target_move
