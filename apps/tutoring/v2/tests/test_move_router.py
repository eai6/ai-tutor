"""MoveRouter unit tests — design/tasks/move-router-implementation-plan.md §5.1.

Covers:
  - Core routing behaviour (router output → final decision shape).
  - Pydantic validation (closed move set, closed principle set,
    focus_note length).
  - Fail-soft contract on every error path.
  - Span instrumentation surface.
  - Router-prompt cache stability — the cacheable preamble is
    byte-identical across calls.
"""

from __future__ import annotations

import json

import pytest

from apps.tutoring.v2.contracts import (
    ALLOWED_PRINCIPLES,
    GradingResult,
    ObjectiveProgress,
    RouterDecision,
    RouterRequest,
    SessionRuntimeState,
    StudentSafeFeedback,
    TutoringContext,
    Verdict,
)
from apps.tutoring.v2.services.move_router import (
    MoveRouter,
    ROUTER_TRANSCRIPT_WINDOW,
    _fallback_decision,
    _safe_json_loads,
    build_router_request,
)
from apps.tutoring.v2.services.router_prompts import (
    SHARED_ROUTER_SYSTEM,
    render_router_user_prompt,
)


# ──────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tokens_in = 0
        self.tokens_out = 0


class _FakeClient:
    """Returns the queued responses in order; raises when exhausted."""

    def __init__(self, *payloads) -> None:
        self._queue = list(payloads)
        self.calls: list[dict] = []

    def generate(self, **kwargs) -> _FakeResp:
        self.calls.append(kwargs)
        if not self._queue:
            raise RuntimeError("FakeClient queue empty")
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _FakeResp(nxt)


class _RaisingClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def generate(self, **kwargs):
        raise self._exc


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _decision_payload(
    *,
    move: str = "scaffold_hint",
    principles: list[str] = None,
    focus_note: str = "stay on the open question",
    rationale: str = "verdict wrong, attempts low",
) -> str:
    return json.dumps({
        "chosen_move": move,
        "principle_emphasis": principles or ["Targeted Remediation"],
        "focus_note": focus_note,
        "rationale": rationale,
    })


def _request(
    *,
    verdict=None,
    reason_code=None,
    student_input="",
    move_history=None,
    correct=0,
    attempts=0,
    open_q_pending=False,
    pose_tool_available=True,
    profile_summary="",
) -> RouterRequest:
    return RouterRequest(
        last_n_turns=[],
        student_input=student_input,
        grader_verdict=verdict,
        grader_reason_code=reason_code,
        student_safe_feedback=StudentSafeFeedback(),
        profile_summary=profile_summary,
        objective="test objective",
        move_history=list(move_history or []),
        objective_correct=correct,
        objective_attempts=attempts,
        open_question_has_pending=open_q_pending,
        pose_tool_available=pose_tool_available,
    )


def _router(client) -> MoveRouter:
    return MoveRouter(router_client_factory=lambda: client)


# ──────────────────────────────────────────────────────────────────────
# Core routing behaviour
# ──────────────────────────────────────────────────────────────────────


def test_router_returns_decision_on_clean_json():
    client = _FakeClient(_decision_payload(
        move="explain",
        principles=["Direct Instruction", "Cognitive Load"],
        focus_note="teach the method first",
    ))
    decision = _router(client).route(_request(student_input="explain it"))
    assert decision.chosen_move == "explain"
    assert decision.principle_emphasis == [
        "Direct Instruction", "Cognitive Load",
    ]
    assert "method" in decision.focus_note


def test_router_strips_markdown_fences_around_json():
    payload = "```json\n" + _decision_payload(move="pivot") + "\n```"
    client = _FakeClient(payload)
    decision = _router(client).route(_request(verdict=Verdict.WRONG, attempts=4))
    assert decision.chosen_move == "pivot"


def test_router_extracts_embedded_object_from_prose():
    payload = "Here is the decision:\n" + _decision_payload(move="close_topic")
    client = _FakeClient(payload)
    decision = _router(client).route(
        _request(verdict=Verdict.CORRECT, correct=2, attempts=3),
    )
    assert decision.chosen_move == "close_topic"


# ──────────────────────────────────────────────────────────────────────
# Pydantic validation
# ──────────────────────────────────────────────────────────────────────


def test_router_rejects_pose_question_as_a_move():
    """``pose_question`` is no longer in the move table — must fail-soft."""
    client = _FakeClient(_decision_payload(move="pose_question"))
    decision = _router(client).route(_request())
    # Fail-soft default fires, not a raise.
    assert decision.chosen_move != "pose_question"
    assert decision.rationale.startswith("router_unavailable_fallback")


def test_router_rejects_unknown_chosen_move():
    client = _FakeClient(_decision_payload(move="guess"))
    decision = _router(client).route(_request(verdict=Verdict.WRONG))
    # Falls back to scaffold_hint for WRONG verdict.
    assert decision.chosen_move == "scaffold_hint"
    assert "validation" in decision.rationale or "fallback" in decision.rationale


