"""Tests for the stem_duplication gate (Phase 5 — 2026-05-28).

Failure mode caught: the LLM authors the full bank stem as a
STATEMENT in its prose AND also calls the ``pose_question`` tool,
producing the stem text twice in the rendered turn (LLM-copy then
engine-appended). Surfaced live on Map Scale L1425 session 123 T2.

Detection: verbatim contiguous substring match between the LLM-
authored prose and the tool-emitted stem text (with answer-shape
suffixes stripped from the stem side first). Tested both at the
detector level (``find_prose_stem_duplicates``) and through the
gate's recovery loop (retry + degrade).
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.services.conformance_check import (
    find_prose_stem_duplicates,
    strip_trailing_tool_stem,
)
from apps.tutoring.v2.services.safety_gates import (
    GateContext,
    RecoveryResult,
    run_gates_with_recovery,
    run_stem_duplication_check,
)


# ---------------------------------------------------------------------------
# Reusable fixtures — drawn from session 123 T2
# ---------------------------------------------------------------------------


# The bank stem the engine appends for L1425 step 13759 (true_false).
SESSION_123_BANK_STEM = (
    "A large-scale map (such as 1:25,000) shows a smaller "
    "geographic area in greater detail than a small-scale map "
    "(such as 1:5,000,000).\n\n(True or False?)"
)

# Verbatim shape of the failing response — LLM copied the stem into
# its prose lead-in, then the engine re-appended the stem.
SESSION_123_T2_DUPED_RESPONSE = (
    "Good — you've already got 'large scale' on your radar, which "
    "is exactly where we're starting.\n\n"
    "Let me walk through how scale actually works before we test "
    "it.\n\n"
    "**Step 1 — Read the ratio.** A map scale is written as a "
    "ratio, like 1:25,000.\n\n"
    "Now try this one:\n\n"
    "A large-scale map (such as 1:25,000) shows a smaller "
    "geographic area in greater detail than a small-scale map "
    "(such as 1:5,000,000).\n\n"  # ← LLM-copied stem (duplicate)
    + SESSION_123_BANK_STEM  # ← engine-appended stem
)


# ---------------------------------------------------------------------------
# Detector — find_prose_stem_duplicates
# ---------------------------------------------------------------------------


def test_detector_flags_verbatim_stem_copy_in_prose() -> None:
    """The session 123 T2 failure shape is detected."""
    prose_only = strip_trailing_tool_stem(
        SESSION_123_T2_DUPED_RESPONSE, SESSION_123_BANK_STEM,
    )
    dups = find_prose_stem_duplicates(prose_only, SESSION_123_BANK_STEM)
    assert dups
    # The duplicated substring is the bank stem body (with the
    # "(True or False?)" suffix stripped — that's curriculum_fidelity's
    # territory).
    primary = dups[0]
    assert "large-scale map" in primary
    assert "1:5,000,000" in primary


def test_detector_passes_clean_brief_lead_in() -> None:
    """A turn with only a brief lead-in + the engine-appended stem passes."""
    response = (
        "Good — you named large-scale. Try this:\n\n"
        + SESSION_123_BANK_STEM
    )
    prose_only = strip_trailing_tool_stem(response, SESSION_123_BANK_STEM)
    dups = find_prose_stem_duplicates(prose_only, SESSION_123_BANK_STEM)
    assert dups == []


def test_detector_passes_partial_overlap_under_threshold() -> None:
    """Incidental keyword mentions below min_chars do not trigger."""
    response = (
        "Good — you named large-scale. The scale ratio is the key "
        "idea here. Try this:\n\n"
        + SESSION_123_BANK_STEM
    )
    prose_only = strip_trailing_tool_stem(response, SESSION_123_BANK_STEM)
    dups = find_prose_stem_duplicates(
        prose_only, SESSION_123_BANK_STEM, min_chars=40,
    )
    # Short keyword overlaps ("large-scale", "scale ratio") are < 40
    # chars and therefore not flagged.
    assert dups == []


def test_detector_strips_true_false_suffix_before_comparison() -> None:
    """The '(True or False?)' suffix on the stem is not the dup signal."""
    # Prose contains JUST the appended suffix, not the body.
    response_prose = "Some random text. (True or False?)"
    dups = find_prose_stem_duplicates(response_prose, SESSION_123_BANK_STEM)
    # The suffix on its own is < min_chars and is stripped from the
    # stem side before comparison anyway.
    assert dups == []


def test_detector_strips_mcq_option_lines_from_stem_side() -> None:
    """MCQ option lines on the stem do not become the dup signal."""
    mcq_stem = (
        "Which type of map zooms in closer?\n\n"
        "A) Large-scale\nB) Small-scale\nC) Topographic\nD) Political"
    )
    # Prose contains ONLY the option line — short, not the question.
    response_prose = "Quick check. A) Large-scale"
    dups = find_prose_stem_duplicates(response_prose, mcq_stem)
    assert dups == []


def test_detector_handles_empty_inputs() -> None:
    """Empty prose or empty stem returns empty list."""
    assert find_prose_stem_duplicates("", SESSION_123_BANK_STEM) == []
    assert find_prose_stem_duplicates("anything", "") == []
    assert find_prose_stem_duplicates("", "") == []


def test_detector_normalizes_whitespace_across_line_wraps() -> None:
    """Line-wrap differences between prose and stem do not break the match."""
    # Prose has the stem text but wrapped at a different column.
    prose = (
        "Now try this one:\n\n"
        "A large-scale map (such as 1:25,000) shows a smaller geographic\n"
        "area in greater detail than a small-scale map (such as 1:5,000,000)."
    )
    dups = find_prose_stem_duplicates(prose, SESSION_123_BANK_STEM)
    assert dups
    primary = " ".join(dups[0].split())
    assert "large-scale map" in primary
    assert "1:5,000,000" in primary


# ---------------------------------------------------------------------------
# Gate — run_stem_duplication_check
# ---------------------------------------------------------------------------


def test_gate_flags_session_123_t2_duplicated_stem() -> None:
    """The actual failure response from session 123 T2 is flagged."""
    result = run_stem_duplication_check(
        SESSION_123_T2_DUPED_RESPONSE,
        selected_move="worked_example",
        posed_via_tool=True,
        pose_tool_stem=SESSION_123_BANK_STEM,
    )
    assert result.passed is False
    assert result.payload["match_chars"] >= 40
    assert result.payload["move"] == "worked_example"


def test_gate_skips_when_no_tool_fired() -> None:
    """No-tool turns have nothing to duplicate."""
    result = run_stem_duplication_check(
        "Some response text.",
        selected_move="explain",
        posed_via_tool=False,
        pose_tool_stem="",
    )
    assert result.passed is True
    assert result.skipped is True
    assert result.reason == "no_tool_pose"


def test_gate_skips_when_pose_tool_stem_empty() -> None:
    """Empty tool stem can't be duplicated."""
    result = run_stem_duplication_check(
        "Some response.",
        selected_move="confirm_and_advance",
        posed_via_tool=True,
        pose_tool_stem="",
    )
    assert result.passed is True
    assert result.skipped is True
    assert result.reason == "empty_tool_stem"


