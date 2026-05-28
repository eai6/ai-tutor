"""Live-LLM router tests for the run-7 P1 scenarios.

Plan §5.5 — these tests hit the REAL ``MOVE_ROUTER`` ModelConfig
(Sonnet 4.6 by default) and pin acceptable router behaviour on the
four P1 turns surfaced in:

  - ``test-reports/MATHS-S1-evaluation-2026-05-27-run7.md``
    - P1-1: resume turn after three fully-shown correct Pythagoras
            answers — engine re-emitted the engage paragraph.
    - P1-4: rich correct streak (3 correct, attempts=3) — engine
            kept reposing rather than closing the topic.
  - ``test-reports/GEO-S5-evaluation-2026-05-27-run7.md``
    - P1-3: student said "i dont understand. what is condensation"
            and the engine fired ``pose_question`` (Direct Instruction
            violation) — the dominant misroute fixed by the router +
            help-regex floor.
    - P1-4: student said "guess B" → CORRECT verdict carrying
            ``reason_code=self_reported_guess`` — engine advanced via
            confirm_and_extend (Mastery Learning violation).

Each test asserts the router's ``chosen_move`` falls into the
acceptable set for the scenario. The asserts are intentionally LOOSE
(set-membership, not equality) because the router is an LLM and a
single "correct" move for a real pedagogical decision is rarely
unique.

Gating
======

These tests are marked ``@pytest.mark.live_llm`` AND probe for an
``ANTHROPIC_API_KEY``; both must be present to run. CI sets neither,
so they are silently skipped in CI. Run pre-cutover and after the
move_prompts / router_prompts are touched.

Invocation:
    pytest apps/tutoring/v2/tests/test_move_router_live.py -m live_llm

Optional model override (Anthropic dashboard):
    MOVE_ROUTER_MODEL_OVERRIDE=anthropic/claude-sonnet-4-6 pytest ...
"""

from __future__ import annotations

import os

import pytest

from apps.tutoring.v2.contracts import (
    RouterRequest,
    StudentSafeFeedback,
    Verdict,
)
from apps.tutoring.v2.services.move_router import MoveRouter


pytestmark = pytest.mark.live_llm


# Default model — overridable via ``MOVE_ROUTER_TEST_MODEL`` for
# experiments without touching the seeded ModelConfig row. Match the
# 0038 migration default.
_DEFAULT_MODEL = "claude-sonnet-4-6"


# ──────────────────────────────────────────────────────────────────────
# Gate helpers
# ──────────────────────────────────────────────────────────────────────


