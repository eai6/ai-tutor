"""Tests for the run-10 fix bundle.

Covers the deterministic, code-side changes from the run-10 evaluation
report (`test-reports/MATHS-S1-evaluation-2026-05-28-run10.md`,
`test-reports/GEO-S5-evaluation-2026-05-28-run10.md`):

  - **Math Fix 1c** — MCQ letter-disagreement extractor +
    ``_maybe_letter_disagreement_override`` guard in
    ``_compare_label_canonical``.
  - **Math Fix 2** — runtime ``difficulty_level`` overrides the LLM's
    ``difficulty_hint`` inside ``_handle_pose_tool_use``.
  - **Geo Fix 4** — Mastery I-2 close floor:
    ``_apply_mastery_close_floor`` overrides ``close_topic`` →
    ``confirm_and_advance`` when the router selects close-via-correct
    with ``unscaffolded_correct_on_open_question_objective == 0``.

Checklist additions to prompts (LLM-A / LLM-B / move bodies / preamble)
are exercised by separate prompt-shape tests when authored; this module
focuses on the deterministic code paths whose behavior can be unit-
tested without an LLM.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apps.tutoring.v2.contracts import (
    GradingResult,
    RouterDecision,
    SessionRuntimeState,
    Verdict,
)
from apps.tutoring.v2.services.student_grader import StudentGrader


# ──────────────────────────────────────────────────────────────────────
# Math Fix 1c — letter-disagreement extractor
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "student_input, expected",
    [
        # Shape 1 — bare letter ± punctuation.
        ("B", "B"),
        ("B.", "B"),
        ("b", "B"),
        ("  D  ", "D"),
        # Shape 2a — letter + immediate reasoning marker (the run-10
        # P1-1 transcript).
        ("B because 23 + 8 = 31", "B"),
        ("A since 4*8=32", "A"),
        # Shape 2b — letter + punctuation gate + downstream marker
        # (the run-10 P1-2 transcript).
        ("D, x = 42 because thats the starting number", "D"),
        ("A, this is my answer because it makes sense", "A"),
        # Shape 2c — letter + punctuation + arithmetic operator.
        ("B, profit = 270 SCR", "B"),
        # Shape 3 — pick-verb prefix.
        ("the answer is C", "C"),
        ("I pick D", "D"),
        ("option B", "B"),
        ("letter A", "A"),
        ("my pick is B", "B"),
        # Article-A guard.
        ("A triangle has 3 sides", ""),
        ("A triangle has 3 sides because they connect", ""),
        # Conflict / ambiguity → return empty.
        ("A or B", ""),
        ("I think A or maybe D", ""),
        # No letter at all.
        ("31", ""),
        ("", ""),
        ("I dont know", ""),
        # Markdown emphasis stripped.
        ("**B**", "B"),
        # Reasoning marker not anchored to the letter — Shape 2a still
        # matches because 'and' is in the marker list.
        ("B and also it shows the slip", "B"),
        # Noise / common student prose with no MCQ intent.
        ("Honestly all five feel clear", ""),
        ("Welcome to the lesson", ""),
        ("Anse Boileau", ""),
        ("a moment please", ""),
    ],
)
def test_extract_unambiguous_mcq_letter(student_input: str, expected: str) -> None:
    """Three-shape extractor matches the design table from run-10."""
    assert StudentGrader._extract_unambiguous_mcq_letter(student_input) == expected


@pytest.mark.parametrize(
    "canon, answer_type, student_input, expected",
    [
        # Run-10 MATHS-S1 P1-1: canonical A, student picked B with bad
        # reasoning → override fires (CORRECT → WRONG).
        ("a", "multiple_choice", "B because 23 + 8 = 31", "B"),
        # Run-10 MATHS-S1 P1-2: canonical B, student picked D with
        # trap reasoning → override fires.
        ("b", "multiple_choice", "D, x = 42 because thats the starting number", "D"),
        # Same letter → no override.
        ("b", "multiple_choice", "B because 23 + 8 = 31", None),
        # Non-MCQ canonical (T/F) → never fires.
        ("true", "true_false", "B because reasons", None),
        # Unknown answer_type → never fires.
        ("a", "", "B", None),
        # Numeric answer_type → never fires.
        ("a", "short_numeric", "B", None),
        # Article-A in MCQ context → no override (no letter extracted).
        ("c", "multiple_choice", "A triangle has 3 sides", None),
        # answer_type "label" is accepted (engine treats label-shape as
        # MCQ/T-F/yes-no — the canonical letter gate filters internally).
        ("a", "label", "B because reasons", "B"),
        ("a", "mcq", "B because reasons", "B"),
    ],
)
def test_maybe_letter_disagreement_override(
    canon: str, answer_type: str, student_input: str, expected,
) -> None:
    """Override fires only on the narrow MCQ-letter-disagreement path."""
    got = StudentGrader._maybe_letter_disagreement_override(
        canon_norm=canon,
        answer_type=answer_type,
        student_input=student_input,
    )
    assert got == expected


# ──────────────────────────────────────────────────────────────────────
# Math Fix 2 — runtime difficulty_level → difficulty_hint plumbing
# ──────────────────────────────────────────────────────────────────────

def _runtime_state_with_difficulty(level: int) -> MagicMock:
    rs = MagicMock(spec=SessionRuntimeState)
    rs.difficulty_level = level
    rs.delivered_lesson_step_ids = []
    return rs


def test_difficulty_too_hard_overrides_to_easier(monkeypatch: pytest.MonkeyPatch) -> None:
    """``difficulty_level < 0`` (UI signal: too_hard) coerces the
    LLM's ``difficulty_hint`` to ``"easier"`` regardless of what the
    tool-use block specified."""
    from apps.tutoring.v2.services import student_tutor as st_mod

    captured: dict = {}

    def fake_select_pose_slot(
        *, lesson_id, delivered_step_ids, topic_or_subskill, difficulty_hint,
    ):
        captured["difficulty_hint"] = difficulty_hint
        return MagicMock(exhausted=True, lesson_step_id=0, stem="")

    monkeypatch.setattr(st_mod, "select_pose_slot", fake_select_pose_slot)

    tutor = MagicMock(spec=st_mod.StudentTutor)
    tutor._handle_pose_tool_use = st_mod.StudentTutor._handle_pose_tool_use.__get__(
        tutor, st_mod.StudentTutor,
    )

    context = MagicMock()
    context.runtime_state = _runtime_state_with_difficulty(-1)
    context.lesson_id = 1
    context.full_transcript = []

    block = MagicMock()
    block.input = {"topic_or_subskill": "anything", "difficulty_hint": "same"}

    pending, stem = tutor._handle_pose_tool_use(block=block, context=context)
    assert captured["difficulty_hint"] == "easier"


def test_difficulty_too_easy_overrides_to_harder(monkeypatch: pytest.MonkeyPatch) -> None:
    """``difficulty_level > 0`` (UI signal: too_easy) coerces the
    LLM's ``difficulty_hint`` to ``"harder"``."""
    from apps.tutoring.v2.services import student_tutor as st_mod

    captured: dict = {}

    def fake_select_pose_slot(
        *, lesson_id, delivered_step_ids, topic_or_subskill, difficulty_hint,
    ):
        captured["difficulty_hint"] = difficulty_hint
        return MagicMock(exhausted=True, lesson_step_id=0, stem="")

    monkeypatch.setattr(st_mod, "select_pose_slot", fake_select_pose_slot)

    tutor = MagicMock(spec=st_mod.StudentTutor)
    tutor._handle_pose_tool_use = st_mod.StudentTutor._handle_pose_tool_use.__get__(
        tutor, st_mod.StudentTutor,
    )

    context = MagicMock()
    context.runtime_state = _runtime_state_with_difficulty(2)
    context.lesson_id = 1
    context.full_transcript = []

    block = MagicMock()
    block.input = {"topic_or_subskill": "anything", "difficulty_hint": "same"}

    tutor._handle_pose_tool_use(block=block, context=context)
    assert captured["difficulty_hint"] == "harder"


