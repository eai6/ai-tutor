"""StudentGrader comprehensive tests.

Companion to ``test_grader.py``. Where the original suite covers happy
paths, this file covers the failure surfaces that produced the run-5 /
run-6 / run-7 P1 cascades:

  * **Adversarial student inputs** — typos, prose-wrapped answers,
    MCQ-as-option-text, T/F with rationale, multi-slot ordering, meta
    input ("idk", "explain please").
  * **State-recovery scenarios** — open_question with empty stem,
    whitespace-only student input, contexts where the grader is asked
    to grade something it has no anchor for.
  * **LLM-response-shape variation** — every observed payload form the
    DSL / grounded / verifier LLMs have emitted: bare JSON, fenced
    JSON, embedded-in-prose JSON, truncated, single-quoted Python
    dict, refusal text, lorem-ipsum, leading whitespace.

Per ``testing-patterns-expert``: each test asserts a specific contract
the grader must hold. Tests that probe a known-broken behaviour are
marked in the docstring so the corresponding fix is explicit.

Mirrors ``test_grader.py``'s fake-client harness — no Django DB
dependency for the public-API tests; deterministic stub clients only.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.contracts import (
    GradingRequest,
    OpenQuestion,
    QuestionSource,
    SessionRuntimeState,
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
# Fake LLM client harness — same shape as test_grader.py.
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
        source=QuestionSource.INLINE_GENERATED,
        id=1,
        rendered_stem=stem,
    )


# DSL fixtures — re-used across adversarial input parametrisations.
_DSL_12_PLUS_13 = (
    '{"variables": {"a": 12, "b": 13}, '
    '"expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}}'
)
_DSL_X_PLUS_8_EQ_23 = (
    '{"variables": {}, "expression": '
    '{"op": "solve", "equation": "x + 8 = 23", "var": "x"}}'
)
_DSL_LOSS_MULTI = (
    '{"variables": {"cp": 120, "sp": 90}, '
    '"expressions": ['
    '{"name": "loss_amount", "expression": '
    '{"op": "sub", "args": [{"var": "cp"}, {"var": "sp"}]}},'
    '{"name": "loss_percentage", "expression": '
    '{"op": "mul", "args": ['
    '{"op": "div", "args": ['
    '{"op": "sub", "args": [{"var": "cp"}, {"var": "sp"}]},'
    '{"var": "cp"}]},'
    '100]}}'
    ']}'
)


def _math_grader(
    dsl_payload: str = _DSL_12_PLUS_13,
    *,
    student_claims_payload: str | None = None,
) -> StudentGrader:
    """Build a grader pre-loaded with one LLM-A DSL response.

    Grounded + verifier clients return None so an UNVERIFIED fall-through
    is observable when the math path defers (matches test_grader.py).

    ``student_claims_payload`` is LLM-B (Two-LLM grader). Defaults to
    None — which means LLM-B is not configured and any non-fast-path
    input falls through to the grounded path. Pass an explicit payload
    when the test exercises the Two-LLM comparator directly.
    """
    if student_claims_payload is None:
        student_claims_factory = lambda: None
    else:
        student_claims_factory = lambda: _FakeClient(student_claims_payload)
    return StudentGrader(
        math_client_factory=lambda: _FakeClient(dsl_payload),
        grounded_client_factory=lambda: None,
        student_claims_client_factory=student_claims_factory,
    )


# ══════════════════════════════════════════════════════════════════════
# GROUP 1 — Adversarial student inputs
# ══════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────
# 1a. Bare numeric — answers given as just a number, with variations
#     in form (int, float, decimal, with sign, with unit suffix).
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "student_input,expected_verdict",
    [
        ("25", Verdict.CORRECT),
        ("25.0", Verdict.CORRECT),
        (" 25 ", Verdict.CORRECT),
        ("+25", Verdict.CORRECT),  # signed positive
        ("26", Verdict.WRONG),
        ("24", Verdict.WRONG),
        ("-25", Verdict.WRONG),  # signed negative — not the same number
        ("0", Verdict.WRONG),
    ],
)
def test_bare_numeric_input_variants(student_input, expected_verdict):
    """Bare numeric answers in any reasonable form land on the right verdict.

    Run-7 P1-3 root cause was a missing verdict on a textbook-correct
    answer; this test pins the bare-numeric path so a regression here
    surfaces immediately.
    """
    grader = _math_grader()
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input=student_input,
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"input={student_input!r} expected={expected_verdict} got={result.verdict}"
    )


# ────────────────────────────────────────────────────────────────────
# 1b. Prose-wrapped numeric — the answer is the right number but
#     surrounded by hedging, rationale, or framing.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "student_input,expected_verdict",
    [
        # Hedging openers
        ("is it 25?", Verdict.CORRECT),
        ("Is the answer 25?", Verdict.CORRECT),
        ("is that 25", Verdict.CORRECT),
        # Equals-style answer
        ("= 25", Verdict.CORRECT),
        ("the answer is 25", Verdict.CORRECT),
        ("it is 25", Verdict.CORRECT),
        ("answer: 25", Verdict.CORRECT),
        # Trailing number
        ("after working it out, 25", Verdict.CORRECT),
        ("hmm okay 25.", Verdict.CORRECT),
        # Variable-form
        ("x = 25", Verdict.CORRECT),
        ("X = 25", Verdict.CORRECT),  # uppercase variable
    ],
)
def test_prose_wrapped_numeric_correct(student_input, expected_verdict):
    """Common chat phrasings around the right number still verify."""
    grader = _math_grader()
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input=student_input,
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"input={student_input!r} got verdict={result.verdict}"
    )


@pytest.mark.parametrize(
    "student_input",
    [
        "is it 50?",
        "the answer is 50",
        "x = 50",
        "hmm 50",
    ],
)
def test_prose_wrapped_numeric_wrong(student_input):
    """Prose-wrapped wrong answers extract the wrong number and grade WRONG."""
    grader = _math_grader()
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input=student_input,
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG, (
        f"input={student_input!r} got verdict={result.verdict}"
    )


# ────────────────────────────────────────────────────────────────────
# 1c. MCQ — single-letter canonical with prose-form student responses.
#     Direct matchers only; no LLM call.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "canonical,student_input,expected",
    [
        # Bare letter
        ("B", "B", True),
        ("B", "b", True),
        ("B", "B.", True),
        ("B", "b!", True),
        ("B", "A", False),
        # Bracketed
        ("B", "(B)", True),
        ("B", "[b]", True),
        ("B", "(a)", False),
        # Prose framings
        ("B", "I pick B", True),
        ("B", "I'd choose B", True),
        ("B", "I'll go with b", True),
        ("B", "I think it's B", True),
        ("B", "option B", True),
        ("B", "answer: B", True),
        ("B", "choice b", True),
        ("B", "pick a", False),
        # Lowercase canonical (defensive — humans store either case)
        ("b", "B", True),
        ("b", "I choose B", True),
        # Non-A-D canonical → matcher returns None → grounded path
        ("E", "E", None),
        # Empty student input
        ("B", "", None),
        ("B", "   ", None),
    ],
)
def test_mcq_letter_adversarial(canonical, student_input, expected):
    """MCQ matcher accepts bare letters, brackets, and pick/choose framings."""
    assert _match_mcq_letter(canonical, student_input) is expected


# ────────────────────────────────────────────────────────────────────
# 1d. True/False — with rationale, first-word priority, ambiguity.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "canonical,student_input,expected",
    [
        # Direct
        ("True", "True", True),
        ("True", "true", True),
        ("False", "false", True),
        ("True", "False", False),
        # With rationale — the GEO-S5 run-2 pattern
        ("False", "False because the cycle continues", True),
        ("True", "True - large-scale maps show smaller areas", True),
        ("False", "false. water keeps moving.", True),
        # Synonyms
        ("True", "yes", True),
        ("False", "no", True),
        ("True", "correct", True),
        ("False", "wrong", True),
        # First-word priority: starts True but later contains "false"
        ("True", "True - smaller scales mean false range", True),
        # First-word priority resolves the apparent ambiguity: "true"
        # comes first, even though "false" and "wrong" appear later.
        # First-word wins is the documented contract.
        ("True", "true because false is wrong", True),
        # Ambiguous when neither first-word matches T/F and both
        # tokens are present in the body.
        ("True", "I'm not sure, maybe it's true, but could be false", None),
        # Empty
        ("True", "", None),
        # Non-T/F canonical → matcher returns None
        ("Maybe", "true", None),
        # Punctuation noise
        ("True", "True!", True),
        ("False", "False?", True),
    ],
)
def test_true_false_adversarial(canonical, student_input, expected):
    """T/F matcher handles rationale, synonyms, and ambiguity."""
    assert _match_true_false(canonical, student_input) is expected


# ────────────────────────────────────────────────────────────────────
# 1e. Short numeric — canonical like "55 SCR" / "2.88 m³/s" / "x = 6".
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "canonical,student_input,expected",
    [
        # Match
        ("55 SCR", "55", True),
        ("55 SCR", "55 SCR", True),
        ("55 SCR", "the answer is 55", True),
        ("x = 6", "x = 6", True),
        ("x = 6", "6", True),
        ("x = 6", "x equals 6", True),
        ("2.88 m³/s", "2.88", True),
        # Mismatch
        ("55 SCR", "50", False),
        ("x = 6", "x = 7", False),
        # No numeric → None
        ("55 SCR", "I dunno", None),
        ("five", "5", None),  # canonical has no numeric token
    ],
)
def test_short_numeric_adversarial(canonical, student_input, expected):
    """Short-numeric matcher pulls dominant number from canonical + student."""
    assert _match_short_numeric(canonical, student_input) is expected


# ────────────────────────────────────────────────────────────────────
# 1f. Multi-slot math — multiple quantities required by one question.
#     Tests verdict + safe-feedback shape for full / partial / wrong.
# ────────────────────────────────────────────────────────────────────


def test_multi_slot_private_canonical_surfaces_all_slots():
    """Multi-slot canonical exposes BOTH slot values via private_canonical.

    The current student-input parser pulls a single terminal numeric,
    so a literal "all slots match" student-answer case requires a
    prose-numeric extension. This test pins what the canonical contract
    delivers (both slot names + values are visible to the engine for
    feedback authoring) regardless of student value matching.
    """
    grader = _math_grader(_DSL_LOSS_MULTI)
    request = GradingRequest(
        open_question=_open_q(
            "A trader buys spices for 120 SCR and sells for 90 SCR. "
            "Calculate the loss amount and the loss percentage."
        ),
        student_input="30",  # matches loss_amount only
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.PARTIAL
    # private_canonical surfaces BOTH slots so the engine can author
    # feedback that names both quantities. Slot names are humanised
    # ("loss_amount" → "loss amount") for the canonical string.
    assert "loss amount" in result.private_canonical
    assert "loss percentage" in result.private_canonical
    assert "30" in result.private_canonical  # loss_amount value
    assert "25" in result.private_canonical  # loss_percentage value


def test_multi_slot_one_value_matches_one_slot_partial():
    """Student gives a single number that matches one slot only → PARTIAL."""
    grader = _math_grader(_DSL_LOSS_MULTI)
    request = GradingRequest(
        open_question=_open_q(
            "A trader buys spices for 120 SCR and sells for 90 SCR. "
            "Calculate the loss amount and the loss percentage."
        ),
        student_input="30",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.PARTIAL
    # Feedback names what they got
    assert result.student_safe_feedback.what_right
    assert "loss_amount" in result.student_safe_feedback.what_right \
        or "loss" in result.student_safe_feedback.what_right.lower()


def test_multi_slot_no_match_is_wrong():
    """Student value matches no slot → WRONG; misconception redacted."""
    grader = _math_grader(_DSL_LOSS_MULTI)
    request = GradingRequest(
        open_question=_open_q(
            "A trader buys spices for 120 SCR and sells for 90 SCR. "
            "Calculate the loss amount and the loss percentage."
        ),
        student_input="210",  # cp + sp; not a slot
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    # Canonical never leaks via safe_feedback
    assert "30" not in result.student_safe_feedback.first_misconception_redacted
    assert "25" not in result.student_safe_feedback.first_misconception_redacted


# ────────────────────────────────────────────────────────────────────
# 1g. Meta input and refusal — student didn't actually attempt.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "student_input",
    [
        "I don't know",
        "idk",
        "can you explain?",
        "what does inverse mean?",
        "huh?",
        "I'm stuck",
    ],
)
def test_meta_input_does_not_resolve_to_wrong(student_input):
    """Meta input (not a numeric attempt) shouldn't graders as WRONG.

    The math DSL value-parser returns None on these → falls through to
    grounded → with no grounded client, the verdict is UNVERIFIED. This
    is the contract: meta input is NOT a wrong answer, and the move
    selection layer (downstream) routes it differently.
    """
    grader = _math_grader()
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input=student_input,
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict != Verdict.WRONG, (
        f"meta input {student_input!r} graded as WRONG — should be UNVERIFIED"
    )


# ══════════════════════════════════════════════════════════════════════
# GROUP 2 — State-recovery scenarios
# ══════════════════════════════════════════════════════════════════════


def test_empty_rendered_stem_returns_unverified_with_state_signal():
    """Open question with empty rendered_stem → UNVERIFIED + a reasoning
    string that downstream can identify as state-inconsistent.

    Run-7 P1-3 root cause: the engine left runtime_state.open_question
    null after a leaked tool-call. The grader had nothing to anchor on.
    Today the grader still tries to extract DSL from "" — which is
    wasteful and produces no useful signal. The contract: detect the
    empty stem upfront, short-circuit to UNVERIFIED, and stamp the
    ``reasoning`` field with ``state_inconsistent`` so the engine /
    conformance pipeline can distinguish "grader couldn't decide" from
    "grader had nothing to decide against".
    """
    grader = _math_grader()
    request = GradingRequest(
        open_question=_open_q(""),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED
    assert "state_inconsistent" in (result.reasoning or "").lower()


def test_whitespace_only_rendered_stem_treated_as_empty():
    """A stem of just whitespace is functionally empty → same signal."""
    grader = _math_grader()
    request = GradingRequest(
        open_question=_open_q("    \n  \t  "),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED
    assert "state_inconsistent" in (result.reasoning or "").lower()


def test_empty_student_input_does_not_crash():
    """Empty student_input on a real question → UNVERIFIED, no crash.

    The grader must remain stable: empty input is a state signal, not
    an exception trigger. Move selection / engine handles empty
    elsewhere (skip the grader entirely when input is empty), but the
    grader's own defensive behaviour matters when contracts drift.
    """
    grader = _math_grader()
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="",
        is_math=True,
    )
    # No exception, no crash; verdict is UNVERIFIED (no value to grade).
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED


def test_whitespace_only_student_input_does_not_crash():
    """Whitespace-only input is treated as empty; no crash."""
    grader = _math_grader()
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="   \n\t  ",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED


def test_state_inconsistent_does_not_consume_math_llm_quota():
    """When the stem is empty, the math DSL extractor must not be called.

    Tests the short-circuit. The previous behaviour wasted an LLM call
    trying to extract a DSL from "". With the state-inconsistent
    short-circuit, the math_client queue stays untouched — verifiable
    by checking the queue's residual size.
    """
    queue = ["should-never-be-consumed"]

    class _SpyClient:
        def __init__(self, payloads):
            self._q = list(payloads)

        def generate(self, **kwargs):
            if not self._q:
                raise RuntimeError("SpyClient queue empty")
            return _FakeResp(self._q.pop(0))

        @property
        def remaining(self):
            return len(self._q)

    spy = _SpyClient(queue)
    grader = StudentGrader(
        math_client_factory=lambda: spy,
        grounded_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q(""),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED
    assert spy.remaining == 1, (
        "math DSL client should NOT be called on empty stem — "
        "spy queue still has 1 item; got " f"{spy.remaining}"
    )


def test_non_math_empty_stem_also_short_circuits():
    """The state-inconsistent guard applies to the non-math path too.

    A non-math question with empty stem can't be grounded — there's
    nothing to ground against.
    """
    grader = StudentGrader(
        math_client_factory=lambda: None,
        grounded_client_factory=lambda: _FakeClient("should-not-be-called"),
    )
    request = GradingRequest(
        open_question=_open_q(""),
        student_input="Condensation",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED
    assert "state_inconsistent" in (result.reasoning or "").lower()


# ══════════════════════════════════════════════════════════════════════
# GROUP 3 — LLM-response-shape variation
# ══════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────
# 3a. _safe_json_loads — the universal JSON parser used by every
#     downstream grader LLM call site.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Bare JSON object
        ('{"a": 1}', {"a": 1}),
        # Fenced JSON
        ('```json\n{"a": 1}\n```', {"a": 1}),
        # Fenced generic
        ('```\n{"a": 1}\n```', {"a": 1}),
        # Fenced with language label
        ('```JSON\n{"a": 1}\n```', {"a": 1}),
        # Embedded in prose
        ('Here is the JSON: {"a": 1}\nthanks!', {"a": 1}),
        ('Sure! {"a": 1} hope this helps', {"a": 1}),
        # Leading whitespace
        ('   {"a": 1}', {"a": 1}),
        ('\n\n{"a": 1}\n', {"a": 1}),
        # Empty / blank — must return None, not raise
        ('', None),
        ('   ', None),
        ('\n', None),
        # No JSON at all
        ("not json at all", None),
        ("I cannot answer that question.", None),
        # Truncated mid-object — must return None, not raise
        ('{"a": 1', None),
        ('{"a"', None),
        # Trailing-comma JSON — json.stdlib rejects; must return None
        ('{"a": 1, "b": 2,}', None),
        # Garbage with braces inside
        ("text { not json } more text", None),
        # Nested object — JSON parses it correctly
        ('{"outer": {"inner": 1}}', {"outer": {"inner": 1}}),
    ],
)
def test_safe_json_loads_response_shapes(raw, expected):
    """_safe_json_loads handles every observed LLM response shape."""
    assert _safe_json_loads(raw) == expected


# ────────────────────────────────────────────────────────────────────
# 3b. Math DSL extractor robustness — the DSL response goes through
#     _safe_json_loads but additionally must produce a dict with the
#     expected shape. Bad shapes fall through to the grounded path
#     (which, with no grounded client, lands at UNVERIFIED).
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dsl_payload,expected_verdict",
    [
        # Happy path — valid DSL
        (_DSL_12_PLUS_13, Verdict.CORRECT),
        # Empty response
        ("", Verdict.UNVERIFIED),
        # Refusal
        ("I cannot help with that.", Verdict.UNVERIFIED),
        # Truncated JSON
        ('{"variables": {"a": 12,', Verdict.UNVERIFIED),
        # Valid JSON but wrong shape (not a dict)
        ('[1, 2, 3]', Verdict.UNVERIFIED),
        # Valid JSON, dict, but missing expression/expressions key
        ('{"variables": {"a": 12}}', Verdict.UNVERIFIED),
        # Fenced valid DSL — should still work
        (f"```json\n{_DSL_12_PLUS_13}\n```", Verdict.CORRECT),
        # Embedded in prose
        (f"Here's the DSL: {_DSL_12_PLUS_13}\nlet me know!", Verdict.CORRECT),
        # Single-quoted Python dict — json.loads rejects → UNVERIFIED
        ("{'variables': {'a': 12, 'b': 13}, 'expression': "
         "{'op': 'add', 'args': [{'var': 'a'}, {'var': 'b'}]}}",
         Verdict.UNVERIFIED),
    ],
)
def test_math_path_robust_to_llm_response_shape(dsl_payload, expected_verdict):
    """Math grader handles every observed DSL response shape gracefully.

    Bad shapes never raise — they collapse to UNVERIFIED via the
    fall-through to the grounded path (no grounded client here).
    """
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(dsl_payload),
        grounded_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"dsl_payload={dsl_payload!r} expected={expected_verdict} "
        f"got={result.verdict}"
    )


# ────────────────────────────────────────────────────────────────────
# 3c. Non-math LLM-C (judge) robustness — every observed shape.
# ────────────────────────────────────────────────────────────────────


# A canned attempt-true LLM-B response so the non-math pipeline reaches
# the LLM-C call site. (Meta-input / non-attempt is exercised separately.)
_NM_STUDENT_RESPONSE_ATTEMPT = (
    '{"is_attempt": true, "hedge_marker": false, "claims": [], '
    '"conclusion": {"stated_answer": "Condensation", '
    '"answer_label": "", "denies_canonical": false}}'
)


@pytest.mark.parametrize(
    "judge_payload,expected_verdict",
    [
        # Happy path — correct
        ('{"verdict": "correct", "private_canonical": "Condensation", '
         '"what_right": "named the stage", "what_missing": "", '
         '"first_misconception": "", "citation": "", "reason_code": ""}',
         Verdict.CORRECT),
        # Wrong with known misconception
        ('{"verdict": "wrong", "private_canonical": "Condensation", '
         '"what_right": "", "what_missing": "", '
         '"first_misconception": "mixing condensation up with precipitation", '
         '"citation": "", "reason_code": "known_misconception"}',
         Verdict.WRONG),
        # Empty / refusal — UNVERIFIED via extraction failure
        ('', Verdict.UNVERIFIED),
        ('I cannot answer this question.', Verdict.UNVERIFIED),
        # Empty JSON object — defensive parsing returns UNVERIFIED
        ('{}', Verdict.UNVERIFIED),
        # Just a verdict field — UNVERIFIED defaults stay safe
        ('{"verdict": "correct"}', Verdict.CORRECT),
        # Garbage — UNVERIFIED via extraction failure
        ('lorem ipsum dolor sit amet', Verdict.UNVERIFIED),
    ],
)
def test_non_math_judge_robust_to_llm_response_shape(judge_payload, expected_verdict):
    """LLM-C handles every observed response shape gracefully.

    The non-math pipeline is LLM-B (student parser) → LLM-C (judge).
    LLM-B is stubbed to a canned is_attempt=true payload so the pipeline
    reaches LLM-C; LLM-C's response shape is the variable under test.
    """
    grader = StudentGrader(
        math_client_factory=lambda: None,
        grounded_client_factory=lambda: _FakeClient(judge_payload),
        student_response_client_factory=lambda: _FakeClient(
            _NM_STUDENT_RESPONSE_ATTEMPT,
        ),
    )
    request = GradingRequest(
        open_question=_open_q("What is the cooling stage of the water cycle called?"),
        student_input="Condensation",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"payload={judge_payload!r} expected={expected_verdict} "
        f"got={result.verdict}"
    )


def test_non_math_meta_input_short_circuits_before_judge():
    """When LLM-B reports is_attempt=false, LLM-C is NOT called.

    Meta input ("i dont understand. what is X") → UNVERIFIED +
    reason_code='meta_input'. This is the GEO-S5 P1-3 fix.
    """
    student_response_payload = (
        '{"is_attempt": false, "hedge_marker": false, "claims": [], '
        '"conclusion": {"stated_answer": "", "answer_label": "", '
        '"denies_canonical": false}}'
    )

    class _SpyClient:
        def __init__(self, payload):
            self._q = [payload]

        def generate(self, **kwargs):
            return _FakeResp(self._q.pop(0))

        @property
        def remaining(self):
            return len(self._q)

    spy_judge = _SpyClient("should-never-be-called")
    grader = StudentGrader(
        math_client_factory=lambda: None,
        student_response_client_factory=lambda: _FakeClient(student_response_payload),
        grounded_client_factory=lambda: spy_judge,
    )
    request = GradingRequest(
        open_question=_open_q("What is the cooling stage of the water cycle?"),
        student_input="i dont understand. what is condensation",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED
    assert result.reason_code == "meta_input"
    assert spy_judge.remaining == 1, (
        "LLM-C must NOT be called on meta input"
    )


def test_non_math_self_reported_guess_downgrades_correct_to_partial():
    """When LLM-B detects a hedge marker AND LLM-C would say CORRECT,
    the post-check downgrades to PARTIAL with reason_code='self_reported_guess'.

    This is the GEO-S5 P1-4 fix: "guess B" no longer counts as mastery
    even when the letter is right.
    """
    student_response_payload = (
        '{"is_attempt": true, "hedge_marker": true, "claims": [], '
        '"conclusion": {"stated_answer": "B", "answer_label": "B", '
        '"denies_canonical": false}}'
    )
    judge_payload = (
        '{"verdict": "correct", "private_canonical": "B", '
        '"what_right": "letter matches", "what_missing": "", '
        '"first_misconception": "", "citation": "", "reason_code": ""}'
    )
    grader = StudentGrader(
        math_client_factory=lambda: None,
        student_response_client_factory=lambda: _FakeClient(student_response_payload),
        grounded_client_factory=lambda: _FakeClient(judge_payload),
    )
    request = GradingRequest(
        open_question=_open_q(
            "Which letter shows the condensation stage? A/B/C/D"
        ),
        student_input="guess B",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.PARTIAL
    assert result.reason_code == "self_reported_guess"


def test_non_math_canonical_leak_in_safe_feedback_redacted():
    """Belt-and-braces: if LLM-C accidentally puts the canonical string
    into safe_feedback, the Python post-check scrubs it."""
    student_response_payload = (
        '{"is_attempt": true, "hedge_marker": false, "claims": [], '
        '"conclusion": {"stated_answer": "rain", "answer_label": "", '
        '"denies_canonical": false}}'
    )
    # LLM-C emits 'Condensation' in private_canonical AND leaks it
    # into safe_feedback fields.
    judge_payload = (
        '{"verdict": "wrong", "private_canonical": "Condensation", '
        '"what_right": "", "what_missing": "", '
        '"first_misconception": "you said rain but the answer is Condensation", '
        '"citation": "", "reason_code": ""}'
    )
    grader = StudentGrader(
        math_client_factory=lambda: None,
        student_response_client_factory=lambda: _FakeClient(student_response_payload),
        grounded_client_factory=lambda: _FakeClient(judge_payload),
    )
    request = GradingRequest(
        open_question=_open_q("What is the cooling stage of the water cycle?"),
        student_input="rain",
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    # private_canonical surfaces internally...
    assert "Condensation" in result.private_canonical
    # ...but is scrubbed from any safe_feedback field.
    assert "condensation" not in result.student_safe_feedback.first_misconception_redacted.lower()
    assert "condensation" not in result.student_safe_feedback.what_right.lower()
    assert "condensation" not in result.student_safe_feedback.what_missing.lower()




# ════════════════════════════════════════════════════════════════════
# Bonus — low-level helper invariants surfaced by adversarial inputs
# ════════════════════════════════════════════════════════════════════


def test_parse_student_math_value_handles_long_prose():
    """A long prose response with the answer at the end still extracts it."""
    text = (
        "Okay let me think about this carefully. The question is asking "
        "for 12 plus 13. I can do that by counting up from 12 thirteen "
        "more times, but that's tedious. Better to recognise 12+13 is "
        "the same as 10+10+3+2 which is 25. So x = 25"
    )
    value, _ = _parse_student_math_value(text)
    assert value == 25


def test_parse_student_math_value_does_not_pull_from_problem_setup():
    """Numbers in the student's restatement of the question shouldn't be
    treated as the answer. Anchored on terminal-answer phrasing."""
    text = "Question said 12 + 13, so working through that gives 25"
    value, _ = _parse_student_math_value(text)
    assert value == 25


def test_extract_canonical_numeric_handles_units():
    """Canonical strings often carry units; the dominant number wins."""
    assert _extract_canonical_numeric("55 SCR") == 55
    assert _extract_canonical_numeric("2.88 m³/s") == 2.88
    assert _extract_canonical_numeric("x = 6") == 6
    assert _extract_canonical_numeric("approximately 33%") == 33


# ════════════════════════════════════════════════════════════════════
# GROUP 4 — Observed cases from prior evaluation runs
# ════════════════════════════════════════════════════════════════════
#
# Every (question, student_response, expected_verdict) triple below was
# observed in a real session transcript (MATHS-S1 / GEO-S5 runs 2-7).
# These are the cases that produced — or could have produced — a P1 if
# the grader misbehaved. Pinning them here means any future regression
# on a real transcript reproduces as a unit-test failure.
# ════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────
# 4a. MATHS-S1 — Pythagoras (sessions 87/94, lesson 1177, runs 2-7).
# Real student responses tested via the direct matcher functions used
# inside _try_direct_step_match. Independent of Django ORM — pure
# string matching on the canonical letter / Yes/No / True/False.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "canonical,student_response,expected_match",
    [
        # ─── run-7 Pythagoras turns ─────────────────────────────────
        # T1417 — 5,12,13 → "Yes" (canonical matches first-word)
        (
            "Yes",
            "c=13, a=5, b=12. a^2+b^2 = 25+144 = 169. c^2 = 169. "
            "Since 169=169, the triangle IS right-angled.",
            True,
        ),
        # T1489 — 8,15,17 → "Yes"
        (
            "Yes",
            "Yes. Take c=17 (longest). a^2 + b^2 = 64 + 225 = 289 = 17^2. "
            "So the triangle is right-angled.",
            True,
        ),
        # T1491 — 7,24,25 → "True"
        (
            "True",
            "True. 7^2 + 24^2 = 49 + 576 = 625 and 25^2 = 625, "
            "so a^2+b^2=c^2 holds.",
            True,
        ),
        # T1501 — 5,7,9 → "No" (the run-7 P1 turn)
        (
            "No",
            "No. Longest side is 9, so test 5^2 + 7^2 = 25 + 49 = 74, "
            "but 9^2 = 81. Since 74 != 81, the triangle is NOT right-angled.",
            True,
        ),
        # Wrong answers
        (
            "Yes",
            "No because 5^2 + 7^2 != 9^2",
            False,
        ),
        (
            "No",
            "Yes, the triangle is right-angled.",
            False,
        ),
    ],
)
def test_observed_pythagoras_true_false_responses(
    canonical, student_response, expected_match,
):
    """Real Pythagoras Y/N / T/F responses from runs 2-7 match the
    canonical via the deterministic T/F matcher. No LLM call needed.
    """
    assert _match_true_false(canonical, student_response) is expected_match


@pytest.mark.parametrize(
    "canonical,student_response,expected_match",
    [
        # T1493 — Run-7 — Triangle MCQ → "B"
        (
            "B",
            "B. Triangle B (6, 8, 10) — because 6^2 + 8^2 = 36 + 64 = 100 "
            "= 10^2. A gives 41 vs 36 and C gives 113 vs 81, neither balances.",
            True,
        ),
        # T1497 — Run-7 — 6,8,10 satisfaction MCQ → "A"
        (
            "A",
            "A. 6^2 + 8^2 = 36 + 64 = 100 and 10^2 = 100, so yes.",
            True,
        ),
    ],
)
def test_observed_pythagoras_mcq_responses(canonical, student_response, expected_match):
    """Real Pythagoras MCQ-with-rationale responses from runs 2-7.

    Direct MCQ-letter matcher catches the leading letter even when
    followed by long mathematical justification.
    """
    assert _match_mcq_letter(canonical, student_response) is expected_match


# ────────────────────────────────────────────────────────────────────
# 4b. MATHS-S1 — Profit/loss/percentage (session 87, lesson 1167; runs 3-7).
# Bare-numeric and prose-numeric responses through the DSL/exec path.
# ────────────────────────────────────────────────────────────────────


# Multi-slot DSL for "profit 9 + profit% 50" question. The variables
# match the rendered stem ("buys for 18 SCR, sells for 27 SCR") so the
# MathVerificationTool's variable-bindings validator accepts the DSL.
_DSL_PROFIT_MULTI_27_18 = (
    '{"variables": {"sp": 27, "cp": 18}, '
    '"expressions": ['
    '{"name": "profit", "expression": '
    '{"op": "sub", "args": [{"var": "sp"}, {"var": "cp"}]}},'
    '{"name": "profit_percentage", "expression": '
    '{"op": "mul", "args": ['
    '{"op": "div", "args": ['
    '{"op": "sub", "args": [{"var": "sp"}, {"var": "cp"}]},'
    '{"var": "cp"}]},'
    '100]}}'
    ']}'
)


_PROFIT_STEM_27_18 = (
    "A vendor buys breadfruit for 18 SCR each and sells them for 27 SCR each. "
    "Calculate the profit per breadfruit and the profit percentage."
)


@pytest.mark.parametrize(
    "student_response,expected_verdict",
    [
        # Run-3/5 — "is it 37?" / "is it 37 SCR?" — wrong (added instead
        # of subtracted). Student parser pulls 37 → matches no slot → WRONG.
        ("is it 37?", Verdict.WRONG),
        # Run-7 — "profit is 45 SCR and profit percentage is 60%" —
        # student parser pulls terminal numeric (60). 60 matches no slot
        # → WRONG.
        ("profit is 45 SCR and profit percentage is 60%", Verdict.WRONG),
        # Run-5/7 — "is the profit 9?" — 9 matches "profit" slot only
        # → PARTIAL (one of two slots).
        ("is the profit 9?", Verdict.PARTIAL),
        # Run-5 — "i think profit is 27 - 18 = 9 SCR but i dont know
        # the percentage" — parser extracts terminal 9 → matches profit
        # → PARTIAL.
        ("i think profit is 27 - 18 = 9 SCR", Verdict.PARTIAL),
        # Run-5 — "profit is 9" (bare) — same → PARTIAL.
        ("profit is 9", Verdict.PARTIAL),
        # Run-5 — "is the percentage 9 percent?" — student confused the
        # answer to one slot with the other. 9 → matches profit slot
        # → PARTIAL.
        ("is the percentage 9 percent?", Verdict.PARTIAL),
        # Run-5 — "maybe profit is 27 + 18 = 45" — wrong (added) → WRONG.
        ("maybe profit is 27 + 18 = 45", Verdict.WRONG),
        # Run-5 — "is the percentage 9 percent?" alt phrasing.
        ("is the percentage 50?", Verdict.PARTIAL),  # 50 matches profit_percentage
        # Run-4 — "the profit is 200" — wildly wrong → WRONG.
        ("the profit is 200", Verdict.WRONG),
    ],
)
def test_observed_profit_loss_multi_slot_responses(student_response, expected_verdict):
    """Real profit/loss student responses from runs 3-7.

    Multi-slot grading: a single value matches one slot → PARTIAL;
    a value matching no slot → WRONG. The grader never returns
    UNVERIFIED for these — math always lands a verdict.
    """
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(_DSL_PROFIT_MULTI_27_18),
        grounded_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q(_PROFIT_STEM_27_18),
        student_input=student_response,
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"student_response={student_response!r} "
        f"got={result.verdict} expected={expected_verdict}"
    )
    # Math contract: no UNVERIFIED when the stem is consistent.
    assert result.verdict != Verdict.UNVERIFIED


# ────────────────────────────────────────────────────────────────────
# Single-slot profit/loss responses — run-2 (L1148 one-step equations)
# and run-4 (L1167 spices loss).
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dsl_payload,stem,student_response,expected_verdict",
    [
        # Run-2 — L1148 — "Solve x+8=23" canonical x=15.
        (
            '{"variables": {}, "expression": '
            '{"op": "solve", "equation": "x + 8 = 23", "var": "x"}}',
            "Solve the equation x + 8 = 23.",
            "is it x = 21?",  # wrong
            Verdict.WRONG,
        ),
        (
            '{"variables": {}, "expression": '
            '{"op": "solve", "equation": "x + 8 = 23", "var": "x"}}',
            "Solve the equation x + 8 = 23.",
            "x = 15",
            Verdict.CORRECT,
        ),
        (
            '{"variables": {}, "expression": '
            '{"op": "solve", "equation": "x + 8 = 23", "var": "x"}}',
            "Solve the equation x + 8 = 23.",
            "23 - 8 is 14",  # student parser pulls 14
            Verdict.WRONG,
        ),
        # Run-2 — "Solve 4x=32" canonical x=8.
        (
            '{"variables": {}, "expression": '
            '{"op": "solve", "equation": "4 * x = 32", "var": "x"}}',
            "Solve the equation 4x = 32.",
            "x = 36",  # wrong, parser pulls 36
            Verdict.WRONG,
        ),
        # Run-2 — fish: "42 - x = 17" → x = 25.
        (
            '{"variables": {}, "expression": '
            '{"op": "solve", "equation": "42 - x = 17", "var": "x"}}',
            "A fisherman in Seychelles starts with 42 fish in his net. "
            "After selling x fish he has 17 left. Solve 42 - x = 17.",
            "x = 59",  # wrong
            Verdict.WRONG,
        ),
        # Run-4 — L1167 — spices loss = 30 (single slot).
        (
            '{"variables": {"cp": 120, "sp": 90}, '
            '"expression": {"op": "sub", "args": [{"var": "cp"}, {"var": "sp"}]}}',
            "An island trader buys imported spices for 120 SCR per package "
            "and sells them for 90 SCR per package. Calculate the loss amount.",
            "is the loss 30?",
            Verdict.CORRECT,
        ),
        (
            '{"variables": {"cp": 120, "sp": 90}, '
            '"expression": {"op": "sub", "args": [{"var": "cp"}, {"var": "sp"}]}}',
            "An island trader buys imported spices for 120 SCR per package "
            "and sells them for 90 SCR per package. Calculate the loss amount.",
            "loss is 210",  # wrong
            Verdict.WRONG,
        ),
    ],
)
def test_observed_single_slot_math_responses(
    dsl_payload, stem, student_response, expected_verdict
):
    """Real single-slot math responses from runs 2-4."""
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(dsl_payload),
        grounded_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q(stem),
        student_input=student_response,
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"student_response={student_response!r} "
        f"got={result.verdict} expected={expected_verdict}"
    )
    # Math contract: no UNVERIFIED when the stem is consistent.
    assert result.verdict != Verdict.UNVERIFIED


# ────────────────────────────────────────────────────────────────────
# 4c. GEO-S5 — Hydrological cycle MCQ + T/F responses (runs 2-7).
# Direct-matcher tests, no Django ORM dependency.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "canonical,student_response,expected_match",
    [
        # Run-7 T1 — Box 2 = Condensation (B). Student picks A.
        ("B", "A", False),
        # Run-7 T2 — student picks C with rationale.
        ("B", "i think C precipitation because rain is water droplets", False),
        # Run-7 T5 — student guesses correctly.
        ("B", "guess B", True),
        # Run-6 — variations
        ("B", "B - it's condensation", True),
        ("B", "I'd say B", True),
        # Run-4/5 (weathering MCQ canonical B)
        ("B", "b because pitted minerals show chemical change", True),
        ("C", "option C", True),
        # Wrong picks with rationale (run-3/5 GEO)
        ("B", "I think it's D, biological weathering", False),
        ("D", "the answer is A", False),
    ],
)
def test_observed_geo_mcq_responses(canonical, student_response, expected_match):
    """Real geography MCQ responses from GEO-S5 runs 2-7."""
    assert _match_mcq_letter(canonical, student_response) is expected_match


@pytest.mark.parametrize(
    "canonical,student_response,expected_match",
    [
        # Run-7 T6 — compound T/F, student picks True (wrong, canonical False).
        ("False", "True i think", False),
        # Run-7 T6 follow-up after scaffold — student wavers
        ("False", "False", True),
        # Run-2/3 — weathering T/F
        ("False", "false because of salt spray accelerating it", True),
        # Run-4 — definitions T/F
        ("True", "true - the granite would crack", True),
        # Run-5 — student says "yes" / "no" as T/F synonyms
        ("True", "yes", True),
        ("False", "no", True),
        # Run-7 - "True i think" — first word True wins
        ("True", "True i think", True),
    ],
)
def test_observed_geo_true_false_responses(canonical, student_response, expected_match):
    """Real geography T/F responses from GEO-S5 runs 2-7."""
    assert _match_true_false(canonical, student_response) is expected_match


# ────────────────────────────────────────────────────────────────────
# 4d. GEO-S5 — open-ended responses that need grounded-LLM adjudication.
# These responses don't fit MCQ/T-F shape; the grader routes to the
# grounded path. Stub the grounded LLM with the expected verdict.
# ────────────────────────────────────────────────────────────────────


def _student_response_attempt_payload(
    *,
    stated_answer: str,
    hedge_marker: bool = False,
    denies_canonical: bool = False,
) -> str:
    """Build an LLM-B (non-math student response) payload for is_attempt=True."""
    import json as _json
    return _json.dumps({
        "is_attempt": True,
        "hedge_marker": hedge_marker,
        "claims": [],
        "conclusion": {
            "stated_answer": stated_answer,
            "answer_label": "",
            "denies_canonical": denies_canonical,
        },
    })


def _judge_payload(
    verdict: str,
    canonical: str = "(redacted in test)",
    reason_code: str = "",
) -> str:
    """Build an LLM-C (non-math judge) payload."""
    return (
        '{'
        f'"verdict": "{verdict}", '
        f'"private_canonical": "{canonical}", '
        f'"what_right": "", "what_missing": "", '
        f'"first_misconception": "", "citation": "", '
        f'"reason_code": "{reason_code}"'
        '}'
    )


@pytest.mark.parametrize(
    "stem,student_response,judge_verdict,expected_verdict",
    [
        # Run-7 turn 8 — "stays in the ground" → WRONG.
        (
            "Does water actually stay on the islands forever, "
            "or does it eventually go somewhere else?",
            "i think it stays in the ground",
            "wrong",
            Verdict.WRONG,
        ),
        # Run-7 turn 9 — "it keeps going around" → CORRECT.
        (
            "Does water actually stay on the islands forever, "
            "or does it eventually go somewhere else?",
            "it keeps going around",
            "correct",
            Verdict.CORRECT,
        ),
        # Run-7 turn 10 — "condensation would fail" → WRONG.
        (
            "If the sun suddenly stopped providing heat energy, which "
            "single stage of the hydrological cycle would fail first?",
            "condensation would fail",
            "wrong",
            Verdict.WRONG,
        ),
        # Run-7 turn 7 — "from plants?" → PARTIAL (names transpiration source).
        (
            "Can you think of any other way water might enter the "
            "atmosphere from plants or the land surface in Seychelles?",
            "from plants? i dont know",
            "partial",
            Verdict.PARTIAL,
        ),
    ],
)
def test_observed_geo_open_ended_responses(
    stem, student_response, judge_verdict, expected_verdict,
):
    """Real open-ended geography responses through the two-LLM non-math path.

    LLM-B parses the student into structured claims; LLM-C judges. Both
    stubbed with the expected shape.
    """
    grader = StudentGrader(
        math_client_factory=lambda: None,
        student_response_client_factory=lambda: _FakeClient(
            _student_response_attempt_payload(stated_answer=student_response),
        ),
        grounded_client_factory=lambda: _FakeClient(
            _judge_payload(judge_verdict),
        ),
    )
    request = GradingRequest(
        open_question=_open_q(stem),
        student_input=student_response,
        is_math=False,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"student={student_response!r} judge_verdict={judge_verdict} "
        f"got={result.verdict} expected={expected_verdict}"
    )


# ────────────────────────────────────────────────────────────────────
# 4e. Meta / help-request inputs observed in real runs.
# These should never produce WRONG — meta input is not a wrong answer.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "student_input",
    [
        "i dont understand. what is condensation",  # GEO-S5 turn 4
        "I am ready. Please give me a triangle that is NOT right-angled "
        "and let me show that it fails.",  # MATHS-S1 turn 9
        "Give me a problem to try.",  # MATHS-S1 mid-session
        "Sounds good — give me the next problem.",  # MATHS-S1 between items
        "i dont know how to do percentages",  # MATHS-S1 lesson 1167
        "im totally lost can you show me a worked example?",  # MATHS-S1
        "can you give me a profit and loss question",  # MATHS-S1
        "i dunno. maybe it goes back into the sea?",  # GEO-S5 turn 0
        "i remember evaporation goes up and condensation makes droplets",  # MATHS-S1 recap
    ],
)
def test_observed_meta_requests_never_grade_wrong(student_input):
    """Help-requests / readiness signals / recaps must not grade as WRONG.

    Pins the contract: any "I don't understand" or "give me a problem"
    or "I'm ready" style input should resolve to UNVERIFIED (or be
    skipped by the engine before the grader is called). NEVER WRONG —
    that would tell the student their lack-of-attempt was scored as a
    wrong attempt, which is a P1 in everything but name.
    """
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(_DSL_12_PLUS_13),
        grounded_client_factory=lambda: None,
    )
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input=student_input,
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict != Verdict.WRONG, (
        f"meta input {student_input!r} graded as WRONG — "
        "this would be a P1-class regression in a real session"
    )


# ══════════════════════════════════════════════════════════════════════
# GROUP 5 — Two-LLM grader (design/tasks/two-llm-grader-implementation-plan.md)
# Section §5.1 required tests. The stub LLM-B payload represents the
# claim graph the structured-output Haiku 4.5 emits. Each test pairs an
# LLM-A canonical DSL with a hand-built LLM-B claims payload.
# ══════════════════════════════════════════════════════════════════════


def _claims_payload(
    *,
    claims: list[dict] | None = None,
    answer_value=None,
    answer_label: str = "",
    statement: str = "",
    is_attempt: bool = True,
    variables: dict | None = None,
    domain_check_required: bool = False,
) -> str:
    """Build a JSON string in LLM-B's expected output shape."""
    import json as _json
    payload = {
        "variables": variables or {},
        "claims": claims or [],
        "conclusion": {
            "statement": statement,
            "answer_extracted_value": answer_value,
            "answer_extracted_label": answer_label,
            "is_attempt": is_attempt,
        },
        "domain_check_required": domain_check_required,
    }
    return _json.dumps(payload)


