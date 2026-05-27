"""Tests for ``classify_conformance_failure`` — Fix 3 (pose-question
two-phase commit).

The classifier decides whether a conformance failure is PROSE_ONLY
(re-render prose, hold the first attempt's PendingPose, Phase A does
NOT re-run) or POSE_RELATED / MIXED (full retry, Phase A may pick a
different slot).
"""

from __future__ import annotations

from types import SimpleNamespace

from apps.tutoring.v2.services.conformance import (
    POSE_RELATED_VIOLATIONS,
    PROSE_ONLY_VIOLATIONS,
    classify_conformance_failure,
)


def _violation(rule: str, detail: str = "") -> str:
    """Match the on-the-wire format ConformanceResult.add_violation
    produces (``rule: detail`` when detail is non-empty)."""
    return f"{rule}: {detail}" if detail else rule


# ──────────────────────────────────────────────────────────────────────
# Single-rule cases
# ──────────────────────────────────────────────────────────────────────


def test_classify_state_coherence_is_prose_only():
    assert classify_conformance_failure(
        [_violation("state_coherence", "open_question_with_no_verdict")],
    ) == "prose_only"


def test_classify_answer_leak_is_prose_only():
    assert classify_conformance_failure(
        [_violation("answer_leak", "leaked canonical")],
    ) == "prose_only"


def test_classify_figure_ref_is_prose_only():
    assert classify_conformance_failure(
        [_violation("figure_ref", "deictic phrase without media")],
    ) == "prose_only"


def test_classify_rule_check_is_prose_only():
    assert classify_conformance_failure(
        [_violation("rule_check", "numeric mutation")],
    ) == "prose_only"


def test_classify_praise_filter_is_prose_only():
    assert classify_conformance_failure(
        [_violation("praise_filter", "bare praise")],
    ) == "prose_only"


def test_classify_safety_is_prose_only():
    assert classify_conformance_failure(
        [_violation("safety", "flagged content")],
    ) == "prose_only"


def test_classify_verdict_correct_rules_are_prose_only():
    assert classify_conformance_failure(
        [_violation("correct__no_refutation")],
    ) == "prose_only"
    assert classify_conformance_failure(
        [_violation("correct__hands_floor_back")],
    ) == "prose_only"


def test_classify_verdict_wrong_rules_are_prose_only():
    assert classify_conformance_failure(
        [_violation("wrong__no_affirmation")],
    ) == "prose_only"


def test_classify_verdict_unverified_rules_are_prose_only():
    for rule in (
        "unverified__no_affirm",
        "unverified__no_refute",
        "unverified__surfaces_uncertainty",
        "unverified__hands_floor_back",
    ):
        assert classify_conformance_failure([_violation(rule)]) == "prose_only"


def test_classify_tutor_claim_violations_are_prose_only():
    assert classify_conformance_failure(
        [_violation("tutor_claim_contradicted")],
    ) == "prose_only"
    assert classify_conformance_failure(
        [_violation("tutor_claim_unverified")],
    ) == "prose_only"


def test_classify_stickiness_is_pose_related():
    """Open-question stickiness fires when the pending_pose drifted to
    a new slot. Holding the drifting pose would re-fail the gate on
    retry — must run the full pipeline so Phase A picks the right
    slot."""
    assert classify_conformance_failure(
        [_violation("open_question_stickiness", "drift")],
    ) == "pose_related"


def test_classify_extractor_violations_are_pose_related():
    assert classify_conformance_failure(
        [_violation("one_question_per_turn")],
    ) == "pose_related"
    assert classify_conformance_failure(
        [_violation("active_end_required")],
    ) == "pose_related"


def test_classify_no_assessment_in_prose_is_pose_related_when_no_pose():
    """Sub-case A from §2.3.1: LLM emitted a prose question with no
    tool_use block. Phase A must re-run so the tool path is taken."""
    assert classify_conformance_failure(
        [_violation("all__no_assessment_in_prose")],
        pending_pose=None,
    ) == "pose_related"


def test_classify_no_assessment_in_prose_flips_to_prose_only_with_pose():
    """Sub-case B from §2.3.1: over-eager LLM emitted BOTH a tool_use
    (valid PendingPose) AND a prose question. The prose is the
    duplicate; re-rendering prose with the held pose is correct."""
    held = SimpleNamespace(question_ref=None, rendered_stem="...")
    assert classify_conformance_failure(
        [_violation("all__no_assessment_in_prose")],
        pending_pose=held,
    ) == "prose_only"


# ──────────────────────────────────────────────────────────────────────
# Multi-violation cases
# ──────────────────────────────────────────────────────────────────────


def test_classify_multiple_prose_only_violations():
    assert classify_conformance_failure(
        [
            _violation("answer_leak"),
            _violation("praise_filter"),
            _violation("wrong__hands_floor_back"),
        ],
    ) == "prose_only"


def test_classify_mixed_prose_and_pose_violations():
    """Mixed → full pipeline retry; the held pose is discarded."""
    assert classify_conformance_failure(
        [
            _violation("answer_leak"),
            _violation("open_question_stickiness"),
        ],
    ) == "mixed"


def test_classify_multiple_pose_related_violations():
    assert classify_conformance_failure(
        [
            _violation("one_question_per_turn"),
            _violation("active_end_required"),
        ],
    ) == "pose_related"


# ──────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────


def test_classify_empty_list_defaults_to_pose_related():
    """Conservative default: run the full pipeline when there's
    nothing to classify."""
    assert classify_conformance_failure([]) == "pose_related"


def test_classify_unknown_rule_defaults_to_mixed():
    """New conformance rules must be added to one of the two sets to
    be classified; until then they default to mixed (safe path)."""
    assert classify_conformance_failure(
        [_violation("brand_new_rule")],
    ) == "mixed"


def test_classify_handles_violation_without_detail_separator():
    """Violations without ``:`` separators should still be parsed."""
    assert classify_conformance_failure(
        ["state_coherence"],
    ) == "prose_only"


def test_prose_and_pose_sets_are_disjoint():
    assert PROSE_ONLY_VIOLATIONS.isdisjoint(POSE_RELATED_VIOLATIONS)
