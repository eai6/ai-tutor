"""StudentGrader — central correctness service for the v2 engine.

Three closely-related responsibilities (refactor-analysis §3):

  1. **Student-answer grading.** Math path uses ``MathVerificationTool``
     (LLM-emitted DSL validated against the visible problem text, then
     Python-executed). Non-math path is tiered: deterministic
     ``bank_grader`` first when a canonical exists, KB-grounded
     adjudication for curriculum content, Gemini Google-grounding for
     general world knowledge. ``unverified`` is a first-class verdict
     and the conservative escape valve.

  2. **Pre-pose check.** Enforces the student-visible derivability
     invariant — the canonical must be derivable from the visible
     prompt + attached figure + recent transcript, with hidden KB
     chunks suppressed. Returns a signed single-use ``pre_pose_token``
     (Phase 1 §4.2) on pass.

  3. **Tutor-claim adjudication.** Same grounded machinery as the
     non-math grading path, applied to factual / arithmetic claims
     surfaced by the conformance classifier (Phase 2 §2.4).

Output redaction is enforced here: the grader emits
``private_canonical`` (never reaches ``StudentTutor`` on wrong /
partial moves) AND ``student_safe_feedback`` (rubric fields safe to
template). The conformance layer enforces that the move prompt
template has no slot named ``canonical_answer`` for those moves.

Span emission (Phase 3 §3.3, owned by Phase 2): every LLM call inside
this module already emits an ``llm_call`` span via
``BaseLLMClient.generate``; the grader adds its own ``grader.*`` spans
so the per-stage breakdown is complete.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    GradingRequest,
    GradingResult,
    OpenQuestion,
    PendingPose,
    QuestionRef,
    QuestionSource,
    StudentSafeFeedback,
    TutoringContext,
    Verdict,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.services.bare_answer import is_bare_answer
from apps.tutoring.v2.services.grader_prompts import (
    ANSWER_CONSISTENCY_VERIFIER_SYSTEM,
    MATH_DSL_SYSTEM,
    NON_MATH_GROUNDED_SYSTEM,
    PRE_POSE_SYSTEM,
    TUTOR_CLAIM_SYSTEM,
    render_answer_consistency_user_prompt,
    render_math_dsl_user_prompt,
    render_non_math_grounded_user_prompt,
    render_pre_pose_user_prompt,
    render_tutor_claim_user_prompt,
)
from apps.tutoring.v2.tools.math_verification import (
    MathVerificationTool,
    values_equivalent,
)
from apps.tutoring.v2.tools.token_cache import token_cache

logger = logging.getLogger(__name__)


# Below this confidence the grounded path consults the answer-consistency
# verifier (Phase 4) instead of unconditionally downgrading to UNVERIFIED.
# The verifier is a Haiku-backed yes/no/cant-tell adjudicator that asks
# only "does the student's response assert the same answer as the
# canonical?". It generalises across subjects (math proof, geography
# reasoning, definition) and breaks the unverified-trap loop that prior
# runs surfaced on rich natural-language proofs.
# See memory/v2_unverified_trap_redesign.md §Fix 1.
_GROUNDED_CONFIDENCE_THRESHOLD = 0.5


@dataclass
class _DSLExtraction:
    program: Optional[dict]
    raw_text: str
    error: Optional[str] = None


@dataclass
class _GroundedAdjudication:
    verdict: Verdict
    private_canonical: str
    what_right: str
    what_missing: str
    first_misconception: str
    citation: str
    confidence: float
    reasoning: str = ""


@dataclass
class _ConsistencyResult:
    """Output of the Phase 4 answer-consistency verifier.

    ``confirmed`` is one of "yes" | "partial" | "no" | "cant_tell".
    The grader maps these to final Verdict values when the grounded
    adjudicator's confidence is below the threshold.
    """
    confirmed: str  # "yes" | "partial" | "no" | "cant_tell"
    why: str
    available: bool  # False when the verifier client could not be reached


class StudentGrader:
    """Stateless central grader. Constructed per-turn."""

    def __init__(
        self,
        *,
        math_client_factory=None,
        grounded_client_factory=None,
        claim_client_factory=None,
        verifier_client_factory=None,
        math_verification_tool: Optional[MathVerificationTool] = None,
    ) -> None:
        """Optional injection seams for tests.

        Each ``*_client_factory`` returns a ``BaseLLMClient``-shaped
        object on demand. When ``None``, the grader resolves the
        ``ModelConfig`` for the appropriate purpose at call time.
        """
        self._math_client_factory = math_client_factory
        self._grounded_client_factory = grounded_client_factory
        self._claim_client_factory = claim_client_factory
        self._verifier_client_factory = verifier_client_factory
        self._math_verification_tool = math_verification_tool or MathVerificationTool()

    # ==================================================================
    # 1. Student-answer grading — entry point
    # ==================================================================

    def grade_student_response(
        self,
        context: TutoringContext,
        request: GradingRequest,
    ) -> GradingResult:
        """Route to math / non-math path based on ``request.is_math``."""
        if request.is_math:
            return self._grade_math(context, request)
        return self._grade_non_math(context, request)

    # ------------------------------------------------------------------
    # Math path (Phase 2 §2.1 — math)
    # ------------------------------------------------------------------

    def _grade_math(
        self,
        context: TutoringContext,
        request: GradingRequest,
    ) -> GradingResult:
        """LLM → DSL → MathVerificationTool → comparator."""
        with emit_span("audit", "grader.math") as span:
            bare = is_bare_answer(request.student_input)
            problem_text = request.open_question.rendered_stem or ""

            # 1. Extract the DSL from the LLM.
            extraction = self._extract_math_dsl(problem_text)
            if extraction.program is None:
                # Phase 4 — math DSL extraction failed (often because
                # the problem is a prose proof, definition, or
                # explain-and-justify question rather than a compute-
                # this-value problem). Fall through to the grounded /
                # verifier path so a correct natural-language proof
                # can be confirmed. Subject-agnostic.
                if span is not None:
                    span["payload"] = {
                        "verdict": "deferred",
                        "stage": "dsl_extraction",
                        "fallthrough": "grounded",
                        "error": extraction.error or "",
                    }
                return self._grade_non_math(context, request)

            # 2. Validate + execute the DSL.
            result = self._math_verification_tool.evaluate(
                problem_text, extraction.program,
            )
            if not result.ok:
                # Same fallthrough rationale as above — DSL extracted
                # but failed to validate / execute (e.g. references a
                # variable the problem text doesn't name).
                if span is not None:
                    span["payload"] = {
                        "verdict": "deferred",
                        "stage": "dsl_validation",
                        "fallthrough": "grounded",
                        "error": result.error or "",
                    }
                return self._grade_non_math(context, request)

            canonical_value = result.canonical_value
            canonical_str = _format_canonical(canonical_value)

            # 3. Parse the student's value via the existing
            #    ast-based working analyzer (R9).
            student_value, student_value_str = _parse_student_math_value(
                request.student_input
            )

            # 4. Comparator.
            if student_value is None:
                # The math DSL produced a canonical value but we could
                # not extract a numeric from the student's prose. Fall
                # through to the grounded / verifier path so a correct
                # answer expressed in words ("the triangle is right-
                # angled because 169 = 169") can be confirmed against
                # the canonical's substance.
                if span is not None:
                    span["payload"] = {
                        "verdict": "deferred",
                        "stage": "student_value_parse",
                        "fallthrough": "grounded",
                    }
                return self._grade_non_math(context, request)

            # 4. Comparator. Multi-slot questions: when the canonical is
            # a list of {name, value} entries, accept the student's
            # single value if it matches ANY slot (verdict=PARTIAL,
            # what_right names the slot). All slots matched →
            # verdict=CORRECT. No slot matched → verdict=WRONG.
            is_multi = isinstance(canonical_value, list) and canonical_value and \
                       all(isinstance(e, dict) and "value" in e for e in canonical_value)
            if is_multi:
                matched_slots = [
                    e for e in canonical_value
                    if values_equivalent(e.get("value"), student_value)
                ]
                if len(matched_slots) == len(canonical_value):
                    verdict_kind = Verdict.CORRECT
                elif matched_slots:
                    verdict_kind = Verdict.PARTIAL
                else:
                    verdict_kind = Verdict.WRONG
                safe = self._build_math_safe_feedback_multi(
                    verdict_kind=verdict_kind,
                    canonical_slots=canonical_value,
                    matched_slots=matched_slots,
                    student_value=student_value,
                    bare_answer=bare,
                )
                if span is not None:
                    span["payload"] = {
                        "verdict": verdict_kind.value,
                        "bare_answer": bare,
                        "multi_slot": True,
                        "matched_slot_count": len(matched_slots),
                        "total_slot_count": len(canonical_value),
                    }
                return GradingResult(
                    verdict=verdict_kind,
                    private_canonical=canonical_str,
                    student_safe_feedback=safe,
                    student_value=student_value_str,
                    reasoning="math: executed multi-slot DSL + comparator",
                    bare_answer=bare,
                )

            # Single-slot path (original behaviour).
            equivalent = values_equivalent(canonical_value, student_value)
            verdict_kind = Verdict.CORRECT if equivalent else Verdict.WRONG

            # 5. Build redacted student-safe feedback.
            safe = self._build_math_safe_feedback(
                verdict_kind=verdict_kind,
                canonical_value=canonical_value,
                student_value=student_value,
                bare_answer=bare,
            )

            if span is not None:
                span["payload"] = {
                    "verdict": verdict_kind.value,
                    "bare_answer": bare,
                }

            return GradingResult(
                verdict=verdict_kind,
                private_canonical=canonical_str,
                student_safe_feedback=safe,
                student_value=student_value_str,
                reasoning="math: executed DSL + comparator",
                bare_answer=bare,
            )

    def _extract_math_dsl(self, problem_text: str) -> _DSLExtraction:
        """Call ``GRADER_MATH`` to produce a JSON DSL for the problem."""
        client = self._resolve_math_client()
        if client is None:
            return _DSLExtraction(
                program=None,
                raw_text="",
                error="no GRADER_MATH client available",
            )
        try:
            response = client.generate(
                messages=[
                    {"role": "user", "content": render_math_dsl_user_prompt(problem_text)},
                ],
                system_prompt=MATH_DSL_SYSTEM,
                max_tokens=600,
            )
            raw_text = response.content or ""
            program = _safe_json_loads(raw_text)
            if not isinstance(program, dict):
                return _DSLExtraction(
                    program=None, raw_text=raw_text,
                    error="DSL extraction did not return a JSON object",
                )
            return _DSLExtraction(program=program, raw_text=raw_text)
        except Exception as exc:
            return _DSLExtraction(
                program=None, raw_text="",
                error=f"DSL extraction raised: {type(exc).__name__}",
            )

    def _build_math_safe_feedback(
        self,
        *,
        verdict_kind: Verdict,
        canonical_value: Any,
        student_value: Any,
        bare_answer: bool,
    ) -> StudentSafeFeedback:
        """Render rubric-shaped feedback that never leaks the canonical."""
        if verdict_kind == Verdict.CORRECT:
            if bare_answer:
                return StudentSafeFeedback(what_right="you have the value")
            return StudentSafeFeedback(
                what_right="your working lands on the right value",
            )
        # Wrong — pick a redacted misconception hint that points at the
        # method without revealing the answer.
        try:
            if student_value is not None and canonical_value is not None:
                if abs(float(student_value)) > 1e-9 and abs(float(canonical_value)) > 1e-9:
                    ratio = abs(float(student_value)) / abs(float(canonical_value))
                    if 1.9 <= ratio <= 2.1 or 0.45 <= ratio <= 0.55:
                        return StudentSafeFeedback(
                            first_misconception_redacted=(
                                "the magnitude is off by a factor of two — "
                                "check whether you've doubled or halved a step"
                            ),
                        )
        except (TypeError, ValueError):
            pass
        return StudentSafeFeedback(
            first_misconception_redacted=(
                "the working ends at the wrong value — re-check the operation "
                "you applied"
            ),
        )

    def _build_math_safe_feedback_multi(
        self,
        *,
        verdict_kind: Verdict,
        canonical_slots: list[dict[str, Any]],
        matched_slots: list[dict[str, Any]],
        student_value: Any,
        bare_answer: bool,
    ) -> StudentSafeFeedback:
        """Redacted feedback for multi-slot question grading.

        CORRECT: student supplied all slot values.
        PARTIAL: matched some slots — name them, prompt for the rest.
        WRONG:   no slot matched — generic operation-check hint.

        Slot names come from the DSL (loss_amount, loss_percentage,
        area, perimeter, …). Phrased generically — no leak of the
        underlying canonicals.
        """
        if verdict_kind == Verdict.CORRECT:
            return StudentSafeFeedback(
                what_right="you have all the values asked for",
            )
        if verdict_kind == Verdict.PARTIAL:
            matched_names = [_humanise_slot(s.get("name", "")) for s in matched_slots]
            remaining = [
                _humanise_slot(s.get("name", ""))
                for s in canonical_slots
                if s not in matched_slots
            ]
            what_right = (
                f"you have {_join_names(matched_names)} right"
                if matched_names else "you've made a start"
            )
            what_missing = (
                f"still need {_join_names(remaining)}"
                if remaining else "still one piece to add"
            )
            return StudentSafeFeedback(
                what_right=what_right,
                what_missing=what_missing,
            )
        # WRONG — no slot matched.
        return StudentSafeFeedback(
            first_misconception_redacted=(
                "the value doesn't line up with any of the quantities asked "
                "for — check which operation you applied and which slot "
                "you're answering"
            ),
        )

    # ------------------------------------------------------------------
    # Non-math path (Phase 2 §2.1 — non-math, tiered)
    # ------------------------------------------------------------------

    def _grade_non_math(
        self,
        context: TutoringContext,
        request: GradingRequest,
    ) -> GradingResult:
        """Tier order: deterministic direct match → bank → KB-grounded."""
        with emit_span("audit", "grader.grounded") as span:
            # Tier 0 — answer_type-aware direct match (LessonStep only).
            # ``bank_grader`` assumes ``question_type`` exists and
            # defaults to ``"mcq"`` when absent; LessonStep rows have
            # ``answer_type`` instead and the default-to-mcq path
            # collapses every short-numeric / true-false / free-text
            # step to UNVERIFIED. This tier catches the common shapes
            # cheaply before ever touching the LLM.
            direct = self._try_direct_step_match(request)
            if direct is not None:
                if span is not None:
                    span["payload"] = {
                        "verdict": direct.verdict.value,
                        "tier": "direct",
                    }
                return direct

            # Tier 1 — deterministic bank grader when a canonical exists.
            bank_result = self._try_bank_grading(request)
            if bank_result is not None:
                if span is not None:
                    span["payload"] = {
                        "verdict": bank_result.verdict.value,
                        "tier": "bank",
                    }
                return bank_result

            # Tier 2 — KB-grounded adjudication when KB chunks are present.
            # Tier 3 — Gemini Google-grounding when KB has no answer.
            # Both share the same prompt shape; the difference is which
            # sources the GRADER_GROUNDED client has access to (the
            # ModelConfig is pinned to Gemini for Google-grounding per
            # Phase 1 §7).
            adjudication = self._call_grounded_adjudicator(
                question_stem=request.open_question.rendered_stem or "",
                student_input=request.student_input,
                sources=list(request.kb_chunks or []),
            )

            # Phase 4 — when grounded confidence is low AND the
            # tentative verdict is correct / partial / wrong, consult
            # the answer-consistency verifier instead of blanket-
            # downgrading to UNVERIFIED. The verifier is a Haiku-backed
            # yes/no/cant-tell adjudicator that asks only whether the
            # student's response asserts the same answer as the
            # canonical. It is subject-agnostic.
            verifier_outcome: Optional[_ConsistencyResult] = None
            verdict_kind = adjudication.verdict
            verdict_kind, verifier_outcome = self._maybe_consult_verifier(
                adjudication=adjudication,
                question_stem=request.open_question.rendered_stem or "",
                canonical=adjudication.private_canonical,
                student_input=request.student_input,
            )

            safe = StudentSafeFeedback(
                what_right=adjudication.what_right,
                what_missing=adjudication.what_missing,
                first_misconception_redacted=adjudication.first_misconception,
            )

            if span is not None:
                payload = {
                    "verdict": verdict_kind.value,
                    "tier": "grounded",
                    "confidence": adjudication.confidence,
                }
                if verifier_outcome is not None:
                    payload["verifier"] = {
                        "confirmed": verifier_outcome.confirmed,
                        "why": verifier_outcome.why[:120],
                        "available": verifier_outcome.available,
                    }
                span["payload"] = payload

            reasoning = adjudication.reasoning or "non-math grounded adjudication"
            if verifier_outcome is not None:
                reasoning = (
                    f"{reasoning}; verifier={verifier_outcome.confirmed}"
                    f" ({verifier_outcome.why})"
                )

            return GradingResult(
                verdict=verdict_kind,
                private_canonical=adjudication.private_canonical,
                student_safe_feedback=safe,
                student_value=request.student_input,
                reasoning=reasoning,
                citation=adjudication.citation,
                bare_answer=False,
            )

    def _try_direct_step_match(
        self,
        request: GradingRequest,
    ) -> Optional[GradingResult]:
        """Answer-type-aware direct match for ``LESSON_STEP`` open questions.

        Returns:
          - ``GradingResult`` (``CORRECT`` or ``WRONG``) when the
            student's input cleanly maps to the canonical via the
            step's ``answer_type`` shape.
          - ``None`` when the step is not present, has no canonical,
            uses ``answer_type='free_text'`` / ``'none'`` (which need
            grounded judgment), or the matcher couldn't extract a
            confident answer from the student's input. Falls through
            to ``_try_bank_grading`` / grounded adjudication.

        Does NOT call any LLM — purely deterministic string / regex
        work. Avoids the no-KB-grounding UNVERIFIED trap (the dominant
        verdict regression observed in S1 + S5 evaluation sessions).
        """
        open_q = request.open_question
        if open_q.source != QuestionSource.LESSON_STEP:
            return None
        try:
            from apps.curriculum.models import LessonStep
            step = LessonStep.objects.filter(pk=open_q.id).first()
        except Exception:
            return None
        if step is None:
            return None
        answer_type = (getattr(step, "answer_type", "") or "").strip().lower()
        canonical = (step.expected_answer or "").strip()
        if not canonical:
            return None
        student_raw = (request.student_input or "").strip()
        if not student_raw:
            return None

        matched: Optional[bool] = None
        if answer_type == "multiple_choice":
            matched = _match_mcq_letter(canonical, student_raw)
        elif answer_type == "true_false":
            matched = _match_true_false(canonical, student_raw)
        elif answer_type == "short_numeric":
            matched = _match_short_numeric(canonical, student_raw)
        # free_text + none + unknown answer_types → return None so
        # we fall through to grounded adjudication.

        if matched is None:
            return None

        verdict_kind = Verdict.CORRECT if matched else Verdict.WRONG
        if verdict_kind == Verdict.CORRECT:
            safe = StudentSafeFeedback(what_right="you matched the answer")
        else:
            safe = StudentSafeFeedback(
                first_misconception_redacted=(
                    "that doesn't match the expected answer"
                ),
            )
        return GradingResult(
            verdict=verdict_kind,
            private_canonical=canonical,
            student_safe_feedback=safe,
            student_value=student_raw,
            reasoning=f"direct_step_match:{answer_type}",
            bare_answer=False,
        )

    def _try_bank_grading(
        self,
        request: GradingRequest,
    ) -> Optional[GradingResult]:
        """Run the lifted-forward ``bank_grader`` when a bank row resolves.

        Returns ``None`` when the open question is not bank-resolvable
        (e.g. inline-generated or pre_pose_token) so the caller can
        fall through to grounded adjudication.
        """
        from apps.tutoring.bank_grader import grade_bank_response

        bank_q = _resolve_bank_question(request.open_question)
        if bank_q is None:
            return None
        try:
            res = grade_bank_response(
                bank_q,
                request.student_input,
                is_math=request.is_math,
            )
        except Exception as exc:
            logger.warning(
                "[StudentGrader] bank_grader raised %s — falling back",
                type(exc).__name__,
            )
            return None
        if res.is_correct is None:
            return None
        verdict_kind = Verdict.CORRECT if res.is_correct else Verdict.WRONG
        canonical = str(res.expected) if res.expected is not None else ""
        if verdict_kind == Verdict.CORRECT:
            safe = StudentSafeFeedback(what_right="you matched the answer")
        else:
            safe = StudentSafeFeedback(
                first_misconception_redacted=(
                    "that doesn't match the expected answer"
                ),
            )
        return GradingResult(
            verdict=verdict_kind,
            private_canonical=canonical,
            student_safe_feedback=safe,
            student_value=(
                str(res.student_parsed)
                if res.student_parsed is not None
                else request.student_input
            ),
            reasoning="bank_grader deterministic match",
            bare_answer=False,
        )

    # ------------------------------------------------------------------
    # Phase 4 — Answer-consistency verifier
    # ------------------------------------------------------------------

    def _maybe_consult_verifier(
        self,
        *,
        adjudication: "_GroundedAdjudication",
        question_stem: str,
        canonical: str,
        student_input: str,
    ) -> tuple[Verdict, Optional["_ConsistencyResult"]]:
        """Run the answer-consistency verifier when grounded was unsure.

        Mapping rule:
          - If grounded.confidence >= threshold → keep grounded verdict
            as-is, no verifier call.
          - If grounded.verdict == UNVERIFIED *and* the student input
            looks meta/empty (no answer attempted) → keep UNVERIFIED.
            (The verifier can't decide nothing.)
          - Otherwise consult the verifier:
              confirmed=yes      → CORRECT  (verifier overrides up)
              confirmed=partial  → PARTIAL  (verifier overrides up)
              confirmed=no       → WRONG    (verifier overrides down)
              confirmed=cant_tell → UNVERIFIED (genuine terminal)
              verifier unavailable → fall back to the old behaviour
                                       (downgrade to UNVERIFIED).

        Returns ``(final_verdict, verifier_result_or_None)``. The
        verifier_result is None when no verifier call was made.
        """
        if adjudication.confidence >= _GROUNDED_CONFIDENCE_THRESHOLD:
            return adjudication.verdict, None
        if not (student_input or "").strip():
            return Verdict.UNVERIFIED, None
        outcome = self._call_consistency_verifier(
            question_stem=question_stem,
            canonical=canonical,
            student_input=student_input,
            tentative_verdict=adjudication.verdict.value,
        )
        if not outcome.available:
            # Fail-soft: preserve prior behaviour when the verifier
            # cannot be reached (no GRADER_VERIFIER ModelConfig, model
            # outage). The grounded verdict still downgrades to
            # UNVERIFIED, exactly as before Phase 4.
            return Verdict.UNVERIFIED, outcome
        mapping = {
            "yes": Verdict.CORRECT,
            "partial": Verdict.PARTIAL,
            "no": Verdict.WRONG,
            "cant_tell": Verdict.UNVERIFIED,
        }
        return mapping.get(outcome.confirmed, Verdict.UNVERIFIED), outcome

    def _call_consistency_verifier(
        self,
        *,
        question_stem: str,
        canonical: str,
        student_input: str,
        tentative_verdict: str,
    ) -> "_ConsistencyResult":
        """Haiku-backed verifier — JUDGE-class temperature (0.0).

        Subject-agnostic. Asks only whether the student's response
        asserts the same final answer as the canonical, ignoring
        wording quality and working-shown.
        """
        with emit_span("audit", "grader.answer_consistency_verifier") as span:
            client = self._resolve_verifier_client()
            if client is None:
                if span is not None:
                    span["payload"] = {
                        "confirmed": "unavailable",
                        "reason": "no GRADER_VERIFIER client",
                    }
                return _ConsistencyResult(
                    confirmed="cant_tell",
                    why="verifier client unavailable",
                    available=False,
                )
            try:
                response = client.generate(
                    messages=[
                        {
                            "role": "user",
                            "content": render_answer_consistency_user_prompt(
                                question_stem=question_stem,
                                canonical=canonical,
                                student_input=student_input,
                                tentative_verdict=tentative_verdict,
                            ),
                        },
                    ],
                    system_prompt=ANSWER_CONSISTENCY_VERIFIER_SYSTEM,
                    max_tokens=400,
                )
                payload = _safe_json_loads(response.content or "") or {}
            except Exception as exc:
                logger.warning(
                    "[StudentGrader] answer-consistency verifier raised %s",
                    type(exc).__name__,
                )
                if span is not None:
                    span["payload"] = {
                        "confirmed": "unavailable",
                        "reason": f"raise: {type(exc).__name__}",
                    }
                return _ConsistencyResult(
                    confirmed="cant_tell",
                    why=f"verifier raise: {type(exc).__name__}",
                    available=False,
                )
            confirmed = str(payload.get("confirmed", "cant_tell")).strip().lower()
            if confirmed not in ("yes", "partial", "no", "cant_tell"):
                confirmed = "cant_tell"
            why = str(payload.get("why", "")).strip()
            if span is not None:
                span["payload"] = {
                    "confirmed": confirmed,
                    "why": why[:120],
                }
            return _ConsistencyResult(
                confirmed=confirmed,
                why=why,
                available=True,
            )

    def _call_grounded_adjudicator(
        self,
        *,
        question_stem: str,
        student_input: str,
        sources: list[str],
    ) -> _GroundedAdjudication:
        """Call ``GRADER_GROUNDED`` (Gemini-pinned)."""
        client = self._resolve_grounded_client()
        if client is None:
            return _GroundedAdjudication(
                verdict=Verdict.UNVERIFIED,
                private_canonical="",
                what_right="",
                what_missing="",
                first_misconception="",
                citation="",
                confidence=0.0,
                reasoning="no GRADER_GROUNDED client available",
            )
        try:
            response = client.generate(
                messages=[
                    {
                        "role": "user",
                        "content": render_non_math_grounded_user_prompt(
                            question_stem=question_stem,
                            student_input=student_input,
                            sources=sources,
                        ),
                    },
                ],
                system_prompt=NON_MATH_GROUNDED_SYSTEM,
                # 2048 tokens. Bumped from 400 — the grounded response
                # has seven JSON fields plus the new confidence-band
                # explanation; Gemini's emitted JSON was being
                # truncated mid-``private_canonical`` on rich free-text
                # answers, which the parser then dropped to
                # ``unverified``. 2048 leaves headroom for any
                # extended reasoning Gemini emits before the JSON;
                # cheap on Gemini 3 Flash.
                max_tokens=2048,
            )
            return _parse_grounded_response(response.content or "")
        except Exception as exc:
            logger.warning(
                "[StudentGrader] grounded adjudicator raised %s",
                type(exc).__name__,
            )
            return _GroundedAdjudication(
                verdict=Verdict.UNVERIFIED,
                private_canonical="",
                what_right="",
                what_missing="",
                first_misconception="",
                citation="",
                confidence=0.0,
                reasoning=f"grounded raise: {type(exc).__name__}",
            )

    # ==================================================================
    # 2. Pre-pose check
    # ==================================================================

    def pre_pose_check(
        self,
        context: TutoringContext,
        question_ref: QuestionRef,
        canonical: str,
        visible_prompt: str,
        attached_media_ids: list[int],
        recent_transcript: list[str],
        attached_figure_description: str = "",
        *,
        issue_token: bool = True,
    ) -> Optional[str]:
        """Derivability gate. Optionally issues a signed single-use token.

        Hidden KB chunks are NOT passed to this check — only the
        student-visible context. Per Phase 1 §4.2 the issued token is
        single-use; ``ContextManager.commit_pending_pose`` consumes it.

        Args:
          issue_token: True for runtime-generated / token-path questions
            (caller will pose with ``pre_pose_token``). False for bank
            verification (the tool boundary already has the canonical
            from the DB; only the derivability decision matters).

        Returns the token string when ``issue_token=True``; ``None``
        when ``issue_token=False``. Raises ``PrePoseRefusedError`` on
        derivability failure so the caller (the tool boundary in
        ``v2/tools/pose_question.py``) can refuse the tool call
        cleanly.
        """
        with emit_span("audit", "grader.pre_pose_check") as span:
            client = self._resolve_grounded_client()
            if client is None:
                if span is not None:
                    span["payload"] = {
                        "derivable": False,
                        "reason": "no GRADER_GROUNDED client",
                    }
                raise PrePoseRefusedError(
                    "no grounded client available for pre-pose check"
                )
            try:
                response = client.generate(
                    messages=[
                        {
                            "role": "user",
                            "content": render_pre_pose_user_prompt(
                                visible_prompt=visible_prompt,
                                attached_figure_description=attached_figure_description,
                                recent_transcript=recent_transcript,
                                canonical=canonical,
                            ),
                        },
                    ],
                    system_prompt=PRE_POSE_SYSTEM,
                    # Bumped from 200 → 2048 alongside the grader and
                    # tutor-claim adjudicator; the pre-pose response is
                    # short but the bigger budget eliminates the
                    # truncation-mid-JSON failure mode.
                    max_tokens=2048,
                )
                payload = _safe_json_loads(response.content or "") or {}
            except Exception as exc:
                logger.warning(
                    "[StudentGrader] pre-pose call raised %s",
                    type(exc).__name__,
                )
                raise PrePoseRefusedError(
                    f"pre-pose adjudication raised: {type(exc).__name__}"
                ) from exc

            derivable = bool(payload.get("derivable"))
            reason = str(payload.get("reason", "")).strip()
            if span is not None:
                span["payload"] = {
                    "derivable": derivable, "reason": reason[:200],
                }
            if not derivable:
                raise PrePoseRefusedError(
                    reason or "canonical not derivable from visible context"
                )

            if not issue_token:
                return None

            # Issue the single-use token. The cache binds the token
            # to (session_id, canonical, visible_context snapshot).
            visible_context = VisibleContextSnapshot(
                visible_prompt=visible_prompt,
                attached_media_ids=list(attached_media_ids or []),
                recent_transcript=list(recent_transcript or []),
            )
            token = token_cache.issue(
                session_id=context.session_id,
                canonical=canonical,
                visible_context_json=visible_context.model_dump_json(),
            )
            return token

    def build_pending_pose(
        self,
        *,
        question_ref: QuestionRef,
        canonical: str,
        rendered_stem: str,
        jaccard_signature: str,
        visible_context: VisibleContextSnapshot,
        token: Optional[str] = None,
    ) -> PendingPose:
        """Convenience constructor — keeps the type at one site."""
        return PendingPose(
            question_ref=question_ref,
            canonical=canonical,
            rendered_stem=rendered_stem,
            jaccard_signature=jaccard_signature,
            visible_context=visible_context,
            token=token,
        )

    # ==================================================================
    # 3. Tutor-claim adjudication
    # ==================================================================

    def adjudicate_tutor_claim(
        self,
        context: TutoringContext,
        claim: str,
        sources: Optional[list[str]] = None,
    ) -> dict:
        """Returns ``{status, citation}``.

        ``status`` is one of ``supported | contradicted | unverified``.
        Below confidence we conservatively return ``unverified``.
        """
        with emit_span("audit", "grader.tutor_claim_adjudication") as span:
            client = self._resolve_claim_client()
            if client is None:
                if span is not None:
                    span["payload"] = {
                        "status": "unverified",
                        "reason": "no TUTOR_CLAIM_ADJUDICATOR client",
                    }
                return {"status": "unverified", "citation": ""}
            try:
                response = client.generate(
                    messages=[
                        {
                            "role": "user",
                            "content": render_tutor_claim_user_prompt(
                                claim=claim,
                                sources=list(sources or []),
                            ),
                        },
                    ],
                    system_prompt=TUTOR_CLAIM_SYSTEM,
                    # 2048 tokens — bumped from 200 to accommodate the
                    # updated source-preference instructions and longer
                    # citation strings. Truncation at 200 was forcing
                    # the parser to fall through to ``unverified`` even
                    # on supported claims.
                    max_tokens=2048,
                )
                payload = _safe_json_loads(response.content or "") or {}
            except Exception as exc:
                logger.warning(
                    "[StudentGrader] adjudicate_tutor_claim raised %s",
                    type(exc).__name__,
                )
                if span is not None:
                    span["payload"] = {
                        "status": "unverified",
                        "reason": f"raise: {type(exc).__name__}",
                    }
                return {"status": "unverified", "citation": ""}

            status = str(payload.get("status", "unverified")).strip().lower()
            if status not in ("supported", "contradicted", "unverified"):
                status = "unverified"
            citation = str(payload.get("citation", "")).strip()
            if span is not None:
                span["payload"] = {"status": status}
            return {"status": status, "citation": citation}

    # ==================================================================
    # Client resolution
    # ==================================================================

    def _resolve_math_client(self):
        if self._math_client_factory is not None:
            return self._math_client_factory()
        return _build_client_for_purpose("grader_math")

    def _resolve_grounded_client(self):
        if self._grounded_client_factory is not None:
            return self._grounded_client_factory()
        return _build_client_for_purpose("grader_grounded")

    def _resolve_claim_client(self):
        if self._claim_client_factory is not None:
            return self._claim_client_factory()
        return _build_client_for_purpose("tutor_claim_adjudicator")

    def _resolve_verifier_client(self):
        if self._verifier_client_factory is not None:
            return self._verifier_client_factory()
        return _build_client_for_purpose("grader_verifier")


# ──────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────


class PrePoseRefusedError(Exception):
    """Raised by ``StudentGrader.pre_pose_check`` when the canonical is
    not derivable from the student-visible context."""


# ──────────────────────────────────────────────────────────────────────
# Module-private helpers
# ──────────────────────────────────────────────────────────────────────


def _build_client_for_purpose(purpose: str):
    """Resolve the ``ModelConfig`` for the purpose and return a client."""
    try:
        from apps.llm.client import get_llm_client
        from apps.llm.models import ModelConfig
    except Exception:
        return None
    cfg = ModelConfig.get_for(purpose)
    if cfg is None:
        return None
    try:
        return get_llm_client(cfg)
    except Exception as exc:
        logger.warning(
            "[StudentGrader] get_llm_client(%s) raised %s",
            purpose, type(exc).__name__,
        )
        return None


def _safe_json_loads(text: str) -> Optional[Any]:
    """Best-effort JSON parse — strips ```json fences if the model added them."""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        if raw.endswith("```"):
            raw = raw[: -3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _format_canonical(value: Any) -> str:
    """Render the canonical value for ``private_canonical``.

    Single-slot: returns the numeric value as a string. Multi-slot:
    returns ``"loss_amount=30; loss_percentage=25"`` style — joined
    on ``"; "``, slot names humanised, numbers rendered like the
    single-slot path.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            name = _humanise_slot(entry.get("name", ""))
            slot_val = entry.get("value")
            if isinstance(slot_val, float):
                if slot_val.is_integer():
                    slot_val = int(slot_val)
                else:
                    slot_val = f"{slot_val:g}"
            parts.append(f"{name}={slot_val}")
        return "; ".join(parts)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:g}"
    return str(value)


def _humanise_slot(name: str) -> str:
    """Render a DSL slot name in student-facing prose.

    "loss_amount" -> "loss amount"; "profit_pct" -> "profit pct".
    Subject-agnostic; just turns underscores / camelCase into spaces.
    """
    s = (name or "").strip()
    if not s:
        return "this value"
    s = s.replace("_", " ").replace("-", " ")
    return " ".join(p for p in s.split() if p)


def _join_names(names: list[str]) -> str:
    """Oxford-style join for a short list of slot names."""
    cleaned = [n for n in names if n]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def _parse_student_math_value(student_input: str) -> tuple[Optional[Any], str]:
    """Extract a numeric value from ``student_input`` for math comparison.

    Returns ``(value, rendered_str)``. ``value`` is ``None`` when no
    numeric value can be extracted.

    Pipeline:
      1. Bare-arithmetic fast path — ``safe_eval_arithmetic`` parses
         clean numeric strings like ``"25"`` or ``"3 + 4"``.
      2. Prose-numeric fast path — pulls a number out of common student
         framings ("is it 21?", "ohhh x = 6", "the answer is 7",
         "= 6", trailing "… 6"). These are the patterns that show up in
         real S1–S3 chat transcripts; without this fallback every
         prose-wrapped answer collapses to UNVERIFIED.
      3. Multi-step chain fallback — when the student typed actual
         working (``"3x = 18 → x = 18 ÷ 3 = 6"``), use the working
         analyzer to walk steps and take the final ``Step.computed``.
    """
    from apps.tutoring.student_working_analyzer import (
        analyze_chain,
        extract_steps,
        safe_eval_arithmetic,
    )

    text = (student_input or "").strip()
    if not text:
        return None, ""

    # 1. Bare-arithmetic fast path.
    bare = safe_eval_arithmetic(text)
    if bare is not None:
        return bare, str(bare)

    # 2. Prose-numeric fast path. Order matters — the most specific
    # patterns first so "x = 6" doesn't get clobbered by the trailing
    # "any number" rule that would also match the "3" in "add 3 to
    # both sides".
    prose_value = _extract_prose_numeric(text)
    if prose_value is not None:
        return prose_value, str(prose_value)

    # 3. Multi-step working chain fallback.
    try:
        steps = extract_steps(text)
    except Exception:
        steps = []
    if not steps:
        return None, ""
    try:
        final_index, _ = analyze_chain(steps)
    except Exception:
        final_index = None
    if final_index is None or final_index < 0 or final_index >= len(steps):
        return None, ""
    final_step = steps[final_index]
    # Step.computed is the analyzer's evaluated result; Step.claim is
    # the student's stated value. Prefer claim (what they SAID) — that's
    # what we're grading against. Fall back to computed if claim is
    # not parseable as a number.
    claim_str = (getattr(final_step, "claim", "") or "").strip()
    try:
        value: Any = float(claim_str)
        if value.is_integer():
            value = int(value)
    except (TypeError, ValueError):
        value = getattr(final_step, "computed", None)
    if value is None:
        return None, ""
    return value, str(value)


# Regex set for prose-numeric extraction. Kept narrow so it doesn't
# misfire on numbers that are part of the problem setup ("3x = 18" in
# the student echoing the question) — we anchor on terminal-answer
# phrasing.
_PROSE_NUMERIC_PATTERNS = (
    # "x = 6", "y = -3.5", "answer: 6", "answer is 7"
    re.compile(
        r"(?ix)\b(?:x|y|z|n|answer)\s*(?:=|:|is)\s*"
        r"(-?\d+(?:\.\d+)?)\s*$"
    ),
    # "is it 21?", "is the answer 21?"
    re.compile(
        r"(?ix)\b(?:is\s+it|is\s+the\s+answer|is\s+that)\s+"
        r"(-?\d+(?:\.\d+)?)\s*\??\s*$"
    ),
    # "the answer is 7", "it is 7", "= 7"
    re.compile(
        r"(?ix)(?:the\s+answer\s+is|it\s+is|=)\s+"
        r"(-?\d+(?:\.\d+)?)\s*\.?\s*$"
    ),
    # Trailing bare number: "… so 6" / "ohhh 6" / final "6."
    re.compile(r"(?:^|[^\w.])(-?\d+(?:\.\d+)?)\s*[.!?]?\s*$"),
)


_MCQ_PROSE_PATTERNS = (
    # "option B", "answer: B", "choice B", "pick B"
    re.compile(r"(?i)\b(?:option|answer|choice|pick)\s*[:\-]?\s*([A-Da-d])\b"),
    # "I pick B", "I choose B", "I'd choose B", "I'll go with B"
    re.compile(
        r"(?i)\bi(?:'?ll|'?d|\s+would)?\s+"
        r"(?:pick|choose|select|go\s+with|say|think)\s+"
        r"(?:it\s+is\s+|is\s+|the\s+answer\s+is\s+|that\s+)?([A-Da-d])\b"
    ),
    # "(B)", "[B]" — bracketed letter
    re.compile(r"(?i)^\s*[\(\[]([A-Da-d])[\)\]]"),
    # Bare letter alone or with terminal punctuation: "B", "b.", "B!"
    re.compile(r"(?i)^\s*([A-Da-d])\s*[\.\!]?\s*$"),
)


def _match_mcq_letter(canonical: str, student_input: str) -> Optional[bool]:
    """Map ``student_input`` to True/False for an MCQ-letter canonical.

    Accepts the canonical as a single letter (A/B/C/D, case-insensitive).
    Returns ``None`` when the canonical is not a single letter — that
    means the lesson author stored full option text, not a letter, and
    we let the grounded grader decide.
    """
    canon = (canonical or "").strip().upper()
    if len(canon) != 1 or canon not in "ABCD":
        return None
    text = (student_input or "").strip()
    if not text:
        return None
    for pat in _MCQ_PROSE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).upper() == canon
    return None


_TRUE_TOKENS = {"true", "t", "yes", "y", "correct", "right"}
_FALSE_TOKENS = {"false", "f", "no", "n", "incorrect", "wrong"}


def _match_true_false(canonical: str, student_input: str) -> Optional[bool]:
    """True/False canonical comparator with prose-aware extraction.

    Returns ``None`` when the canonical isn't a recognisable T/F token
    or the student input contains BOTH a true and a false token (we
    can't disambiguate "true because false is wrong" cheaply — that's
    grounded-grader territory).
    """
    canon = (canonical or "").strip().lower()
    if canon in _TRUE_TOKENS:
        canon_is_true: Optional[bool] = True
    elif canon in _FALSE_TOKENS:
        canon_is_true = False
    else:
        return None

    text = (student_input or "").strip().lower()
    if not text:
        return None
    # Strip punctuation so "True." / "false?" still match.
    text_norm = re.sub(r"[^a-z0-9\s]", " ", text)
    words = set(text_norm.split())
    has_true = bool(words & _TRUE_TOKENS)
    has_false = bool(words & _FALSE_TOKENS)
    # First word fast-path: "True - large-scale maps…" should still
    # match True even though the rest of the prose mentions "smaller".
    first_word = (text_norm.split() or [""])[0]
    if first_word in _TRUE_TOKENS:
        return canon_is_true is True
    if first_word in _FALSE_TOKENS:
        return canon_is_true is False
    if has_true and not has_false:
        return canon_is_true is True
    if has_false and not has_true:
        return canon_is_true is False
    return None


def _match_short_numeric(canonical: str, student_input: str) -> Optional[bool]:
    """Compare a short-numeric canonical against student input.

    Uses the prose-numeric extractor on both sides so canonicals
    like ``"x = 6"`` / ``"55 SCR"`` / ``"2.88 m³/s"`` and student
    inputs like ``"ohhh x = 6"`` all resolve to a comparable number.
    Returns ``None`` when either side can't be pinned to a numeric
    value (grounded grader handles those).
    """
    canon_val = _extract_canonical_numeric(canonical)
    if canon_val is None:
        return None
    student_val, _ = _parse_student_math_value(student_input)
    if student_val is None:
        # Last-ditch: pull any number out of the student prose.
        student_val = _extract_canonical_numeric(student_input)
    if student_val is None:
        return None
    try:
        return abs(float(canon_val) - float(student_val)) < 1e-6
    except (TypeError, ValueError):
        return None


def _extract_canonical_numeric(text: str) -> Optional[Any]:
    """Pull the dominant numeric value out of a canonical answer string.

    Looser than ``_extract_prose_numeric`` (which is anchored at end of
    string to avoid stripping numbers out of the problem setup).
    Canonicals like ``"55 SCR"`` / ``"2.88 m³/s"`` / ``"x = 6"`` all
    have a single dominant number; this returns it. Returns ``None``
    only when the string has no numeric token.
    """
    if not text:
        return None
    # Prefer the same prose patterns first (catches "x = 6").
    val = _extract_prose_numeric(text)
    if val is not None:
        return val
    # Fall back to first numeric token in the string.
    m = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        f = float(m.group(1))
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def _extract_prose_numeric(text: str) -> Optional[Any]:
    """Pull a terminal numeric answer out of student prose.

    Returns ``int`` for whole numbers, ``float`` for non-integers,
    ``None`` when no terminal numeric is found. Anchored on the END of
    the string so we don't capture numbers from the problem setup the
    student is echoing back.
    """
    text = (text or "").strip()
    if not text:
        return None
    for pattern in _PROSE_NUMERIC_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(1)
        try:
            f = float(raw)
        except (TypeError, ValueError):
            continue
        return int(f) if f.is_integer() else f
    return None


def _resolve_bank_question(open_q: OpenQuestion):
    """Resolve ``OpenQuestion`` → bank row when possible.

    Returns ``None`` when the source is not bank-resolvable (inline
    generated, pre-pose token, etc.).
    """
    src = open_q.source
    if src == QuestionSource.EXIT_TICKET_QUESTION:
        try:
            from apps.tutoring.models import ExitTicketQuestion
            return ExitTicketQuestion.objects.filter(pk=open_q.id).first()
        except Exception:
            return None
    if src == QuestionSource.LESSON_STEP:
        try:
            from apps.curriculum.models import LessonStep
            return LessonStep.objects.filter(pk=open_q.id).first()
        except Exception:
            return None
    return None


def _parse_grounded_response(raw_text: str) -> _GroundedAdjudication:
    """Parse the structured-JSON response from the grounded adjudicator."""
    payload = _safe_json_loads(raw_text) or {}

    raw_verdict = str(payload.get("verdict", "unverified")).strip().lower()
    if raw_verdict not in ("correct", "partial", "wrong", "unverified"):
        raw_verdict = "unverified"
    verdict = Verdict(raw_verdict)

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return _GroundedAdjudication(
        verdict=verdict,
        private_canonical=str(payload.get("private_canonical", "")).strip(),
        what_right=str(payload.get("what_right", "")).strip(),
        what_missing=str(payload.get("what_missing", "")).strip(),
        first_misconception=str(payload.get("first_misconception", "")).strip(),
        citation=str(payload.get("citation", "")).strip(),
        confidence=confidence,
        reasoning="grounded adjudicator JSON",
    )