def test_gate_skips_on_close_topic() -> None:
    """Terminal moves never pose; gate skips."""
    result = run_stem_duplication_check(
        "Let's wrap up.",
        selected_move="close_topic",
        posed_via_tool=False,
        pose_tool_stem="",
    )
    assert result.passed is True
    assert result.skipped is True


def test_gate_passes_brief_lead_in_with_tool_stem() -> None:
    """A turn whose prose is just a brief lead-in passes."""
    response = "Try this:\n\n" + SESSION_123_BANK_STEM
    result = run_stem_duplication_check(
        response,
        selected_move="confirm_and_advance",
        posed_via_tool=True,
        pose_tool_stem=SESSION_123_BANK_STEM,
    )
    assert result.passed is True


@pytest.mark.parametrize(
    "move",
    [
        "confirm_and_advance",
        "confirm_and_extend",
        "scaffold_hint",
        "name_misconception",
        "worked_example",
        "explain",
        "pivot",
    ],
)
def test_gate_scope_covers_all_non_terminal_moves(move: str) -> None:
    """The gate fires on every move that can pose via tool."""
    result = run_stem_duplication_check(
        SESSION_123_T2_DUPED_RESPONSE,
        selected_move=move,
        posed_via_tool=True,
        pose_tool_stem=SESSION_123_BANK_STEM,
    )
    assert result.passed is False, (
        f"gate should fire on {move}; reason={result.reason!r}"
    )


