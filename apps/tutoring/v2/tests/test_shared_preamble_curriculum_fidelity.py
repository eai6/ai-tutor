"""Content-regression tests for the SHARED_PREAMBLE curriculum-fidelity section.

Per ``memory/curriculum_fidelity_principle.md`` Phase 3: lift the
curriculum-fidelity rule into the shared preamble so it applies to
all 7 non-terminal moves and appears EARLY in every prompt — before
Voice, before structural rules, before any move-specific guidance.

These tests verify:
  - The new section exists with the expected anchor text.
  - It appears in the rendered preamble for every non-terminal move.
  - It's positioned EARLY (before "Voice (every turn):").
  - It names the verifiable shapes that must go through the tool.
  - It names the reflective shapes that remain allowed in prose.
  - It mentions the structural curriculum_fidelity gate enforcement.

Actual behavior is LLM-driven and validated via Phase 4 live
verification, not unit tests.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.services.move_prompts import (
    SHARED_PREAMBLE_TEMPLATE,
    render_shared_preamble,
)


# ---------------------------------------------------------------------------
# Section presence
# ---------------------------------------------------------------------------


def test_curriculum_fidelity_section_present_in_template() -> None:
    """The template contains a top-level Curriculum-fidelity contract section."""
    assert "Curriculum-fidelity contract" in SHARED_PREAMBLE_TEMPLATE
    assert "non-negotiable" in SHARED_PREAMBLE_TEMPLATE
    assert "structurally enforced" in SHARED_PREAMBLE_TEMPLATE


def test_curriculum_fidelity_section_precedes_voice_block() -> None:
    """Section appears BEFORE 'Voice (every turn):' so the LLM sees it first."""
    cf_idx = SHARED_PREAMBLE_TEMPLATE.find("Curriculum-fidelity contract")
    voice_idx = SHARED_PREAMBLE_TEMPLATE.find("Voice (every turn):")
    assert cf_idx > -1
    assert voice_idx > -1
    assert cf_idx < voice_idx, (
        "Curriculum-fidelity contract section must appear BEFORE the Voice "
        "block so it's read first by the LLM."
    )


def test_curriculum_fidelity_section_precedes_operational_rules() -> None:
    """Section sits ABOVE all the per-facet operational rules.

    The operational rules (One question per turn, Structural rules,
    Tool-vs-prose dedup, Mid-move pose dedup) are sentence-level
    facets of the contract — the contract must appear first so the
    LLM internalizes the principle before reading the mechanics.
    """
    cf_idx = SHARED_PREAMBLE_TEMPLATE.find("Curriculum-fidelity contract")
    for op_rule in (
        "One question per turn — always",
        "Structural rules (every turn):",
        "Tool-vs-prose dedup",
        "Mid-move pose dedup",
    ):
        op_idx = SHARED_PREAMBLE_TEMPLATE.find(op_rule)
        assert op_idx > -1, f"missing operational rule: {op_rule!r}"
        assert cf_idx < op_idx, (
            f"Curriculum-fidelity contract must precede {op_rule!r}"
        )


# ---------------------------------------------------------------------------
# Content — verifiable shapes are enumerated
# ---------------------------------------------------------------------------


def test_forbidden_verifiable_shapes_named() -> None:
    """Each verifiable shape category is explicitly named as forbidden in prose."""
    body = SHARED_PREAMBLE_TEMPLATE
    for label in (
        "compute-value",
        "closed-set picks",
        "yes/no facts",
        "ordered sequences",
        "named terms",
        "MCQ shapes",
    ):
        assert label in body, f"forbidden category {label!r} not enumerated"


def test_forbidden_verifiable_examples_present() -> None:
    """Concrete examples of forbidden shapes are inline so the LLM has targets."""
    body = SHARED_PREAMBLE_TEMPLATE
    # compute-value
    assert "what is X + Y?" in body or "what is the value of" in body
    # closed-set picks
    assert "which is bigger" in body or "which type — A or B" in body
    # yes/no
    assert "true or false: X" in body or "is X true?" in body
    # ordered sequence
    assert "rank these from largest" in body or "put X in order" in body
    # named terms
    assert "name the X" in body
    # MCQ shapes (the Map Scale screenshot's pattern)
    assert "A) " in body and "B) " in body


# ---------------------------------------------------------------------------
# Content — reflective shapes remain allowed
# ---------------------------------------------------------------------------


def test_reflective_shapes_named_as_still_allowed() -> None:
    """The escape valve — reflective prose Qs — is explicit so the LLM doesn't overcorrect."""
    body = SHARED_PREAMBLE_TEMPLATE
    assert "REMAIN ALLOWED" in body
    assert "no single canonical answer" in body
    # Verbatim shapes from the run-11 GEO opener that worked.
    assert "what do you already know about X?" in body
    assert "which of these matches your intuition?" in body


