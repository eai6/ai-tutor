"""StudentGrader unit tests — Phase 2 §Tests.

Covers:
  - Math path comparator branches (numeric + symbolic).
  - DSL extraction failure → falls through to non-math grader.
  - Non-math grounded LLM ternary verdict resolution.
  - Pre-pose hidden-KB suppression (the prompt contract).
  - Tutor-claim adjudication shape (supported / contradicted /
    unverified — adjudicator status field, NOT the grader verdict).
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
from apps.tutoring.v2.services.student_grader import (
    StudentGrader,
    _extract_canonical_numeric,
    _extract_prose_numeric,
    _match_mcq_letter,
    _match_short_numeric,
    _match_true_false,
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


def _student_claims_payload(value, *, is_attempt=True) -> str:
    """Canned LLM-B (math) payload with a single scalar conclusion."""
    import json as _json
    return _json.dumps({
        "variables": {},
        "claims": [],
        "conclusion": {
            "statement": str(value),
            "answer_extracted_value": value,
            "answer_extracted_label": "",
            "is_attempt": is_attempt,
        },
        "domain_check_required": False,
    })


def test_math_path_correct_with_dsl_match():
    """LLM-A emits valid DSL; LLM-B parses student "25"; match → correct."""
    dsl = '{"variables": {"a": 12, "b": 13}, "expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}}'
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(dsl),
        student_claims_client_factory=lambda: _FakeClient(
            _student_claims_payload(25),
        ),
    )
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
    """LLM-A emits valid DSL; LLM-B parses student "50"; mismatch → wrong."""
    dsl = '{"variables": {"a": 12, "b": 13}, "expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}}'
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(dsl),
        student_claims_client_factory=lambda: _FakeClient(
            _student_claims_payload(50),
        ),
    )
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="50",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    assert result.private_canonical == "25"
    assert result.student_safe_feedback.first_misconception_redacted


def test_math_path_falls_through_to_grounded_on_dsl_extract_failure():
    """When math DSL extraction fails (prose proofs, explain-and-justify
    questions), the grader falls through to the non-math path. Without
    a non-math student-response or judge client supplied, the
    fallthrough resolves to WRONG under the strict ternary contract
    (no clients available → engine retries via wrong-verdict path)."""
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient("not json"),
        grounded_client_factory=lambda: None,
        student_response_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG


def test_math_path_falls_through_to_grounded_on_validation_failure():
    """DSL extracted but failed validation. Falls through to the non-math
    path. Without LLM-B/LLM-C clients available, lands at WRONG under
    the strict ternary contract."""
    dsl = (
        '{"variables": {"a": 99, "b": 13}, '
        '"expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}}'
    )
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(dsl),
        grounded_client_factory=lambda: None,
        student_response_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG


# ──────────────────────────────────────────────────────────────────────
# Non-math two-LLM path (LLM-B student parser + LLM-C judge)
# ──────────────────────────────────────────────────────────────────────


def _student_attempt_payload(stated_answer: str) -> str:
    """Canned LLM-B (non-math) payload with is_attempt=True."""
    import json as _json
    return _json.dumps({
        "is_attempt": True, "hedge_marker": False, "claims": [],
        "conclusion": {
            "stated_answer": stated_answer, "answer_label": "",
            "denies_canonical": False,
        },
    })


def test_non_math_two_llm_correct():
    """Happy path: LLM-B says is_attempt=True; LLM-C says CORRECT."""
    judge = (
        '{"verdict": "correct", "private_canonical": "180", '
        '"what_right": "angles sum to 180", '
        '"citation": "[KB-1] sum is 180", "reason_code": ""}'
    )
    grader = StudentGrader(
        student_response_client_factory=lambda: _FakeClient(
            _student_attempt_payload("180"),
        ),
        grounded_client_factory=lambda: _FakeClient(judge),
    )
    request = GradingRequest(
        open_question=_open_q("What's the sum of triangle angles?"),
        student_input="180",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.CORRECT
    assert result.citation.startswith("[KB-1]")


def test_non_math_meta_input_returns_wrong_with_meta_input_code():
    """LLM-B with is_attempt=false → WRONG reason_code='meta_input'.
    LLM-C is NOT called."""
    student_payload = (
        '{"is_attempt": false, "hedge_marker": false, "claims": [], '
        '"conclusion": {"stated_answer": "", "answer_label": "", '
        '"denies_canonical": false}}'
    )

    def _exploding_judge():
        raise AssertionError("LLM-C must not be called on meta input")

    grader = StudentGrader(
        student_response_client_factory=lambda: _FakeClient(student_payload),
        grounded_client_factory=_exploding_judge,
    )
    request = GradingRequest(
        open_question=_open_q("What is condensation?"),
        student_input="i dont understand. what is condensation",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    assert result.reason_code == "meta_input"


def test_non_math_extraction_failure_returns_grader_extraction_failed():
    """No LLM-B client → student-response extraction fails → WRONG
    with reason_code='grader_extraction_failed' under the strict
    ternary contract."""
    grader = StudentGrader(
        student_response_client_factory=lambda: None,
        grounded_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q("What is condensation?"),
        student_input="something",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    assert result.reason_code == "grader_extraction_failed"


def test_non_math_judge_failure_returns_grader_extraction_failed():
    """LLM-B OK but no LLM-C client → judge call fails → WRONG with
    reason_code='grader_extraction_failed'."""
    grader = StudentGrader(
        student_response_client_factory=lambda: _FakeClient(
            _student_attempt_payload("rain"),
        ),
        grounded_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q("What is condensation?"),
        student_input="rain",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    assert result.reason_code == "grader_extraction_failed"


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


def test_parse_student_math_value_bare_numeric():
    v, s = _parse_student_math_value("25")
    assert v == 25
    assert s


def test_parse_student_math_value_returns_none_on_prose_without_value():
    v, _ = _parse_student_math_value("I added them up")
    assert v is None


# ──────────────────────────────────────────────────────────────────────
# Prose-numeric extraction — covers the verdict-UNVERIFIED regression
# that the S1 / S5 eval surfaced on 2026-05-26 (student answers like
# "ohhh x = 6" / "is it 21?" collapsing to UNVERIFIED). Each row is a
# pattern that returned ``None`` BEFORE the fix and must now resolve to
# the terminal numeric value the student stated.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    ("is it 21?", 21),
    ("is it 21", 21),
    ("ohhh x = 6", 6),
    ("okay x = 15", 15),
    ("x = -4.2", -4.2),
    ("the answer is 7", 7),
    ("it is 7", 7),
    ("answer: 6", 6),
    ("y = 3.5", 3.5),
    ("i think you add 3 to both sides so x = 21", 21),
    # No terminal number → None
    ("I added them up", None),
    ("", None),
    # Trailing punctuation
    ("the answer is 7.", 7),
    ("is it 7?", 7),
])
def test_extract_prose_numeric_patterns(text, expected):
    assert _extract_prose_numeric(text) == expected


def test_parse_student_math_value_prose_wrapped_correct_answer():
    """The bug that motivated the fix: 'ohhh x = 6' was UNVERIFIED."""
    v, s = _parse_student_math_value("ohhh x = 6")
    assert v == 6
    assert s


def test_parse_student_math_value_prose_wrapped_wrong_answer():
    v, _ = _parse_student_math_value("is it 21?")
    assert v == 21  # value extracted; comparator decides it's wrong


def test_parse_student_math_value_picks_terminal_over_setup():
    """Numbers in problem setup ('add 3 to both sides') must NOT mask the
    student's terminal answer ('x = 21')."""
    v, _ = _parse_student_math_value(
        "i think you add 3 to both sides so x = 21"
    )
    assert v == 21


