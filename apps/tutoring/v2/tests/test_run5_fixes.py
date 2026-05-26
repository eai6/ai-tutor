"""Tests for the run-5 MATHS-S1 / GEO-S5 evaluation fixes.

Covers (in order):

  1. ``open_question_stickiness`` no longer applies to
     ``confirm_and_extend`` — its contract is to advance, not stay.
     This was the root cause of the GEO-S5 P1 cascade where every
     correct rich answer fell into the "let's slow down" terminal.

  2. ``classify_student_intent`` — Haiku-backed LLM classifier
     replaces the regex ``detect_help_request``. We mock the client
     to assert routing under realistic phrasings the regex used to
     miss ("i dont know how to do percentages",
     "can you teach me?", "i forgot what oxidation means").

  3. ``select_move`` accepts a pre-computed ``help_request_move``
     so the engine doesn't run the LLM twice per turn.

  4. ``TutorEngine._advance_step_if_possible`` advances the session's
     ``current_step_index`` when more steps remain, returns ``None``
     on the final step.

The fail-soft default for the intent classifier (``attempting`` on
LLM outage) is exercised via a client that raises.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apps.tutoring.v2.contracts import (
    GradingResult,
    ObjectiveProgress,
    OpenQuestion,
    PendingPose,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
    Verdict,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.services.conformance.gates import (
    run_open_question_stickiness_check,
)
from apps.tutoring.v2.services import intent_classifier
from apps.tutoring.v2.services.intent_classifier import (
    INTENT_ASKING_HELP_EXAMPLE,
    INTENT_ASKING_HELP_EXPLAIN,
    INTENT_ATTEMPTING,
    INTENT_META,
    classify_student_intent,
    intent_to_move,
)
from apps.tutoring.v2.services.move_selection import (
    detect_help_request,
    select_move,
)


# ──────────────────────────────────────────────────────────────────────
# 1. open_question_stickiness — confirm_and_extend is no longer in
#    scope. The move's contract is to advance after a correct answer.
# ──────────────────────────────────────────────────────────────────────


def _open_q(qid: int = 100) -> OpenQuestion:
    return OpenQuestion(
        source=QuestionSource.LESSON_STEP,
        id=qid,
        rendered_stem="prior question",
    )


def _new_pose(qid: int = 200) -> PendingPose:
    return PendingPose(
        question_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=qid),
        canonical="42",
        rendered_stem="new question",
        jaccard_signature="sig",
        visible_context=VisibleContextSnapshot(
            visible_prompt="", attached_media_ids=[], recent_transcript=[],
        ),
    )


def test_stickiness_skips_confirm_and_extend_with_new_pose():
    """confirm_and_extend posing a NEW item must pass the gate.

    Before the run-5 fix this was treated as a stay-on-item move; a
    new pose triggered a stickiness violation and the safety
    terminal shipped "let's slow down on the same question" on a
    correct answer — the GEO-S5 P1.
    """
    state = SessionRuntimeState(open_question=_open_q(100))
    result = run_open_question_stickiness_check(
        selected_move="confirm_and_extend",
        runtime_state=state,
        pending_pose=_new_pose(200),
    )
    assert result.passed
    assert result.skipped


def test_stickiness_still_blocks_scaffold_hint_on_new_pose():
    """scaffold_hint posing a NEW item must still trigger a failure.

    The fix only removed confirm_and_extend; scaffold_hint /
    name_misconception / pose_question are still stay-on-item moves
    and the safety floor still applies.
    """
    state = SessionRuntimeState(open_question=_open_q(100))
    result = run_open_question_stickiness_check(
        selected_move="scaffold_hint",
        runtime_state=state,
        pending_pose=_new_pose(200),
    )
    assert not result.passed
    assert "open_question_stickiness" in result.name


def test_stickiness_passes_scaffold_hint_when_pose_matches_open():
    """scaffold_hint re-posing the SAME open item passes the gate."""
    state = SessionRuntimeState(open_question=_open_q(100))
    result = run_open_question_stickiness_check(
        selected_move="scaffold_hint",
        runtime_state=state,
        pending_pose=_new_pose(100),  # same id
    )
    assert result.passed


def test_stickiness_skips_close_topic_and_pivot():
    """close_topic / pivot are out of scope for the stickiness gate."""
    state = SessionRuntimeState(open_question=_open_q(100))
    for move in ("close_topic", "pivot", "worked_example", "explain"):
        result = run_open_question_stickiness_check(
            selected_move=move,
            runtime_state=state,
            pending_pose=_new_pose(200),
        )
        assert result.passed, f"move={move} unexpectedly failed gate"
        assert result.skipped


# ──────────────────────────────────────────────────────────────────────
# 2. classify_student_intent — LLM-backed, replaces regex.
# ──────────────────────────────────────────────────────────────────────


def _mock_intent_client(intent: str):
    """Build a fake llm_client that returns the given intent JSON."""
    client = MagicMock()
    client.generate.return_value = SimpleNamespace(
        content=f'{{"intent": "{intent}"}}',
    )
    return client


@pytest.mark.parametrize(
    "intent,expected_move",
    [
        (INTENT_ATTEMPTING, None),
        (INTENT_ASKING_HELP_EXAMPLE, "worked_example"),
        (INTENT_ASKING_HELP_EXPLAIN, "explain"),
        (INTENT_META, None),
    ],
)
def test_classify_intent_maps_each_label(intent, expected_move):
    """Each classifier label maps to the correct override move."""
    client = _mock_intent_client(intent)
    result = classify_student_intent(
        student_input="anything",
        open_question_stem="some open question",
        llm_client=client,
    )
    assert result == intent
    assert intent_to_move(result) == expected_move


def test_classify_intent_failsoft_on_client_exception():
    """LLM raise → attempting (the conservative no-op default).

    Routes to the verdict-driven move; no false help-request.
    """
    client = MagicMock()
    client.generate.side_effect = RuntimeError("boom")
    result = classify_student_intent(
        student_input="i dont know how to do this",
        llm_client=client,
    )
    assert result == INTENT_ATTEMPTING


def test_classify_intent_failsoft_on_bad_payload():
    """LLM returns non-JSON → attempting default."""
    client = MagicMock()
    client.generate.return_value = SimpleNamespace(content="not json at all")
    result = classify_student_intent(
        student_input="anything", llm_client=client,
    )
    assert result == INTENT_ATTEMPTING


def test_classify_intent_failsoft_on_unknown_intent():
    """LLM returns an intent string not in the allowed set → default."""
    client = MagicMock()
    client.generate.return_value = SimpleNamespace(
        content='{"intent": "freestyle_thinking"}',
    )
    result = classify_student_intent(
        student_input="anything", llm_client=client,
    )
    assert result == INTENT_ATTEMPTING


def test_classify_intent_empty_input_returns_attempting():
    """Empty / whitespace input short-circuits before any LLM call."""
    client = MagicMock()
    assert (
        classify_student_intent(student_input="", llm_client=client)
        == INTENT_ATTEMPTING
    )
    assert (
        classify_student_intent(student_input="   ", llm_client=client)
        == INTENT_ATTEMPTING
    )
    client.generate.assert_not_called()


def test_classify_intent_strips_markdown_fences():
    """LLM wraps JSON in ```json fences — parser must strip them."""
    client = MagicMock()
    client.generate.return_value = SimpleNamespace(
        content='```json\n{"intent": "asking_help_explain"}\n```',
    )
    result = classify_student_intent(
        student_input="i dont understand percentages",
        llm_client=client,
    )
    assert result == INTENT_ASKING_HELP_EXPLAIN


# ──────────────────────────────────────────────────────────────────────
# 3. detect_help_request delegates to the LLM classifier.
# ──────────────────────────────────────────────────────────────────────


def test_detect_help_request_routes_explain(monkeypatch):
    """Patch classify_student_intent → asking_help_explain."""
    monkeypatch.setattr(
        "apps.tutoring.v2.services.move_selection.classify_student_intent",
        lambda **_: INTENT_ASKING_HELP_EXPLAIN,
    )
    assert detect_help_request("i dont know how to do percentages") == "explain"


def test_detect_help_request_routes_worked_example(monkeypatch):
    """Patch classify_student_intent → asking_help_example."""
    monkeypatch.setattr(
        "apps.tutoring.v2.services.move_selection.classify_student_intent",
        lambda **_: INTENT_ASKING_HELP_EXAMPLE,
    )
    assert detect_help_request("show me how") == "worked_example"


def test_detect_help_request_passes_through_none(monkeypatch):
    """attempting / meta → no override."""
    for intent in (INTENT_ATTEMPTING, INTENT_META):
        monkeypatch.setattr(
            "apps.tutoring.v2.services.move_selection.classify_student_intent",
            lambda intent=intent, **_: intent,
        )
        assert detect_help_request("is it 21?") is None


def test_detect_help_request_short_circuits_empty():
    """Empty input never reaches the classifier."""
    assert detect_help_request("") is None
    assert detect_help_request("  \n\t  ") is None


# ──────────────────────────────────────────────────────────────────────
# 4. select_move accepts pre-computed help_request_move.
# ──────────────────────────────────────────────────────────────────────


def test_select_move_honors_precomputed_help_request_explain():
    """Engine pre-classified intent → select_move uses it, no second call."""
    state = SessionRuntimeState(attempts_on_open_question=1)
    move = select_move(
        verdict=GradingResult(verdict=Verdict.WRONG),
        runtime_state=state,
        student_input="i dont know how to do percentages",
        help_request_move="explain",
    )
    assert move == "explain"


def test_select_move_honors_precomputed_help_request_worked_example():
    """Pre-computed worked_example override beats the verdict branch."""
    state = SessionRuntimeState(attempts_on_open_question=1)
    move = select_move(
        verdict=GradingResult(verdict=Verdict.PARTIAL),
        runtime_state=state,
        student_input="show me how",
        help_request_move="worked_example",
    )
    assert move == "worked_example"


def test_select_move_precomputed_none_falls_through(monkeypatch):
    """help_request_move=None still triggers the on-demand classifier.

    Direct callers (template renderer, tests) that don't pre-classify
    should still get help-routing.
    """
    monkeypatch.setattr(
        "apps.tutoring.v2.services.move_selection.classify_student_intent",
        lambda **_: INTENT_ATTEMPTING,
    )
    state = SessionRuntimeState(attempts_on_open_question=1)
    move = select_move(
        verdict=GradingResult(verdict=Verdict.WRONG),
        runtime_state=state,
        student_input="is it 21?",
        # help_request_move omitted — on-demand classifier runs.
    )
    # Verdict.WRONG with attempts<3 → scaffold_hint.
    assert move == "scaffold_hint"


def test_select_move_empty_input_does_not_call_classifier(monkeypatch):
    """No input → no classifier call (cheap default path)."""
    calls = {"count": 0}

    def _spy(**_):
        calls["count"] += 1
        return INTENT_ATTEMPTING

    monkeypatch.setattr(
        "apps.tutoring.v2.services.move_selection.classify_student_intent",
        _spy,
    )
    state = SessionRuntimeState()
    select_move(verdict=None, runtime_state=state, student_input="")
    # Empty input is short-circuited before reaching the classifier.
    assert calls["count"] == 0


# ──────────────────────────────────────────────────────────────────────
# 5. TurnResult.is_lesson_complete is False when more steps remain
#    (engine-level test would need DB setup; we test the helper
#    semantics in isolation via a stub engine.).
# ──────────────────────────────────────────────────────────────────────


def test_advance_step_returns_next_when_more_remain():
    """When steps[idx+1] exists, _advance_step_if_possible returns idx+1."""
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    # Stub session + lesson with 5 steps and current index 1.
    fake_lesson = SimpleNamespace(
        steps=SimpleNamespace(count=lambda: 5, all=lambda: [object()] * 5),
    )
    fake_session = SimpleNamespace(
        lesson=fake_lesson,
        current_step_index=1,
        save=lambda update_fields=None: None,
    )
    fake_cm = SimpleNamespace(session=fake_session)
    engine = TutorEngine.__new__(TutorEngine)
    engine.context_manager = fake_cm

    state = SessionRuntimeState(
        open_question=_open_q(100),
        attempts_on_open_question=2,
        unverified_run_length=1,
    )
    next_idx = engine._advance_step_if_possible(runtime_state=state)
    assert next_idx == 2
    # Side effects: state reset.
    assert state.open_question is None
    assert state.attempts_on_open_question == 0
    assert state.unverified_run_length == 0
    # And the session row was advanced.
    assert fake_session.current_step_index == 2


def test_advance_step_returns_none_on_final_step():
    """When the active step is the LAST, advance returns None (lesson done)."""
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    fake_lesson = SimpleNamespace(
        steps=SimpleNamespace(count=lambda: 5, all=lambda: [object()] * 5),
    )
    fake_session = SimpleNamespace(
        lesson=fake_lesson,
        current_step_index=4,  # last index
        save=lambda update_fields=None: None,
    )
    fake_cm = SimpleNamespace(session=fake_session)
    engine = TutorEngine.__new__(TutorEngine)
    engine.context_manager = fake_cm

    state = SessionRuntimeState(open_question=_open_q(100))
    result = engine._advance_step_if_possible(runtime_state=state)
    assert result is None
    # State NOT reset (engine will complete the session instead).
    assert state.open_question is not None


def test_advance_step_failsoft_on_missing_lesson():
    """No lesson → returns None (treated as final, session completes)."""
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    fake_session = SimpleNamespace(lesson=None, current_step_index=0)
    fake_cm = SimpleNamespace(session=fake_session)
    engine = TutorEngine.__new__(TutorEngine)
    engine.context_manager = fake_cm

    state = SessionRuntimeState()
    assert engine._advance_step_if_possible(runtime_state=state) is None
