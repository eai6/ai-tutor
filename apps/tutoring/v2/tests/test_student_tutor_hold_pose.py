"""Tests for ``StudentTutor.respond(hold_pending_pose=...)`` — Fix 3.

On a prose-only conformance retry, TutorEngine passes the first
attempt's PendingPose back to StudentTutor. The tutor must:

  1. Skip the tool path entirely (no ``generate_with_tools`` call).
  2. Render prose with a text-only ``generate`` call.
  3. Reattach the held PendingPose to the returned TutorResponse so
     Phase B can commit it on conformance accept.
  4. Splice the held pose's rendered stem onto the end of the prose
     so the student-visible turn still ends on the same question.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.contracts import (
    PendingPose,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
    TutoringContext,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.services.student_tutor import StudentTutor


class _FakeResp:
    def __init__(self, content: str):
        self.content = content
        self.tokens_in = 0
        self.tokens_out = 0


class _SpyClient:
    """Fake LLM client. Counts generate() and generate_with_tools()
    invocations and returns a canned text response from generate()."""

    def __init__(self, *, text: str = "Nice try — let me re-frame.") -> None:
        self._text = text
        self.generate_calls = 0
        self.tool_calls = 0

    def generate(self, *, messages, system_prompt, max_tokens):
        self.generate_calls += 1
        self.last_messages = messages
        self.last_system_prompt = system_prompt
        return _FakeResp(self._text)

    def generate_with_tools(self, **kwargs):
        self.tool_calls += 1
        raise AssertionError(
            "generate_with_tools must NOT be called when hold_pending_pose "
            "is set — the tool path is supposed to be skipped"
        )


def _pose(stem: str = "What is the largest planet?") -> PendingPose:
    return PendingPose(
        question_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=42),
        canonical="Jupiter",
        rendered_stem=stem,
        jaccard_signature="planet-largest",
        visible_context=VisibleContextSnapshot(visible_prompt=stem),
    )


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


def test_hold_pose_skips_tool_path():
    """``generate_with_tools`` must NOT be called when a held pose is
    present. The SpyClient raises if it is — see _SpyClient."""
    client = _SpyClient()
    tutor = StudentTutor(tutor_client_factory=lambda: client)
    held = _pose()
    resp = tutor.respond(
        _context(),
        verdict=None,
        move="scaffold_hint",
        student_input="anything",
        hold_pending_pose=held,
    )
    assert client.generate_calls == 1
    assert client.tool_calls == 0
    assert resp.pending_pose is held


def test_hold_pose_splices_stem_onto_response():
    """The held stem must appear at the end of the visible response."""
    client = _SpyClient(text="OK — let me re-frame the question.")
    tutor = StudentTutor(tutor_client_factory=lambda: client)
    held = _pose(stem="What is the largest planet?")
    resp = tutor.respond(
        _context(),
        verdict=None,
        move="scaffold_hint",
        student_input="anything",
        hold_pending_pose=held,
    )
    assert resp.text.endswith("What is the largest planet?")
    assert "re-frame" in resp.text


def test_hold_pose_user_prompt_directs_llm_to_skip_question_emission():
    """The user prompt sent to the LLM must include a sticky-pose
    directive telling it to produce ONLY prose."""
    client = _SpyClient()
    tutor = StudentTutor(tutor_client_factory=lambda: client)
    held = _pose(stem="What is the largest planet?")
    tutor.respond(
        _context(),
        verdict=None,
        move="scaffold_hint",
        student_input="anything",
        hold_pending_pose=held,
    )
    user_content = client.last_messages[0]["content"]
    assert "Sticky pose" in user_content
    assert "What is the largest planet?" in user_content


def test_hold_pose_response_contains_grounding_dict():
    """The text-only path returns a grounding dict (empty when the
    LLM didn't emit GRADER/EVIDENCE headers). Sticky-pose path must
    not break this contract."""
    client = _SpyClient(text="Re-framing the question.")
    tutor = StudentTutor(tutor_client_factory=lambda: client)
    held = _pose()
    resp = tutor.respond(
        _context(),
        verdict=None,
        move="scaffold_hint",
        student_input="anything",
        hold_pending_pose=held,
    )
    assert isinstance(resp.grounding, dict)