# ---------------------------------------------------------------------------
# Recovery loop — retry + degrade
# ---------------------------------------------------------------------------


def _gate_ctx(
    move: str = "worked_example",
    posed_via_tool: bool = True,
    pose_tool_stem: str = SESSION_123_BANK_STEM,
) -> GateContext:
    return GateContext(
        selected_move=move,
        posed_via_tool=posed_via_tool,
        pose_tool_stem=pose_tool_stem,
        lesson_has_media=False,
    )


def test_recovery_retry_success_adopts_clean_text() -> None:
    """Retry with a brief lead-in is adopted; the duped attempt is replaced."""
    compliant_retry = (
        "Good — you named large-scale. Try this:\n\n"
        + SESSION_123_BANK_STEM
    )

    def retry_fn(reminder: str) -> str:
        return compliant_retry

    recovery: RecoveryResult = run_gates_with_recovery(
        SESSION_123_T2_DUPED_RESPONSE,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("stem_duplication",),
    )

    assert recovery.text == compliant_retry
    assert len(recovery.failures) == 1
    assert recovery.failures[0].gate == "stem_duplication"
    assert recovery.failures[0].attempt == 1
    assert recovery.failures[0].degraded is False


def test_recovery_degrade_strips_duplicate_preserves_engine_stem() -> None:
    """When retry also dupes, degrade removes the LLM-copy but keeps the engine stem."""

    def retry_fn(reminder: str) -> str:
        # Retry returns the same duped shape.
        return SESSION_123_T2_DUPED_RESPONSE

    recovery: RecoveryResult = run_gates_with_recovery(
        SESSION_123_T2_DUPED_RESPONSE,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("stem_duplication",),
    )

    assert recovery.degraded is True
    # The bank stem appears EXACTLY ONCE in the final text (the
    # engine-appended copy survives; the LLM-copy is stripped).
    stem_body = (
        "A large-scale map (such as 1:25,000) shows a smaller "
        "geographic area in greater detail than a small-scale map "
        "(such as 1:5,000,000)."
    )
    norm_text = " ".join(recovery.text.split())
    norm_stem = " ".join(stem_body.split())
    assert norm_text.count(norm_stem) == 1


def test_recovery_skip_on_no_tool_means_no_retry() -> None:
    """No-tool turns skip the gate; retry_fn is never called."""
    retry_calls: list[str] = []

    def retry_fn(reminder: str) -> str:
        retry_calls.append(reminder)
        return "unreachable"

    recovery = run_gates_with_recovery(
        "Some text without any tool stem.",
        ctx=_gate_ctx(posed_via_tool=False, pose_tool_stem=""),
        retry_fn=retry_fn,
        gates=("stem_duplication",),
    )

    assert recovery.text == "Some text without any tool stem."
    assert retry_calls == []
    assert recovery.failures == []


def test_reminder_names_duplicate_and_brief_lead_in_targets() -> None:
    """The reminder includes the duplicated text + acceptable lead-ins."""
    from apps.tutoring.v2.services.safety_gates import _reminder_for
    result = run_stem_duplication_check(
        SESSION_123_T2_DUPED_RESPONSE,
        selected_move="worked_example",
        posed_via_tool=True,
        pose_tool_stem=SESSION_123_BANK_STEM,
    )
    reminder = _reminder_for("stem_duplication", result, _gate_ctx())
    assert "verbatim" in reminder
    assert "tool" in reminder
    # The reminder names brief lead-in alternatives.
    assert "Try this:" in reminder
    assert "Next:" in reminder


# ---------------------------------------------------------------------------
# Integration with other gates — order matters
# ---------------------------------------------------------------------------


def test_gate_order_runs_curriculum_fidelity_before_stem_duplication() -> None:
    """curriculum_fidelity fires first; stem_duplication is second."""
    from apps.tutoring.v2.services.safety_gates import _GATE_ORDER
    assert _GATE_ORDER.index("curriculum_fidelity") < _GATE_ORDER.index(
        "stem_duplication"
    )
    # Both come before safety/figure_ref/answer_leak.
    assert _GATE_ORDER.index("stem_duplication") < _GATE_ORDER.index("safety")
