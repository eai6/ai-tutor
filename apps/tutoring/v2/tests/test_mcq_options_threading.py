"""MCQ options threading + Phase A safety floor — Phase 4 Fix 4a/4b."""

from __future__ import annotations

from types import SimpleNamespace

from apps.tutoring.v2.services.student_tutor import (
    _extract_mcq_letters,
    _render_bank_stem_with_options,
)
from apps.tutoring.v2.tools.pose_question import (
    _looks_like_mcq_stem_without_options,
)


# ──────────────────────────────────────────────────────────────────────
# Render helper
# ──────────────────────────────────────────────────────────────────────


def _step(*, question: str, answer_type: str = "none", choices=None):
    return SimpleNamespace(
        question=question,
        answer_type=answer_type,
        choices=choices,
        expected_answer="",
    )


def test_render_appends_choices_for_mcq():
    step = _step(
        question="Which of the following best describes runoff?",
        answer_type="multiple_choice",
        choices=[
            "A) water moving over land surface",
            "B) water seeping into the ground",
            "C) water vapor cooling and forming clouds",
            "D) ocean tides",
        ],
    )
    rendered = _render_bank_stem_with_options(step)
    assert "Which of the following" in rendered
    assert "A) water moving over land surface" in rendered
    assert "D) ocean tides" in rendered
    # Choices appended after a blank line, in order.
    assert rendered.index("A)") < rendered.index("B)") < rendered.index("D)")


def test_render_non_mcq_returns_bare_question():
    step = _step(
        question="What is 12 + 13?",
        answer_type="short_numeric",
        choices=None,
    )
    assert _render_bank_stem_with_options(step) == "What is 12 + 13?"


def test_render_mcq_without_choices_falls_back_to_stem():
    """Authored MCQ row missing choices — render the bare stem rather
    than crashing. The Phase A guard then refuses the pose if the stem
    reads as an MCQ."""
    step = _step(
        question="Which of the following is the largest?",
        answer_type="multiple_choice",
        choices=[],
    )
    assert _render_bank_stem_with_options(step) == (
        "Which of the following is the largest?"
    )


def test_extract_mcq_letters_returns_ordered_letters():
    step = _step(
        question="?",
        answer_type="multiple_choice",
        choices=["A) one", "B. two", "C: three", "D - four"],
    )
    assert _extract_mcq_letters(step) == ["A", "B", "C", "D"]


def test_extract_mcq_letters_empty_for_non_mcq():
    step = _step(
        question="?",
        answer_type="short_numeric",
        choices=["A) one"],
    )
    assert _extract_mcq_letters(step) == []


# ──────────────────────────────────────────────────────────────────────
# Phase A safety floor: MCQ stem missing options
# ──────────────────────────────────────────────────────────────────────


def test_mcq_stem_without_options_detected():
    """Run-6 GEO T16/T18/T20 P1: stem says 'which of the following'
    with no options inlined."""
    stem = (
        "Using a six-figure grid reference, which of the following best "
        "describes what happens to the search area?"
    )
    assert _looks_like_mcq_stem_without_options(stem) is True


def test_mcq_stem_with_inline_options_passes():
    """Stem with A)/B)/C)/D) inline — student can answer; do not
    refuse."""
    stem = (
        "Which of the following is correct?\n"
        "A) first option\n"
        "B) second option\n"
        "C) third option\n"
        "D) fourth option"
    )
    assert _looks_like_mcq_stem_without_options(stem) is False


def test_non_mcq_stem_not_flagged():
    """A short-numeric question shouldn't trigger the safety floor."""
    stem = "What is the six-figure grid reference for the boat?"
    assert _looks_like_mcq_stem_without_options(stem) is False


def test_passing_mention_of_following_not_flagged():
    """A stem that mentions 'the following' in passing (not as MCQ
    list intro) shouldn't be falsely flagged."""
    stem = (
        "Describe how erosion shapes the following geographical "
        "feature: a river meander."
    )
    assert _looks_like_mcq_stem_without_options(stem) is False