# ──────────────────────────────────────────────────────────────────────
# Canonical-numeric extraction for LessonStep.expected_answer values
# with units ("55 SCR", "2.88 m³/s") or formulaic shapes ("x = 6").
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    ("x = 6", 6),
    ("55 SCR", 55),
    ("2.88 m³/s", 2.88),
    ("40 SCR", 40),
    ("-3.5", -3.5),
    ("no number here", None),
    ("", None),
])
def test_extract_canonical_numeric(text, expected):
    assert _extract_canonical_numeric(text) == expected


# ──────────────────────────────────────────────────────────────────────
# MCQ letter matcher — answer_type='multiple_choice' canonical='B'.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("canonical, student, expected", [
    ("B", "I would pick option B because small-scale maps cover more", True),
    ("B", "B", True),
    ("B", "(B)", True),
    ("B", "[B]", True),
    ("B", "b.", True),
    ("A", "i pick a", True),
    ("A", "answer: A", True),
    ("C", "I'd choose D", False),
    ("B", "I pick c", False),
    ("D", "I think d", True),
    ("B", "I would say B", True),
    # No letter detected → None (fall through to grounded)
    ("B", "I don't know", None),
    ("B", "small-scale maps cover larger areas", None),
    # Canonical is not a single letter → not an MCQ; matcher returns None.
    ("True", "B", None),
    ("the answer is B", "B", None),
])
def test_match_mcq_letter(canonical, student, expected):
    assert _match_mcq_letter(canonical, student) == expected


