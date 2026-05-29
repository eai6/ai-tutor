"""Bare-answer detection tests — Phase 2 §Tests / §2.1.1.

Tests the deterministic detector (``is_bare_answer``). The behaviour
contract that bare_answer is NOT a move-selection input is enforced
structurally now: the LLM ``MoveRouter`` (post-cutover replacement for
the deterministic ladder) does not receive bare_answer as a discrete
input on ``RouterRequest`` — it sees verdict + reason_code +
student_safe_feedback. Move-selection-equivalence under
bare_answer=True/False therefore can't be unit-tested by calling a
pure function; it is covered indirectly by the router contract tests
in ``test_move_router.py``.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.services.bare_answer import is_bare_answer


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
