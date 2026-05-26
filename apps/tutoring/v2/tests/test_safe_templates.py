"""Safe terminal template tests — Phase 2 §Tests.

Per the plan: deliberate two-strike failures route to the verdict-
keyed template; canonical NEVER appears in the template output.
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import (
    GradingResult,
    StudentSafeFeedback,
    Verdict,
)
from apps.tutoring.v2.services.templates import render_safe_template


_CANONICAL = "47.42_PRIVATE_CANONICAL_TOKEN"
_SAFE_RIGHT = "you applied the rule"
_SAFE_MISSING = "the squared term is missing"
_SAFE_MISCONCEPTION = "you swapped the operands"


def _verdict(kind, **kw):
    return GradingResult(
        verdict=kind,
        private_canonical=_CANONICAL,
        student_safe_feedback=StudentSafeFeedback(**kw),
    )


def test_correct_template_uses_what_right():
    text = render_safe_template(
        verdict=_verdict(Verdict.CORRECT, what_right=_SAFE_RIGHT),
        next_action_text="Try the next one.",
    )
    assert _SAFE_RIGHT in text
    assert "Try the next one" in text
    assert _CANONICAL not in text


def test_partial_template_has_both_what_right_and_missing():
    text = render_safe_template(
        verdict=_verdict(
            Verdict.PARTIAL,
            what_right=_SAFE_RIGHT,
            what_missing=_SAFE_MISSING,
        ),
        next_action_text="Let's look again.",
    )
    assert _SAFE_RIGHT in text
    assert _SAFE_MISSING in text
    assert "Let's look again" in text
    assert _CANONICAL not in text


def test_wrong_template_uses_misconception_redacted():
    text = render_safe_template(
        verdict=_verdict(
            Verdict.WRONG,
            first_misconception_redacted=_SAFE_MISCONCEPTION,
        ),
        next_action_text="Try the working step by step.",
    )
    assert _SAFE_MISCONCEPTION in text
    assert _CANONICAL not in text


def test_unverified_template_surfaces_uncertainty():
    text = render_safe_template(
        verdict=_verdict(Verdict.UNVERIFIED),
        next_action_text="Let's verify together.",
    )
    lower = text.lower()
    assert "check" in lower or "not sure" in lower or "sure" in lower
    assert "Let's verify together" in text
    assert _CANONICAL not in text


def test_no_verdict_with_student_claim_template():
    text = render_safe_template(
        verdict=None,
        student_claim_present=True,
        next_action_text="Probe what you mean.",
    )
    assert "check that together" in text.lower() or "rather than guess" in text.lower()
    assert "Probe what you mean" in text


def test_no_verdict_no_claim_neutral_template():
    text = render_safe_template(
        verdict=None,
        student_claim_present=False,
        next_action_text="Let's pick a question.",
    )
    assert "pick this back up" in text.lower() or "Let's pick a question" in text


# ──────────────────────────────────────────────────────────────────────
# Invariant: canonical NEVER appears under wrong / partial / unverified
# ──────────────────────────────────────────────────────────────────────


def test_canonical_never_leaks_under_wrong():
    text = render_safe_template(
        verdict=_verdict(Verdict.WRONG, first_misconception_redacted=_CANONICAL[:5]),
        next_action_text="",
    )
    # The misconception field shouldn't echo the canonical token —
    # the test sets a redacted field deliberately distinct from the
    # canonical literal.
    assert _CANONICAL not in text


def test_canonical_never_leaks_under_partial():
    text = render_safe_template(
        verdict=_verdict(
            Verdict.PARTIAL, what_right=_SAFE_RIGHT, what_missing=_SAFE_MISSING,
        ),
        next_action_text="",
    )
    assert _CANONICAL not in text


def test_canonical_never_leaks_under_unverified():
    text = render_safe_template(
        verdict=_verdict(Verdict.UNVERIFIED),
        next_action_text="",
    )
    assert _CANONICAL not in text