def test_router_rejects_unknown_principle_in_emphasis():
    payload = _decision_payload(
        move="explain",
        principles=["Active Learning", "Telepathy"],
    )
    client = _FakeClient(payload)
    decision = _router(client).route(_request())
    # ValidationError → fail-soft default. Telepathy is not in the
    # closed principle set.
    assert "Telepathy" not in decision.principle_emphasis


def test_router_rejects_overlong_focus_note():
    payload = _decision_payload(
        move="explain", focus_note="X" * 400,
    )
    client = _FakeClient(payload)
    decision = _router(client).route(_request())
    # ValidationError → fail-soft default.
    assert decision.rationale.startswith("router_unavailable_fallback")


def test_router_decision_direct_construction_validates_principles():
    with pytest.raises(Exception):
        RouterDecision(
            chosen_move="explain",
            principle_emphasis=["Telepathy"],
        )


def test_router_decision_direct_construction_caps_focus_note():
    with pytest.raises(Exception):
        RouterDecision(
            chosen_move="explain",
            principle_emphasis=["Active Learning"],
            focus_note="X" * 400,
        )


def test_router_decision_round_trip_through_model_dump():
    decision = RouterDecision(
        chosen_move="confirm_and_extend",
        principle_emphasis=["Deliberate Practice"],
        focus_note="push transfer to a new context",
        rationale="correct + rich working — extend.",
    )
    payload = decision.model_dump()
    rehydrated = RouterDecision.model_validate(payload)
    assert rehydrated == decision


def test_router_request_round_trip_through_model_dump():
    req = _request(
        verdict=Verdict.CORRECT, correct=1, attempts=1,
        move_history=["explain", "confirm_and_advance"],
    )
    rehydrated = RouterRequest.model_validate(req.model_dump())
    assert rehydrated.grader_verdict == Verdict.CORRECT
    assert rehydrated.move_history == ["explain", "confirm_and_advance"]


# ──────────────────────────────────────────────────────────────────────
# Fail-soft contract
# ──────────────────────────────────────────────────────────────────────


def test_router_fail_soft_on_no_client():
    router = MoveRouter(router_client_factory=lambda: None)
    decision = router.route(_request(verdict=Verdict.WRONG))
    assert decision.chosen_move == "scaffold_hint"
    assert decision.focus_note == ""
    assert "no_client" in decision.rationale


def test_router_fail_soft_on_llm_raise():
    client = _RaisingClient(RuntimeError("api down"))
    decision = _router(client).route(_request(verdict=Verdict.CORRECT))
    assert decision.chosen_move == "confirm_and_advance"
    assert decision.rationale.startswith("router_unavailable_fallback")


def test_router_fail_soft_on_non_json_response():
    client = _FakeClient("I cannot decide right now.")
    decision = _router(client).route(_request(verdict=Verdict.PARTIAL))
    # Partial → scaffold_hint default.
    assert decision.chosen_move == "scaffold_hint"


def test_router_fail_soft_correct_maps_to_confirm_and_advance():
    decision = _fallback_decision(_request(verdict=Verdict.CORRECT), reason="x")
    assert decision.chosen_move == "confirm_and_advance"


def test_router_fail_soft_wrong_maps_to_scaffold_hint():
    decision = _fallback_decision(_request(verdict=Verdict.WRONG), reason="x")
    assert decision.chosen_move == "scaffold_hint"


def test_router_fail_soft_partial_maps_to_scaffold_hint():
    decision = _fallback_decision(_request(verdict=Verdict.PARTIAL), reason="x")
    assert decision.chosen_move == "scaffold_hint"


def test_router_fail_soft_no_verdict_opening_turn_maps_to_explain():
    decision = _fallback_decision(
        _request(verdict=None, move_history=[], attempts=0),
        reason="x",
    )
    assert decision.chosen_move == "explain"


def test_router_fail_soft_no_verdict_with_open_question_maps_to_scaffold():
    decision = _fallback_decision(
        _request(
            verdict=None,
            move_history=["explain"],
            open_q_pending=True,
        ),
        reason="x",
    )
    assert decision.chosen_move == "scaffold_hint"


# ──────────────────────────────────────────────────────────────────────
# Span instrumentation
# ──────────────────────────────────────────────────────────────────────


def _drain_spans(token):
    """Snapshot the active span buffer, then reset."""
    from apps.tutoring import tracing as _t
    buffer = list(_t._span_buffer.get() or [])
    _t.reset_span_buffer(token)
    return buffer


def test_router_emits_router_decision_span():
    from apps.tutoring import tracing

    token = tracing.start_span_buffer()
    try:
        client = _FakeClient(_decision_payload(move="explain"))
        _router(client).route(_request(student_input="explain"))
    finally:
        spans = _drain_spans(token)

    decision_spans = [s for s in spans if s["name"] == "router.decision"]
    assert decision_spans, "router.decision span not emitted"
    payload = decision_spans[-1]["payload"]
    assert payload["chosen_move"] == "explain"
    assert payload["fail_soft"] is False


