"""Tests for the run-5 MATHS-S1 / GEO-S5 evaluation fixes (post-router cutover).

Covers what survives the router cutover:

  1. ``open_question_stickiness`` no longer applies to
     ``confirm_and_extend`` — its contract is to advance, not stay.
     This was the root cause of the GEO-S5 P1 cascade where every
     correct rich answer fell into the "let's slow down" terminal.

  2. ``TutorEngine._advance_step_if_possible`` advances the session's
     ``current_step_index`` when more steps remain, returns ``None``
     on the final step.

The intent-classifier and ``select_move`` behaviour sections that
lived here originally have been retired alongside their modules
(plan §3). Equivalent coverage is in:
  - ``apps/tutoring/v2/tests/test_move_router.py`` — router pick on
    help-request shapes + fail-soft contract.
  - ``apps/tutoring/v2/tests/test_safety_floors.py`` — the help-request
    regex safety floor that backstops the router LLM.
"""

from __future__ import annotations

from types import SimpleNamespace

from apps.tutoring.v2.contracts import (
    OpenQuestion,
    PendingPose,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.services.conformance.gates import (
    run_open_question_stickiness_check,
)


# ──────────────────────────────────────────────────────────────────────
# 1. open_question_stickiness — confirm_and_extend is no longer in
#    scope. The move's contract is to advance after a correct answer.
# ──────────────────────────────────────────────────────────────────────


def _open_q(qid: int = 100) -> OpenQuestion:
    return OpenQuestion(
        source=QuestionSource.LESSON_STEP,
        id=qid,
        rendered_stem="prior question",
    )


def _new_pose(qid: int = 200) -> PendingPose:
    return PendingPose(
        question_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=qid),
        canonical="42",
        rendered_stem="new question",
        jaccard_signature="sig",
        visible_context=VisibleContextSnapshot(
            visible_prompt="", attached_media_ids=[], recent_transcript=[],
        ),
    )


def test_stickiness_skips_confirm_and_extend_with_new_pose():
    """confirm_and_extend posing a NEW item must pass the gate.

    Before the run-5 fix this was treated as a stay-on-item move; a
    new pose triggered a stickiness violation and the safety
    terminal shipped "let's slow down on the same question" on a
    correct answer — the GEO-S5 P1.
    """
    state = SessionRuntimeState(open_question=_open_q(100))
    result = run_open_question_stickiness_check(
        selected_move="confirm_and_extend",
        runtime_state=state,
        pending_pose=_new_pose(200),
    )
    assert result.passed
    assert result.skipped


def test_stickiness_still_blocks_scaffold_hint_on_new_pose():
    """scaffold_hint posing a NEW item must still trigger a failure.

    The fix only removed confirm_and_extend; scaffold_hint /
    name_misconception are still stay-on-item moves and the safety
    floor still applies.
    """
    state = SessionRuntimeState(open_question=_open_q(100))
    result = run_open_question_stickiness_check(
        selected_move="scaffold_hint",
        runtime_state=state,
        pending_pose=_new_pose(200),
    )
    assert not result.passed
    assert "open_question_stickiness" in result.name


def test_stickiness_passes_scaffold_hint_when_pose_matches_open():
    """scaffold_hint re-posing the SAME open item passes the gate."""
    state = SessionRuntimeState(open_question=_open_q(100))
    result = run_open_question_stickiness_check(
        selected_move="scaffold_hint",
        runtime_state=state,
        pending_pose=_new_pose(100),  # same id
    )
    assert result.passed


def test_stickiness_skips_close_topic_and_pivot():
    """close_topic / pivot are out of scope for the stickiness gate."""
    state = SessionRuntimeState(open_question=_open_q(100))
    for move in ("close_topic", "pivot", "worked_example", "explain"):
        result = run_open_question_stickiness_check(
            selected_move=move,
            runtime_state=state,
            pending_pose=_new_pose(200),
        )
        assert result.passed, f"move={move} unexpectedly failed gate"
        assert result.skipped


# ──────────────────────────────────────────────────────────────────────
# 2. TutorEngine._advance_step_if_possible step-advance contract.
# ──────────────────────────────────────────────────────────────────────


def test_advance_step_returns_next_when_more_remain():
    """When steps[idx+1] exists, _advance_step_if_possible returns idx+1."""
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    # Stub session + lesson with 5 steps and current index 1.
    fake_lesson = SimpleNamespace(
        steps=SimpleNamespace(count=lambda: 5, all=lambda: [object()] * 5),
    )
    fake_session = SimpleNamespace(
        lesson=fake_lesson,
        current_step_index=1,
        save=lambda update_fields=None: None,
    )
    fake_cm = SimpleNamespace(session=fake_session)
    engine = TutorEngine.__new__(TutorEngine)
    engine.context_manager = fake_cm

    state = SessionRuntimeState(
        open_question=_open_q(100),
        attempts_on_open_question=2,
        unverified_run_length=1,
    )
    next_idx = engine._advance_step_if_possible(runtime_state=state)
    assert next_idx == 2
    # Side effects: state reset.
    assert state.open_question is None
    assert state.attempts_on_open_question == 0
    assert state.unverified_run_length == 0
    # And the session row was advanced.
    assert fake_session.current_step_index == 2


def test_advance_step_returns_none_on_final_step():
    """When the active step is the LAST, advance returns None (lesson done)."""
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    fake_lesson = SimpleNamespace(
        steps=SimpleNamespace(count=lambda: 5, all=lambda: [object()] * 5),
    )
    fake_session = SimpleNamespace(
        lesson=fake_lesson,
        current_step_index=4,  # last index
        save=lambda update_fields=None: None,
    )
    fake_cm = SimpleNamespace(session=fake_session)
    engine = TutorEngine.__new__(TutorEngine)
    engine.context_manager = fake_cm

    state = SessionRuntimeState(open_question=_open_q(100))
    result = engine._advance_step_if_possible(runtime_state=state)
    assert result is None
    # State NOT reset (engine will complete the session instead).
    assert state.open_question is not None


def test_advance_step_failsoft_on_missing_lesson():
    """No lesson → returns None (treated as final, session completes)."""
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    fake_session = SimpleNamespace(lesson=None, current_step_index=0)
    fake_cm = SimpleNamespace(session=fake_session)
    engine = TutorEngine.__new__(TutorEngine)
    engine.context_manager = fake_cm

    state = SessionRuntimeState()
    assert engine._advance_step_if_possible(runtime_state=state) is None