def test_difficulty_zero_respects_llm_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """``difficulty_level == 0`` leaves the LLM's hint untouched."""
    from apps.tutoring.v2.services import student_tutor as st_mod

    captured: dict = {}

    def fake_select_pose_slot(
        *, lesson_id, delivered_step_ids, topic_or_subskill, difficulty_hint,
    ):
        captured["difficulty_hint"] = difficulty_hint
        return MagicMock(exhausted=True, lesson_step_id=0, stem="")

    monkeypatch.setattr(st_mod, "select_pose_slot", fake_select_pose_slot)

    tutor = MagicMock(spec=st_mod.StudentTutor)
    tutor._handle_pose_tool_use = st_mod.StudentTutor._handle_pose_tool_use.__get__(
        tutor, st_mod.StudentTutor,
    )

    context = MagicMock()
    context.runtime_state = _runtime_state_with_difficulty(0)
    context.lesson_id = 1
    context.full_transcript = []

    block = MagicMock()
    block.input = {"topic_or_subskill": "anything", "difficulty_hint": "harder"}

    tutor._handle_pose_tool_use(block=block, context=context)
    assert captured["difficulty_hint"] == "harder"


# ──────────────────────────────────────────────────────────────────────
# Geo Fix 4 — Mastery I-2 close floor
# ──────────────────────────────────────────────────────────────────────

