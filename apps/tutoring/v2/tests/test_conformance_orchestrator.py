"""Conformance orchestrator integration tests — Phase 2 §Tests.

Exercises ``ConformanceCheck.run()`` end-to-end with a fake
classifier client, covering the deterministic-gate short-circuit
paths AND the matrix-driven rejection paths.
"""

from __future__ import annotations

import json

import pytest

from apps.tutoring.v2.contracts import (
    GradingResult,
    OpenQuestion,
    QuestionSource,
    SessionRuntimeState,
    StudentSafeFeedback,
    Verdict,
)
from apps.tutoring.v2.services.conformance import (
    ConformanceCheck,
    ConformanceResult,
)


class _FakeResp:
    def __init__(self, content): self.content = content; self.tokens_in = 0; self.tokens_out = 0


class _FakeClient:
    def __init__(self, payload): self.payload = payload
    def generate(self, **kw): return _FakeResp(self.payload)


def _clean_labels_json(**overrides) -> str:
    """All-False default labels with optional overrides."""
    base = {
        "affirms_correctness": False,
        "refutes_correctness": False,
        "surfaces_uncertainty": False,
        "contains_assessment_question_in_prose": False,
        "hands_floor_back_or_transitions": True,  # default to passes-handback
        "contains_partial_feedback_shape": False,
        "contains_factual_claim": False,
        "contains_arithmetic_claim": False,
        "student_claim_present": False,
    }
    base.update(overrides)
    return json.dumps(base)


def _state_with_open_q() -> SessionRuntimeState:
    return SessionRuntimeState(
        open_question=OpenQuestion(
            source=QuestionSource.LESSON_STEP, id=1,
            rendered_stem="What is 12+13?",
        ),
    )


def _verdict(kind, **kw) -> GradingResult:
    return GradingResult(verdict=kind, **kw)


# ──────────────────────────────────────────────────────────────────────
# Deterministic gate short-circuits
# ──────────────────────────────────────────────────────────────────────


def test_state_coherence_fails_on_bad_move():
    cc = ConformanceCheck()
    res = cc.run(
        candidate_response="anything",
        verdict=None,
        runtime_state=_state_with_open_q(),
        selected_move="not_a_real_move",
        open_question_stem="What is 12+13?",
    )
    assert res.passed is False
    assert any("state_coherence" in v for v in res.violations)


def test_state_coherence_fails_on_verdict_without_open_question():
    cc = ConformanceCheck()
    res = cc.run(
        candidate_response="anything",
        verdict=_verdict(Verdict.CORRECT),
        runtime_state=SessionRuntimeState(),  # NO open question
        selected_move="confirm_and_advance",
    )
    assert res.passed is False
    assert any("state_coherence" in v for v in res.violations)


def test_rule_check_rejects_authored_number():
    """The candidate introduces a number not in the bank or
    transcript → rule_check fails."""
    cc = ConformanceCheck(
        classifier_client_factory=lambda: _FakeClient(_clean_labels_json()),
    )
    res = cc.run(
        candidate_response="Great — the answer is 47 right?",
        verdict=None,
        runtime_state=SessionRuntimeState(),
        selected_move="scaffold_hint",
        open_question_stem="",
        bank_stems=["What is 12+13?"],
        recent_student_turns=[],
    )
    assert res.passed is False
    assert any("rule_check" in v for v in res.violations)


def test_praise_filter_rejects_bare_praise_under_wrong():
    cc = ConformanceCheck(
        classifier_client_factory=lambda: _FakeClient(_clean_labels_json()),
    )
    res = cc.run(
        candidate_response="Correct! Try again.",
        verdict=_verdict(
            Verdict.WRONG,
            student_safe_feedback=StudentSafeFeedback(
                first_misconception_redacted="slip",
            ),
        ),
        runtime_state=_state_with_open_q(),
        selected_move="scaffold_hint",
        open_question_stem="What is 12+13?",
        bank_stems=["What is 12+13?"],
    )
    assert res.passed is False
    assert any("praise_filter" in v for v in res.violations)


