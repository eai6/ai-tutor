"""MoveRouter unit tests — Commit D shape.

Covers:
  - Core routing behaviour for both decision shapes
    (non-answer-attempt: {case, move, verdict_needed: false, reason};
     answer-attempt: {case: "answer_attempt", verdict_needed: true,
                      moves_by_verdict, reason}).
  - Pydantic validation (case/verdict_needed mutual-exclusion,
    moves_by_verdict keys, closed move set).
  - Fail-soft contract on every error path.
  - Span instrumentation surface.
  - Router-prompt cache stability.
"""

from __future__ import annotations

import json

import pytest

from apps.tutoring.v2.contracts import (
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


def _non_attempt_payload(
    *,
    case: str = "help_request",
    move: str = "explain",
    reason: str = "student asked to be taught",
) -> str:
    return json.dumps({
        "case": case,
        "move": move,
        "verdict_needed": False,
        "reason": reason,
    })


def _attempt_payload(
    *,
    correct: str = "confirm_and_advance",
    partial: str = "scaffold_hint",
    wrong: str = "scaffold_hint",
    reason: str = "wrong_attempts=0; saturation not reached",
) -> str:
    return json.dumps({
        "case": "answer_attempt",
        "verdict_needed": True,
        "moves_by_verdict": {
            "correct": correct,
            "partial": partial,
            "wrong": wrong,
        },
        "reason": reason,
    })


def _request(
    *,
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
        grader_verdict=None,
        grader_reason_code=None,
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
# Core routing behaviour — non-answer-attempt shape
# ──────────────────────────────────────────────────────────────────────


def test_router_returns_help_request_decision():
    client = _FakeClient(_non_attempt_payload(
        case="help_request",
        move="explain",
        reason="student asked what condensation is",
    ))
    decision = _router(client).route(_request(student_input="what is condensation"))
    assert decision.case == "help_request"
    assert decision.verdict_needed is False
    assert decision.move == "explain"
    assert decision.moves_by_verdict is None
    assert "condensation" in decision.reason


def test_router_returns_opening_turn_decision():
    client = _FakeClient(_non_attempt_payload(
        case="opening_turn",
        move="explain",
        reason="lesson just opened",
    ))
    decision = _router(client).route(_request())
    assert decision.case == "opening_turn"
    assert decision.move == "explain"


def test_router_returns_forced_close_decision():
    client = _FakeClient(_non_attempt_payload(
        case="forced_close",
        move="close_topic",
        reason="objective_turn_count=12, correct_on_objective=0",
    ))
    decision = _router(client).route(_request())
    assert decision.case == "forced_close"
    assert decision.move == "close_topic"


# ──────────────────────────────────────────────────────────────────────
# Core routing behaviour — answer-attempt shape
# ──────────────────────────────────────────────────────────────────────


def test_router_returns_answer_attempt_decision_with_full_moves_by_verdict():
    client = _FakeClient(_attempt_payload(
        correct="confirm_and_advance",
        partial="scaffold_hint",
        wrong="scaffold_hint",
        reason="open question pending; no saturation",
    ))
    decision = _router(client).route(_request(open_q_pending=True))
    assert decision.case == "answer_attempt"
    assert decision.verdict_needed is True
    assert decision.move is None
    assert decision.moves_by_verdict == {
        "correct": "confirm_and_advance",
        "partial": "scaffold_hint",
        "wrong": "scaffold_hint",
    }


def test_router_strips_markdown_fences_around_json():
    payload = "```json\n" + _attempt_payload(wrong="pivot") + "\n```"
    client = _FakeClient(payload)
    decision = _router(client).route(_request(open_q_pending=True))
    assert decision.case == "answer_attempt"
    assert decision.moves_by_verdict["wrong"] == "pivot"


def test_router_extracts_embedded_object_from_prose():
    payload = "Here is the decision:\n" + _non_attempt_payload(
        case="forced_close", move="close_topic",
    )
    client = _FakeClient(payload)
    decision = _router(client).route(_request())
    assert decision.move == "close_topic"


# ──────────────────────────────────────────────────────────────────────
# Pydantic validation
# ──────────────────────────────────────────────────────────────────────


def test_router_rejects_pose_question_as_a_move():
    """``pose_question`` is not in the move table — must fail-soft."""
    client = _FakeClient(_non_attempt_payload(
        case="help_request", move="pose_question",
    ))
    decision = _router(client).route(_request())
    # Fail-soft default fires.
    assert decision.reason.startswith("router_unavailable_fallback")


def test_router_rejects_unknown_move_in_move_field():
    client = _FakeClient(_non_attempt_payload(
        case="help_request", move="guess",
    ))
    decision = _router(client).route(_request(open_q_pending=True))
    # Fail-soft answer-attempt branch fires (open_q_pending=True).
    assert decision.case == "answer_attempt"
    assert "validation_error" in decision.reason or "fallback" in decision.reason


def test_router_rejects_answer_attempt_with_move_field():
    """answer_attempt MUST use moves_by_verdict, not move."""
    payload = json.dumps({
        "case": "answer_attempt",
        "verdict_needed": True,
        "move": "scaffold_hint",  # invalid — should use moves_by_verdict
        "reason": "test",
    })
    client = _FakeClient(payload)
    decision = _router(client).route(_request(open_q_pending=True))
    assert decision.reason.startswith("router_unavailable_fallback")


def test_router_rejects_non_attempt_with_moves_by_verdict():
    payload = json.dumps({
        "case": "help_request",
        "verdict_needed": False,
        "moves_by_verdict": {
            "correct": "explain", "partial": "explain", "wrong": "explain",
        },
        "reason": "test",
    })
    client = _FakeClient(payload)
    decision = _router(client).route(_request())
    assert decision.reason.startswith("router_unavailable_fallback")


def test_router_rejects_moves_by_verdict_with_missing_key():
    payload = json.dumps({
        "case": "answer_attempt",
        "verdict_needed": True,
        "moves_by_verdict": {
            "correct": "confirm_and_advance",
            "wrong": "scaffold_hint",
            # missing "partial"
        },
        "reason": "test",
    })
    client = _FakeClient(payload)
    decision = _router(client).route(_request(open_q_pending=True))
    assert decision.reason.startswith("router_unavailable_fallback")


def test_router_rejects_overlong_reason():
    payload = json.dumps({
        "case": "help_request",
        "move": "explain",
        "verdict_needed": False,
        "reason": "X" * 500,
    })
    client = _FakeClient(payload)
    decision = _router(client).route(_request())
    assert decision.reason.startswith("router_unavailable_fallback")


def test_router_decision_direct_construction_answer_attempt():
    d = RouterDecision(
        case="answer_attempt",
        verdict_needed=True,
        moves_by_verdict={
            "correct": "confirm_and_advance",
            "partial": "scaffold_hint",
            "wrong": "scaffold_hint",
        },
        reason="test",
    )
    assert d.move is None
    assert d.case == "answer_attempt"


def test_router_decision_direct_construction_non_attempt():
    d = RouterDecision(
        case="opening_turn",
        verdict_needed=False,
        move="explain",
        reason="opening",
    )
    assert d.moves_by_verdict is None


def test_router_decision_rejects_verdict_needed_with_move():
    with pytest.raises(Exception):
        RouterDecision(
            case="answer_attempt",
            verdict_needed=True,
            move="scaffold_hint",
            reason="test",
        )


def test_router_decision_rejects_non_attempt_without_move():
    with pytest.raises(Exception):
        RouterDecision(
            case="help_request",
            verdict_needed=False,
            reason="test",
        )


def test_router_decision_round_trip_through_model_dump():
    decision = RouterDecision(
        case="answer_attempt",
        verdict_needed=True,
        moves_by_verdict={
            "correct": "confirm_and_extend",
            "partial": "scaffold_hint",
            "wrong": "scaffold_hint",
        },
        reason="rich correct → extend; partial → scaffold; wrong → scaffold",
    )
    payload = decision.model_dump()
    rehydrated = RouterDecision.model_validate(payload)
    assert rehydrated == decision


def test_router_request_round_trip_through_model_dump():
    req = _request(
        correct=1, attempts=1,
        move_history=["explain", "confirm_and_advance"],
    )
    rehydrated = RouterRequest.model_validate(req.model_dump())
    assert rehydrated.move_history == ["explain", "confirm_and_advance"]


# ──────────────────────────────────────────────────────────────────────
# Fail-soft contract
# ──────────────────────────────────────────────────────────────────────


def test_router_fail_soft_on_no_client_with_open_question():
    router = MoveRouter(router_client_factory=lambda: None)
    decision = router.route(_request(open_q_pending=True))
    assert decision.case == "answer_attempt"
    assert decision.verdict_needed is True
    assert decision.moves_by_verdict == {
        "correct": "confirm_and_advance",
        "partial": "scaffold_hint",
        "wrong": "scaffold_hint",
    }
    assert "no_client" in decision.reason


def test_router_fail_soft_on_no_client_without_open_question():
    router = MoveRouter(router_client_factory=lambda: None)
    decision = router.route(_request(open_q_pending=False))
    assert decision.case == "opening_turn"
    assert decision.verdict_needed is False
    assert decision.move == "explain"


def test_router_fail_soft_on_llm_raise():
    client = _RaisingClient(RuntimeError("api down"))
    decision = _router(client).route(_request(open_q_pending=True))
    assert decision.case == "answer_attempt"
    assert decision.reason.startswith("router_unavailable_fallback")


def test_router_fail_soft_on_non_json_response():
    client = _FakeClient("I cannot decide right now.")
    decision = _router(client).route(_request(open_q_pending=True))
    assert decision.case == "answer_attempt"


def test_router_fail_soft_open_question_returns_answer_attempt_shape():
    decision = _fallback_decision(_request(open_q_pending=True), reason="x")
    assert decision.case == "answer_attempt"
    assert decision.verdict_needed is True
    assert decision.moves_by_verdict["wrong"] == "scaffold_hint"


def test_router_fail_soft_no_open_question_returns_opening_turn_shape():
    decision = _fallback_decision(_request(open_q_pending=False), reason="x")
    assert decision.case == "opening_turn"
    assert decision.move == "explain"


# ──────────────────────────────────────────────────────────────────────
# Span instrumentation
# ──────────────────────────────────────────────────────────────────────


def _drain_spans(token):
    from apps.tutoring import tracing as _t
    buffer = list(_t._span_buffer.get() or [])
    _t.reset_span_buffer(token)
    return buffer


def test_router_emits_router_decision_span_with_new_fields():
    from apps.tutoring import tracing

    token = tracing.start_span_buffer()
    try:
        client = _FakeClient(_non_attempt_payload(case="help_request", move="explain"))
        _router(client).route(_request(student_input="explain"))
    finally:
        spans = _drain_spans(token)

    decision_spans = [s for s in spans if s["name"] == "router.decision"]
    assert decision_spans
    payload = decision_spans[-1]["payload"]
    assert payload["case"] == "help_request"
    assert payload["verdict_needed"] is False
    assert payload["move"] == "explain"
    assert payload["moves_by_verdict"] is None
    assert payload["fail_soft"] is False


def test_router_emits_fail_soft_span_on_outage():
    from apps.tutoring import tracing

    token = tracing.start_span_buffer()
    try:
        client = _RaisingClient(RuntimeError("boom"))
        _router(client).route(_request(open_q_pending=True))
    finally:
        spans = _drain_spans(token)

    decision_spans = [s for s in spans if s["name"] == "router.decision"]
    assert decision_spans
    payload = decision_spans[-1]["payload"]
    assert payload["fail_soft"] is True
    assert payload.get("fail_soft_reason") == "RuntimeError"


# ──────────────────────────────────────────────────────────────────────
# Prompt cache stability
# ──────────────────────────────────────────────────────────────────────


def test_router_system_prompt_is_byte_stable():
    """The cacheable preamble must not embed per-turn content."""
    once = SHARED_ROUTER_SYSTEM
    twice = SHARED_ROUTER_SYSTEM
    assert once == twice
    assert isinstance(once, str)
    # The 8 moves are listed.
    for move in (
        "confirm_and_advance", "confirm_and_extend",
        "scaffold_hint", "name_misconception", "worked_example",
        "explain", "pivot", "close_topic",
    ):
        assert move in once
    # The three turn-classification cases are present.
    for case in ("ANSWER_ATTEMPT", "HELP_REQUEST", "OPENING_TURN"):
        assert case in once


def test_router_user_prompt_includes_student_input():
    req = _request(
        student_input="I think it's B because the longer side is opposite the right angle",
        attempts=2,
    )
    prompt = render_router_user_prompt(req)
    assert "B because" in prompt


def test_router_user_prompt_includes_pose_tool_available_flag():
    prompt_yes = render_router_user_prompt(_request(pose_tool_available=True))
    prompt_no = render_router_user_prompt(_request(pose_tool_available=False))
    assert "pose_tool_available: True" in prompt_yes
    assert "pose_tool_available: False" in prompt_no


def test_router_user_prompt_includes_new_counter_fields():
    req = _request(open_q_pending=True)
    # Force a few named counter values via direct construction.
    req = RouterRequest(
        last_n_turns=[],
        student_input="12",
        open_question_has_pending=True,
        wrong_attempts_on_open_question=3,
        partial_attempts_on_open_question=1,
        consecutive_wrong_on_open_question=2,
        objective_turn_count=5,
        prior_answer_attempts_on_objective=4,
        correct_on_objective=2,
        unscaffolded_correct_on_objective=1,
        recent_verdicts=["wrong", "partial", "wrong"],
    )
    prompt = render_router_user_prompt(req)
    assert "wrong_attempts_on_open_question: 3" in prompt
    assert "partial_attempts_on_open_question: 1" in prompt
    assert "consecutive_wrong_on_open_question: 2" in prompt
    assert "unscaffolded_correct_on_objective: 1" in prompt
    assert "['wrong', 'partial', 'wrong']" in prompt


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
        context=ctx, student_input="hi", pose_tool_available=True,
    )
    assert len(req.last_n_turns) == ROUTER_TRANSCRIPT_WINDOW


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
        context=ctx, student_input="", pose_tool_available=True,
    )
    assert req.objective_attempts == 3
    assert req.objective_correct == 2
    assert req.objective_wrong == 1
    # New counter fields — derived from existing state.
    assert req.prior_answer_attempts_on_objective == 3
    assert req.correct_on_objective == 2


