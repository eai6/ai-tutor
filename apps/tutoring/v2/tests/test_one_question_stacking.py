"""one_question_per_turn gate — deterministic floor + Haiku ceiling.

open_question_authority_redesign.md §7 step 5 (belt-and-suspenders): a
deterministic floor catches the stacked '?'/MCQ shapes the dormant Haiku
extractor provably missed (session-100 T1560 / image-#2 Mahé turn); the
Haiku extractor generalises to action prompts the regex can't see
(imperatives, fill-ins) and enforces the active-end rule. Extractor is
mocked here so the unit tests make no LLM calls.
"""

from __future__ import annotations

from unittest.mock import patch

from apps.tutoring.v2.services.conformance_check import contains_mcq_option_block
from apps.tutoring.v2.services.question_extractor import ExtractorResult
from apps.tutoring.v2.services.safety_gates import run_one_question_check


def _extractor(action_count=1, has_active_end=True, primary="", stacked=None):
    return ExtractorResult(
        action_count=action_count,
        has_active_end=has_active_end,
        primary_action=primary,
        stacked_examples=stacked or [],
    )


def _run(text, *, move="explain", posed_via_tool=False, pose_tool_stem="",
         extractor=None):
    """Run the gate with the extractor mocked (None => fail-soft)."""
    with patch(
        "apps.tutoring.v2.services.question_extractor.extract_action_prompts",
        return_value=extractor,
    ):
        return run_one_question_check(
            text,
            selected_move=move,
            posed_via_tool=posed_via_tool,
            pose_tool_stem=pose_tool_stem,
        )


# ---------------------------------------------------------------------------
# MCQ option-block detector
# ---------------------------------------------------------------------------


def test_mcq_block_detected_with_distinct_letters() -> None:
    assert contains_mcq_option_block("A) North\nB) South\nC) East\nD) West")
    assert contains_mcq_option_block("a. one\nb. two")


def test_mcq_block_not_tripped_by_single_or_prose_lines() -> None:
    assert not contains_mcq_option_block("A) only one option")
    assert not contains_mcq_option_block("A. sentence that starts with A.")
    assert not contains_mcq_option_block("just some prose with no options")
    assert not contains_mcq_option_block("")


# ---------------------------------------------------------------------------
# Deterministic floor (fails before the extractor is consulted)
# ---------------------------------------------------------------------------


_IMG2 = """Let me walk you through it.
Step 3 — If NE is halfway between them, what bearing do you get when you split the difference?
You are standing at the port of Mahe. Which direction is the second ferry heading, and is it more easterly, westerly, northerly, or southerly than the first?
A) South-East; more southerly
B) South-West; more southerly
C) South-East; more westerly
D) South; more easterly"""


def test_floor_flags_image2_stacked_no_tool() -> None:
    res = _run(_IMG2, move="explain", posed_via_tool=False)
    assert res.passed is False
    assert res.payload["kind"] == "stacked"
    assert res.payload["match_count"] >= 2


def test_floor_flags_two_questions_no_tool() -> None:
    res = _run("What is the bearing for East? And what is it for South?")
    assert res.passed is False
    assert res.payload["kind"] == "stacked"


def test_floor_flags_mcq_block_with_no_trailing_question_no_tool() -> None:
    # MCQ stem as a statement + options — ends on an option line, the
    # trailing-'?' scan misses it; the floor's mcq-block check catches it
    # only when stacked with another prompt. Here it is the ONLY prompt
    # so it is allowed on a no-tool turn (1 allowed) — sanity check it
    # PASSES the floor (extractor would judge the rest).
    text = "Pick the compass point.\nA) North\nB) East\nC) South\nD) West"
    res = _run(text, extractor=_extractor(action_count=1, has_active_end=True))
    assert res.passed is True


def test_floor_flags_tool_posed_prose_question() -> None:
    stem = "Convert NE to a three-figure bearing."
    text = "If NE is halfway, what bearing is it when you split the difference?\n\n" + stem
    res = _run(text, move="worked_example", posed_via_tool=True, pose_tool_stem=stem)
    assert res.passed is False
    assert res.payload["kind"] == "stacked"


def test_floor_flags_tool_posed_mcq_block_in_prose() -> None:
    # The gap curriculum_fidelity misses: an MCQ block in prose with no
    # '?' alongside the bank stem (allowed=0 on a tool turn).
    stem = "Convert NE to a three-figure bearing."
    text = "Here is one to study.\nA) North\nB) East\nC) South\nD) West\n\n" + stem
    res = _run(text, move="worked_example", posed_via_tool=True, pose_tool_stem=stem)
    assert res.passed is False


# ---------------------------------------------------------------------------
# Haiku ceiling (floor passes → extractor decides)
# ---------------------------------------------------------------------------


def test_single_clean_question_passes() -> None:
    res = _run(
        "Nice work. What is the three-figure bearing for East?",
        extractor=_extractor(action_count=1, has_active_end=True),
    )
    assert res.passed is True


def test_extractor_action_count_two_fails() -> None:
    # One '?' passes the floor, but the extractor sees a second action
    # prompt the regex can't (e.g. "now you try").
    res = _run(
        "Great. Now you try the next one. What is the bearing for East?",
        extractor=_extractor(
            action_count=2, has_active_end=True,
            stacked=["Now you try the next one", "What is the bearing for East?"],
        ),
    )
    assert res.passed is False
    assert res.payload["kind"] == "stacked"


def test_extractor_passive_end_fails_on_teaching_move() -> None:
    res = _run(
        "North is 0 degrees and East is 90 degrees. That's the setup.",
        move="explain",
        extractor=_extractor(action_count=0, has_active_end=False),
    )
    assert res.passed is False
    assert res.payload["kind"] == "passive_end"


def test_extractor_passive_end_allowed_on_close_topic() -> None:
    res = _run(
        "You've nailed bearings. Let's move on to the next part of the lesson.",
        move="close_topic",
        extractor=_extractor(action_count=0, has_active_end=False),
    )
    assert res.passed is True


def test_extractor_unavailable_is_failsoft_pass() -> None:
    # Floor passes + extractor returns None → gate passes (never blocks on
    # an LLM failure).
    res = _run(
        "Nice. What is the bearing for East?",
        extractor=None,
    )
    assert res.passed is True
