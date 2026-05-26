"""StudentGrader unit tests — Phase 2 §Tests.

Covers:
  - Math path comparator branches (numeric + symbolic).
  - DSL extraction → unverified on parse failure.
  - Non-math grounded confidence threshold → unverified below 0.6.
  - Pre-pose hidden-KB suppression (the prompt contract).
  - Tutor-claim adjudication shape (supported / contradicted /
    unverified).
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.contracts import (
    GradingRequest,
    GradingResult,
    OpenQuestion,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
    StudentSafeFeedback,
    TutoringContext,
    Verdict,
)
from apps.tutoring.v2.services.grader_prompts import (
    render_pre_pose_user_prompt,
)
from apps.tutoring.v2.services.student_grader import (
    PrePoseRefusedError,
    StudentGrader,
    _parse_grounded_response,
    _parse_student_math_value,
    _safe_json_loads,
)


# ──────────────────────────────────────────────────────────────────────
# Fake LLM client harness
# ──────────────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tokens_in = 0
        self.tokens_out = 0


class _FakeClient:
    """Returns the queued responses in order."""

    def __init__(self, *payloads: str) -> None:
        self._queue = list(payloads)

    def generate(self, **kwargs) -> _FakeResp:
        if not self._queue:
            raise RuntimeError("FakeClient queue empty")
        return _FakeResp(self._queue.pop(0))


def _context() -> TutoringContext:
    return TutoringContext(
        session_id=1,
        student_id=1,
        institution_id=1,
        lesson_id=1,
        runtime_state=SessionRuntimeState(),
    )


def _open_q(stem: str = "What is 12 + 13?") -> OpenQuestion:
    return OpenQuestion(
        source=QuestionSource.LESSON_STEP,
        id=1,
        rendered_stem=stem,
    )


# ──────────────────────────────────────────────────────────────────────
# JSON parsing helpers
# ──────────────────────────────────────────────────────────────────────


def test_safe_json_loads_strips_fenced_block():
    payload = "```json\n{\"a\": 1}\n```"
    assert _safe_json_loads(payload) == {"a": 1}


def test_safe_json_loads_extracts_embedded_object():
    payload = "Here is the JSON: {\"a\": 2}\nthanks!"
    assert _safe_json_loads(payload) == {"a": 2}


def test_safe_json_loads_returns_none_on_garbage():
    assert _safe_json_loads("not json at all") is None


# ──────────────────────────────────────────────────────────────────────
# Math path
# ──────────────────────────────────────────────────────────────────────


def test_math_path_correct_with_dsl_match():
    """LLM emits valid DSL → executes → student value matches → correct."""
    dsl = '{"variables": {"a": 12, "b": 13}, "expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}}'
    grader = StudentGrader(math_client_factory=lambda: _FakeClient(dsl))
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.CORRECT
    assert result.private_canonical == "25"
    assert result.bare_answer is True


def test_math_path_wrong_with_dsl_match():
    """Student value differs → verdict=wrong; canonical surfaces privately."""
    dsl = '{"variables": {"a": 12, "b": 13}, "expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}}'
    grader = StudentGrader(math_client_factory=lambda: _FakeClient(dsl))
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="50",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    assert result.private_canonical == "25"
    assert result.student_safe_feedback.first_misconception_redacted


def test_math_path_unverified_on_dsl_extract_failure():
    """LLM returns garbage → unverified, never fabricates a verdict."""
    grader = StudentGrader(math_client_factory=lambda: _FakeClient("not json"))
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED


def test_math_path_unverified_on_validation_failure():
    """DSL references a number not in the problem → unverified."""
    # Problem text says "12 + 13"; DSL invents a "99" not present.
    dsl = '{"variables": {"a": 99, "b": 13}, "expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}}'
    grader = StudentGrader(math_client_factory=lambda: _FakeClient(dsl))
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED


# ──────────────────────────────────────────────────────────────────────
# Non-math grounded path — confidence threshold
# ──────────────────────────────────────────────────────────────────────


def test_grounded_high_confidence_correct():
    payload = (
        '{"verdict": "correct", "confidence": 0.92, '
        '"private_canonical": "180", "what_right": "angles sum to 180", '
        '"citation": "[KB-1] sum is 180"}'
    )
    grader = StudentGrader(grounded_client_factory=lambda: _FakeClient(payload))
    request = GradingRequest(
        open_question=_open_q("What's the sum of triangle angles?"),
        student_input="180",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.CORRECT
    assert result.citation.startswith("[KB-1]")


def test_grounded_low_confidence_escalates_to_unverified():
    payload = (
        '{"verdict": "correct", "confidence": 0.3, '
        '"private_canonical": "180", "what_right": ""}'
    )
    grader = StudentGrader(grounded_client_factory=lambda: _FakeClient(payload))
    request = GradingRequest(
        open_question=_open_q("Trick question with low grounding"),
        student_input="180",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    # Even though the LLM said correct, low confidence forces unverified.
    assert result.verdict == Verdict.UNVERIFIED


def test_grounded_explicit_unverified_passes_through():
    payload = '{"verdict": "unverified", "confidence": 0.4}'
    grader = StudentGrader(grounded_client_factory=lambda: _FakeClient(payload))
    request = GradingRequest(
        open_question=_open_q("Open question"),
        student_input="something",
        is_math=False,
    )
    assert grader.grade_student_response(_context(), request).verdict == Verdict.UNVERIFIED


# ──────────────────────────────────────────────────────────────────────
# Pre-pose check — derivability + hidden KB suppression
# ──────────────────────────────────────────────────────────────────────


def test_pre_pose_check_passes_with_token():
    """Derivable=True + issue_token=True → returns a non-empty token string."""
    payload = '{"derivable": true, "reason": "all info present"}'
    grader = StudentGrader(grounded_client_factory=lambda: _FakeClient(payload))
    token = grader.pre_pose_check(
        _context(),
        question_ref=QuestionRef(source=QuestionSource.PRE_POSE_TOKEN, id=0),
        canonical="25",
        visible_prompt="What is 12+13?",
        attached_media_ids=[],
        recent_transcript=[],
    )
    assert isinstance(token, str) and len(token) > 0


def test_pre_pose_check_passes_without_token_when_flagged_off():
    """issue_token=False → returns None on pass (bank-path use)."""
    payload = '{"derivable": true, "reason": "ok"}'
    grader = StudentGrader(grounded_client_factory=lambda: _FakeClient(payload))
    out = grader.pre_pose_check(
        _context(),
        question_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=42),
        canonical="25",
        visible_prompt="What is 12+13?",
        attached_media_ids=[],
        recent_transcript=[],
        issue_token=False,
    )
    assert out is None


def test_pre_pose_check_refuses_on_not_derivable():
    payload = '{"derivable": false, "reason": "missing the second number"}'
    grader = StudentGrader(grounded_client_factory=lambda: _FakeClient(payload))
    with pytest.raises(PrePoseRefusedError):
        grader.pre_pose_check(
            _context(),
            question_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=42),
            canonical="25",
            visible_prompt="What is 12 + ?",  # missing the second operand
            attached_media_ids=[],
            recent_transcript=[],
        )


def test_pre_pose_prompt_suppresses_hidden_kb_chunks():
    """The pre-pose prompt template MUST contain only visible context
    — visible_prompt + figure + recent_transcript + canonical — and
    NOT accept a kb_chunks parameter."""
    rendered = render_pre_pose_user_prompt(
        visible_prompt="Visible Q",
        attached_figure_description="Figure: a triangle",
        recent_transcript=["[student] hi"],
        canonical="42",
    )
    # Sanity — visible context is present.
    assert "Visible Q" in rendered
    assert "Figure: a triangle" in rendered
    assert "[student] hi" in rendered
    # And there is no slot for hidden KB chunks (which would let
    # the canonical leak into the derivability check from sources
    # the student can't see).
    assert "kb_chunk" not in rendered.lower()
    assert "knowledge_base" not in rendered.lower()


# ──────────────────────────────────────────────────────────────────────
# Tutor-claim adjudication
# ──────────────────────────────────────────────────────────────────────


def test_tutor_claim_supported_passes_through():
    payload = '{"status": "supported", "citation": "[KB-1] yes"}'
    grader = StudentGrader(claim_client_factory=lambda: _FakeClient(payload))
    out = grader.adjudicate_tutor_claim(
        _context(), "Photosynthesis happens in chloroplasts.",
    )
    assert out == {"status": "supported", "citation": "[KB-1] yes"}


def test_tutor_claim_contradicted_passes_through():
    payload = '{"status": "contradicted", "citation": "[KB-1] no"}'
    grader = StudentGrader(claim_client_factory=lambda: _FakeClient(payload))
    out = grader.adjudicate_tutor_claim(
        _context(), "Photosynthesis happens in the mitochondria.",
    )
    assert out["status"] == "contradicted"


def test_tutor_claim_no_client_returns_unverified():
    grader = StudentGrader(claim_client_factory=lambda: None)
    out = grader.adjudicate_tutor_claim(_context(), "any claim")
    assert out == {"status": "unverified", "citation": ""}


def test_tutor_claim_unknown_status_normalized_to_unverified():
    payload = '{"status": "yeah probably", "citation": ""}'
    grader = StudentGrader(claim_client_factory=lambda: _FakeClient(payload))
    out = grader.adjudicate_tutor_claim(_context(), "a claim")
    assert out["status"] == "unverified"


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def test_parse_grounded_response_handles_missing_fields():
    g = _parse_grounded_response('{}')
    assert g.verdict == Verdict.UNVERIFIED
    assert g.confidence == 0.0


def test_parse_student_math_value_bare_numeric():
    v, s = _parse_student_math_value("25")
    assert v == 25
    assert s


def test_parse_student_math_value_returns_none_on_prose_without_value():
    v, _ = _parse_student_math_value("I added them up")
    assert v is None