_DSL_2X_EQ_16 = (
    '{"variables": {}, "expression": '
    '{"op": "solve", "equation": "2 * x = 16", "var": "x"}}'
)


def test_two_llm_grader_handles_word_form_answer():
    """Plan §5.1 — the "eight" case.

    Student says "I multiplied the variable by two and got 16 which
    means that the hidden variable is eight". LLM-B extracts
    answer_extracted_value=8 (canonical=8). Comparator returns CORRECT.

    The old regex grader picked '16' from the intermediate working and
    returned WRONG (a P1). The Two-LLM grader's word-form detection
    routes the input to LLM-B which disambiguates correctly.
    """
    claims = _claims_payload(
        claims=[{
            "id": "c1",
            "description": "2 times 8 equals 16",
            "expression": {"op": "mul", "args": [2, 8]},
            "asserted_value": 16,
        }],
        answer_value=8,
        statement="the hidden variable is eight",
        is_attempt=True,
        variables={"x": 8},
    )
    grader = _math_grader(
        _DSL_2X_EQ_16,
        student_claims_payload=claims,
    )
    request = GradingRequest(
        open_question=_open_q("2x = 16. Solve for x."),
        student_input=(
            "I multiplied the variable by two and got 16 which "
            "means that the hidden variable is eight"
        ),
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.CORRECT
    assert result.reason_code is None


def test_two_llm_grader_grades_pythagoras_negative_case_correctly():
    """Plan §5.1 — the run-7 P1 case.

    Student: '5²+7²=74, 9²=81, 74≠81 so NOT right-angled.' All claims
    verify and the conclusion label ('no') matches the canonical
    (False for Pythagoras YES/NO formulation). Verdict: CORRECT.
    """
    # Canonical is the boolean: is 5²+7² == 9²? Answer: False (74 != 81).
    dsl = (
        '{"variables": {"a": 5, "b": 7, "c": 9}, '
        '"expression": {"op": "eq", "args": ['
        '{"op": "add", "args": ['
        '{"op": "pow", "args": [{"var": "a"}, 2]}, '
        '{"op": "pow", "args": [{"var": "b"}, 2]}]}, '
        '{"op": "pow", "args": [{"var": "c"}, 2]}]}}'
    )
    claims = _claims_payload(
        claims=[
            {"id": "c1", "description": "5 squared is 25",
             "expression": {"op": "pow", "args": [5, 2]},
             "asserted_value": 25},
            {"id": "c2", "description": "7 squared is 49",
             "expression": {"op": "pow", "args": [7, 2]},
             "asserted_value": 49},
            {"id": "c3", "description": "25 + 49 is 74",
             "expression": {"op": "add", "args": [25, 49]},
             "asserted_value": 74},
            {"id": "c4", "description": "9 squared is 81",
             "expression": {"op": "pow", "args": [9, 2]},
             "asserted_value": 81},
        ],
        answer_label="no",
        statement="not right-angled",
        is_attempt=True,
    )
    grader = _math_grader(
        dsl,
        student_claims_payload=claims,
    )
    request = GradingRequest(
        open_question=_open_q(
            "A triangle has sides 5, 7, 9. Is it right-angled? "
            "Answer yes or no."
        ),
        student_input=(
            "5^2 + 7^2 = 25 + 49 = 74, 9^2 = 81, 74 != 81 so NOT right-angled."
        ),
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.CORRECT


def test_two_llm_grader_distinguishes_arithmetic_step_error_from_conclusion_error():
    """Plan §5.1 — arithmetic-step failure mode.

    Student: '5²+7² = 25+49 = 70, 9²=81, 70≠81 so not right-angled.'
    Conclusion happens to be right (canonical: not right-angled), but
    claim c3 (25+49=70) is arithmetically wrong. Verdict: WRONG,
    reason_code='arithmetic_failed'. Only the Two-LLM path can surface
    this — the regex grader saw the right final conclusion only.
    """
    dsl = (
        '{"variables": {"a": 5, "b": 7, "c": 9}, '
        '"expression": {"op": "eq", "args": ['
        '{"op": "add", "args": ['
        '{"op": "pow", "args": [{"var": "a"}, 2]}, '
        '{"op": "pow", "args": [{"var": "b"}, 2]}]}, '
        '{"op": "pow", "args": [{"var": "c"}, 2]}]}}'
    )
    claims = _claims_payload(
        claims=[
            {"id": "c1", "description": "5 squared is 25",
             "expression": {"op": "pow", "args": [5, 2]},
             "asserted_value": 25},
            {"id": "c2", "description": "7 squared is 49",
             "expression": {"op": "pow", "args": [7, 2]},
             "asserted_value": 49},
            {"id": "c3", "description": "25 + 49 is 70 (student got it wrong)",
             "expression": {"op": "add", "args": [25, 49]},
             "asserted_value": 70},
            {"id": "c4", "description": "9 squared is 81",
             "expression": {"op": "pow", "args": [9, 2]},
             "asserted_value": 81},
        ],
        answer_label="no",
        statement="not right-angled",
        is_attempt=True,
    )
    grader = _math_grader(
        dsl,
        student_claims_payload=claims,
    )
    request = GradingRequest(
        open_question=_open_q(
            "A triangle has sides 5, 7, 9. Is it right-angled?"
        ),
        student_input=(
            "5^2 + 7^2 = 25 + 49 = 70, 9^2 = 81, 70 != 81 so not right-angled."
        ),
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    assert result.reason_code == "arithmetic_failed"


def test_two_llm_grader_distinguishes_conclusion_error_from_arithmetic_error():
    """Plan §5.1 — conclusion-error mode (opposite of the previous test).

    Student: '5²+7²=74, 9²=81. 74≠81. So the triangle IS right-angled.'
    All claims verify. Conclusion contradicts the rule. Verdict: WRONG,
    reason_code='conclusion_inconsistent_with_canonical'.
    """
    dsl = (
        '{"variables": {"a": 5, "b": 7, "c": 9}, '
        '"expression": {"op": "eq", "args": ['
        '{"op": "add", "args": ['
        '{"op": "pow", "args": [{"var": "a"}, 2]}, '
        '{"op": "pow", "args": [{"var": "b"}, 2]}]}, '
        '{"op": "pow", "args": [{"var": "c"}, 2]}]}}'
    )
    claims = _claims_payload(
        claims=[
            {"id": "c1", "description": "5 squared is 25",
             "expression": {"op": "pow", "args": [5, 2]},
             "asserted_value": 25},
            {"id": "c2", "description": "7 squared is 49",
             "expression": {"op": "pow", "args": [7, 2]},
             "asserted_value": 49},
            {"id": "c3", "description": "25 + 49 is 74",
             "expression": {"op": "add", "args": [25, 49]},
             "asserted_value": 74},
            {"id": "c4", "description": "9 squared is 81",
             "expression": {"op": "pow", "args": [9, 2]},
             "asserted_value": 81},
        ],
        answer_label="yes",
        statement="the triangle IS right-angled",
        is_attempt=True,
    )
    grader = _math_grader(
        dsl,
        student_claims_payload=claims,
    )
    request = GradingRequest(
        open_question=_open_q(
            "A triangle has sides 5, 7, 9. Is it right-angled?"
        ),
        student_input=(
            "5^2+7^2=74, 9^2=81. 74 != 81. So the triangle IS right-angled."
        ),
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    assert result.reason_code == "conclusion_inconsistent_with_canonical"


@pytest.mark.parametrize(
    "student_input,canonical_value,answer_value,expected_verdict",
    [
        ("the hidden variable is eight", 8, 8, Verdict.CORRECT),
        ("twenty-five", 25, 25, Verdict.CORRECT),
        ("I think it's about half", 0.5, 0.5, Verdict.CORRECT),
        ("two and a half", 2.5, 2.5, Verdict.CORRECT),
    ],
)
def test_two_llm_grader_word_form_answers(
    student_input, canonical_value, answer_value, expected_verdict,
):
    """Plan §5.1 — word-form numerics drive the redesign.

    LLM-B extracts the answer value from word-form. No regex involved —
    inputs containing word-form tokens are routed to LLM-B regardless
    of any deterministic shortcut.
    """
    # A trivial canonical DSL that just emits the target value as a
    # constant. The MathVerificationTool's variable-bindings check
    # requires every numeric variable to appear in the problem text,
    # so we embed the number into the stem.
    dsl = (
        f'{{"variables": {{"v": {canonical_value}}}, '
        f'"expression": {{"var": "v"}}}}'
    )
    claims = _claims_payload(
        claims=[],
        answer_value=answer_value,
        statement=student_input,
        is_attempt=True,
    )
    grader = _math_grader(
        dsl,
        student_claims_payload=claims,
    )
    request = GradingRequest(
        open_question=_open_q(
            f"What is the answer? It equals {canonical_value}."
        ),
        student_input=student_input,
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"input={student_input!r} expected={expected_verdict} "
        f"got={result.verdict}"
    )


def test_two_llm_grader_recognises_meta_input_as_non_attempt():
    """Plan §5.1 — meta input → UNVERIFIED with reason_code='meta_input'.

    Student: "i dont understand. what is an equation?". LLM-B emits
    is_attempt=False. Grader returns UNVERIFIED with the structured
    reason_code so the move layer can branch (e.g. route to explain).
    """
    claims = _claims_payload(
        claims=[],
        answer_value=None,
        statement="asks for help understanding the concept",
        is_attempt=False,
    )
    grader = _math_grader(
        _DSL_X_PLUS_8_EQ_23,
        student_claims_payload=claims,
    )
    request = GradingRequest(
        open_question=_open_q("Solve x + 8 = 23."),
        student_input="i dont understand. what is an equation",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED
    assert result.reason_code == "meta_input"


@pytest.mark.parametrize(
    "answer_value,expected_verdict",
    [
        ([9, 50], Verdict.CORRECT),       # both slots match
        ([9, 60], Verdict.PARTIAL),       # one slot right
        ([45, 60], Verdict.WRONG),        # neither slot right
    ],
)
def test_two_llm_grader_multi_slot_word_and_numeric(
    answer_value, expected_verdict,
):
    """Plan §5.1 — multi-slot grading through LLM-B.

    Student gives two values in prose (e.g. 'profit is 9 and percentage
    is 50%'). LLM-B emits answer_extracted_value as a list. Comparator
    counts slot matches:
        all matched  → CORRECT
        some matched → PARTIAL
        none matched → WRONG
    """
    claims = _claims_payload(
        claims=[],
        answer_value=answer_value,
        statement=f"two values: {answer_value}",
        is_attempt=True,
    )
    grader = _math_grader(
        _DSL_PROFIT_MULTI_27_18,
        student_claims_payload=claims,
    )
    request = GradingRequest(
        open_question=_open_q(_PROFIT_STEM_27_18),
        student_input=f"two values: {answer_value}",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == expected_verdict, (
        f"answer_value={answer_value} expected={expected_verdict} "
        f"got={result.verdict}"
    )


def test_two_llm_grader_arithmetic_failed_does_not_leak_canonical():
    """The misconception field redacts: it names the failing step's
    description, not the canonical answer. Belt-and-braces test of the
    redaction invariant on the arithmetic_failed branch.
    """
    dsl = (
        '{"variables": {"a": 25}, "expression": {"var": "a"}}'
    )
    claims = _claims_payload(
        claims=[{
            "id": "c1",
            "description": "12 plus 13 is 50",
            "expression": {"op": "add", "args": [12, 13]},
            "asserted_value": 50,  # student claims 12+13=50; actually 25
        }],
        answer_value=50,
        statement="50",
        is_attempt=True,
    )
    grader = _math_grader(
        dsl,
        student_claims_payload=claims,
    )
    request = GradingRequest(
        open_question=_open_q("What is 12+13? (Answer: 25)"),
        # Word-form forces the LLM-B path (bypasses the regex fast-path).
        student_input="twelve plus thirteen equals fifty",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.WRONG
    assert result.reason_code == "arithmetic_failed"
    # Redaction: the canonical (25) is NOT in the safe-feedback fields.
    assert "25" not in result.student_safe_feedback.first_misconception_redacted


def test_two_llm_grader_falls_through_when_llm_b_returns_garbage():
    """LLM-B returns invalid JSON → math path falls through to grounded
    (the same fail-soft pattern as LLM-A). With no grounded client,
    lands at UNVERIFIED — exactly the existing behaviour for callers
    that haven't configured a downstream tier.
    """
    grader = _math_grader(
        _DSL_X_PLUS_8_EQ_23,
        student_claims_payload="not json at all",
    )
    request = GradingRequest(
        open_question=_open_q("Solve x + 8 = 23."),
        student_input="i think x is around fifteen",  # word-form → LLM-B path
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.UNVERIFIED


def test_two_llm_grader_skips_llm_b_on_fast_path_bare_numeric():
    """When the student answer is unambiguously bare ("25"), LLM-B is
    skipped — saves a round-trip per the plan §4 step 4 'whenever
    applicable' caveat. The deterministic regex chain produces the
    value; comparator confirms CORRECT against the canonical."""

    class _SpyClient:
        def __init__(self, payload):
            self._q = [payload]

        def generate(self, **kwargs):
            return _FakeResp(self._q.pop(0))

        @property
        def remaining(self):
            return len(self._q)

    spy = _SpyClient(
        _claims_payload(answer_value=999, is_attempt=True)
    )
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(_DSL_12_PLUS_13),
        grounded_client_factory=lambda: None,
        student_claims_client_factory=lambda: spy,
    )
    request = GradingRequest(
        open_question=_open_q("12 + 13 = ?"),
        student_input="25",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.CORRECT
    assert spy.remaining == 1, (
        "LLM-B should NOT be called when the student input is bare"
    )


def test_two_llm_grader_word_form_input_skips_fast_path():
    """Word-form numerics in the student input force the LLM-B path
    regardless of any bare-numeric look-alike. Verifiable by spying on
    LLM-B and asserting its queue was consumed."""

    class _SpyClient:
        def __init__(self, payload):
            self._q = [payload]

        def generate(self, **kwargs):
            return _FakeResp(self._q.pop(0))

        @property
        def remaining(self):
            return len(self._q)

    spy = _SpyClient(
        _claims_payload(answer_value=8, is_attempt=True)
    )
    grader = StudentGrader(
        math_client_factory=lambda: _FakeClient(_DSL_2X_EQ_16),
        grounded_client_factory=lambda: None,
        student_claims_client_factory=lambda: spy,
    )
    request = GradingRequest(
        open_question=_open_q("2x = 16. Solve for x."),
        student_input="eight",
        is_math=True,
    )
    result = grader.grade_student_response(_context(), request)
    assert result.verdict == Verdict.CORRECT
    assert spy.remaining == 0, (
        "LLM-B SHOULD be called for word-form inputs"
    )
