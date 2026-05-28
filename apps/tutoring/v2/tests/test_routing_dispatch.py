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
    SafetyValveCounters,
    SessionRuntimeState,
    StudentSafeFeedback,
    TutoringContext,
    Verdict,
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
    principles=None,
    focus_note: str = "stay on the open question",
) -> RouterDecision:
    return RouterDecision(
        chosen_move=move,
        principle_emphasis=principles or ["Targeted Remediation"],
        focus_note=focus_note,
        rationale="test",
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


def test_pick_move_returns_router_chosen_move_unchanged_when_no_floor():
    router = _FakeRouter(_decision(move="scaffold_hint"))
    engine = _engine_with(router)
    move, focus, principles, decision = engine.pick_move(
        context=_context(),
        verdict=None,
        student_input="12",
        pose_tool_available=True,
    )
    assert move == "scaffold_hint"
    assert focus == "stay on the open question"
    assert principles == ["Targeted Remediation"]
    assert decision.chosen_move == "scaffold_hint"


def test_pick_move_passes_through_focus_note_and_principles():
    router = _FakeRouter(_decision(
        move="explain",
        principles=["Direct Instruction", "Cognitive Load"],
        focus_note="define condensation in plain language",
    ))
    engine = _engine_with(router)
    move, focus, principles, _ = engine.pick_move(
        context=_context(),
        verdict=None,
        student_input="i don't understand",
        pose_tool_available=True,
    )
    assert move == "explain"
    assert "condensation" in focus
    assert "Direct Instruction" in principles


def test_pick_move_threads_objective_progress_into_router_request():
    state = SessionRuntimeState()
    state.objective_progress["obj1"] = ObjectiveProgress(
        objective="obj1", attempts=2, correct=1, wrong=1,
    )
    router = _FakeRouter(_decision(move="scaffold_hint"))
    engine = _engine_with(router)
    engine.pick_move(
        context=_context(runtime_state=state),
        verdict=None,
        student_input="12",
        pose_tool_available=True,
    )
    assert router.last_request.objective_attempts == 2
    assert router.last_request.objective_correct == 1


def test_pick_move_threads_pose_tool_available_into_request():
    router = _FakeRouter(_decision(move="scaffold_hint"))
    engine = _engine_with(router)
    engine.pick_move(
        context=_context(),
        verdict=None,
        student_input="12",
        pose_tool_available=False,
    )
    assert router.last_request.pose_tool_available is False


def test_pick_move_fail_soft_when_router_returns_unknown_move():
    """Defensive normalization — should never fire in production but
    must produce a valid move under any router output."""
    # Construct a RouterDecision that bypasses Pydantic's Literal check
    # by hand-crafting via model_construct (unsafe constructor).
    bad_decision = RouterDecision.model_construct(
        chosen_move="not_a_real_move",
        principle_emphasis=["Active Learning"],
        focus_note="",
        rationale="",
    )
    router = _FakeRouter(bad_decision)
    engine = _engine_with(router)
    move, _, _, _ = engine.pick_move(
        context=_context(),
        verdict=None,
        student_input="12",
        pose_tool_available=True,
    )
    assert move == "scaffold_hint"
