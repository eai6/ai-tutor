"""Layer 2 tests — curriculum_fidelity gate wired into the recovery loop.

Tests the gate's behavior end-to-end through ``run_gates_with_recovery``:
skip semantics, first-attempt failure → retry-success, retry-also-fails →
degrade-by-truncation. Uses mock ``retry_fn`` closures to inject specific
LLM responses; no live LLM call.

The Phase 1.2 design (memory/curriculum_fidelity_principle.md) keeps the
gate scoped to detection of verifiable prose Qs paired with no
``pose_question`` tool call. Path C (tool-pose + mid-move prose-Q
stacking) is documented as out-of-scope here.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.services.safety_gates import (
    GateContext,
    GateResult,
    RecoveryResult,
    run_curriculum_fidelity_check,
    run_gates_with_recovery,
)


# ---------------------------------------------------------------------------
# Direct unit tests on the gate function
# ---------------------------------------------------------------------------


# Verbatim from the 2026-05-28 Map Scale screenshot opener.
SCREENSHOT_OPENER = (
    "Today we're learning about Map Scale and Map Types — how to "
    "tell the difference between large-scale and small-scale maps "
    "and pick the right one for what you need to do. Think of it "
    "this way: a large-scale map shows a small area in lots of "
    "detail. Which type of map — large-scale or small-scale — "
    "would be more useful if you needed to find a particular "
    "street in your town?"
)

# Verbatim from the run-11 GEO L1454 opener that worked correctly.
RUN11_GEO_OPENER = (
    "Today's lesson is Infiltration and Percolation — two "
    "processes that explain how rainwater travels from the surface "
    "down into the ground. Before we dig in, which of these "
    "matches your intuition?"
)

CLOSE_TOPIC_TEXT = (
    "You nailed the pore-size reasoning. Let's move on to the "
    "next part of the lesson."
)


def test_gate_blocks_verifiable_prose_question_with_no_tool() -> None:
    """The screenshot's exact failure mode is detected."""
    result = run_curriculum_fidelity_check(
        SCREENSHOT_OPENER,
        selected_move="explain",
        posed_via_tool=False,
    )
    assert result.passed is False
    assert result.skipped is False
    assert result.name == "curriculum_fidelity"
    assert "verifiable" in result.reason
    assert "large-scale or small-scale" in (
        result.payload.get("trailing_question", "").lower()
    )


def test_gate_passes_reflective_prose_question() -> None:
    """The run-11 GEO reflective opener is NOT flagged."""
    result = run_curriculum_fidelity_check(
        RUN11_GEO_OPENER,
        selected_move="explain",
        posed_via_tool=False,
    )
    assert result.passed is True
    assert result.skipped is False


def test_gate_passes_tool_posed_response_with_clean_lead_in() -> None:
    """Tool fired + clean prose lead-in → strip stem → no violation.

    The bank stem (appended by ``_render_bank_stem_with_options``)
    is the legitimate assessment. When passed as ``pose_tool_stem``
    it gets stripped before the residual-prose scan.
    """
    bank_stem = (
        "A large-scale map (such as 1:25,000) shows a smaller "
        "geographic area in greater detail than a small-scale map "
        "(such as 1:5,000,000).\n\n(True or False?)"
    )
    full_response = f"Let's apply that to a new figure.\n\n{bank_stem}"
    result = run_curriculum_fidelity_check(
        full_response,
        selected_move="confirm_and_advance",
        posed_via_tool=True,
        pose_tool_stem=bank_stem,
    )
    assert result.passed is True
    # Not "skipped" — the scan actively ran and found no violation.
    assert result.skipped is False


def test_gate_skips_on_close_topic_move() -> None:
    """Terminal move never authors assessments; gate skips."""
    result = run_curriculum_fidelity_check(
        CLOSE_TOPIC_TEXT,
        selected_move="close_topic",
        posed_via_tool=False,
    )
    assert result.passed is True
    assert result.skipped is True
    assert result.reason == "terminal_move"