# ──────────────────────────────────────────────────────────────────────
# Classifier + matrix paths
# ──────────────────────────────────────────────────────────────────────


def test_clean_candidate_passes_under_correct_verdict():
    fake = _FakeClient(_clean_labels_json(affirms_correctness=True))
    cc = ConformanceCheck(classifier_client_factory=lambda: fake)
    res = cc.run(
        candidate_response="Yes — that lands at 25. Try the next one together.",
        verdict=_verdict(Verdict.CORRECT, private_canonical="25"),
        runtime_state=_state_with_open_q(),
        selected_move="confirm_and_advance",
        open_question_stem="What is 12+13?",
        bank_stems=["What is 12+13?"],
        recent_student_turns=["25"],
    )
    assert res.passed is True
    assert res.violations == []
    assert res.labels is not None
    assert res.labels.affirms_correctness is True


def test_classifier_unavailable_returns_conservative_default():
    """Classifier failure → all-False labels → matrix flags multiple
    missing-required-True rules → fails."""
    class _RaisingClient:
        def generate(self, **kw):
            raise RuntimeError("simulated classifier outage")
    cc = ConformanceCheck(classifier_client_factory=lambda: _RaisingClient())
    res = cc.run(
        candidate_response="ok",
        verdict=_verdict(Verdict.CORRECT, private_canonical="25"),
        runtime_state=_state_with_open_q(),
        selected_move="confirm_and_advance",
        open_question_stem="What is 12+13?",
        bank_stems=["What is 12+13?"],
        recent_student_turns=["25"],
    )
    # Conservative default has hands_floor_back_or_transitions=False
    # → matrix flags missing handback under verdict=correct.
    assert res.passed is False


# ──────────────────────────────────────────────────────────────────────
# Tutor-claim adjudication routing
# ──────────────────────────────────────────────────────────────────────


class _FakeGrader:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = 0

    def adjudicate_tutor_claim(self, ctx, claim, sources=None):
        self.calls += 1
        return {"status": self.status, "citation": ""}


def test_factual_claim_routes_to_grader_and_rejects_on_contradicted():
    fake_labels = _FakeClient(_clean_labels_json(
        affirms_correctness=True, contains_factual_claim=True,
    ))
    fake_grader = _FakeGrader(status="contradicted")
    cc = ConformanceCheck(
        grader=fake_grader,
        classifier_client_factory=lambda: fake_labels,
    )
    res = cc.run(
        candidate_response="Yes — actually photosynthesis happens in mitochondria.",
        verdict=_verdict(Verdict.CORRECT, private_canonical="25"),
        runtime_state=_state_with_open_q(),
        selected_move="confirm_and_advance",
        open_question_stem="What is 12+13?",
        bank_stems=["What is 12+13?"],
        recent_student_turns=["25"],
        context=object(),  # any non-None
    )
    assert res.passed is False
    assert fake_grader.calls == 1
    assert any("tutor_claim_contradicted" in v for v in res.violations)


def test_factual_claim_routes_and_rejects_on_unverified():
    fake_labels = _FakeClient(_clean_labels_json(
        affirms_correctness=True, contains_factual_claim=True,
    ))
    fake_grader = _FakeGrader(status="unverified")
    cc = ConformanceCheck(
        grader=fake_grader,
        classifier_client_factory=lambda: fake_labels,
    )
    res = cc.run(
        candidate_response="Yes — and definitely cars run on yelling at them.",
        verdict=_verdict(Verdict.CORRECT, private_canonical="25"),
        runtime_state=_state_with_open_q(),
        selected_move="confirm_and_advance",
        open_question_stem="What is 12+13?",
        bank_stems=["What is 12+13?"],
        recent_student_turns=["25"],
        context=object(),
    )
    assert res.passed is False
    assert any("tutor_claim_unverified" in v for v in res.violations)


