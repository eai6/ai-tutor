"""Tests for the multi-turn pose_question tool loop — Fix 2 (pose-
question two-phase commit).

Up to MAX_POSE_ATTEMPTS_PER_TURN tool_use rounds within a single tutor
turn. On a Phase A rejection, the LLM receives a
``tool_result(is_error=True, content=reason)`` block and can pick a
different slot on the next call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.tutoring.v2.contracts import (
    PendingPose,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
    TutoringContext,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.services.student_tutor import (
    MAX_POSE_ATTEMPTS_PER_TURN,
    StudentTutor,
    _format_rejection_for_llm,
)
from apps.tutoring.v2.tools.pose_question import ToolRejection


# ──────────────────────────────────────────────────────────────────────
# Fake LLM Message / blocks
# ──────────────────────────────────────────────────────────────────────


class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, *, name: str, input: dict, id: str = "toolu_1"):
        self.name = name
        self.input = input
        self.id = id


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _ScriptedToolClient:
    """Returns a scripted sequence of ``_FakeMessage`` objects from
    successive ``generate_with_tools`` calls. Records the ``messages``
    argument of each invocation for inspection."""

    def __init__(self, scripted_messages: list[_FakeMessage]):
        self._scripted = list(scripted_messages)
        self.call_log: list[dict] = []

    def generate_with_tools(self, *, messages, system_prompt, tools,
                            max_tokens, tool_choice):
        self.call_log.append({
            "messages": [dict(m) for m in messages],
            "tool_choice": tool_choice,
        })
        if not self._scripted:
            raise AssertionError(
                "generate_with_tools called more times than scripted"
            )
        return self._scripted.pop(0)


def _context() -> TutoringContext:
    return TutoringContext(
        session_id=1,
        student_id=1,
        institution_id=1,
        lesson_id=1,
        runtime_state=SessionRuntimeState(),
        current_objective="obj1",
        full_transcript=[],
    )


def _step(*, id: int, question: str, expected_answer: str = "x"):
    return SimpleNamespace(
        id=id,
        question=question,
        expected_answer=expected_answer,
        answer_type="short_numeric",
        choices=None,
    )


def _pose_for(slot_step, *, stem: str | None = None) -> PendingPose:
    s = stem or slot_step.question
    return PendingPose(
        question_ref=QuestionRef(
            source=QuestionSource.LESSON_STEP, id=slot_step.id,
        ),
        canonical=slot_step.expected_answer or "x",
        rendered_stem=s,
        jaccard_signature="sig",
        visible_context=VisibleContextSnapshot(visible_prompt=s),
    )


# ──────────────────────────────────────────────────────────────────────
# _format_rejection_for_llm
# ──────────────────────────────────────────────────────────────────────


def test_format_rejection_mcq_options_missing():
    msg = _format_rejection_for_llm(
        rejection=ToolRejection(reason="mcq_options_missing", detail=""),
        slot=3, attempted_slots=[3],
    )
    assert "Slot 3 rejected" in msg
    assert "multiple-choice" in msg
    assert "[3]" in msg


def test_format_rejection_in_session_repeat_lists_attempted_slots():
    msg = _format_rejection_for_llm(
        rejection=ToolRejection(reason="in_session_repeat", detail=""),
        slot=5, attempted_slots=[3, 5],
    )
    assert "Slot 5 rejected" in msg
    assert "already been posed" in msg
    assert "[3, 5]" in msg


def test_format_rejection_cross_session_repeat():
    msg = _format_rejection_for_llm(
        rejection=ToolRejection(reason="cross_session_repeat", detail=""),
        slot=2, attempted_slots=[2],
    )
    assert "prior session" in msg


def test_format_rejection_ref_unresolved_includes_detail():
    msg = _format_rejection_for_llm(
        rejection=ToolRejection(
            reason="ref_unresolved", detail="no canonical for slot 9",
        ),
        slot=9, attempted_slots=[9],
    )
    assert "tool arguments invalid" in msg
    assert "no canonical" in msg


def test_format_rejection_not_derivable():
    msg = _format_rejection_for_llm(
        rejection=ToolRejection(reason="not_derivable", detail=""),
        slot=1, attempted_slots=[1],
    )
    assert "cannot be derived" in msg


def test_format_rejection_token_invalid():
    msg = _format_rejection_for_llm(
        rejection=ToolRejection(reason="token_invalid", detail="exp"),
        slot=None, attempted_slots=[],
    )
    assert "Tool call rejected" in msg
    assert "invalid or already consumed" in msg


def test_format_rejection_unknown_reason_uses_default_template():
    msg = _format_rejection_for_llm(
        rejection=ToolRejection(reason="something_new", detail="d"),
        slot=0, attempted_slots=[0],
    )
    assert "tool call was rejected" in msg
    assert "d" in msg


# ──────────────────────────────────────────────────────────────────────
# _call_with_tools — multi-turn loop behavior
# ──────────────────────────────────────────────────────────────────────


def _make_tutor(client):
    return StudentTutor(tutor_client_factory=lambda: client)


def test_max_pose_attempts_per_turn_is_2():
    assert MAX_POSE_ATTEMPTS_PER_TURN == 2


def test_tool_loop_returns_first_pending_pose_immediately():
    """First tool_use passes Phase A. No second LLM call."""
    step = _step(id=10, question="What is 2+2?")
    slot_map = {0: step}
    tutor = StudentTutor()

    msg0 = _FakeMessage([
        _ToolUseBlock(name="pose_question", input={"slot": 0}),
    ])
    client = _ScriptedToolClient([msg0])

    pending = _pose_for(step)
    with patch(
        "apps.tutoring.v2.services.student_tutor.StudentTutor._handle_pose_tool_use",
        return_value=(pending, step.question, 0),
    ):
        raw, pose, text, grounding = tutor._call_with_tools(
            client=client,
            system_prompt="sys",
            user_prompt="user",
            tool_dict={"name": "pose_question"},
            slot_map=slot_map,
            context=_context(),
            move="scaffold_hint",
        )
    assert pose is pending
    assert text == step.question
    assert len(client.call_log) == 1


def test_tool_loop_retries_on_phase_a_rejection_and_picks_different_slot():
    """First tool_use(slot=3) → in_session_repeat. Second call (with
    tool_result) emits tool_use(slot=5); Phase A passes. Returns the
    slot=5 PendingPose."""
    step5 = _step(id=15, question="What is the largest planet?")
    slot_map = {3: _step(id=13, question="x"), 5: step5}
    tutor = StudentTutor()

    msg0 = _FakeMessage([
        _ToolUseBlock(name="pose_question", input={"slot": 3}, id="tu_a"),
    ])
    msg1 = _FakeMessage([
        _ToolUseBlock(name="pose_question", input={"slot": 5}, id="tu_b"),
    ])
    client = _ScriptedToolClient([msg0, msg1])

    pending5 = _pose_for(step5)
    side_effects = [
        (ToolRejection(reason="in_session_repeat", detail="dup"), "", 3),
        (pending5, step5.question, 5),
    ]
    with patch(
        "apps.tutoring.v2.services.student_tutor.StudentTutor._handle_pose_tool_use",
        side_effect=side_effects,
    ):
        _raw, pose, text, _g = tutor._call_with_tools(
            client=client,
            system_prompt="sys",
            user_prompt="user",
            tool_dict={"name": "pose_question"},
            slot_map=slot_map,
            context=_context(),
            move="scaffold_hint",
        )
    assert pose is pending5
    assert text == step5.question
    assert len(client.call_log) == 2
    # Second call's messages should include the rejection tool_result.
    second_msgs = client.call_log[1]["messages"]
    assert any(
        isinstance(m["content"], list)
        and any(
            isinstance(b, dict)
            and b.get("type") == "tool_result"
            and b.get("is_error") is True
            for b in m["content"]
        )
        for m in second_msgs
    ), "Second call should carry a tool_result(is_error=True) block"
    # Second call must use force-mode so the LLM commits to a slot.
    assert client.call_log[1]["tool_choice"] == {"type": "any"}
    # First call must use auto.
    assert client.call_log[0]["tool_choice"] == {"type": "auto"}


def test_tool_loop_rejection_message_lists_attempted_slots():
    """After first rejection, the tool_result content must mention
    slots already attempted this turn."""
    step5 = _step(id=15, question="?")
    slot_map = {3: _step(id=13, question="y"), 5: step5}
    tutor = StudentTutor()

    msg0 = _FakeMessage([
        _ToolUseBlock(name="pose_question", input={"slot": 3}, id="tu_a"),
    ])
    msg1 = _FakeMessage([
        _ToolUseBlock(name="pose_question", input={"slot": 5}, id="tu_b"),
    ])
    client = _ScriptedToolClient([msg0, msg1])
    pending5 = _pose_for(step5)
    side_effects = [
        (ToolRejection(reason="in_session_repeat", detail="x"), "", 3),
        (pending5, step5.question, 5),
    ]
    with patch(
        "apps.tutoring.v2.services.student_tutor.StudentTutor._handle_pose_tool_use",
        side_effect=side_effects,
    ):
        tutor._call_with_tools(
            client=client,
            system_prompt="sys",
            user_prompt="user",
            tool_dict={"name": "pose_question"},
            slot_map=slot_map,
            context=_context(),
            move="scaffold_hint",
        )
    second_msgs = client.call_log[1]["messages"]
    tool_result_content = ""
    for m in second_msgs:
        if isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tool_result_content += b.get("content", "")
    assert "[3]" in tool_result_content
    assert "already attempted" in tool_result_content


def test_tool_loop_stops_after_max_attempts_when_both_rejected():
    """Both tool calls rejected. Loop exits without committing a
    pose. No third LLM call."""
    step3 = _step(id=13, question="x")
    step5 = _step(id=15, question="y")
    slot_map = {3: step3, 5: step5}
    tutor = StudentTutor()

    msg0 = _FakeMessage([
        _ToolUseBlock(name="pose_question", input={"slot": 3}, id="a"),
    ])
    msg1 = _FakeMessage([
        _ToolUseBlock(name="pose_question", input={"slot": 5}, id="b"),
    ])
    client = _ScriptedToolClient([msg0, msg1])
    side_effects = [
        (ToolRejection(reason="in_session_repeat", detail=""), "", 3),
        (ToolRejection(reason="in_session_repeat", detail=""), "", 5),
    ]
    with patch(
        "apps.tutoring.v2.services.student_tutor.StudentTutor._handle_pose_tool_use",
        side_effect=side_effects,
    ):
        _raw, pose, text, _g = tutor._call_with_tools(
            client=client,
            system_prompt="sys",
            user_prompt="user",
            tool_dict={"name": "pose_question"},
            slot_map=slot_map,
            context=_context(),
            move="scaffold_hint",
        )
    assert pose is None
    assert len(client.call_log) == 2  # exactly MAX_POSE_ATTEMPTS_PER_TURN


def test_tool_loop_handles_no_tool_use_on_first_call():
    """First call returns text only (no tool_use). Loop exits cleanly
    without a second call — auto tool_choice means prose is a valid
    LLM output (conformance will judge it)."""
    tutor = StudentTutor()
    msg0 = _FakeMessage([_TextBlock("Let me think aloud first.")])
    client = _ScriptedToolClient([msg0])
    _raw, pose, text, _g = tutor._call_with_tools(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tool_dict={"name": "pose_question"},
        slot_map={0: _step(id=1, question="?")},
        context=_context(),
        move="scaffold_hint",
    )
    assert pose is None
    assert "think aloud" in text
    assert len(client.call_log) == 1


def test_tool_loop_handles_no_tool_use_on_retry():
    """First tool_use rejected. Second call returns text only.
    Loop exits cleanly with the text response and no pose."""
    step3 = _step(id=13, question="x")
    slot_map = {3: step3}
    tutor = StudentTutor()
    msg0 = _FakeMessage([
        _ToolUseBlock(name="pose_question", input={"slot": 3}, id="a"),
    ])
    msg1 = _FakeMessage([
        _TextBlock("I'll explain instead — condensation is..."),
    ])
    client = _ScriptedToolClient([msg0, msg1])
    side_effects = [
        (ToolRejection(reason="in_session_repeat", detail=""), "", 3),
    ]
    with patch(
        "apps.tutoring.v2.services.student_tutor.StudentTutor._handle_pose_tool_use",
        side_effect=side_effects,
    ):
        _raw, pose, text, _g = tutor._call_with_tools(
            client=client,
            system_prompt="sys",
            user_prompt="user",
            tool_dict={"name": "pose_question"},
            slot_map=slot_map,
            context=_context(),
            move="scaffold_hint",
        )
    assert pose is None
    assert "I'll explain" in text
    assert len(client.call_log) == 2
