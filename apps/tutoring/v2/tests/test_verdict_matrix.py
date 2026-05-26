"""Verdict-keyed conformance rule matrix tests — Phase 2 §Tests.

One fixture per (verdict × violated-rule) combination, exhaustive on
the rule matrix.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.contracts import (
    GradingResult,
    StudentSafeFeedback,
    Verdict,
)
from apps.tutoring.v2.services.conformance import (
    ClassifierLabels,
    MatrixViolation,
    apply_verdict_matrix,
)


def _verdict(kind):
    return GradingResult(verdict=kind)


def _labels(**kw):
    return ClassifierLabels(**kw)


# ──────────────────────────────────────────────────────────────────────
# verdict=correct
# ──────────────────────────────────────────────────────────────────────


def test_correct_passes_when_affirms_and_handback():
    rules = apply_verdict_matrix(
        labels=_labels(affirms_correctness=True, hands_floor_back_or_transitions=True),
        verdict=_verdict(Verdict.CORRECT),
    )
    assert rules == []


def test_correct_rejects_refutation():
    rules = apply_verdict_matrix(
        labels=_labels(refutes_correctness=True, hands_floor_back_or_transitions=True),
        verdict=_verdict(Verdict.CORRECT),
    )
    assert any(v.rule == "correct__no_refutation" for v in rules)


def test_correct_rejects_no_handback():
    rules = apply_verdict_matrix(
        labels=_labels(affirms_correctness=True),
        verdict=_verdict(Verdict.CORRECT),
    )
    assert any(v.rule == "correct__hands_floor_back" for v in rules)


# ──────────────────────────────────────────────────────────────────────
# verdict=wrong
# ──────────────────────────────────────────────────────────────────────


def test_wrong_passes_when_no_affirm_and_handback():
    rules = apply_verdict_matrix(
        labels=_labels(hands_floor_back_or_transitions=True),
        verdict=_verdict(Verdict.WRONG),
    )
    assert rules == []


def test_wrong_rejects_affirmation():
    rules = apply_verdict_matrix(
        labels=_labels(affirms_correctness=True, hands_floor_back_or_transitions=True),
        verdict=_verdict(Verdict.WRONG),
    )
    assert any(v.rule == "wrong__no_affirmation" for v in rules)


def test_wrong_rejects_no_handback():
    rules = apply_verdict_matrix(
        labels=_labels(refutes_correctness=False),
        verdict=_verdict(Verdict.WRONG),
    )
    assert any(v.rule == "wrong__hands_floor_back" for v in rules)


# ──────────────────────────────────────────────────────────────────────
# verdict=partial
# ──────────────────────────────────────────────────────────────────────


def test_partial_passes_when_shape_and_handback():
    rules = apply_verdict_matrix(
        labels=_labels(
            contains_partial_feedback_shape=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=_verdict(Verdict.PARTIAL),
    )
    assert rules == []


def test_partial_rejects_bare_affirm():
    rules = apply_verdict_matrix(
        labels=_labels(
            affirms_correctness=True,
            contains_partial_feedback_shape=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=_verdict(Verdict.PARTIAL),
    )
    assert any(v.rule == "partial__no_bare_affirm" for v in rules)


def test_partial_rejects_bare_refute():
    rules = apply_verdict_matrix(
        labels=_labels(
            refutes_correctness=True,
            contains_partial_feedback_shape=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=_verdict(Verdict.PARTIAL),
    )
    assert any(v.rule == "partial__no_bare_refute" for v in rules)


def test_partial_rejects_missing_feedback_shape():
    rules = apply_verdict_matrix(
        labels=_labels(hands_floor_back_or_transitions=True),
        verdict=_verdict(Verdict.PARTIAL),
    )
    assert any(v.rule == "partial__feedback_shape" for v in rules)


def test_partial_rejects_no_handback():
    rules = apply_verdict_matrix(
        labels=_labels(contains_partial_feedback_shape=True),
        verdict=_verdict(Verdict.PARTIAL),
    )
    assert any(v.rule == "partial__hands_floor_back" for v in rules)


# ──────────────────────────────────────────────────────────────────────
# verdict=unverified
# ──────────────────────────────────────────────────────────────────────


def test_unverified_passes_when_uncertain_and_handback():
    rules = apply_verdict_matrix(
        labels=_labels(
            surfaces_uncertainty=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=_verdict(Verdict.UNVERIFIED),
    )
    assert rules == []


def test_unverified_rejects_affirmation():
    rules = apply_verdict_matrix(
        labels=_labels(
            affirms_correctness=True,
            surfaces_uncertainty=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=_verdict(Verdict.UNVERIFIED),
    )
    assert any(v.rule == "unverified__no_affirm" for v in rules)


def test_unverified_rejects_refutation():
    rules = apply_verdict_matrix(
        labels=_labels(
            refutes_correctness=True,
            surfaces_uncertainty=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=_verdict(Verdict.UNVERIFIED),
    )
    assert any(v.rule == "unverified__no_refute" for v in rules)


def test_unverified_rejects_no_uncertainty():
    rules = apply_verdict_matrix(
        labels=_labels(hands_floor_back_or_transitions=True),
        verdict=_verdict(Verdict.UNVERIFIED),
    )
    assert any(v.rule == "unverified__surfaces_uncertainty" for v in rules)


def test_unverified_rejects_no_handback():
    rules = apply_verdict_matrix(
        labels=_labels(surfaces_uncertainty=True),
        verdict=_verdict(Verdict.UNVERIFIED),
    )
    assert any(v.rule == "unverified__hands_floor_back" for v in rules)


# ──────────────────────────────────────────────────────────────────────
# No-verdict turn + student_claim_present
# ──────────────────────────────────────────────────────────────────────


def test_no_verdict_claim_passes_when_no_adjudication_and_handback():
    rules = apply_verdict_matrix(
        labels=_labels(
            student_claim_present=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=None,
    )
    assert rules == []


def test_no_verdict_claim_rejects_affirmation():
    rules = apply_verdict_matrix(
        labels=_labels(
            student_claim_present=True,
            affirms_correctness=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=None,
    )
    assert any(v.rule == "no_verdict_claim__no_affirm" for v in rules)


def test_no_verdict_claim_rejects_refutation():
    rules = apply_verdict_matrix(
        labels=_labels(
            student_claim_present=True,
            refutes_correctness=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=None,
    )
    assert any(v.rule == "no_verdict_claim__no_refute" for v in rules)


def test_no_verdict_claim_rejects_no_handback():
    rules = apply_verdict_matrix(
        labels=_labels(student_claim_present=True),
        verdict=None,
    )
    assert any(v.rule == "no_verdict_claim__hands_floor_back" for v in rules)


def test_no_verdict_no_claim_no_rules():
    """No verdict + no student claim → no rules from the matrix
    (only the universal all-verdicts assessment-in-prose check)."""
    rules = apply_verdict_matrix(labels=_labels(), verdict=None)
    assert rules == []


# ──────────────────────────────────────────────────────────────────────
# Universal across all verdicts: contains_assessment_question_in_prose
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "verdict_kind",
    [Verdict.CORRECT, Verdict.WRONG, Verdict.PARTIAL, Verdict.UNVERIFIED],
)
def test_assessment_in_prose_rejected_under_every_verdict(verdict_kind):
    rules = apply_verdict_matrix(
        labels=_labels(
            contains_assessment_question_in_prose=True,
            surfaces_uncertainty=True,
            hands_floor_back_or_transitions=True,
            contains_partial_feedback_shape=True,
        ),
        verdict=_verdict(verdict_kind),
    )
    assert any(v.rule == "all__no_assessment_in_prose" for v in rules)


def test_assessment_in_prose_rejected_on_no_verdict_with_claim():
    rules = apply_verdict_matrix(
        labels=_labels(
            student_claim_present=True,
            contains_assessment_question_in_prose=True,
            hands_floor_back_or_transitions=True,
        ),
        verdict=None,
    )
    assert any(v.rule == "all__no_assessment_in_prose" for v in rules)
