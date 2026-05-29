"""Thin open-question → bank resolution (open_question authority redesign).

Memo: memory/open_question_authority_redesign.md §5 — the router PERCEIVES
the open question from the transcript and hands the grader a "thin"
``OpenQuestion`` (verbatim ``rendered_stem`` text, ``id <= 0``, empty
``canonical``). The grader owns bank-matching end-to-end: it resolves
that text back to a ``LessonStep`` so the deterministic + LLM grading
paths run against a real canonical.

These tests pin the resolver's contract:
  - thin + confident text match → request rebuilt with bank id/canonical
  - committed question (id>0) or canonical present → untouched (fast path)
  - no confident match / ambiguous match → untouched (graded as authored)
  - the normalization + similarity helpers behave (pure-function unit).
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock, patch

from apps.tutoring.v2.contracts import (
    GradingRequest,
    OpenQuestion,
    QuestionSource,
    SessionRuntimeState,
    TutoringContext,
)
from apps.tutoring.v2.services.student_grader import (
    StudentGrader,
    _normalize_question_core,
    _question_core_similarity,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StepStub:
    """Minimal LessonStep stand-in for the matcher path."""

    def __init__(
        self,
        *,
        pk: int,
        question: str,
        expected_answer: str,
        answer_type: str,
        choices: Optional[list] = None,
        order_index: int = 0,
    ) -> None:
        self.id = pk
        self.pk = pk
        self.question = question
        self.expected_answer = expected_answer
        self.answer_type = answer_type
        self.choices = choices
        self.order_index = order_index


def _ctx(lesson_id: int = 1426) -> TutoringContext:
    return TutoringContext(
        session_id=1,
        student_id=1,
        institution_id=1,
        lesson_id=lesson_id,
        runtime_state=SessionRuntimeState(),
    )


def _thin_request(stem_text: str, student_input: str) -> GradingRequest:
    return GradingRequest(
        open_question=OpenQuestion(
            source=QuestionSource.LESSON_STEP,
            id=0,
            canonical="",
            rendered_stem=stem_text,
        ),
        student_input=student_input,
        is_math=False,
    )


def _patch_steps(steps: list[_StepStub]):
    """Patch the LessonStep ORM chain used by _match_text_to_lesson_step."""
    objects_mock = MagicMock()
    chain = objects_mock.filter.return_value.exclude.return_value
    chain = chain.exclude.return_value
    chain.order_by.return_value = steps
    return patch("apps.curriculum.models.LessonStep.objects", objects_mock)


# ---------------------------------------------------------------------------
# Pure-function units
# ---------------------------------------------------------------------------


def test_normalize_strips_mcq_options_and_tf_suffix() -> None:
    raw = (
        "Which direction is the second ferry heading?\n\n"
        "A) South-East\nB) South-West\nC) East\nD) North"
    )
    core = _normalize_question_core(raw)
    assert "a)" not in core and "south-east" not in core
    assert core == "which direction is the second ferry heading"

    tf = "The bearing 090 is East.\n\n(True or False?)"
    assert _normalize_question_core(tf) == "the bearing 090 is east"


def test_similarity_exact_substring_and_jaccard() -> None:
    assert _question_core_similarity("convert ne to a bearing", "convert ne to a bearing") == 1.0
    # substring → 0.95
    assert _question_core_similarity(
        "convert ne to a bearing",
        "convert ne to a bearing for the trip",
    ) == 0.95
    # disjoint → 0.0
    assert _question_core_similarity("apples oranges", "rivers mountains") == 0.0


# ---------------------------------------------------------------------------
# Resolver contract
# ---------------------------------------------------------------------------


def test_thin_question_resolves_to_bank_step() -> None:
    """Verbatim transcript text → matched LessonStep id/canonical/answer_type."""
    grader = StudentGrader()
    steps = [
        _StepStub(
            pk=13784,
            question="Convert the compass direction North-East (NE) to a three-figure bearing.",
            expected_answer="045",
            answer_type="short_numeric",
            order_index=3,
        ),
        _StepStub(
            pk=13785,
            question="A bearing of 180 points in which compass direction?",
            expected_answer="B",
            answer_type="multiple_choice",
            choices=["A) North", "B) South", "C) East", "D) West"],
            order_index=4,
        ),
    ]
    req = _thin_request(
        "Convert the compass direction North-East (NE) to a three-figure bearing.",
        "45",
    )
    with _patch_steps(steps):
        out = grader._resolve_thin_open_question(_ctx(), req)
    assert out.open_question.id == 13784
    assert out.open_question.canonical == "045"
    assert out.open_question.answer_type == "short_numeric"
    assert "North-East" in out.open_question.rendered_stem


def test_committed_question_is_untouched() -> None:
    """A real bank id (>0) is the fast path — resolver must not re-match."""
    grader = StudentGrader()
    req = GradingRequest(
        open_question=OpenQuestion(
            source=QuestionSource.LESSON_STEP,
            id=13784,
            canonical="045",
            rendered_stem="Convert NE to a three-figure bearing.",
            answer_type="short_numeric",
        ),
        student_input="45",
        is_math=False,
    )
    # No ORM patch — if the resolver tried to query, it would hit the real
    # (empty test) DB and still return the request untouched; assert identity.
    out = grader._resolve_thin_open_question(_ctx(), req)
    assert out is req


def test_canonical_present_is_untouched() -> None:
    grader = StudentGrader()
    req = GradingRequest(
        open_question=OpenQuestion(
            source=QuestionSource.LESSON_STEP,
            id=0,
            canonical="045",  # already has a canonical → not thin
            rendered_stem="Convert NE to a three-figure bearing.",
        ),
        student_input="45",
        is_math=False,
    )
    out = grader._resolve_thin_open_question(_ctx(), req)
    assert out is req


def test_no_confident_match_returns_request_untouched() -> None:
    grader = StudentGrader()
    steps = [
        _StepStub(
            pk=1,
            question="What is the capital city of the country?",
            expected_answer="Victoria",
            answer_type="free_text",
        ),
    ]
    req = _thin_request("Explain how ocean currents redistribute heat.", "they move warm water")
    with _patch_steps(steps):
        out = grader._resolve_thin_open_question(_ctx(), req)
    assert out is req  # below threshold → untouched


def test_ambiguous_match_returns_request_untouched() -> None:
    """Two near-identical steps within 0.05 → ambiguous → no match."""
    grader = StudentGrader()
    steps = [
        _StepStub(
            pk=1,
            question="Convert the compass direction to a three-figure bearing",
            expected_answer="045",
            answer_type="short_numeric",
        ),
        _StepStub(
            pk=2,
            question="compass direction to a three-figure bearing",
            expected_answer="135",
            answer_type="short_numeric",
        ),
    ]
    # Both step questions are substrings of the (longer) target, neither
    # exact → both score 0.95 → tie within 0.05 → ambiguous → untouched.
    req = _thin_request(
        "Convert the compass direction to a three-figure bearing right now please",
        "45",
    )
    with _patch_steps(steps):
        out = grader._resolve_thin_open_question(_ctx(), req)
    assert out is req


def test_failsoft_on_orm_error_returns_request_untouched() -> None:
    grader = StudentGrader()
    req = _thin_request("Convert NE to a three-figure bearing.", "45")
    objects_mock = MagicMock()
    objects_mock.filter.side_effect = RuntimeError("db down")
    with patch("apps.curriculum.models.LessonStep.objects", objects_mock):
        out = grader._resolve_thin_open_question(_ctx(), req)
    assert out is req
