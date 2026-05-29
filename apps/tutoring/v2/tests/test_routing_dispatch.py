"""Routing-dispatch tests.

Two unrelated dispatch surfaces — kept in one file because both are
the "routing" entry point the engine code refers to:

  1. ENGINE dispatch (legacy vs v2) via ``ensure_engine_version_set``
     — Phase 1 contract: ``NEW_TUTOR=off`` keeps legacy; ``NEW_TUTOR=on``
     stamps engine_version='v2' and initializes runtime_state.

  2. MOVE dispatch via ``TutorEngine.pick_move`` — post-prune (commit
     A of v2-prune-plan.md): the router LLM picks a move and the
     engine does not override (no safety_floors layer). The tests use
     a ``FakeRouter`` so we don't burn an LLM in unit tests.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import pytest

from apps.tutoring.v2.contracts import (
    ObjectiveProgress,
    RouterDecision,
    SessionRuntimeState,
    TutoringContext,
)
from apps.tutoring.v2.routing import (
    ensure_engine_version_set,
    is_v2_session,
)


class _FakeSession:
    def __init__(self):
        self.engine_version = ""
        self.runtime_state = {}
        self.saved_fields: list[list[str]] = []

    def save(self, update_fields=None):
        self.saved_fields.append(list(update_fields or []))


class RoutingDispatchTest(TestCase):
    def test_new_tutor_off_picks_legacy(self):
        sess = _FakeSession()
        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            chosen = ensure_engine_version_set(sess)
        self.assertEqual(chosen, "legacy")
        self.assertEqual(sess.engine_version, "legacy")
        self.assertFalse(is_v2_session(sess))
        # runtime_state untouched on legacy
        self.assertEqual(sess.runtime_state, {})

    def test_new_tutor_on_picks_v2_and_writes_runtime_state(self):
        sess = _FakeSession()
        with patch.dict("os.environ", {"NEW_TUTOR": "on"}):
            chosen = ensure_engine_version_set(sess)
        self.assertEqual(chosen, "v2")
        self.assertEqual(sess.engine_version, "v2")
        self.assertTrue(is_v2_session(sess))
        self.assertNotEqual(sess.runtime_state, {})
        # runtime_state must round-trip through SessionRuntimeState
        state = SessionRuntimeState.from_jsonable(sess.runtime_state)
        self.assertIsInstance(state, SessionRuntimeState)
        self.assertEqual(state.schema_version, 1)

    def test_sticky_engine_version_does_not_flip(self):
        sess = _FakeSession()
        sess.engine_version = "legacy"
        with patch.dict("os.environ", {"NEW_TUTOR": "on"}):
            chosen = ensure_engine_version_set(sess)
        self.assertEqual(chosen, "legacy")

    def test_sticky_v2_preserved(self):
        sess = _FakeSession()
        sess.engine_version = "v2"
        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            chosen = ensure_engine_version_set(sess)
        self.assertEqual(chosen, "v2")


# ──────────────────────────────────────────────────────────────────────
# MOVE dispatch (TutorEngine.pick_move + safety floors)
# ──────────────────────────────────────────────────────────────────────


class _FakeRouter:
    """Returns a pre-canned ``RouterDecision`` and records the request."""

    def __init__(self, decision: RouterDecision) -> None:
        self.decision = decision
        self.last_request = None

    def route(self, request):
        self.last_request = request
        return self.decision


def _decision(
    move: str = "scaffold_hint",
    *,
    case: str = "help_request",
    reason: str = "stay on the open question",
) -> RouterDecision:
    """Build a non-answer-attempt RouterDecision."""
    return RouterDecision(
        case=case,
        verdict_needed=False,
        move=move,
        reason=reason,
    )


def _attempt_decision(
    *,
    correct: str = "confirm_and_advance",
    partial: str = "scaffold_hint",
    wrong: str = "scaffold_hint",
    reason: str = "test answer-attempt decision",
) -> RouterDecision:
    return RouterDecision(
        case="answer_attempt",
        verdict_needed=True,
        moves_by_verdict={
            "correct": correct,
            "partial": partial,
            "wrong": wrong,
        },
        reason=reason,
    )


def _context(
    *,
    runtime_state: SessionRuntimeState = None,
    current_objective: str = "obj1",
    transcript: list[dict] = None,
) -> TutoringContext:
    return TutoringContext(
        session_id=1,
        student_id=1,
        institution_id=1,
        lesson_id=1,
        runtime_state=runtime_state or SessionRuntimeState(),
        current_objective=current_objective,
        full_transcript=transcript or [],
    )


def _engine_with(router: _FakeRouter):
    """Minimal TutorEngine wired to a FakeRouter."""
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    engine = TutorEngine.__new__(TutorEngine)
    engine.move_router = router
    return engine


def test_pick_move_returns_router_move_for_non_attempt():
    router = _FakeRouter(_decision(move="scaffold_hint"))
    engine = _engine_with(router)
    move, reason, decision = engine.pick_move(
        context=_context(),
        student_input="12",
        pose_tool_available=True,
    )
    assert move == "scaffold_hint"
    assert reason == "stay on the open question"
    assert decision.case == "help_request"
    assert decision.move == "scaffold_hint"


def test_pick_move_passes_through_router_reason():
    router = _FakeRouter(_decision(
        move="explain",
        reason="define condensation in plain language",
    ))
    engine = _engine_with(router)
    move, reason, _ = engine.pick_move(
        context=_context(),
        student_input="i don't understand",
        pose_tool_available=True,
    )
    assert move == "explain"
    assert "condensation" in reason


def test_pick_move_threads_objective_progress_into_router_request():
    state = SessionRuntimeState()
    state.objective_progress["obj1"] = ObjectiveProgress(
        objective="obj1", attempts=2, correct=1, wrong=1,
    )
    router = _FakeRouter(_decision(move="scaffold_hint"))
    engine = _engine_with(router)
    engine.pick_move(
        context=_context(runtime_state=state),
        student_input="12",
        pose_tool_available=True,
    )
    assert router.last_request.objective_attempts == 2
    assert router.last_request.objective_correct == 1
    # New counter fields derived from progress.
    assert router.last_request.prior_answer_attempts_on_objective == 2
    assert router.last_request.correct_on_objective == 1


def test_pick_move_threads_pose_tool_available_into_request():
    router = _FakeRouter(_decision(move="scaffold_hint"))
    engine = _engine_with(router)
    engine.pick_move(
        context=_context(),
        student_input="12",
        pose_tool_available=False,
    )
    assert router.last_request.pose_tool_available is False


def test_pick_move_raises_on_unknown_move_from_router():
    """Closed-set contract: MoveRouter.route promises every move it
    returns is in ALLOWED_MOVES (validated + one retry on violation).
    If a move outside the set somehow reaches the engine — a contract
    violation — the engine raises rather than silently coercing to a
    default. The dispatch layer's exception handler then ships the
    standard graceful envelope.

    This test exercises the engine in isolation via a _FakeRouter that
    bypasses the validating route() loop; in production the LLM
    router enforces the set itself.
    """
    bad_decision = RouterDecision.model_construct(
        case="help_request",
        verdict_needed=False,
        move="not_a_real_move",
        reason="",
        moves_by_verdict=None,
    )
    router = _FakeRouter(bad_decision)
    engine = _engine_with(router)
    with pytest.raises(RuntimeError, match="not in ALLOWED_MOVES"):
        engine.pick_move(
            context=_context(),
            student_input="12",
            pose_tool_available=True,
        )


def test_pick_move_resolves_answer_attempt_to_wrong_branch_without_verdict():
    """start_session-style call (no verdict): when the router returns
    an answer-attempt decision, the engine resolves to the wrong
    branch (most-conservative)."""
    router = _FakeRouter(_attempt_decision(
        correct="confirm_and_advance",
        partial="scaffold_hint",
        wrong="pivot",
    ))
    engine = _engine_with(router)
    move, _, _ = engine.pick_move(
        context=_context(),
        student_input="hi",
        pose_tool_available=True,
    )
    # No verdict on this code path → use moves_by_verdict["wrong"].
    assert move == "pivot"
