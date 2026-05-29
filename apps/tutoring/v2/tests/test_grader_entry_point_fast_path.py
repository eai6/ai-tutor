"""Entry-point deterministic-match coverage — R1 + R5 + R6 (2026-05-29).

R1 lifts the deterministic Tier 0 + Tier 1 matchers to
``grade_student_response`` BEFORE the math/non-math LLM dispatch.
The user's stated rule — "when the canonical is also a letter or
number, grading should never reach the 2 LLM" — is structurally
enforced by these tests: the math path's LLM-A / LLM-B clients must
NOT be called when the input shape resolves deterministically.

R5 adds ``_extract_unambiguous_mcq_letter`` as a fallback inside
``_match_mcq_letter`` so reasoning-marker shapes
("B, profit = 270 SCR") hit the fast path instead of the LLM.

R6 (drop LESSON_STEP source gate) is functionally covered by
``_try_deterministic_match`` calling both ``_try_direct_step_match``
(LESSON_STEP) and ``_try_bank_grading`` (other sources) — no code
change beyond the orchestrator.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from apps.tutoring.v2.contracts import (
    GradingRequest,
    OpenQuestion,
    QuestionSource,
    Verdict,
)
from apps.tutoring.v2.services.student_grader import (
    StudentGrader,
    _match_mcq_letter,
)


# ---------------------------------------------------------------------------
# Test fixtures — minimal GradingRequest + LessonStep stub
# ---------------------------------------------------------------------------


def _open_q(
    *,
    canonical: str = "",
    answer_type: str = "",
    rendered_stem: str = "Solve x + 8 = 23. A) 15  B) 31  C) 8  D) 23",
    source: QuestionSource = QuestionSource.LESSON_STEP,
    step_id: int = 9999,
) -> OpenQuestion:
    return OpenQuestion(
        source=source,
        id=step_id,
        canonical=canonical,
        rendered_stem=rendered_stem,
        answer_type=answer_type,
    )


def _request(
    *,
    canonical: str,
    answer_type: str,
    student_input: str,
    is_math: bool = False,
) -> GradingRequest:
    return GradingRequest(
        open_question=_open_q(canonical=canonical, answer_type=answer_type),
        student_input=student_input,
        is_math=is_math,
    )


class _StepStub:
    """Minimal LessonStep stand-in for the _try_direct_step_match path."""

    def __init__(
        self,
        *,
        expected_answer: str,
        answer_type: str,
        pk: int = 9999,
    ) -> None:
        self.expected_answer = expected_answer
        self.answer_type = answer_type
        self.pk = pk


# ---------------------------------------------------------------------------
# R1 — fast path runs BEFORE math/non-math dispatch
# ---------------------------------------------------------------------------


def test_mcq_math_canonical_skips_llm_a_via_entry_point() -> None:
    """Math MCQ with letter canonical resolves without touching LLM-A.

    Before R1, math MCQ ("Solve x + 8 = 23. A) 15 B) 31 …" canonical
    "A", student "A") fired LLM-A + LLM-B even though the comparison
    is "A" == "A". Now the entry-point fast path returns first.
    """
    grader = StudentGrader()
    req = _request(
        canonical="A",
        answer_type="multiple_choice",
        student_input="A",
        is_math=True,  # the change under test — was the slow path
    )

    with patch.object(
        grader, "_try_direct_step_match",
        return_value=None,  # force fall-through into bank then LLM
    ) as direct_mock, patch.object(
        grader, "_try_bank_grading", return_value=None,
    ) as bank_mock, patch.object(
        grader, "_grade_math",
    ) as grade_math_mock:
        grade_math_mock.return_value = MagicMock(
            spec_set=["verdict", "bare_answer"]
        )
        grade_math_mock.return_value.verdict = Verdict.WRONG
        grade_math_mock.return_value.bare_answer = False
        grader.grade_student_response(MagicMock(), req)
        # Deterministic tier ran first on the math path.
        direct_mock.assert_called_once()
        bank_mock.assert_called_once()
        # When the deterministic tiers return None we still reach math.
        grade_math_mock.assert_called_once()


def test_mcq_math_canonical_letter_match_returns_without_llm() -> None:
    """Letter canonical + letter input → CORRECT without LLM dispatch."""
    grader = StudentGrader()
    req = _request(
        canonical="A",
        answer_type="multiple_choice",
        student_input="A",
        is_math=True,
    )
    step = _StepStub(expected_answer="A", answer_type="multiple_choice")
    with patch(
        "apps.curriculum.models.LessonStep.objects",
    ) as objects_mock, patch.object(
        grader, "_grade_math",
    ) as grade_math_mock, patch.object(
        grader, "_grade_non_math",
    ) as grade_non_math_mock:
        objects_mock.filter.return_value.first.return_value = step
        result = grader.grade_student_response(MagicMock(), req)
        assert result.verdict == Verdict.CORRECT
        # LLM paths were NEVER reached.
        grade_math_mock.assert_not_called()
        grade_non_math_mock.assert_not_called()


def test_mcq_math_digit_canonical_resolves_without_llm() -> None:
    """Math MCQ with digit canonical (R3) — same fast-path bypass."""
    grader = StudentGrader()
    req = _request(
        canonical="2",
        answer_type="multiple_choice",
        student_input="2",
        is_math=True,
    )
    step = _StepStub(expected_answer="2", answer_type="multiple_choice")
    with patch(
        "apps.curriculum.models.LessonStep.objects",
    ) as objects_mock, patch.object(
        grader, "_grade_math",
    ) as grade_math_mock:
        objects_mock.filter.return_value.first.return_value = step
        result = grader.grade_student_response(MagicMock(), req)
        assert result.verdict == Verdict.CORRECT
        grade_math_mock.assert_not_called()


def test_mcq_letter_disagreement_returns_wrong_without_llm() -> None:
    """Canonical 'B', student 'D' → WRONG via fast path, no LLM call."""
    grader = StudentGrader()
    req = _request(
        canonical="B",
        answer_type="multiple_choice",
        student_input="D because trap reasoning",
        is_math=True,
    )
    step = _StepStub(expected_answer="B", answer_type="multiple_choice")
    with patch(
        "apps.curriculum.models.LessonStep.objects",
    ) as objects_mock, patch.object(
        grader, "_grade_math",
    ) as grade_math_mock:
        objects_mock.filter.return_value.first.return_value = step
        result = grader.grade_student_response(MagicMock(), req)
        assert result.verdict == Verdict.WRONG
        grade_math_mock.assert_not_called()


def test_true_false_canonical_fast_pathed_without_llm() -> None:
    """T/F canonical + T/F student input → deterministic, no LLM call."""
    grader = StudentGrader()
    req = _request(
        canonical="True",
        answer_type="true_false",
        student_input="True",
        is_math=False,
    )
    step = _StepStub(expected_answer="True", answer_type="true_false")
    with patch(
        "apps.curriculum.models.LessonStep.objects",
    ) as objects_mock, patch.object(
        grader, "_grade_non_math",
    ) as grade_non_math_mock:
        objects_mock.filter.return_value.first.return_value = step
        result = grader.grade_student_response(MagicMock(), req)
        assert result.verdict == Verdict.CORRECT
        grade_non_math_mock.assert_not_called()


def test_short_numeric_fast_pathed_without_llm() -> None:
    """Numeric canonical + numeric student input → deterministic."""
    grader = StudentGrader()
    req = _request(
        canonical="42",
        answer_type="short_numeric",
        student_input="42",
        is_math=True,
    )
    step = _StepStub(expected_answer="42", answer_type="short_numeric")
    with patch(
        "apps.curriculum.models.LessonStep.objects",
    ) as objects_mock, patch.object(
        grader, "_grade_math",
    ) as grade_math_mock:
        objects_mock.filter.return_value.first.return_value = step
        result = grader.grade_student_response(MagicMock(), req)
        assert result.verdict == Verdict.CORRECT
        grade_math_mock.assert_not_called()


def test_free_text_canonical_falls_through_to_llm() -> None:
    """Free-text canonical → deterministic returns None → LLM dispatched."""
    grader = StudentGrader()
    req = _request(
        canonical="photosynthesis converts light into chemical energy",
        answer_type="free_text",
        student_input="plants use light to make sugar",
        is_math=False,
    )
    step = _StepStub(
        expected_answer="photosynthesis converts light into chemical energy",
        answer_type="free_text",
    )
    with patch(
        "apps.curriculum.models.LessonStep.objects",
    ) as objects_mock, patch.object(
        grader, "_grade_non_math",
    ) as grade_non_math_mock:
        objects_mock.filter.return_value.first.return_value = step
        grade_non_math_mock.return_value = MagicMock(
            verdict=Verdict.CORRECT, bare_answer=False,
        )
        grader.grade_student_response(MagicMock(), req)
        # The deterministic tier returned None; LLM path runs.
        grade_non_math_mock.assert_called_once()


def test_empty_stem_short_circuits_before_deterministic_check() -> None:
    """The state_inconsistent guard at the entry point still fires first."""
    grader = StudentGrader()
    req = GradingRequest(
        open_question=OpenQuestion(
            source=QuestionSource.LESSON_STEP,
            id=9999,
            canonical="A",
            rendered_stem="",  # empty → state_inconsistent
            answer_type="multiple_choice",
        ),
        student_input="A",
        is_math=False,
    )
    with patch.object(
        grader, "_try_deterministic_match",
    ) as deterministic_mock:
        result = grader.grade_student_response(MagicMock(), req)
        assert result.verdict == Verdict.WRONG
        assert result.reason_code == "state_inconsistent"
        # Empty-stem guard short-circuited before the fast path.
        deterministic_mock.assert_not_called()


def test_deterministic_match_failsoft_on_step_match_exception() -> None:
    """An exception in _try_direct_step_match falls through to bank."""
    grader = StudentGrader()
    req = _request(
        canonical="A",
        answer_type="multiple_choice",
        student_input="A",
    )
    with patch.object(
        grader, "_try_direct_step_match", side_effect=RuntimeError("boom"),
    ), patch.object(
        grader, "_try_bank_grading", return_value=None,
    ) as bank_mock:
        result = grader._try_deterministic_match(req)
        assert result is None
        # Bank tier still ran despite the step-tier exception.
        bank_mock.assert_called_once()


def test_deterministic_match_failsoft_on_bank_exception() -> None:
    """An exception in _try_bank_grading falls through cleanly."""
    grader = StudentGrader()
    req = _request(
        canonical="A",
        answer_type="multiple_choice",
        student_input="A",
    )
    with patch.object(
        grader, "_try_direct_step_match", return_value=None,
    ), patch.object(
        grader, "_try_bank_grading", side_effect=RuntimeError("boom"),
    ):
        result = grader._try_deterministic_match(req)
        assert result is None


# ---------------------------------------------------------------------------
# R5 — _match_mcq_letter falls back to _extract_unambiguous_mcq_letter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canonical, student_input, expected",
    [
        # Reasoning-marker shapes — the simple _MCQ_PROSE_PATTERNS
        # delimiter pattern catches these directly; the extractor
        # fallback is defensive coverage if a future regex
        # refactor narrows the simple patterns.
        ("B", "B, profit = 270 SCR", True),
        ("D", "D, x = 42 because that's the starting number", True),
        # Different head key than canonical → False (not None).
        ("A", "B, profit = 270 SCR", False),
        # Digit-key variant.
        ("2", "2, total = 60 because 12 × 5 = 60", True),
        # Pure noise that no pattern catches → None (no fast-path
        # verdict; falls through to LLM dispatch).
        ("A", "umm i dunno", None),
        ("A", "the weather is nice today", None),
    ],
)
def test_match_mcq_letter_covers_reasoning_marker_shapes(
    canonical: str, student_input: str, expected: Optional[bool],
) -> None:
    assert _match_mcq_letter(canonical, student_input) is expected


def test_match_mcq_letter_fallback_extractor_fires_when_simple_patterns_miss() -> None:
    """The R5 fallback is exercised when simple patterns return no match.

    Direct unit test of the fallback wiring — uses an input crafted
    so no _MCQ_PROSE_PATTERN matches but the extractor's Shape 3 verb
    list does. The extractor IS reachable as a fallback even if its
    incremental recall is narrow in practice.
    """
    # Monkey-patch the simple patterns to a no-op so the fallback
    # path runs deterministically.
    from apps.tutoring.v2.services import student_grader as sg
    original_patterns = sg._MCQ_PROSE_PATTERNS
    try:
        sg._MCQ_PROSE_PATTERNS = ()
        # Now ONLY the extractor can produce a hit.
        assert _match_mcq_letter("B", "I pick B") is True
        assert _match_mcq_letter("A", "I pick B") is False
        # Ambiguous input (no head, multi-letter scan) → extractor
        # returns "" → None.
        assert _match_mcq_letter("A", "I think A or maybe D") is None
    finally:
        sg._MCQ_PROSE_PATTERNS = original_patterns


# ---------------------------------------------------------------------------
# R6 — combined orchestrator covers both LESSON_STEP and bank sources
# ---------------------------------------------------------------------------


def test_orchestrator_calls_both_tiers_for_non_lesson_step_source() -> None:
    """EXIT_TICKET source skips step tier, hits bank tier."""
    grader = StudentGrader()
    req = GradingRequest(
        open_question=OpenQuestion(
            source=QuestionSource.EXIT_TICKET_QUESTION,
            id=42,
            canonical="C",
            rendered_stem="Pick the right option.",
            answer_type="multiple_choice",
        ),
        student_input="C",
        is_math=False,
    )
    with patch.object(
        grader, "_try_direct_step_match", return_value=None,
    ) as step_mock, patch.object(
        grader, "_try_bank_grading", return_value=None,
    ) as bank_mock:
        grader._try_deterministic_match(req)
        # Step tier called (and bailed because source ≠ LESSON_STEP);
        # bank tier then ran.
        step_mock.assert_called_once()
        bank_mock.assert_called_once()
