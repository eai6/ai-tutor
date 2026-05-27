"""Verdict-keyed rule matrix — Phase 2 §2.4.

Exactly per refactor-analysis §3. All four verdicts (``correct``,
``wrong``, ``partial``, ``unverified``) plus the no-verdict-with-claim
case have explicit rules. Implementation = small table, not nested
if/else.

This module owns ONLY the rule lookup. The classifier (which produces
the label vector) lives in ``classifier.py``; the orchestrator (which
runs the gates + classifier + matrix + retry loop) lives in
``check.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from apps.tutoring.v2.contracts import GradingResult, Verdict
from apps.tutoring.v2.services.conformance.classifier import ClassifierLabels


@dataclass
class MatrixViolation:
    """A single verdict-matrix rule violation."""

    rule: str
    description: str


# Each rule: (label_predicate, description, must_be_true_or_false)
# We model rules as small lambdas over ClassifierLabels so the matrix
# stays declarative.
_Rule = tuple[str, Callable[[ClassifierLabels], bool], str]


def _ruleset_correct() -> List[_Rule]:
    """Per analysis §3:
       verdict=correct → reject if `refutes_correctness`;
                         reject if NOT `hands_floor_back_or_transitions`.
    """
    return [
        (
            "correct__no_refutation",
            lambda l: not l.refutes_correctness,
            "verdict=correct: response must not refute correctness",
        ),
        (
            "correct__hands_floor_back",
            lambda l: l.hands_floor_back_or_transitions,
            "verdict=correct: response must hand the floor back or transition",
        ),
    ]


def _ruleset_wrong() -> List[_Rule]:
    """verdict=wrong → reject if `affirms_correctness`;
                       reject if NOT `hands_floor_back_or_transitions`.
    """
    return [
        (
            "wrong__no_affirmation",
            lambda l: not l.affirms_correctness,
            "verdict=wrong: response must not affirm correctness",
        ),
        (
            "wrong__hands_floor_back",
            lambda l: l.hands_floor_back_or_transitions,
            "verdict=wrong: response must hand the floor back or transition",
        ),
    ]


def _ruleset_partial() -> List[_Rule]:
    """verdict=partial → reject if `affirms_correctness` (bare affirm wrong);
                         reject if `refutes_correctness` (bare refute wrong);
                         reject if NOT `contains_partial_feedback_shape`;
                         reject if NOT `hands_floor_back_or_transitions`.
    """
    return [
        (
            "partial__no_bare_affirm",
            lambda l: not l.affirms_correctness,
            "verdict=partial: bare affirmation is wrong shape",
        ),
        (
            "partial__no_bare_refute",
            lambda l: not l.refutes_correctness,
            "verdict=partial: bare refutation is wrong shape",
        ),
        (
            "partial__feedback_shape",
            lambda l: l.contains_partial_feedback_shape,
            "verdict=partial: response must contain 'what's right / what's missing' shape",
        ),
        (
            "partial__hands_floor_back",
            lambda l: l.hands_floor_back_or_transitions,
            "verdict=partial: response must hand the floor back or transition",
        ),
    ]


def _ruleset_unverified() -> List[_Rule]:
    """verdict=unverified → reject if `affirms_correctness` or `refutes_correctness`;
                            reject if NOT `surfaces_uncertainty`;
                            reject if NOT `hands_floor_back_or_transitions`.
    """
    return [
        (
            "unverified__no_affirm",
            lambda l: not l.affirms_correctness,
            "verdict=unverified: response must not affirm correctness",
        ),
        (
            "unverified__no_refute",
            lambda l: not l.refutes_correctness,
            "verdict=unverified: response must not refute correctness",
        ),
        (
            "unverified__surfaces_uncertainty",
            lambda l: l.surfaces_uncertainty,
            "verdict=unverified: response must surface uncertainty",
        ),
        (
            "unverified__hands_floor_back",
            lambda l: l.hands_floor_back_or_transitions,
            "verdict=unverified: response must hand the floor back or transition",
        ),
    ]


def _ruleset_no_verdict_with_claim() -> List[_Rule]:
    """No-verdict turn + student_claim_present:
       reject if `affirms_correctness` or `refutes_correctness`
       (the tutor must not adjudicate without a grader verdict);
       reject if NOT `hands_floor_back_or_transitions`.
    """
    return [
        (
            "no_verdict_claim__no_affirm",
            lambda l: not l.affirms_correctness,
            "no-verdict + student claim: tutor must not adjudicate (affirm)",
        ),
        (
            "no_verdict_claim__no_refute",
            lambda l: not l.refutes_correctness,
            "no-verdict + student claim: tutor must not adjudicate (refute)",
        ),
        (
            "no_verdict_claim__hands_floor_back",
            lambda l: l.hands_floor_back_or_transitions,
            "no-verdict + student claim: response must hand the floor back or transition",
        ),
    ]


def _ruleset_all_verdicts() -> List[_Rule]:
    """Rules that apply across every branch."""
    return [
        (
            "all__no_assessment_in_prose",
            lambda l: not l.contains_assessment_question_in_prose,
            "verifiable-answer questions must use the pose_question tool, not prose",
        ),
    ]


def apply_verdict_matrix(
    *,
    labels: ClassifierLabels,
    verdict: Optional[GradingResult],
    posed_via_tool: bool = False,
    selected_move: str = "",
    pose_tool_available: bool = True,
) -> List[MatrixViolation]:
    """Run the verdict-keyed rules and return all violations.

    Returns an empty list when the candidate satisfies every applicable
    rule. The orchestrator (``check.py``) decides whether any violation
    triggers a retry.

    ``posed_via_tool`` — when True, the candidate response already
    carries a verified bank stem committed via the pose_question tool
    (Phase A passed; Phase B will commit on conformance accept). The
    ``all__no_assessment_in_prose`` rule is skipped in that case
    because the candidate's question text DID come through the tool;
    the classifier reads only the visible characters and cannot
    distinguish a tool-rendered stem from a prose-authored one.

    ``pose_tool_available`` — when False, no posable bank slots remain
    for the lesson and the LLM had no tool surface to call. Skip the
    no-prose rule in that case: rejecting the prose practice prompt
    would strand the student with a safe-template apology when the
    curriculum genuinely doesn't have a tool slot to offer. When True,
    the LLM had the tool and chose prose — that IS the failure mode
    (GEO-S5 2026-05-27 T1463 broke the verdict loop by posing the
    cycle-ordering question in prose despite a tool slot being
    available).

    The narrower skip — gated on tool-availability rather than move
    name — keeps the active-end / verdict-loop contract intact whenever
    the LLM could have honoured it.
    (Principle #11 Testing Effect / Retrieval Practice Ch.20 —
    retrieval needs feedback to consolidate; a prose-posed verifiable
    Q lands without a verdict and breaks the loop.)
    """
    rules: List[_Rule] = []
    if not posed_via_tool and pose_tool_available:
        rules.extend(_ruleset_all_verdicts())

    if verdict is None:
        # No-verdict turn: only the student-claim path adds rules.
        if labels.student_claim_present:
            rules.extend(_ruleset_no_verdict_with_claim())
    else:
        kind = verdict.verdict
        if kind == Verdict.CORRECT:
            rules.extend(_ruleset_correct())
        elif kind == Verdict.WRONG:
            rules.extend(_ruleset_wrong())
        elif kind == Verdict.PARTIAL:
            rules.extend(_ruleset_partial())
        elif kind == Verdict.UNVERIFIED:
            rules.extend(_ruleset_unverified())

    violations: List[MatrixViolation] = []
    for rule_name, predicate, description in rules:
        if not predicate(labels):
            violations.append(
                MatrixViolation(rule=rule_name, description=description)
            )
    return violations