def test_gate_passes_text_with_no_trailing_question() -> None:
    """No '?' at end → detector returns False → gate passes."""
    result = run_curriculum_fidelity_check(
        "We've explained the concept. Let's keep going together.",
        selected_move="explain",
        posed_via_tool=False,
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
def test_gate_fires_on_every_non_terminal_move(move: str) -> None:
    """Scope is all 7 non-terminal moves per the memo."""
    result = run_curriculum_fidelity_check(
        "Let's check your work. What is 18 ÷ 3?",
        selected_move=move,
        posed_via_tool=False,
    )
    assert result.passed is False, (
        f"gate should fire on {move}; reason={result.reason!r}"
    )


# ---------------------------------------------------------------------------
# Recovery-loop integration — retry + degrade
# ---------------------------------------------------------------------------


def _gate_ctx(
    move: str = "explain",
    posed_via_tool: bool = False,
    pose_tool_stem: str = "",
) -> GateContext:
    """Minimal GateContext for the curriculum_fidelity-only recovery."""
    return GateContext(
        selected_move=move,
        posed_via_tool=posed_via_tool,
        pose_tool_stem=pose_tool_stem,
        lesson_has_media=False,
    )


def test_recovery_loop_retry_success_adopts_retried_text() -> None:
    """First attempt fails; retry produces compliant text; loop adopts it."""
    compliant_retry = (
        "Today we're learning about Map Scale. Maps shrink the real "
        "world to fit on a page. What do you already know about how "
        "maps show big and small areas?"
    )
    retry_calls: list[str] = []

    def retry_fn(reminder: str) -> str:
        retry_calls.append(reminder)
        return compliant_retry

    recovery: RecoveryResult = run_gates_with_recovery(
        SCREENSHOT_OPENER,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    assert recovery.text == compliant_retry
    assert len(retry_calls) == 1
    assert "curriculum-fidelity" in retry_calls[0].lower()
    # First-attempt failure is recorded; degraded is False.
    assert len(recovery.failures) == 1
    assert recovery.failures[0].gate == "curriculum_fidelity"
    assert recovery.failures[0].attempt == 1
    assert recovery.failures[0].degraded is False
    assert recovery.degraded is False


def test_recovery_loop_retry_also_fails_falls_through_to_degrade() -> None:
    """Both attempts violate; degrade strips trailing prose Q sentence."""
    second_violation = (
        "Maps come in different scales. Which scale would be more "
        "useful for finding a street: large-scale or small-scale?"
    )

    def retry_fn(reminder: str) -> str:
        return second_violation

    recovery: RecoveryResult = run_gates_with_recovery(
        SCREENSHOT_OPENER,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    # Final text is the SECOND attempt (we retry against the working
    # text), with the trailing prose Q truncated.
    assert recovery.degraded is True
    assert recovery.text.endswith(".") or recovery.text.endswith("scales.")
    assert "?" not in recovery.text
    # Two failure records: first-attempt + second-attempt-degraded.
    assert len(recovery.failures) == 2
    assert recovery.failures[0].attempt == 1
    assert recovery.failures[1].attempt == 2
    assert recovery.failures[1].degraded is True


def test_recovery_loop_passes_clean_tool_posed_turn_no_retry() -> None:
    """Tool fired + clean prose lead-in → no retry, no failure."""
    retry_calls: list[str] = []

    def retry_fn(reminder: str) -> str:
        retry_calls.append(reminder)
        return "should never be reached"

    bank_stem = "What is 18 ÷ 3?"
    full_response = f"Let's check what you know.\n\n{bank_stem}"
    recovery: RecoveryResult = run_gates_with_recovery(
        full_response,
        ctx=_gate_ctx(
            posed_via_tool=True,
            pose_tool_stem=bank_stem,
        ),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    assert recovery.text == full_response
    assert len(retry_calls) == 0
    assert recovery.failures == []


def test_recovery_loop_skip_on_reflective_means_no_retry() -> None:
    """Reflective trailing Q → gate passes → retry_fn untouched."""
    retry_calls: list[str] = []

    def retry_fn(reminder: str) -> str:
        retry_calls.append(reminder)
        return "should never be reached"

    recovery: RecoveryResult = run_gates_with_recovery(
        RUN11_GEO_OPENER,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    assert recovery.text == RUN11_GEO_OPENER
    assert len(retry_calls) == 0
    assert recovery.failures == []


def test_recovery_loop_skip_on_close_topic_means_no_retry() -> None:
    """close_topic move skips the gate even if trailing Q is verifiable."""
    # Pathological input — close_topic shouldn't end with a Q, but if
    # the LLM somehow emitted one, the gate must still skip.
    text = (
        "You nailed the pore-size reasoning. Let's move on. Which "
        "type of map — large-scale or small-scale — would help you "
        "find a street?"
    )
    retry_calls: list[str] = []

    def retry_fn(reminder: str) -> str:
        retry_calls.append(reminder)
        return "should never be reached"

    recovery: RecoveryResult = run_gates_with_recovery(
        text,
        ctx=_gate_ctx(move="close_topic"),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    assert recovery.text == text
    assert len(retry_calls) == 0
    assert recovery.failures == []


def test_recovery_loop_retry_raise_treated_as_failure() -> None:
    """Retry raising falls through to degrade, never crashes."""

    def retry_fn(reminder: str) -> str:
        raise RuntimeError("LLM timed out")

    recovery: RecoveryResult = run_gates_with_recovery(
        SCREENSHOT_OPENER,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    # Degrade ran on the ORIGINAL text (no retry text available).
    assert recovery.degraded is True
    assert "?" not in recovery.text
    assert len(recovery.failures) == 2
    assert recovery.failures[1].reason == "retry produced no text"


def test_reminder_includes_trailing_question_clip() -> None:
    """The retry reminder names the flagged trailing Q for the LLM."""
    captured_reminder: list[str] = [""]

    def retry_fn(reminder: str) -> str:
        captured_reminder[0] = reminder
        return "Let's start. What do you already know about maps?"

    run_gates_with_recovery(
        SCREENSHOT_OPENER,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    reminder = captured_reminder[0]
    assert "curriculum-fidelity" in reminder.lower()
    assert "large-scale or small-scale" in reminder.lower()
    # The reminder names the three legal options for the retry.
    assert "pose_question" in reminder
    assert "reflective" in reminder.lower()


# ---------------------------------------------------------------------------
# Degrade behavior — the truncation must preserve the framing
# ---------------------------------------------------------------------------


def test_degrade_strips_trailing_question_preserves_framing() -> None:
    """Degrade keeps the explanation; only the trailing prose Q is stripped."""

    def retry_fn(reminder: str) -> str:
        # Retry also fails (returns same shape).
        return (
            "Maps shrink the real world to fit on paper. Which "
            "type of map — large-scale or small-scale — shows "
            "more detail?"
        )

    recovery = run_gates_with_recovery(
        SCREENSHOT_OPENER,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    assert recovery.degraded is True
    # The framing sentences survive the truncation.
    assert "Maps shrink" in recovery.text
    # The trailing Q is removed.
    assert "shows more detail" not in recovery.text
    assert "?" not in recovery.text


def test_degrade_uses_safe_fallback_when_no_framing_left() -> None:
    """If stripping the trailing Q leaves nothing, we ship a safe line."""

    def retry_fn(reminder: str) -> str:
        # A response that is JUST a verifiable prose Q (no framing).
        return "Which is larger — A or B?"

    recovery = run_gates_with_recovery(
        SCREENSHOT_OPENER,
        ctx=_gate_ctx(),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    assert recovery.degraded is True
    # Safe-line fallback, not an empty string.
    assert recovery.text.strip() != ""
    assert "?" not in recovery.text


# ---------------------------------------------------------------------------
# Backward-compat — full default gate order still runs all 4 gates
# ---------------------------------------------------------------------------


def test_default_gate_order_still_includes_existing_three() -> None:
    """The pre-existing gates (safety, figure_ref, answer_leak) survive."""
    from apps.tutoring.v2.services import safety_gates as sg

    assert "curriculum_fidelity" in sg._GATE_ORDER
    assert "safety" in sg._GATE_ORDER
    assert "figure_ref" in sg._GATE_ORDER
    assert "answer_leak" in sg._GATE_ORDER
    # Curriculum fidelity runs FIRST so a prose-Q rewrite happens
    # before safety/figure_ref/answer_leak run on the rewritten text.
    assert sg._GATE_ORDER.index("curriculum_fidelity") == 0


def test_GateContext_carries_selected_move() -> None:
    """selected_move is a real field (not just a kwargs catch-all)."""
    ctx = GateContext(selected_move="explain", posed_via_tool=False)
    assert ctx.selected_move == "explain"
    # Default empty when not specified.
    ctx_default = GateContext()
    assert ctx_default.selected_move == ""


def test_GateContext_carries_pose_tool_stem() -> None:
    """pose_tool_stem is threaded through GateContext for stem-strip."""
    ctx = GateContext(
        selected_move="confirm_and_advance",
        posed_via_tool=True,
        pose_tool_stem="What is 18 ÷ 3?",
    )
    assert ctx.pose_tool_stem == "What is 18 ÷ 3?"
    assert GateContext().pose_tool_stem == ""


# ---------------------------------------------------------------------------
# Path C — stacking: tool fired + verifiable prose Q in lead-in
# ---------------------------------------------------------------------------


# Reconstructed from MATHS run-11 T1853 (§3 R1 example) — worked_example
# body with a labelled-subgoals walkthrough that closes with a prose
# diagnostic ("what is 18 ÷ 3?") while the engine also tool-poses a
# fresh bank slot (the "60 leaflets" stem).
MATHS_T1853_STACKED = (
    "Totally fair — let's strip it right back.\n\n"
    "Subgoal 1: 3x = 18 means three groups make 18.\n"
    "Subgoal 2: undo the multiplication with division.\n"
    "Subgoal 3: 18 ÷ 3 isolates x.\n"
    "Subgoal 4 — Your turn: what is 18 ÷ 3?\n\n"
    "You are distributing 60 copies of a leaflet equally to "
    "5 students. The equation 5x = 60 captures this. "
    "How many leaflets does each student receive?"
)
MATHS_T1853_STEM = (
    "You are distributing 60 copies of a leaflet equally to "
    "5 students. The equation 5x = 60 captures this. "
    "How many leaflets does each student receive?"
)


def test_gate_detects_stacking_with_tool_posed_stem() -> None:
    """Verifiable lead-in Q + tool-posed stem = violation (Path C)."""
    result = run_curriculum_fidelity_check(
        MATHS_T1853_STACKED,
        selected_move="worked_example",
        posed_via_tool=True,
        pose_tool_stem=MATHS_T1853_STEM,
    )
    assert result.passed is False
    payload = result.payload
    assert payload["stacked_with_tool"] is True
    assert payload["match_count"] >= 1
    # The mid-prose diagnostic is the offender — NOT the tool stem.
    assert any("18 ÷ 3" in q for q in payload["offending_questions"])
    # The tool-posed stem must NOT be in the offending list — that's
    # the legitimate assessment.
    assert not any(
        "leaflets does each student receive" in q
        for q in payload["offending_questions"]
    )


def test_gate_stacking_reminder_says_stacked() -> None:
    """Stacking-shape reminder differs from no-tool reminder."""
    from apps.tutoring.v2.services.safety_gates import _reminder_for
    result = run_curriculum_fidelity_check(
        MATHS_T1853_STACKED,
        selected_move="worked_example",
        posed_via_tool=True,
        pose_tool_stem=MATHS_T1853_STEM,
    )
    reminder = _reminder_for(
        "curriculum_fidelity",
        result,
        _gate_ctx(move="worked_example"),
    )
    assert "tool" in reminder.lower()
    assert "SAME tool call" in reminder or "same tool call" in reminder.lower()


# ---------------------------------------------------------------------------
# Multi-violation: 2+ verifiable prose Qs in a single response
# ---------------------------------------------------------------------------


# Multi-violation tests use the TOOL-FIRED path where the full-prose
# scan applies. The no-tool path is trailing-only (Path A — the Map
# Scale screenshot shape), so multi-violation reporting is by design
# scoped to stacking turns (Path C).
MULTI_VIOLATION_STACKED_PROSE = (
    "Let me walk through this step by step.\n\n"
    "What is 1:25,000 in plain English? "
    "Maps come in different shapes. "
    "Given that small-scale maps cover huge areas with less detail, "
    "what does that tell you about which map you'd use to plan a "
    "journey across the Indian Ocean instead?"
)
MULTI_VIOLATION_BANK_STEM = (
    "A large-scale map (such as 1:25,000) shows a smaller "
    "geographic area in greater detail than a small-scale map "
    "(such as 1:5,000,000).\n\n(True or False?)"
)
MULTI_VIOLATION_STACKED_RESPONSE = (
    MULTI_VIOLATION_STACKED_PROSE
    + "\n\nTry this one:\n\n"
    + MULTI_VIOLATION_BANK_STEM
)


def test_gate_collects_all_violations_on_tool_fired_stacking() -> None:
    """Tool-fired turn with multiple non-reflective prose Qs surfaces ALL."""
    result = run_curriculum_fidelity_check(
        MULTI_VIOLATION_STACKED_RESPONSE,
        selected_move="worked_example",
        posed_via_tool=True,
        pose_tool_stem=MULTI_VIOLATION_BANK_STEM,
    )
    assert result.passed is False
    payload = result.payload
    # Two prose Qs: the verifiable "What is 1:25,000…" and the
    # Socratic-unclassified "what does that tell you about which map…"
    # — both flagged because Option 2 treats unclassified as offender.
    assert payload["match_count"] >= 2
    offending = payload["offending_questions"]
    assert any("plain English" in q for q in offending)
    assert any("tell you about which map" in q for q in offending)
    # The bank stem itself is NOT in the offending list.
    assert not any("True or False" in q for q in offending)


def test_gate_reminder_lists_all_violations() -> None:
    """The retry reminder names every offending sentence on stacking turns."""
    from apps.tutoring.v2.services.safety_gates import _reminder_for
    result = run_curriculum_fidelity_check(
        MULTI_VIOLATION_STACKED_RESPONSE,
        selected_move="worked_example",
        posed_via_tool=True,
        pose_tool_stem=MULTI_VIOLATION_BANK_STEM,
    )
    reminder = _reminder_for(
        "curriculum_fidelity",
        result,
        _gate_ctx(
            move="worked_example",
            posed_via_tool=True,
            pose_tool_stem=MULTI_VIOLATION_BANK_STEM,
        ),
    )
    for q in result.payload["offending_questions"]:
        snippet = q[:60]
        assert snippet in reminder, (
            f"reminder did not include offender snippet {snippet!r}"
        )
    assert "Remove ALL" in reminder or "remove all" in reminder.lower()


def test_recovery_loop_degrade_strips_all_violations() -> None:
    """Degrade pass on a multi-violation stacking response removes every offender."""

    def retry_fn(reminder: str) -> str:
        return MULTI_VIOLATION_STACKED_RESPONSE

    recovery = run_gates_with_recovery(
        MULTI_VIOLATION_STACKED_RESPONSE,
        ctx=_gate_ctx(
            move="worked_example",
            posed_via_tool=True,
            pose_tool_stem=MULTI_VIOLATION_BANK_STEM,
        ),
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    assert recovery.degraded is True
    # Neither prose Q survives in the final text.
    assert "plain English" not in recovery.text
    assert "tell you about which map" not in recovery.text
    # The bank stem (the legitimate assessment) survives.
    assert "True or False" in recovery.text


def test_recovery_loop_degrade_on_stacking_preserves_tool_stem() -> None:
    """Stacking degrade strips the prose Q but keeps the bank stem."""

    def retry_fn(reminder: str) -> str:
        # Retry keeps the same stacked shape.
        return MATHS_T1853_STACKED

    ctx = _gate_ctx(
        move="worked_example",
        posed_via_tool=True,
        pose_tool_stem=MATHS_T1853_STEM,
    )
    recovery = run_gates_with_recovery(
        MATHS_T1853_STACKED,
        ctx=ctx,
        retry_fn=retry_fn,
        gates=("curriculum_fidelity",),
    )

    assert recovery.degraded is True
    # The mid-prose diagnostic SENTENCE is stripped — but mentions of
    # "18 ÷ 3" elsewhere (e.g. inside the labelled subgoals) survive;
    # only the offending '?'-ending sentence is removed.
    assert "what is 18 ÷ 3?" not in recovery.text.lower()
    assert "Your turn: what is" not in recovery.text
    # The bank-posed stem survives — it's the legitimate assessment.
    assert "leaflets does each student receive" in recovery.text


# ---------------------------------------------------------------------------
# Unclassified-fallback path — precision-favoring trailing-only check
# ---------------------------------------------------------------------------


def test_gate_unclassified_qs_flagged_in_both_paths() -> None:
    """Under Option 2, unclassified Qs are flagged in BOTH paths.

    Tool-fired path: an unclassified Socratic prose Q stacked next to
    the bank stem is a one-question-per-turn violation regardless of
    whether the explicit verifiable patterns match.

    No-tool path: an unclassified trailing Q is the silent-skip risk
    (Path A — the Map Scale screenshot) and is precision-favoring
    flagged.

    The earlier asymmetric semantics (let unclassified slip through
    on the tool-fired path because "the bank stem is already a valid
    assessment") were tightened on 2026-05-28 after live verification
    surfaced a Socratic "what does that tell you about which map…"
    construction that the explicit verifiable patterns did not catch.
    The SHARED_PREAMBLE "Mid-move pose dedup" rule already promised
    the LLM this enforcement; the gate now matches the contract.
    """
    # Unclassified-shape trailing Q (no explicit verifiable or
    # reflective patterns match).
    unclassified_text = (
        "Maps come in many shapes. Why might that be the case?"
    )

    # No tool: precision-favoring → flag.
    result_no_tool = run_curriculum_fidelity_check(
        unclassified_text,
        selected_move="explain",
        posed_via_tool=False,
    )
    assert result_no_tool.passed is False
    assert result_no_tool.payload["stacked_with_tool"] is False

    # Tool fired: ALSO flag (Option 2 — closes the Socratic-Q stacking gap).
    bank_stem = "What is 18 ÷ 3?"
    result_with_tool = run_curriculum_fidelity_check(
        unclassified_text + f"\n\n{bank_stem}",
        selected_move="confirm_and_advance",
        posed_via_tool=True,
        pose_tool_stem=bank_stem,
    )
    assert result_with_tool.passed is False
    assert result_with_tool.payload["stacked_with_tool"] is True
    # The bank stem itself ("What is 18 ÷ 3?") must NOT be flagged —
    # only the prose Q ("Why might that be the case?").
    assert any(
        "Why might that be" in q
        for q in result_with_tool.payload["offending_questions"]
    )
    assert not any(
        "18 ÷ 3" in q
        for q in result_with_tool.payload["offending_questions"]
    )


def test_socratic_tell_you_about_blocked_on_tool_fired() -> None:
    """The Map Scale L1425 live-verification miss case is now caught.

    Captured 2026-05-28 — Sonnet authored a Socratic
    "Given that …, what does that tell you about which map you'd
    use to plan a journey across the Indian Ocean instead?" inside
    a worked_example body alongside a tool-posed bank T/F. The
    explicit verifiable patterns did not match (no disjunction, no
    compute-value, no MCQ shape). Option 2's non-reflective default
    catches it.
    """
    bank_stem = (
        "A large-scale map (such as 1:25,000) shows a smaller "
        "geographic area in greater detail than a small-scale map "
        "(such as 1:5,000,000).\n\n(True or False?)"
    )
    response = (
        "You're on the right track with large-scale — that instinct "
        "is solid.\n\n"
        "**Subgoal 3 — Match the map type to the task**\n"
        "The task here is navigating a specific hiking trail inside "
        "Morne Seychellois National Park — a small area where detail "
        "matters. Given that large-scale = small area + high detail, "
        "and small-scale = large area + low detail, what does that "
        "tell you about which map you'd use to plan a journey across "
        "the Indian Ocean instead?\n\n"
        "Try this one:\n\n"
        + bank_stem
    )
    result = run_curriculum_fidelity_check(
        response,
        selected_move="worked_example",
        posed_via_tool=True,
        pose_tool_stem=bank_stem,
    )
    assert result.passed is False
    assert result.payload["stacked_with_tool"] is True
    assert any(
        "tell you about which map" in q
        for q in result.payload["offending_questions"]
    )


def test_gate_passes_reflective_mid_prose_q_on_tool_fired() -> None:
    """Explicitly reflective mid-prose Qs are still allowed on tool turns.

    The detector's reflective patterns are the ONLY escape valve
    under Option 2. A turn that combines a recognized reflective
    construction with the tool pose passes through. (In practice
    SHARED_PREAMBLE discourages even this, but the gate doesn't
    block it — only non-reflective constructions trip the gate.)
    """
    bank_stem = "What is 18 ÷ 3?"
    response = (
        "We've covered the rule. Which of these matches your "
        "intuition?\n\n"
        + bank_stem
    )
    result = run_curriculum_fidelity_check(
        response,
        selected_move="confirm_and_advance",
        posed_via_tool=True,
        pose_tool_stem=bank_stem,
    )
    assert result.passed is True
