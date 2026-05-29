"""Layer 1 tests for the curriculum-fidelity detector.

Cases drawn from:
  - The Map Scale (L1425) preview regression (2026-05-28 screenshot).
  - Run-11 GEO (L1454) + MATHS (L1148) transcripts where reflective
    openers worked correctly.
  - Synthesized variants exercising the verifiable / reflective
    pattern surface.

The detector is precision-favoring per
``memory/curriculum_fidelity_principle.md``. False positives on
genuinely reflective prompts cost a retry-loop latency; false
negatives on assessable prose Qs corrupt the assessment chain. The
tests below enforce ZERO false negatives on the documented verifiable
cases and ZERO false positives on the documented reflective cases.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.services.conformance_check import (
    _last_question_sentence,
    is_verifiable_prose_question,
)


# ---------------------------------------------------------------------------
# Verifiable cases — detector MUST return True
# ---------------------------------------------------------------------------

VERIFIABLE_CASES: list[tuple[str, str]] = [
    (
        "map_scale_screenshot",
        # The exact opener from the 2026-05-28 preview regression.
        "Today we're learning about Map Scale and Map Types. Think of "
        "it this way: a large-scale map shows a small area in lots of "
        "detail. Which type of map — large-scale or small-scale — "
        "would be more useful if you needed to find a particular "
        "street in your town?",
    ),
    (
        "math_compute_value",
        "Division is just asking how many groups fit. What is 18 ÷ 3?",
    ),
    (
        "math_solve_imperative",
        "We use the inverse operation to undo addition. Solve the "
        "equation x + 8 = 23?",
    ),
    (
        "math_what_is_value",
        "We need to find the unknown. What is the value of x when "
        "4x = 32?",
    ),
    (
        "binary_disjunction",
        "Maps can vary in detail. Is this map large-scale or "
        "small-scale?",
    ),
    (
        "true_false_framing",
        "A large-scale map shows a smaller geographic area in greater "
        "detail than a small-scale map. True or False?",
    ),
    (
        "mcq_options_listed",
        "Pick the correct option. A) sandy / B) clay / C) loam — "
        "which is correct?",
    ),
    (
        "rank_imperative",
        "We can compare infiltration rates across soils. Rank these "
        "soil types from fastest to slowest infiltration?",
    ),
    (
        "closed_set_which_is",
        "Two options have very different scales. Which of these is "
        "the larger-scale map?",
    ),
    (
        "yes_no_verifiable",
        "Let's check the canopy structure. Is the rainforest a "
        "closed-canopy ecosystem?",
    ),
    (
        "numeric_expression",
        "Try this arithmetic. 5 + 3 = ?",
    ),
    (
        "how_many_counting",
        "We need to count the regions. How many provinces are in "
        "Seychelles, roughly 25 or 30?",
    ),
    (
        "name_the_x",
        "We have studied the three layers. Name the topmost layer of "
        "soil?",
    ),
]


@pytest.mark.parametrize(
    "name,response_text",
    VERIFIABLE_CASES,
    ids=[c[0] for c in VERIFIABLE_CASES],
)
def test_verifiable_prose_questions_are_detected(
    name: str, response_text: str
) -> None:
    """Every documented verifiable case is detected (no false negatives)."""
    assert is_verifiable_prose_question(response_text) is True, (
        f"verifiable case {name!r} was not detected — false negative; "
        f"trailing Q was: {_last_question_sentence(response_text)!r}"
    )


# ---------------------------------------------------------------------------
# Reflective cases — detector MUST return False
# ---------------------------------------------------------------------------

REFLECTIVE_CASES: list[tuple[str, str]] = [
    (
        "run11_geo_intuition",
        # Verbatim from the run-11 GEO T1877 opener that worked.
        "Today's lesson is Infiltration and Percolation — two processes "
        "that explain how rainwater travels from the surface down into "
        "the ground. Before we dig in, which of these matches your "
        "intuition?",
    ),
    (
        "what_do_you_think_cause",
        "Soils respond differently to rain. What do you think might "
        "cause that?",
    ),
    (
        "have_you_seen_local",
        "Coastal erosion shapes our shoreline. Have you seen this "
        "happen near you?",
    ),
    (
        "feels_familiar",
        "Several ideas could fit. Which of those ideas feels most "
        "familiar?",
    ),
    (
        "feels_clearest",
        "Two parts move together here. What part of this feels "
        "clearest?",
    ),
    (
        "would_you_check",
        "Order of operations matters. What would you check first?",
    ),
    (
        "have_you_seen_before",
        "Maps come in many shapes. Have you seen a map like that "
        "before?",
    ),
    (
        "bring_to_mind",
        "Different soils behave differently. What does that bring to "
        "mind for you?",
    ),
    (
        "tell_me_what_you_know",
        "We're about to study map scale. Could you tell me what you "
        "already know about scale?",
    ),
    (
        "your_starting_intuition",
        "Two scales feel quite different. What's your starting "
        "intuition here?",
    ),
    (
        "in_your_view",
        "There are several reasonable angles. In your view, which "
        "framing feels right?",
    ),
    (
        "where_have_you_seen",
        "Erosion shows up in many places. Where have you seen "
        "weathering like that?",
    ),
]


@pytest.mark.parametrize(
    "name,response_text",
    REFLECTIVE_CASES,
    ids=[c[0] for c in REFLECTIVE_CASES],
)
def test_reflective_prose_questions_are_not_detected(
    name: str, response_text: str
) -> None:
    """Every documented reflective case passes through (no false positives)."""
    assert is_verifiable_prose_question(response_text) is False, (
        f"reflective case {name!r} was incorrectly detected as verifiable; "
        f"trailing Q was: {_last_question_sentence(response_text)!r}"
    )


# ---------------------------------------------------------------------------
# No-question / edge cases — detector MUST return False
# ---------------------------------------------------------------------------

NO_QUESTION_CASES: list[tuple[str, str]] = [
    (
        "close_topic_transition",
        # Verbatim from run-11 GEO T1885.
        "You nailed the pore-size reasoning — larger pores, lower "
        "capillary suction, higher hydraulic conductivity — and you "
        "correctly dismissed the distractors too. Let's move on to "
        "the next part of the lesson.",
    ),
    (
        "explanation_only_no_q",
        "Today we're learning about Map Scale. Maps can shrink the "
        "real world to fit on paper.",
    ),
    (
        "empty_string",
        "",
    ),
    (
        "whitespace_only",
        "    \n\n   \t  ",
    ),
    (
        "mid_paragraph_question_not_trailing",
        "Why do we use scale? Because the real world doesn't fit on "
        "paper. Today we'll see how that works.",
    ),
    (
        "ends_with_period_after_explanation",
        "Pore size determines how fast water moves through soil. "
        "Large pores allow fast infiltration; small pores slow it "
        "dramatically.",
    ),
]


@pytest.mark.parametrize(
    "name,response_text",
    NO_QUESTION_CASES,
    ids=[c[0] for c in NO_QUESTION_CASES],
)
def test_no_trailing_question_passes_through(
    name: str, response_text: str
) -> None:
    """Responses without a trailing question are never flagged."""
    assert is_verifiable_prose_question(response_text) is False, (
        f"no-question case {name!r} was incorrectly flagged"
    )


# ---------------------------------------------------------------------------
# Trailing-sentence extraction (helper) — sanity tests
# ---------------------------------------------------------------------------


def test_last_question_sentence_pulls_trailing_sentence() -> None:
    text = (
        "Today we're learning about Map Scale. "
        "Think of it this way: a large-scale map shows lots of detail. "
        "Which type of map would be more useful?"
    )
    trailing = _last_question_sentence(text)
    assert trailing == "Which type of map would be more useful?"


def test_last_question_sentence_handles_paragraph_breaks() -> None:
    text = (
        "Paragraph one ends here.\n\n"
        "Paragraph two opens with content.\n\n"
        "Which option fits best?"
    )
    trailing = _last_question_sentence(text)
    assert trailing == "Which option fits best?"


def test_last_question_sentence_empty_when_no_trailing_question() -> None:
    text = "We worked through that one. Let's move on."
    assert _last_question_sentence(text) == ""


def test_last_question_sentence_empty_for_empty_input() -> None:
    assert _last_question_sentence("") == ""
    assert _last_question_sentence(None) == ""  # type: ignore[arg-type]
