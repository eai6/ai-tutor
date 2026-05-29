"""Content-regression tests for the CONFIRM_AND_ADVANCE prompt rewrite.

Per ``memory/curriculum_fidelity_principle.md`` Phase 2: the
``confirm_and_advance`` move's NO-verdict branch must distinguish two
sub-cases — (a) forward signal → pure transition, (b) substantive
engagement on a reflective prompt → warm acknowledgment that
references what the student shared.

These tests verify the prompt body carries the expected guidance.
Actual behavior is LLM-driven and validated via Phase 4 live
verification, not unit tests.
"""

from __future__ import annotations

from apps.tutoring.v2.services.move_prompts import CONFIRM_AND_ADVANCE


# ---------------------------------------------------------------------------
# CORRECT-verdict branch — preserved from prior version
# ---------------------------------------------------------------------------


def test_correct_verdict_branch_preserved() -> None:
    """The CORRECT-verdict guidance survives the rewrite."""
    body = CONFIRM_AND_ADVANCE.body
    # Original intent line is intact.
    assert "CORRECT verdict in hand" in body
    # Bare-answer "because…" rule is intact.
    assert "bare numeric / letter / T-F" in body
    # Stand-alone praise avoidance is intact.
    assert "stand-alone praise" in body
    # MCQ tautology guard is intact.
    assert "tautology" in body


# ---------------------------------------------------------------------------
# Sub-case (a) — forward signal → pure transition
# ---------------------------------------------------------------------------


def test_forward_signal_subcase_present() -> None:
    """Sub-case (a) — forward signal handling is explicit."""
    body = CONFIRM_AND_ADVANCE.body
    # Branch header names the forward-signal trigger.
    assert "NO verdict + forward signal" in body
    # Lists the typical forward-signal phrases.
    for cue in ['"ready"', '"next"', '"ok"']:
        assert cue in body
    # Praise-filler is explicitly forbidden.
    assert "praise filler" in body


# ---------------------------------------------------------------------------
# Sub-case (b) — substantive engagement on reflective prompt
# ---------------------------------------------------------------------------


def test_substantive_engagement_subcase_present() -> None:
    """Sub-case (b) — warm acknowledgment of engagement is taught."""
    body = CONFIRM_AND_ADVANCE.body
    # Branch header names the substantive-engagement trigger.
    assert "NO verdict + substantive engagement" in body
    # Names the upstream condition that produces this sub-case.
    assert "reflective" in body.lower()
    # Acceptable shape examples are inline.
    assert "starting intuition" in body
    # Counter-shape examples are inline.
    assert "Great answer!" in body
    # Acknowledgment must NOT claim correctness.
    assert "NOT claim their response was" in body
    # Acknowledgment must REFERENCE the content.
    assert "REFERENCES what they shared" in body


def test_substantive_engagement_bounded_to_one_sentence() -> None:
    """The acknowledgment is bounded — 3-12 words, ONE sentence."""
    body = CONFIRM_AND_ADVANCE.body
    assert "3-12 words" in body
    assert "ONE content-bearing acknowledgment sentence" in body


def test_substantive_engagement_forbids_mechanism_rederivation() -> None:
    """Acknowledgment must not re-derive the mechanism the student named."""
    body = CONFIRM_AND_ADVANCE.body
    assert "NOT re-derive" in body
    assert "expertise-reversal" in body.lower()


# ---------------------------------------------------------------------------
# What-NOT-to-do — silent transition rejected
# ---------------------------------------------------------------------------


def test_silent_transition_after_engagement_rejected() -> None:
    """Skipping the acknowledgment on a substantive answer is forbidden."""
    body = CONFIRM_AND_ADVANCE.body
    # New What-NOT-to-do item targets silent transition specifically.
    assert "ungradeable noise" in body or "Silent transition" in body


# ---------------------------------------------------------------------------
# Checklist — sub-case-aware items
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse whitespace so substring assertions are robust to line wraps."""
    return " ".join(text.split())


def test_checklist_has_subcase_specific_items() -> None:
    """The checklist names both no-verdict sub-cases distinctly."""
    body = CONFIRM_AND_ADVANCE.body
    checklist_start = body.find("RESPONSE QUALITY CHECKLIST")
    assert checklist_start > -1
    checklist = _normalize(body[checklist_start:])
    # Forward-signal sub-case item — PURE forward signal language.
    assert "PURE forward signal" in checklist
    # Substantive-engagement sub-case item — broad trigger language.
    assert "ANY content beyond a pure forward signal" in checklist
    # The acknowledgment-bounded shape is named in the checklist.
    assert "3-12 words" in checklist
    # The dominant failure mode is called out explicitly.
    assert "silently emit only the tool stem" in checklist


def test_substantive_engagement_marks_silent_stem_as_dominant_failure() -> None:
    """The body explicitly names 'emit only the tool stem' as the worst failure.

    Production observation 2026-05-28 (session 122 T2): Sonnet emitted
    only the bank stem with no prose lead-in after the student offered
    a substantive 2-word answer to a reflective opener. The tightened
    prompt frames this as the dominant failure mode so the LLM
    recognizes it.
    """
    body = CONFIRM_AND_ADVANCE.body
    assert "DOMINANT FAILURE MODE" in body
    assert "silently emit only the tool stem" in body
    # The body distinguishes "pure forward signal" from "any content".
    assert "PURE forward signal" in body
    assert "ANY content beyond a pure forward signal" in body


def test_substantive_engagement_requires_acknowledgment_non_optional() -> None:
    """The body uses 'MUST emit' / 'non-optional' for the acknowledgment."""
    body = CONFIRM_AND_ADVANCE.body
    assert "MUST emit" in body
    assert "non-optional" in body


# ---------------------------------------------------------------------------
# Tool pose remains load-bearing
# ---------------------------------------------------------------------------


def test_tool_pose_still_load_bearing() -> None:
    """In every no-verdict path, pose_question is the load-bearing action."""
    body = _normalize(CONFIRM_AND_ADVANCE.body)
    # Both no-verdict sub-cases end with a pose via the tool.
    assert "pose the next bank slot via ``pose_question``" in body.lower()
    # The forward-signal path explicitly names the tool as load-bearing.
    assert "tool call is the load-bearing part of the turn" in body
    # The substantive-engagement path frames bank pose as the assessment.
    assert "bank pose is the assessment" in body
    # Checklist enforces tool-posed OR explicit close (no filler).
    assert "tool-posed next question OR an explicit topic close" in body


# ---------------------------------------------------------------------------
# Principle attributions preserved
# ---------------------------------------------------------------------------


def test_principle_attributions_preserved() -> None:
    """Active Learning Ch.10 and Cognitive Load Ch.14 still cited."""
    body = CONFIRM_AND_ADVANCE.body
    assert "Active Learning (Ch.10)" in body
    assert "Cognitive Load (Ch.14)" in body
    # MovePrompt.principles tuple unchanged (1, 5 = Active Learning, Cognitive Load).
    assert CONFIRM_AND_ADVANCE.principles == (1, 5)
