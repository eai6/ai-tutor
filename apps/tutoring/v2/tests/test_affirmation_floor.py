"""CORRECT-verdict affirmation floor (engine-side, option 2).

open_question_authority_redesign.md §7: a pose-bearing move forces the
tool; the LLM can return a tool_use block with no text block, shipping a
bare bank stem with no acknowledgment of a correct answer (GEO-S5
run-12 / run-13 T2). The engine detects the empty lead-in exactly
(shipped text == committed stem) and, on CORRECT, prepends a one-line
affirmation synthesised from the grader's verified output.

These pin the synthesiser (pure). The end-to-end empty-lead-in detection
is verified live (it depends on the StudentTutor returning a tool_use
block with no text).
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import (
    GradingResult,
    StudentSafeFeedback,
    Verdict,
)
from apps.tutoring.v2.services.tutor_engine import TutorEngine


def _gr(what_right="", student_value="", canonical="") -> GradingResult:
    return GradingResult(
        verdict=Verdict.CORRECT,
        private_canonical=canonical,
        student_safe_feedback=StudentSafeFeedback(what_right=what_right),
        student_value=student_value,
    )


def test_uses_content_bearing_what_right() -> None:
    a = TutorEngine._synthesize_affirmation(_gr(what_right="you used the inverse step"))
    assert a == "You used the inverse step."


def test_skips_generic_placeholder_and_names_student_value() -> None:
    # The deterministic matcher's generic "you matched the answer" is not
    # content-bearing — fall back to the student's own value.
    a = TutorEngine._synthesize_affirmation(
        _gr(what_right="you matched the answer", student_value="45")
    )
    assert a == "Correct — 45."


def test_falls_back_to_canonical_when_no_student_value() -> None:
    a = TutorEngine._synthesize_affirmation(_gr(canonical="045"))
    assert a == "Correct — 045."


def test_bare_correct_when_no_material() -> None:
    a = TutorEngine._synthesize_affirmation(_gr())
    assert a == "Correct."


def test_always_ends_with_terminal_punctuation_and_capitalised() -> None:
    a = TutorEngine._synthesize_affirmation(_gr(what_right="nice place-value split"))
    assert a[0].isupper()
    assert a.endswith((".", "!", "?"))