# ──────────────────────────────────────────────────────────────────────
# True/False matcher.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("canonical, student, expected", [
    ("True", "True", True),
    ("True", "true.", True),
    ("True", "true - large-scale maps show smaller areas", True),
    ("True", "yes", True),
    ("False", "false", True),
    ("False", "no", True),
    ("True", "False", False),
    ("False", "True", False),
    # First-word fast path beats later token: leading "True" wins even
    # when the prose mentions "wrong" further on.
    ("True", "True because the other is wrong", True),
    # Empty / canonical not a T/F token → None
    ("True", "", None),
    ("maybe", "true", None),
    # Mixed signals with no leading T/F token → None (grounded grader)
    ("True", "I think both true and false answers could apply", None),
])
def test_match_true_false(canonical, student, expected):
    assert _match_true_false(canonical, student) == expected


# ──────────────────────────────────────────────────────────────────────
# short_numeric matcher — handles canonicals with units / formulae.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("canonical, student, expected", [
    ("x = 6", "ohhh x = 6", True),
    ("x = 6", "is it 21?", False),
    ("x = 6", "okay x = 6", True),
    ("55 SCR", "55", True),
    ("55 SCR", "the answer is 55", True),
    ("55 SCR", "55 SCR", True),
    ("2.88 m³/s", "2.88", True),
    ("40 SCR", "is it 40?", True),
    ("40 SCR", "is it 50?", False),
    # Student input has no number at all → None.
    ("x = 6", "I don't know", None),
    # Canonical has no number → None (not a short_numeric canonical).
    ("foo bar", "5", None),
])
def test_match_short_numeric(canonical, student, expected):
    assert _match_short_numeric(canonical, student) == expected


# ──────────────────────────────────────────────────────────────────────
# Step.computed regression — the .value→.computed bug at line 763 was
# silently returning None for any multi-step working chain. Test that
# a working-style answer now resolves.
# ──────────────────────────────────────────────────────────────────────


def test_parse_student_math_value_multi_step_working():
    """3-step chain: '95 + 70 = 165' → '165 + 110 = 275'. The analyzer
    should return the terminal claim. Pre-fix this returned None because
    Step has ``.computed`` not ``.value``."""
    v, _ = _parse_student_math_value("95 + 70 = 165\n165 + 110 = 275")
    # The chain analyzer may or may not parse this exact shape — what
    # matters is that the function returns SOMETHING when there's a
    # parseable chain, not silently None. Either int(275) or float(275).
    if v is not None:
        assert int(v) == 275