# ---------------------------------------------------------------------------
# Content — gate awareness
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse whitespace so substring assertions are robust to line wraps."""
    return " ".join(text.split())


def test_gate_enforcement_mentioned() -> None:
    """The LLM is told a structural gate exists and what it does."""
    body = _normalize(SHARED_PREAMBLE_TEMPLATE)
    assert "curriculum_fidelity gate" in body
    assert "retry" in body
    assert "stripped from your response" in body
    # The LLM is told gate intervention is a quality regression.
    assert "quality regression" in body


# ---------------------------------------------------------------------------
# Section integrates with the rendered preamble (template still functional)
# ---------------------------------------------------------------------------


def test_render_shared_preamble_includes_new_section() -> None:
    """End-to-end render carries the curriculum-fidelity section through."""
    rendered = render_shared_preamble(
        locale="en",
        institution_name="Test School",
        grade_level="S3",
        tutor_persona="encouraging",
        client_kind="web",
        lesson_title="Map Scale and Map Types",
        lesson_subject="geography",
        current_objective="distinguish large-scale from small-scale maps",
    )
    assert "Curriculum-fidelity contract" in rendered
    # Lesson context is still threaded in correctly.
    assert "Map Scale and Map Types" in rendered
    assert "Test School" in rendered
    # The section precedes Voice in the rendered form too.
    cf_idx = rendered.find("Curriculum-fidelity contract")
    voice_idx = rendered.find("Voice (every turn):")
    assert cf_idx < voice_idx


def test_render_shared_preamble_does_not_break_on_minimal_inputs() -> None:
    """Render with all-default inputs still produces the curriculum-fidelity section."""
    rendered = render_shared_preamble(
        locale="",
        institution_name="",
        grade_level="",
        tutor_persona="",
        client_kind="web",
    )
    assert "Curriculum-fidelity contract" in rendered
    assert "what do you already know about X?" in rendered


def test_mobile_directive_still_renders_after_curriculum_fidelity() -> None:
    """Adding the new section doesn't displace the trailing {mobile_directive} slot."""
    rendered = render_shared_preamble(
        locale="en",
        institution_name="Test",
        grade_level="S3",
        tutor_persona="encouraging",
        client_kind="mobile",
    )
    assert "Curriculum-fidelity contract" in rendered
    assert "The student is on mobile" in rendered


# ---------------------------------------------------------------------------
# Position sanity — the section is "early" in the absolute sense
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_kind", ["web", "mobile"])
def test_curriculum_fidelity_in_first_half_of_preamble(client_kind: str) -> None:
    """The section sits in the first half of the rendered preamble."""
    rendered = render_shared_preamble(
        locale="en",
        institution_name="Test",
        grade_level="S3",
        tutor_persona="encouraging",
        client_kind=client_kind,
        lesson_title="Map Scale",
        lesson_subject="geography",
        current_objective="distinguish scales",
    )
    cf_idx = rendered.find("Curriculum-fidelity contract")
    assert cf_idx > -1
    assert cf_idx < len(rendered) / 2, (
        f"Curriculum-fidelity contract should appear in the first half of "
        f"the preamble ({cf_idx} of {len(rendered)} chars)"
    )