def test_build_router_request_threads_new_per_open_question_counters():
    state = SessionRuntimeState()
    state.wrong_attempts_on_open_question = 2
    state.partial_attempts_on_open_question = 1
    state.consecutive_wrong_on_open_question = 1
    state.unscaffolded_correct_on_open_question_objective = 3
    state.recent_verdicts = ["wrong", "wrong", "partial"]
    ctx = TutoringContext(
        session_id=1, student_id=1, institution_id=1, lesson_id=1,
        runtime_state=state, current_objective="obj1",
    )
    req = build_router_request(
        context=ctx, student_input="x", pose_tool_available=True,
    )
    assert req.wrong_attempts_on_open_question == 2
    assert req.partial_attempts_on_open_question == 1
    assert req.consecutive_wrong_on_open_question == 1
    assert req.unscaffolded_correct_on_objective == 3
    assert req.recent_verdicts == ["wrong", "wrong", "partial"]


def test_build_router_request_threads_pose_tool_flag():
    state = SessionRuntimeState()
    ctx = TutoringContext(
        session_id=1, student_id=1, institution_id=1, lesson_id=1,
        runtime_state=state,
    )
    req = build_router_request(
        context=ctx, student_input="12", pose_tool_available=False,
    )
    assert req.pose_tool_available is False


# ──────────────────────────────────────────────────────────────────────
# JSON helper
# ──────────────────────────────────────────────────────────────────────


def test_safe_json_loads_returns_none_on_garbage():
    assert _safe_json_loads("not json at all") is None


def test_safe_json_loads_strips_fenced_block():
    assert _safe_json_loads("```json\n{\"a\":1}\n```") == {"a": 1}