def test_factual_claim_supported_passes():
    fake_labels = _FakeClient(_clean_labels_json(
        affirms_correctness=True, contains_factual_claim=True,
    ))
    fake_grader = _FakeGrader(status="supported")
    cc = ConformanceCheck(
        grader=fake_grader,
        classifier_client_factory=lambda: fake_labels,
    )
    res = cc.run(
        candidate_response="Yes — that lands at 25. Let's try the next one together.",
        verdict=_verdict(Verdict.CORRECT, private_canonical="25"),
        runtime_state=_state_with_open_q(),
        selected_move="confirm_and_advance",
        open_question_stem="What is 12+13?",
        bank_stems=["What is 12+13?"],
        recent_student_turns=["25"],
        context=object(),
    )
    assert res.passed is True


def test_no_claim_does_not_call_grader():
    fake_labels = _FakeClient(_clean_labels_json(
        affirms_correctness=True,
    ))
    fake_grader = _FakeGrader(status="supported")
    cc = ConformanceCheck(
        grader=fake_grader,
        classifier_client_factory=lambda: fake_labels,
    )
    cc.run(
        candidate_response="Yes — try the next one.",
        verdict=_verdict(Verdict.CORRECT, private_canonical="25"),
        runtime_state=_state_with_open_q(),
        selected_move="confirm_and_advance",
        open_question_stem="What is 12+13?",
        bank_stems=["What is 12+13?"],
        recent_student_turns=["25"],
        context=object(),
    )
    assert fake_grader.calls == 0


# ──────────────────────────────────────────────────────────────────────
# figure_ref skip on empty-catalog tool-posed turns
# ──────────────────────────────────────────────────────────────────────


def test_figure_ref_skipped_when_bank_stem_deictic_and_no_media():
    """Bank stem mentions 'the diagram' but lesson has no media —
    the deictic is curriculum-authored, not LLM-authored, so the
    figure_ref gate must skip. Reproduces the GEO-S5 regression at
    L1459 where step 4 ('Tributary X flows ... on the diagram')
    blocked every tool-posed turn."""
    fake = _FakeClient(_clean_labels_json())
    cc = ConformanceCheck(classifier_client_factory=lambda: fake)
    res = cc.run(
        candidate_response=(
            "Try this:\n\nIn a river system shown on the diagram, "
            "classify each tributary by order."
        ),
        verdict=None,
        runtime_state=_state_with_open_q(),
        selected_move="scaffold_hint",
        open_question_stem="In a river system ...",
        bank_stems=["In a river system shown on the diagram"],
        recent_student_turns=[],
        posed_via_tool=True,
        lesson_has_media=False,
    )
    assert res.passed is True
    assert not any("figure_ref" in v for v in res.violations)


def test_figure_ref_still_fires_when_lesson_has_media_but_none_attached():
    """When the lesson DOES have media available (just not attached
    this turn), the gate still fires — the curriculum exists, the LLM
    forgot to attach it."""
    cc = ConformanceCheck()
    res = cc.run(
        candidate_response=(
            "Try this:\n\nIn a river system shown on the diagram, "
            "classify each tributary."
        ),
        verdict=None,
        runtime_state=_state_with_open_q(),
        selected_move="scaffold_hint",
        open_question_stem="What's shown?",
        attached_media_count=0,
        posed_via_tool=True,
        lesson_has_media=True,
    )
    assert res.passed is False
    assert any("figure_ref" in v for v in res.violations)


def test_figure_ref_still_fires_for_llm_authored_deictic():
    """When the candidate was NOT posed via the tool (LLM authored
    the deictic in prose), the gate fires even on a media-less lesson
    — keeps the LLM honest."""
    cc = ConformanceCheck()
    res = cc.run(
        candidate_response="Have a look at the diagram and tell me what you see.",
        verdict=None,
        runtime_state=_state_with_open_q(),
        selected_move="explain",
        open_question_stem="",
        attached_media_count=0,
        posed_via_tool=False,
        lesson_has_media=False,
    )
    assert res.passed is False
    assert any("figure_ref" in v for v in res.violations)


# ──────────────────────────────────────────────────────────────────────
# Result shape
# ──────────────────────────────────────────────────────────────────────


def test_conformance_result_default_shape():
    r = ConformanceResult(passed=True)
    assert r.violations == []
    assert r.labels is None
    assert r.retry_used is False
    assert r.fallback_used is False
