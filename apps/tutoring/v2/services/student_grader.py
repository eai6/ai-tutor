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
    MATH_DSL_SYSTEM,
    NON_MATH_GROUNDED_SYSTEM,
    PRE_POSE_SYSTEM,
    TUTOR_CLAIM_SYSTEM,
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


# Below this confidence the grounded path escalates to ``unverified``.
# Conservative starting point per §7 item 1; sub-decision tunes from
# pilot data.
_GROUNDED_CONFIDENCE_THRESHOLD = 0.6


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


class StudentGrader:
    """Stateless central grader. Constructed per-turn."""

    def __init__(
        self,
        *,
        math_client_factory=None,
        grounded_client_factory=None,
        claim_client_factory=None,
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
                if span is not None:
                    span["payload"] = {
                        "verdict": Verdict.UNVERIFIED.value,
                        "stage": "dsl_extraction",
                        "error": extraction.error or "",
                    }
                return GradingResult(
                    verdict=Verdict.UNVERIFIED,
                    reasoning=f"math DSL extraction failed: {extraction.error or ''}",
                    bare_answer=bare,
                )

            # 2. Validate + execute the DSL.
            result = self._math_verification_tool.evaluate(
                problem_text, extraction.program,
            )
            if not result.ok:
                if span is not None:
                    span["payload"] = {
                        "verdict": Verdict.UNVERIFIED.value,
                        "stage": "dsl_validation",
                        "error": result.error or "",
                    }
                return GradingResult(
                    verdict=Verdict.UNVERIFIED,
                    reasoning=f"math DSL validation failed: {result.error or ''}",
                    bare_answer=bare,
                )

            canonical_value = result.canonical_value
            canonical_str = _format_canonical(canonical_value)

            # 3. Parse the student's value via the existing
            #    ast-based working analyzer (R9).
            student_value, student_value_str = _parse_student_math_value(
                request.student_input
            )

            # 4. Comparator.
            if student_value is None:
                if span is not None:
                    span["payload"] = {
                        "verdict": Verdict.UNVERIFIED.value,
                        "stage": "student_value_parse",
                    }
                return GradingResult(
                    verdict=Verdict.UNVERIFIED,
                    private_canonical=canonical_str,
                    reasoning="could not parse a numeric value from student input",
                    bare_answer=bare,
                )

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

    # ------------------------------------------------------------------
    # Non-math path (Phase 2 §2.1 — non-math, tiered)
    # ------------------------------------------------------------------

    def _grade_non_math(
        self,
        context: TutoringContext,
        request: GradingRequest,
    ) -> GradingResult:
        """Tier order: deterministic bank → KB-grounded → Google-grounded."""
        with emit_span("audit", "grader.grounded") as span:
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
            verdict_kind = adjudication.verdict
            if adjudication.confidence < _GROUNDED_CONFIDENCE_THRESHOLD:
                verdict_kind = Verdict.UNVERIFIED

            safe = StudentSafeFeedback(
                what_right=adjudication.what_right,
                what_missing=adjudication.what_missing,
                first_misconception_redacted=adjudication.first_misconception,
            )

            if span is not None:
                span["payload"] = {
                    "verdict": verdict_kind.value,
                    "tier": "grounded",
                    "confidence": adjudication.confidence,
                }

            return GradingResult(
                verdict=verdict_kind,
                private_canonical=adjudication.private_canonical,
                student_safe_feedback=safe,
                student_value=request.student_input,
                reasoning=adjudication.reasoning or "non-math grounded adjudication",
                citation=adjudication.citation,
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
                max_tokens=400,
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
                    max_tokens=200,
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
                    max_tokens=200,
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
    """Render the canonical value for ``private_canonical``."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:g}"
    return str(value)


def _parse_student_math_value(student_input: str) -> tuple[Optional[Any], str]:
    """Extract a numeric value via the existing ast-based working analyzer (R9).

    Returns ``(value, rendered_str)``. ``value`` is ``None`` when no
    numeric value can be extracted.
    """
    from apps.tutoring.student_working_analyzer import (
        analyze_chain,
        extract_steps,
        safe_eval_arithmetic,
    )

    text = (student_input or "").strip()
    if not text:
        return None, ""
    # First try a direct numeric parse — bare-answer fast path.
    bare = safe_eval_arithmetic(text)
    if bare is not None:
        return bare, str(bare)
    # Otherwise extract the chain of steps and take the final result.
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
    value = getattr(final_step, "value", None)
    if value is None:
        return None, ""
    return value, str(value)


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
