"""MCQ fast-path coverage — R2 + R3 + R4 + R7 (2026-05-29).

Per the grader review (conversation 2026-05-29): 90% of curriculum
lesson questions are MCQ with single letter (A/B/C/D/E) or single
digit (1/2/3/4/5) option keys. The deterministic matchers must
handle every common student input shape without falling through to
the LLM grader.

This file covers:

  R2 — letter range A-D → A-E across the matchers, the bare-answer
       detector, and the bank-grader regex.
  R3 — digit-key MCQ prose patterns ("option 2", "I pick 3", "(2)",
       "2 because …") so digit-keyed MCQ falls through to the same
       fast path.
  R4 — head-shape priority in ``_extract_unambiguous_mcq_letter``:
       when a head-position shape resolves a key, it wins even if a
       Shape 3 verb later in the response mentions a different key.
  R7 — ``is_bare_answer`` extended to A-E + digits 1-9.

R1 + R5 + R6 (lift to entry point, fallback chain, drop source gate)
are covered in a follow-up commit + test file.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.services.bare_answer import is_bare_answer
from apps.tutoring.v2.services.student_grader import (
    MCQ_DIGIT_CHARS,
    MCQ_LETTER_CHARS,
    MCQ_OPTION_CHARS,
    StudentGrader,
    _is_mcq_option_canonical,
    _match_mcq_letter,
)


# ---------------------------------------------------------------------------
# Module constants — parameterization sanity
# ---------------------------------------------------------------------------


def test_mcq_letter_range_covers_a_through_e() -> None:
    assert "A" in MCQ_LETTER_CHARS
    assert "E" in MCQ_LETTER_CHARS
    assert "F" not in MCQ_LETTER_CHARS
    # No lowercase in the canonical-side range; lowercase entries are
    # built into the regex char class only.
    assert MCQ_LETTER_CHARS.isupper()


def test_mcq_digit_range_excludes_zero() -> None:
    assert "0" not in MCQ_DIGIT_CHARS
    assert "1" in MCQ_DIGIT_CHARS
    assert "9" in MCQ_DIGIT_CHARS


def test_mcq_option_chars_combines_letters_and_digits() -> None:
    for ch in "ABCDE" + "abcde" + "123456789":
        assert ch in MCQ_OPTION_CHARS
    for ch in "0" + "FGHfg":
        assert ch not in MCQ_OPTION_CHARS


@pytest.mark.parametrize(
    "canonical, expected",
    [
        # Letters A-E.
        ("A", True), ("B", True), ("C", True), ("D", True), ("E", True),
        ("a", True), ("e", True),
        # Digits 1-9.
        ("1", True), ("2", True), ("3", True), ("5", True), ("9", True),
        # Outside the range.
        ("0", False), ("F", False), ("AB", False), ("", False),
        ("  ", False), ("10", False),
    ],
)
def test_is_mcq_option_canonical(canonical: str, expected: bool) -> None:
    assert _is_mcq_option_canonical(canonical) is expected


# ---------------------------------------------------------------------------
# R2 — letter range extended to E
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canonical, student_input, expected",
    [
        # Bare E matches an E canonical.
        ("E", "E", True),
        ("E", "e", True),
        ("E", "E.", True),
        # Mismatched letter under the extended range.
        ("E", "B", False),
        ("A", "E", False),
        # Existing A-D coverage still works.
        ("A", "A", True),
        ("B", "B because reasons", True),
        ("C", "I pick C", True),
        ("D", "(D)", True),
    ],
)
def test_match_mcq_letter_extended_to_e(
    canonical: str, student_input: str, expected: bool,
) -> None:
    assert _match_mcq_letter(canonical, student_input) is expected


# ---------------------------------------------------------------------------
# R3 — digit-key prose patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canonical, student_input, expected",
    [
        # Bare digit.
        ("2", "2", True),
        ("3", "3", True),
        ("2", "3", False),
        # Bracketed digit.
        ("3", "(3)", True),
        ("2", "[2]", True),
        # "option 2", "answer 3", "choice 4".
        ("2", "option 2", True),
        ("3", "answer 3", True),
        ("4", "answer is 4", True),
        ("4", "I pick 4", True),
        # "I'll go with 2", "I'd choose 3".
        ("2", "I'll go with 2", True),
        ("3", "I'd choose 3", True),
        # "2 because ...".
        ("2", "2 because 60 / 5 = 12", True),
        ("3", "3, since 4 * 8 = 32", True),
        # "It's 2".
        ("2", "It's 2", True),
        ("3", "it is 3", True),
        # Wrong digit picked.
        ("2", "I pick 3", False),
        ("3", "option 4", False),
        # Letters and digits don't cross-match.
        ("A", "1", False),
        ("1", "A", False),
    ],
)
def test_match_mcq_digit_options(
    canonical: str, student_input: str, expected: bool,
) -> None:
    assert _match_mcq_letter(canonical, student_input) is expected


# ---------------------------------------------------------------------------
# R4 — head-shape priority in unambiguous extractor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "student_input, expected",
    [
        # Head position wins over a later Shape 3 verb mentioning a
        # different key. Previously these returned "" (ambiguous); now
        # they return the head pick.
        ("B because I picked C earlier", "B"),
        # Shape 2b head + Shape 3 elsewhere.
        ("D, x = 42 because I'd pick A normally", "D"),
        # Bare key followed by a separate "answer is X" elsewhere.
        # (Both Shape 1 and Shape 3 fire; head wins.)
    ],
)
def test_head_shape_priority_in_unambiguous_extractor(
    student_input: str, expected: str,
) -> None:
    assert StudentGrader._extract_unambiguous_mcq_letter(student_input) == expected


@pytest.mark.parametrize(
    "student_input, expected",
    [
        # Genuine ambiguity (no head shape matches) still returns "".
        ("A or B", ""),
        ("I think A or maybe D", ""),
        ("option A or option C", ""),
        # Two head shapes disagreeing → still ambiguous.
        # (Shape 1 says X and Shape 2a says Y would have to both fire
        # — that's only possible on a bare-key input that also has a
        # marker; the regexes wouldn't both fire on the same input,
        # but the safe behavior is to ambiguous-bail when head_found
        # has >1 entry.)
    ],
)
def test_extractor_still_bails_on_genuine_ambiguity(
    student_input: str, expected: str,
) -> None:
    assert StudentGrader._extract_unambiguous_mcq_letter(student_input) == expected


# ---------------------------------------------------------------------------
# Extractor — letters A-E + digits 1-9 work across all four shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "student_input, expected",
    [
        # Shape 1: bare key.
        ("E", "E"),
        ("3", "3"),
        # Shape 2a: key + marker.
        ("E because 5 × 5 = 25", "E"),
        ("3 because 60 / 20 = 3", "3"),
        # Shape 2b: key + punctuation + marker downstream.
        ("E, x = 5 because the diagonal is 5", "E"),
        ("3, profit = 270 because 60 + 210 = 270", "3"),
        # Shape 3: pick verb + key.
        ("the answer is E", "E"),
        ("option 3", "3"),
        ("I pick 4", "4"),
    ],
)
def test_extractor_handles_extended_keys(
    student_input: str, expected: str,
) -> None:
    assert StudentGrader._extract_unambiguous_mcq_letter(student_input) == expected


# ---------------------------------------------------------------------------
# Disagreement override — covers extended range
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canon, answer_type, student_input, expected",
    [
        # E canonical, student picked B → override fires.
        ("e", "multiple_choice", "B because reasons", "B"),
        # Digit canonical, student picked different digit → override fires.
        ("2", "multiple_choice", "3 because reasons", "3"),
        # Same digit → no override.
        ("2", "multiple_choice", "2 because reasons", None),
        # Digit canonical, label answer_type also works.
        ("3", "label", "I pick 4", "4"),
        # Non-MCQ answer_type → never fires.
        ("3", "short_numeric", "4", None),
        # E canonical, same E pick → no override.
        ("e", "multiple_choice", "E because reasons", None),
    ],
)
def test_letter_disagreement_override_extended_range(
    canon: str, answer_type: str, student_input: str, expected,
) -> None:
    got = StudentGrader._maybe_letter_disagreement_override(
        canon_norm=canon,
        answer_type=answer_type,
        student_input=student_input,
    )
    assert got == expected


# ---------------------------------------------------------------------------
# R7 — is_bare_answer extended to A-E + digits 1-9
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "student_input, expected",
    [
        # A-E letter bare answers.
        ("A", True), ("B", True), ("C", True), ("D", True), ("E", True),
        ("a", True), ("e", True), ("E.", True), ("  d  ", True),
        # Digit option keys 1-9.
        ("1", True), ("2", True), ("3", True), ("9", True),
        ("2.", True), ("  3  ", True),
        # Numeric values (not option keys) — still detected by the
        # numeric branch.
        ("42", True), ("3.14", True), ("-5", True), ("3/4", True),
        # Not bare — has prose / working.
        ("E because 5 × 5 = 25", False),
        ("I pick A", False),
        ("the answer is 7", False),
        # Outside the extended range.
        ("F", False),
        ("0", True),  # 0 is a valid numeric, just not an MCQ key.
        # Empty / whitespace-only.
        ("", False),
        ("   ", False),
    ],
)
def test_is_bare_answer_extended(student_input: str, expected: bool) -> None:
    assert is_bare_answer(student_input) is expected