def _grading_result(verdict: Verdict) -> GradingResult:
    return GradingResult(
        verdict=verdict,
        private_canonical="",
        student_value="",
        reasoning="",
        bare_answer=False,
    )


def _router_decision(
    *,
    case: str = "answer_attempt",
    verdict_needed: bool = True,
    move=None,
    moves_by_verdict=None,
) -> RouterDecision:
    if verdict_needed:
        # Rule 7 — moves_by_verdict carries the per-verdict routing;
        # the top-level ``move`` is unset.
        return RouterDecision(
            case=case,
            verdict_needed=True,
            move=None,
            moves_by_verdict=moves_by_verdict or {
                "correct": "close_topic",
                "partial": "scaffold_hint",
                "wrong": "scaffold_hint",
            },
            reason="",
            intent="answer_attempt",
            method_evidence_present=True,
            named_their_reasoning=False,
            richness="bare",
            rule_fired="Rule 7 (answer_attempt)",
        )
    return RouterDecision(
        case=case,
        verdict_needed=False,
        move=move or "close_topic",
        moves_by_verdict=None,
        reason="",
        intent="answer_attempt",
        method_evidence_present=True,
        named_their_reasoning=False,
        richness=None,
        rule_fired="Rule 1 (Mastery Ch.13 lesson_complete)",
    )


def _runtime_state_with_unscaffolded_correct(prior_count: int) -> SessionRuntimeState:
    return SessionRuntimeState(
        unscaffolded_correct_on_open_question_objective=prior_count,
    )


def _get_engine_method():
    """Return ``_apply_mastery_close_floor`` as a free callable.

    Builds a bare ``TutorEngine`` shell via ``__new__`` (no DB / no
    context_manager required) and binds the method.
    """
    from apps.tutoring.v2.services.tutor_engine import TutorEngine
    engine = TutorEngine.__new__(TutorEngine)
    return engine._apply_mastery_close_floor


def test_mastery_close_floor_overrides_on_first_correct() -> None:
    """Router picks close_topic via Rule 7 correct branch on the FIRST
    correct (prior count == 0) → I-2 violation → override to
    confirm_and_advance.
    """
    apply_floor = _get_engine_method()
    result = apply_floor(
        selected_move="close_topic",
        router_decision=_router_decision(),
        verdict=_grading_result(Verdict.CORRECT),
        runtime_state=_runtime_state_with_unscaffolded_correct(0),
    )
    assert result == "confirm_and_advance"


def test_mastery_close_floor_allows_on_second_correct() -> None:
    """Prior unscaffolded correct == 1 → this becomes the 2nd; close is
    permitted by I-2 → no override."""
    apply_floor = _get_engine_method()
    result = apply_floor(
        selected_move="close_topic",
        router_decision=_router_decision(),
        verdict=_grading_result(Verdict.CORRECT),
        runtime_state=_runtime_state_with_unscaffolded_correct(1),
    )
    assert result == "close_topic"


def test_mastery_close_floor_ignores_non_close_moves() -> None:
    """Non-close moves are passed through untouched."""
    apply_floor = _get_engine_method()
    result = apply_floor(
        selected_move="confirm_and_advance",
        router_decision=_router_decision(),
        verdict=_grading_result(Verdict.CORRECT),
        runtime_state=_runtime_state_with_unscaffolded_correct(0),
    )
    assert result == "confirm_and_advance"


def test_mastery_close_floor_ignores_non_answer_attempt() -> None:
    """Rule 1 (lesson_complete) / Rule 3 (forced_close) close decisions
    are not verdict-driven; floor must not interfere."""
    apply_floor = _get_engine_method()
    result = apply_floor(
        selected_move="close_topic",
        router_decision=_router_decision(
            case="lesson_complete",
            verdict_needed=False,
            move="close_topic",
            moves_by_verdict=None,
        ),
        verdict=None,
        runtime_state=_runtime_state_with_unscaffolded_correct(0),
    )
    assert result == "close_topic"


def test_mastery_close_floor_ignores_non_correct_verdict() -> None:
    """Floor only applies on the CORRECT branch of Rule 7. A close on
    wrong / partial (rare but possible via pivot escalations) is not
    overridden."""
    apply_floor = _get_engine_method()
    for non_correct in (Verdict.WRONG, Verdict.PARTIAL):
        result = apply_floor(
            selected_move="close_topic",
            router_decision=_router_decision(),
            verdict=_grading_result(non_correct),
            runtime_state=_runtime_state_with_unscaffolded_correct(0),
        )
        assert result == "close_topic", f"verdict={non_correct} should pass through"