def _build_live_anthropic_client():
    """Construct an ``AnthropicClient`` directly from the env, bypassing
    the ``ModelConfig`` DB lookup.

    Test isolation reason: pytest-django spins up a clean test DB and
    the 0038 seed migration gates its insert on the ``Institution.global``
    row existing — which it doesn't in a fresh test DB. Building the
    client from env is simpler and exercises exactly the LLM call the
    scenarios care about.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip(
            "no ANTHROPIC_API_KEY set — live router scenario tests "
            "need real Anthropic credentials"
        )
    # Build an unsaved ``ModelConfig`` instance so ``AnthropicClient``
    # gets the temperature / max_tokens / model-name surface it expects.
    from apps.llm.client import AnthropicClient
    from apps.llm.models import ModelConfig

    model_name = os.getenv("MOVE_ROUTER_TEST_MODEL", _DEFAULT_MODEL)
    cfg = ModelConfig(
        name="live-router-test",
        provider=ModelConfig.Provider.ANTHROPIC,
        model_name=model_name,
        api_key_env_var="ANTHROPIC_API_KEY",
        api_key_encrypted="",
        max_tokens=900,
        temperature=0.0,
        purpose=ModelConfig.Purpose.MOVE_ROUTER,
        is_active=True,
    )
    return AnthropicClient(cfg)


def _route(request: RouterRequest):
    """Call the router once against a real Anthropic client."""
    client = _build_live_anthropic_client()
    router = MoveRouter(router_client_factory=lambda: client)
    return router.route(request)


def _effective_move_for_verdict(decision, verdict_value: str) -> str:
    """Resolve the move the engine would pick for a given grader verdict.

    Mirrors ``TutorEngine._resolve_move``: non-answer-attempt → use
    ``decision.move``; answer-attempt → look up the matching row in
    ``decision.moves_by_verdict``.
    """
    if decision.verdict_needed:
        return (decision.moves_by_verdict or {}).get(verdict_value, "")
    return decision.move or ""


# ──────────────────────────────────────────────────────────────────────
# GEO-S5 P1-3 — help-request misroute
# ──────────────────────────────────────────────────────────────────────


def test_router_live_geo_p1_3_help_request_picks_teaching_move():
    """The student said "i dont understand. what is condensation"
    after two wrong MCQ attempts. The router must NOT pick a
    retrieval-style move; pick ``explain`` (define the term) or
    ``worked_example`` (walk the step).

    Reference: test-reports/GEO-S5-evaluation-2026-05-27-run7.md P1-3.
    """
    request = RouterRequest(
        last_n_turns=[
            {"role": "assistant", "content": (
                "Welcome to the water cycle lesson. Look at the diagram "
                "below — what happens to water from the ocean when the "
                "sun heats it up?"
            )},
            {"role": "student", "content": "i dunno. maybe it goes back into the sea?"},
            {"role": "assistant", "content": (
                "Let me set this up. Here's a multiple-choice question: "
                "A. evaporation  B. condensation  C. precipitation  "
                "D. runoff. Which step turns liquid water into vapor?"
            )},
            {"role": "student", "content": "A"},
            {"role": "assistant", "content": (
                "Not quite — let's slow this down and work through it "
                "one step at a time."
            )},
            {"role": "student", "content": (
                "i think C precipitation because rain is water droplets"
            )},
            {"role": "assistant", "content": (
                "Let's slow this down and work through it one step at a time. "
                "Here's the question again, in plain words: …"
            )},
            {"role": "student", "content": "i dont understand. what is condensation"},
        ],
        student_input="i dont understand. what is condensation",
        grader_verdict=None,  # help-request — engine skipped grading
        grader_reason_code=None,
        student_safe_feedback=StudentSafeFeedback(),
        profile_summary="",
        objective="Identify the steps of the water cycle",
        lesson_title="The Water Cycle",
        lesson_subject="geography",
        move_history=["explain", "scaffold_hint", "scaffold_hint"],
        objective_correct=0,
        objective_wrong=2,
        objective_attempts=2,
        turns_in_session=4,
        turns_on_current_objective=4,
        verdictless_turns=1,
        attempts_on_open_question=2,
        open_question_stem=(
            "Which step of the water cycle turns liquid water into vapor?"
        ),
        open_question_has_pending=True,
        pose_tool_available=True,
    )
    decision = _route(request)
    # Help-request → non-answer-attempt shape; decision.move is set.
    assert decision.move in {"explain", "worked_example"}, (
        f"help-request must route to a teaching move; got "
        f"{decision.move!r} (decision={decision!r})"
    )


# ──────────────────────────────────────────────────────────────────────
# MATHS-S1 P1-1 — resume frame after correct streak
# ──────────────────────────────────────────────────────────────────────


def test_router_live_maths_p1_1_resume_after_correct_streak():
    """Student delivered three fully-shown correct Pythagoras answers
    on resume. The router must NOT pick ``explain`` (re-emitting the
    Fishing Port engage paragraph the student already saw); acceptable
    picks are ``confirm_and_extend``, ``confirm_and_advance``, or
    ``close_topic`` — anything that acknowledges the demonstrated
    mastery and moves the session forward.

    Reference: test-reports/MATHS-S1-evaluation-2026-05-27-run7.md P1-1.
    """
    request = RouterRequest(
        last_n_turns=[
            {"role": "assistant", "content": (
                "Imagine you're building a boat ramp at the Seychelles "
                "Fishing Port. A triangle has sides 3, 4, 5 — is it "
                "right-angled?"
            )},
            {"role": "student", "content": (
                "3²+4²=25 and 5²=25 so yes it's right-angled."
            )},
            {"role": "assistant", "content": (
                "Yes — you applied the converse cleanly. Try the next "
                "one: a triangle with sides 9, 12, 15."
            )},
            {"role": "student", "content": (
                "9²+12²=81+144=225 and 15²=225 so yes."
            )},
            {"role": "assistant", "content": (
                "Right — same pattern. How about sides 5, 7, 9?"
            )},
            {"role": "student", "content": (
                "No. Longest side is 9, so test 5²+7² = 25+49 = 74, "
                "but 9²=81. Since 74≠81, the triangle is NOT right-angled."
            )},
            # ← session resumes here, student's latest fully-shown
            # correct answer on the open question
            {"role": "student", "content": (
                "c=13, a=5, b=12. a²+b² = 25+144 = 169. c² = 169. "
                "Since 169=169, the triangle IS right-angled."
            )},
        ],
        student_input=(
            "c=13, a=5, b=12. a²+b² = 25+144 = 169. c² = 169. "
            "Since 169=169, the triangle IS right-angled."
        ),
        grader_verdict=Verdict.CORRECT,
        grader_reason_code=None,
        student_safe_feedback=StudentSafeFeedback(
            what_right="your working applies the converse cleanly",
        ),
        profile_summary="",
        objective="Apply the converse of Pythagoras' theorem",
        lesson_title="Pythagoras' Theorem — Right-angled Triangles",
        lesson_subject="mathematics",
        move_history=[
            "explain", "confirm_and_extend", "confirm_and_extend",
            "scaffold_hint", "confirm_and_extend",
        ],
        objective_correct=3,
        objective_wrong=0,
        objective_attempts=3,
        turns_in_session=4,
        turns_on_current_objective=4,
        verdictless_turns=0,
        attempts_on_open_question=1,
        open_question_stem=(
            "A triangle has sides 5, 12, and 13. Is it right-angled?"
        ),
        open_question_has_pending=True,
        pose_tool_available=True,
    )
    decision = _route(request)
    effective = _effective_move_for_verdict(decision, "correct")
    assert effective in {
        "confirm_and_extend", "confirm_and_advance", "close_topic",
    }, (
        f"resume after correct streak must NOT re-explain; effective "
        f"correct-move={effective!r} (decision={decision!r})"
    )


# ──────────────────────────────────────────────────────────────────────
# MATHS-S1 P1-4 — atomic close+advance on objective evidence
# ──────────────────────────────────────────────────────────────────────


def test_router_live_maths_p1_4_objective_evidence_close():
    """After 3 correct attempts on the same objective, the router
    should pick ``close_topic`` directly. Safety floor #2 (objective
    evidence saturation) would force-close otherwise — agreeing with
    the floor produces a cleaner trace.

    Reference: test-reports/MATHS-S1-evaluation-2026-05-27-run7.md P1-4.
    """
    request = RouterRequest(
        last_n_turns=[
            {"role": "assistant", "content": (
                "A triangle has sides 3, 4, 5 — is it right-angled?"
            )},
            {"role": "student", "content": "Yes — 9+16=25=5²."},
            {"role": "assistant", "content": (
                "Right — you applied the converse cleanly. Try 9, 12, 15."
            )},
            {"role": "student", "content": "Yes — 81+144=225=15²."},
            {"role": "assistant", "content": (
                "Same pattern. How about 6, 8, 10?"
            )},
            {"role": "student", "content": "Yes — 36+64=100=10²."},
        ],
        student_input="Yes — 36+64=100=10².",
        grader_verdict=Verdict.CORRECT,
        grader_reason_code=None,
        student_safe_feedback=StudentSafeFeedback(
            what_right="you applied the converse correctly",
        ),
        profile_summary="",
        objective="Apply the converse of Pythagoras' theorem",
        lesson_title="Pythagoras' Theorem — Right-angled Triangles",
        lesson_subject="mathematics",
        move_history=[
            "explain", "confirm_and_extend", "confirm_and_extend",
        ],
        objective_correct=3,
        objective_wrong=0,
        objective_attempts=3,
        turns_in_session=6,
        turns_on_current_objective=6,
        verdictless_turns=0,
        attempts_on_open_question=1,
        open_question_stem=(
            "A triangle has sides 6, 8, 10. Is it right-angled?"
        ),
        open_question_has_pending=True,
        pose_tool_available=True,
    )
    decision = _route(request)
    effective = _effective_move_for_verdict(decision, "correct")
    assert effective in {
        "close_topic", "confirm_and_extend", "confirm_and_advance",
    }, (
        f"objective evidence saturated — router must pick close/extend; "
        f"effective correct-move={effective!r} "
        f"(decision={decision!r})"
    )


# ──────────────────────────────────────────────────────────────────────
# GEO-S5 P1-4 — self-reported guess must NOT advance
# ──────────────────────────────────────────────────────────────────────


def test_router_live_geo_p1_4_self_reported_guess_does_not_extend():
    """Student said "guess B" — grader returned CORRECT with
    reason_code=self_reported_guess. A guessed-correct answer is NOT
    mastery evidence. The router must NOT pick ``confirm_and_extend``
    (which advances at increased difficulty); acceptable:
    ``confirm_and_advance`` (re-pose same difficulty) or
    ``scaffold_hint`` (verify understanding before advancing).

    Reference: test-reports/GEO-S5-evaluation-2026-05-27-run7.md P1-4.
    """
    request = RouterRequest(
        last_n_turns=[
            {"role": "assistant", "content": (
                "Now a multiple-choice: which step is condensation? "
                "A. evaporation  B. condensation  C. precipitation  "
                "D. runoff"
            )},
            {"role": "student", "content": "guess B"},
        ],
        student_input="guess B",
        grader_verdict=Verdict.CORRECT,
        grader_reason_code="self_reported_guess",
        student_safe_feedback=StudentSafeFeedback(
            what_right="you picked the right letter",
        ),
        profile_summary="",
        objective="Identify the steps of the water cycle",
        lesson_title="The Water Cycle",
        lesson_subject="geography",
        move_history=["explain", "scaffold_hint"],
        objective_correct=1,  # this guess counts as a correct
        objective_wrong=0,
        objective_attempts=1,
        turns_in_session=4,
        turns_on_current_objective=4,
        verdictless_turns=0,
        attempts_on_open_question=1,
        open_question_stem="Which step of the water cycle is condensation?",
        open_question_has_pending=True,
        pose_tool_available=True,
    )
    decision = _route(request)
    effective = _effective_move_for_verdict(decision, "correct")
    assert effective != "confirm_and_extend", (
        f"self-reported guess MUST NOT advance via confirm_and_extend; "
        f"effective correct-move={effective!r} (decision={decision!r})"
    )
    assert effective in {
        "confirm_and_advance", "scaffold_hint", "worked_example",
        "explain",
    }, (
        f"unexpected move for self-reported guess: "
        f"effective correct-move={effective!r} (decision={decision!r})"
    )