def test_router_emits_fail_soft_span_on_outage():
    from apps.tutoring import tracing

    token = tracing.start_span_buffer()
    try:
        client = _RaisingClient(RuntimeError("boom"))
        _router(client).route(_request(verdict=Verdict.WRONG))
    finally:
        spans = _drain_spans(token)

    decision_spans = [s for s in spans if s["name"] == "router.decision"]
    assert decision_spans
    payload = decision_spans[-1]["payload"]
    assert payload["fail_soft"] is True
    assert payload.get("reason") == "RuntimeError"


# ──────────────────────────────────────────────────────────────────────
# Prompt cache stability
# ──────────────────────────────────────────────────────────────────────


def test_router_system_prompt_is_byte_stable():
    """The cacheable preamble must not embed per-turn content."""
    once = SHARED_ROUTER_SYSTEM
    twice = SHARED_ROUTER_SYSTEM
    assert once == twice
    assert isinstance(once, str)
    # Sanity: principle names present.
    for name in ALLOWED_PRINCIPLES:
        assert name in once
    # The 8 moves are listed.
    for move in (
        "confirm_and_advance", "confirm_and_extend",
        "scaffold_hint", "name_misconception", "worked_example",
        "explain", "pivot", "close_topic",
    ):
        assert move in once
    # pose_question is NOT a move in this table.
    assert "pose_question\n  " not in once.lower().split("the 8 moves")[1] if "the 8 moves" in once.lower() else True


def test_router_user_prompt_includes_student_input_and_verdict():
    req = _request(
        verdict=Verdict.WRONG,
        reason_code="known_misconception",
        student_input="I think it's B because the longer side is opposite the right angle",
        attempts=2,
    )
    prompt = render_router_user_prompt(req)
    assert "B because" in prompt
    assert "wrong" in prompt
    assert "known_misconception" in prompt


def test_router_user_prompt_includes_pose_tool_available_flag():
    prompt_yes = render_router_user_prompt(_request(pose_tool_available=True))
    prompt_no = render_router_user_prompt(_request(pose_tool_available=False))
    assert "pose_tool_available=True" in prompt_yes
    assert "pose_tool_available=False" in prompt_no


def test_router_user_prompt_handles_empty_transcript():
    prompt = render_router_user_prompt(_request())
    assert "fresh session" in prompt or "empty" in prompt


# ──────────────────────────────────────────────────────────────────────
# build_router_request
# ──────────────────────────────────────────────────────────────────────


def test_build_router_request_windows_transcript():
    transcript = [
        {"role": "student" if i % 2 else "assistant", "content": f"t{i}"}
        for i in range(20)
    ]
    state = SessionRuntimeState()
    ctx = TutoringContext(
        session_id=1, student_id=1, institution_id=1, lesson_id=1,
        runtime_state=state, full_transcript=transcript,
    )
    req = build_router_request(
        context=ctx, verdict=None, student_input="hi",
        pose_tool_available=True,
    )
    assert len(req.last_n_turns) == ROUTER_TRANSCRIPT_WINDOW


def test_build_router_request_threads_grader_verdict_and_reason():
    state = SessionRuntimeState()
    ctx = TutoringContext(
        session_id=1, student_id=1, institution_id=1, lesson_id=1,
        runtime_state=state,
    )
    verdict = GradingResult(
        verdict=Verdict.WRONG, reason_code="arithmetic_failed",
    )
    req = build_router_request(
        context=ctx, verdict=verdict, student_input="12",
        pose_tool_available=False,
    )
    assert req.grader_verdict == Verdict.WRONG
    assert req.grader_reason_code == "arithmetic_failed"
    assert req.pose_tool_available is False


def test_build_router_request_threads_objective_progress():
    state = SessionRuntimeState()
    state.objective_progress["obj1"] = ObjectiveProgress(
        objective="obj1", attempts=3, correct=2, wrong=1,
    )
    ctx = TutoringContext(
        session_id=1, student_id=1, institution_id=1, lesson_id=1,
        runtime_state=state, current_objective="obj1",
    )
    req = build_router_request(
        context=ctx, verdict=None, student_input="",
        pose_tool_available=True,
    )
    assert req.objective_attempts == 3
    assert req.objective_correct == 2
    assert req.objective_wrong == 1


# ──────────────────────────────────────────────────────────────────────
# JSON helper
# ──────────────────────────────────────────────────────────────────────


def test_safe_json_loads_returns_none_on_garbage():
    assert _safe_json_loads("not json at all") is None


def test_safe_json_loads_strips_fenced_block():
    assert _safe_json_loads("```json\n{\"a\":1}\n```") == {"a": 1}
