"""Content-regression tests for the CONFIRM_AND_ADVANCE prompt.

The move's no-verdict branch must distinguish two sub-cases — (a)
forward signal → pure transition, (b) substantive engagement on a
reflective prompt → an acknowledgment that references what the student
shared (NOT a silent bank stem). The silent-bare-stem prohibition is a
real production-incident fix (session 122 T2, 2026-05-28; recurred as
GEO-S5 run-12 T2). These tests pin that behavioral guarantee.

Updated for the prompt-audit consolidation
(open_question_authority_redesign.md §7): universal acknowledgment
phrasing now lives once in the SHARED_PREAMBLE; this move body carries
only the move-specific routing + a ≤3-item checklist, with repeated
inline principle attributions removed and CAPS shouting dropped.
"""

from __future__ import annotations

from apps.tutoring.v2.services.move_prompts import CONFIRM_AND_ADVANCE


def _normalize(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# CORRECT-verdict branch
# ---------------------------------------------------------------------------


def test_correct_verdict_branch_preserved() -> None:
    body = CONFIRM_AND_ADVANCE.body
    assert "CORRECT verdict" in body
    # Bare-answer "because…" rule.
    assert "bare numeric / letter / T-F" in body
    # Stand-alone praise avoidance.
    assert "stand-alone praise" in body
    # MCQ tautology guard.
    assert "tautology" in body


# ---------------------------------------------------------------------------
# No-verdict sub-cases (a) forward signal, (b) substantive engagement
# ---------------------------------------------------------------------------


def test_forward_signal_subcase_present() -> None:
    body = CONFIRM_AND_ADVANCE.body
    assert "pure forward signal" in body
    for cue in ['"ready"', '"next"', '"ok"']:
        assert cue in body
    # No fabricated praise on a contentless turn.
    assert "fabricate praise" in body


def test_substantive_engagement_subcase_present() -> None:
    body = CONFIRM_AND_ADVANCE.body
    norm = _normalize(body)
    assert "substantive engagement" in body
    # Broad trigger.
    assert "ANY content beyond a forward signal" in norm
    # Acknowledge engagement, not correctness.
    assert "acknowledge engagement, not correctness" in norm
    # Must reference what the student offered.
    assert "references what they offered" in norm


def test_silent_bare_stem_is_forbidden_as_dominant_failure() -> None:
    """The core behavioral guarantee (session 122 T2 / run-12 T2): do not
    ship only the bank stem with no lead-in after the student gave content.
    """
    body = _normalize(CONFIRM_AND_ADVANCE.body)
    assert "dominant failure of this branch" in body
    assert "ship only the bank stem" in body


# ---------------------------------------------------------------------------
# Consolidation invariants (audit §7)
# ---------------------------------------------------------------------------


def test_checklist_is_focused_max_three_items() -> None:
    body = CONFIRM_AND_ADVANCE.body
    start = body.find("RESPONSE QUALITY CHECKLIST")
    assert start > -1
    checklist = body[start:]
    assert checklist.count("□") <= 3
    # The three items are move-specific (not re-listing universals).
    assert "stand-alone praise word" in checklist
    assert "did not ship only the bank stem" in _normalize(checklist)
    assert "tautology" in checklist


def test_no_repeated_inline_principle_attributions() -> None:
    """One motivation per rule lives in the PRINCIPLE header; the repeated
    inline '(Science of learning principle: …)' parentheticals are gone."""
    body = CONFIRM_AND_ADVANCE.body
    assert "Science of learning principle:" not in body


def test_no_caps_shouting() -> None:
    body = CONFIRM_AND_ADVANCE.body
    assert "DOMINANT FAILURE MODE" not in body
    assert "MUST emit" not in body


def test_principle_provenance_preserved() -> None:
    body = CONFIRM_AND_ADVANCE.body
    assert "Active Learning (Ch.10)" in body
    assert "Cognitive Load (Ch.14)" in body
    assert CONFIRM_AND_ADVANCE.principles == (1, 5)
