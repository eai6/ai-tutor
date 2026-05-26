"""Session state utilities for the simple-tutor engine.

Pure functions that read/write engine state without doing any LLM
calls. Used by the engine main loop (M9) to:
  - Build the rolling context window for the system prompt
  - Compute per-step summaries on advance_step (NOT mid-turn)
  - Manage the pose_question / record_answer state field

Design rules (auto-memory/feedback_simple_tutor_engine_design.md):
  - Sliding window: last 6-10 turns verbatim, scoped to the current step.
  - Older steps replaced by ONE-LINE deterministic summary keyed by
    LessonStep.id. NEVER summarize mid-turn via LLM (cost + variance).
  - All state lives in DB rows mutated only by tool handlers. The LLM
    never writes engine state directly.

Module shape — all functions accept a ``TutorSession`` and return
plain Python types (list / str). No I/O beyond ORM reads + simple
field updates.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Union

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from apps.tutoring.models import TutorSession, SessionTurn
    from apps.curriculum.models import LessonStep

    StepArg = Union[LessonStep, int]
else:
    StepArg = Union[object, int]


# Defaults — see auto-memory/feedback_simple_tutor_engine_design.md
DEFAULT_RECENT_WINDOW_TURNS = 8


# ============================================================================
# Sliding window — last N turns of the CURRENT step
# ============================================================================


def build_recent_window(
    session: 'TutorSession',
    max_turns: int = DEFAULT_RECENT_WINDOW_TURNS,
) -> 'list[SessionTurn]':
    """Return the last ``max_turns`` SessionTurns from the session's
    CURRENT step.

    Scoped to the current step so older steps' turns don't pollute the
    context. Older steps are represented by step-anchored summaries via
    :func:`step_summary_log` instead.

    Args:
        session: TutorSession instance.
        max_turns: Cap on the number of turns returned. Defaults to 8.

    Returns:
        List of SessionTurn instances, oldest → newest.
        Empty list if session has no turns yet OR if the current step
        has no turns yet (e.g., session just advanced).
    """
    if max_turns <= 0:
        return []

    current_step = _current_step(session)
    if current_step is None:
        # No active step (session not started or completed). Window
        # spans the entire session as a last-ditch fallback.
        qs = session.turns.order_by('-created_at')[:max_turns]
        return list(reversed(list(qs)))

    qs = (
        session.turns
        .filter(step=current_step)
        .order_by('-created_at')[:max_turns]
    )
    return list(reversed(list(qs)))


# ============================================================================
# Step summaries — pre-computed on advance_step, NOT mid-turn
# ============================================================================


def build_step_summary(session: 'TutorSession', step: StepArg) -> str:
    """Compute a one-line summary of student performance on a step.

    Reads SessionTurn.judge_outputs (the grader verdicts written by the
    record_answer tool handler) to count attempts + verdict breakdown.

    Deterministic: same inputs → same string. No LLM. Cheap.

    Args:
        session: TutorSession instance.
        step: LessonStep instance OR int (order_index).

    Returns:
        One-line string like
        ``"Step 3 (Explore) — 2 student attempts; verdicts: correct"``
        or
        ``"Step 1 (Engage) — no student responses"``
    """
    step_label = _step_label(step)

    turns = list(session.turns.filter(step=step).order_by('created_at'))
    if not turns:
        return f"{step_label} — no student responses"

    # Count student attempts (one per student turn) and collect verdicts
    # from the immediately-following tutor turn's judge_outputs.
    student_count = sum(1 for t in turns if t.role == 'student')

    verdicts = []
    for t in turns:
        if t.role != 'tutor':
            continue
        jo = t.judge_outputs or {}
        if not isinstance(jo, dict):
            continue
        # The simple-tutor's record_answer handler writes its GradeResult
        # under a 'grader' key inside judge_outputs.
        grader_entry = jo.get('grader')
        if isinstance(grader_entry, dict):
            verdict = grader_entry.get('verdict')
            if verdict in ('correct', 'partial', 'incorrect'):
                verdicts.append(verdict)

    if not verdicts:
        # Student responded but no grader verdicts were recorded — they
        # were probably clarifying questions, not answer attempts.
        return f"{step_label} — {student_count} student response(s), no graded answers"

    return (
        f"{step_label} — {student_count} student attempt(s); "
        f"verdicts: {', '.join(verdicts)}"
    )


def step_summary_log(session: 'TutorSession') -> list[str]:
    """One-line summary per COMPLETED step, in order.

    Completed steps = those whose order_index < session.current_step_index.
    The current step (in-progress) is NOT in the log — its turns live in
    the recent_window instead.

    Used to build the ``<history_summary>`` block of the system prompt.

    Returns:
        List of one-line summary strings, oldest → newest. Empty list
        if the session is on the first step.
    """
    current_index = getattr(session, 'current_step_index', 0) or 0
    if current_index <= 0:
        return []

    from apps.curriculum.models import LessonStep
    completed_steps = (
        LessonStep.objects
        .filter(lesson=session.lesson, order_index__lt=current_index)
        .order_by('order_index')
    )
    return [build_step_summary(session, step) for step in completed_steps]


# ============================================================================
# pose_question / record_answer state
# ============================================================================


def set_current_question(session: 'TutorSession', question_id: int) -> None:
    """Mark a question as currently posed by the tutor.

    Mutates and saves ``session.current_question_id``. The next student
    turn is expected to be an answer attempt against this question.

    Idempotent — calling twice with the same id is a no-op for the
    persisted state (the timestamp on ``updated_at`` may move).

    Args:
        session: TutorSession instance.
        question_id: The ExitTicketQuestion (or step-question) PK the
            tutor is asking about.
    """
    if session.current_question_id == question_id:
        return
    session.current_question_id = question_id
    session.save(update_fields=['current_question_id'])


def clear_current_question(session: 'TutorSession') -> None:
    """Clear the currently-posed-question marker.

    Called by record_answer (after grading) and by advance_step (when
    moving to the next step). After clearing, the next student turn is
    treated as a clarification or open response, not an answer attempt.
    """
    if session.current_question_id is None:
        return
    session.current_question_id = None
    session.save(update_fields=['current_question_id'])


# ============================================================================
# Internal helpers
# ============================================================================


def _current_step(session: 'TutorSession'):
    """Resolve the session's current LessonStep by current_step_index.

    Returns None if the session is past the last step (exit ticket
    mode) or if the lesson has no steps.
    """
    from apps.curriculum.models import LessonStep
    current_index = getattr(session, 'current_step_index', 0) or 0
    return (
        LessonStep.objects
        .filter(lesson=session.lesson, order_index=current_index)
        .first()
    )


def _step_label(step: StepArg) -> str:
    """Human-readable label for a step. Accepts a LessonStep instance
    or an int order_index (returns a coarser label in the latter case).
    """
    if step is None:
        return "Step ?"
    if isinstance(step, int):
        return f"Step {step + 1}"
    phase = (getattr(step, 'phase', '') or '').capitalize()
    idx = getattr(step, 'order_index', None)
    n = (idx + 1) if isinstance(idx, int) else '?'
    if phase:
        return f"Step {n} ({phase})"
    return f"Step {n}"
