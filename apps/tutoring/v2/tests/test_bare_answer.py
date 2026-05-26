"""Bare-answer detection + behavior tests — Phase 2 §Tests / §2.1.1.

Tests both the deterministic detector (is_bare_answer) and the
behavior contract: bare_answer is NOT a move-selection input but DOES
ride on the GradingResult for the move prompt to consume.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.contracts import (
    GradingResult,
    SessionRuntimeState,
    Verdict,
)
from apps.tutoring.v2.services.bare_answer import is_bare_answer
from apps.tutoring.v2.services.move_selection import select_move


# ──────────────────────────────────────────────────────────────────────
# Detector
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "42",
        "-3.14",
        "  42 ",
        "180°",
        "75 degrees",
        "3/4",
        "-1/2",
        "25.",
        "50%",
        "B",
        "  d.",
        "12 kg",
    ],
)
def test_bare_answer_detected(raw):
    assert is_bare_answer(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "I added 12 and 13 to get 25",
        "First I divide 100 by 4",
        "because area is base times height",
        "",
        "   ",
        "I think the answer is around 50 maybe",
        "let me check: 12+13=25",
    ],
)
def test_not_bare_answer(raw):
    assert not is_bare_answer(raw)


# ──────────────────────────────────────────────────────────────────────
# Behavior contract: bare_answer biases the prompt, NOT move selection
# ──────────────────────────────────────────────────────────────────────


def test_bare_answer_does_not_change_correct_move():
    """verdict=correct + bare_answer → still confirm_and_advance."""
    state = SessionRuntimeState(attempts_on_open_question=2)
    move_bare = select_move(
        verdict=GradingResult(verdict=Verdict.CORRECT, bare_answer=True),
        runtime_state=state,
    )
    move_full = select_move(
        verdict=GradingResult(verdict=Verdict.CORRECT, bare_answer=False),
        runtime_state=state,
    )
    assert move_bare == move_full == "confirm_and_advance"


def test_bare_answer_does_not_change_wrong_move():
    """verdict=wrong + bare_answer → still scaffold_hint."""
    state = SessionRuntimeState(attempts_on_open_question=1)
    move_bare = select_move(
        verdict=GradingResult(verdict=Verdict.WRONG, bare_answer=True),
        runtime_state=state,
    )
    move_full = select_move(
        verdict=GradingResult(verdict=Verdict.WRONG, bare_answer=False),
        runtime_state=state,
    )
    assert move_bare == move_full == "scaffold_hint"
