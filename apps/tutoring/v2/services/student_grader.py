"""StudentGrader — central correctness service for the v2 engine.

Three closely-related responsibilities (refactor-analysis §3):

  1. **Student-answer grading.** Math path uses ``MathVerificationTool``
     (LLM-emitted DSL validated against the visible problem text, then
     Python-executed). Non-math path is tiered: deterministic
     ``bank_grader`` first when a canonical exists, KB-grounded
     adjudication for curriculum content, Gemini Google-grounding for
     general world knowledge. The grader returns the strict ternary
     CORRECT | PARTIAL | WRONG per v2-prune-plan §4.1 — no fourth
     option, no `unverified` escape valve.

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
    NON_MATH_JUDGE_SYSTEM,
    STUDENT_CLAIMS_SYSTEM,
    STUDENT_RESPONSE_SYSTEM,
    TUTOR_CLAIM_SYSTEM,
    render_math_dsl_user_prompt,
    render_non_math_judge_user_prompt,
    render_student_claims_user_prompt,
    render_student_response_user_prompt,
    render_tutor_claim_user_prompt,
)
from apps.tutoring.v2.tools.math_verification import (
    DSLValidationError,
    MathTrace,
    MathVerificationTool,
    _evaluate as _evaluate_dsl_node,
    values_equivalent,
)

logger = logging.getLogger(__name__)


@dataclass
class _DSLExtraction:
    program: Optional[dict]
    raw_text: str
    error: Optional[str] = None


@dataclass
class _StudentClaim:
    """One discrete arithmetic / logical step the student stated."""
    description: str
    expression: Any  # DSL node — bare number, {"var":...}, {"op":..., "args":...}
    asserted_value: Any


@dataclass
class _StudentConclusion:
    statement: str
    answer_extracted_value: Any  # scalar | list[scalar] | None
    answer_extracted_label: str  # "yes"|"no"|"true"|"false"|"A"|... | ""
    is_attempt: bool


@dataclass
class _StudentClaimsExtraction:
    """Output of LLM-B (student-claims extractor)."""
    variables: dict
    claims: list[_StudentClaim]
    conclusion: _StudentConclusion
    domain_check_required: bool
    raw_text: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class _StudentResponseConclusion:
    """LLM-B (non-math) conclusion sub-object."""
    stated_answer: str
    answer_label: str
    denies_canonical: bool


@dataclass
class _StudentResponseExtraction:
    """Output of LLM-B for the non-math path (STUDENT_RESPONSE_SYSTEM)."""
    is_attempt: bool
    hedge_marker: bool
    claims: list[dict]  # [{"id": str, "text": str}, ...]
    conclusion: _StudentResponseConclusion
    raw_text: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_judge_payload(self) -> dict:
        """Render the structured object passed to LLM-C as input."""
        return {
            "is_attempt": self.is_attempt,
            "hedge_marker": self.hedge_marker,
            "claims": list(self.claims),
            "conclusion": {
                "stated_answer": self.conclusion.stated_answer,
                "answer_label": self.conclusion.answer_label,
                "denies_canonical": self.conclusion.denies_canonical,
            },
        }


@dataclass
class _NonMathJudgement:
    """Output of LLM-C for the non-math path (NON_MATH_JUDGE_SYSTEM)."""
    verdict: Verdict
    private_canonical: str
    what_right: str
    what_missing: str
    first_misconception: str
    citation: str
    reason_code: str  # "" | "known_misconception" | "denies_canonical" | "off_topic"
    raw_text: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class StudentGrader:
    """Stateless central grader. Constructed per-turn."""

    def __init__(
        self,
        *,
        math_client_factory=None,
        grounded_client_factory=None,
        claim_client_factory=None,
        student_claims_client_factory=None,
        student_response_client_factory=None,
        math_verification_tool: Optional[MathVerificationTool] = None,
    ) -> None:
        """Optional injection seams for tests.

        Each ``*_client_factory`` returns a ``BaseLLMClient``-shaped
        object on demand. When ``None``, the grader resolves the
        ``ModelConfig`` for the appropriate purpose at call time.

        Math two-LLM grader:
          * ``math_client_factory``           — LLM-A (canonical extractor)
          * ``student_claims_client_factory`` — LLM-B (student claim graph)

        Non-math two-LLM grader (companion redesign):
          * ``student_response_client_factory`` — LLM-B (student response parser)
          * ``grounded_client_factory``         — LLM-C (judge, KB-grounded)
        """
        self._math_client_factory = math_client_factory
        self._grounded_client_factory = grounded_client_factory
        self._claim_client_factory = claim_client_factory
        self._student_claims_client_factory = student_claims_client_factory
        self._student_response_client_factory = student_response_client_factory
        self._math_verification_tool = math_verification_tool or MathVerificationTool()

    # ==================================================================
    # 1. Student-answer grading — entry point
    # ==================================================================

    def grade_student_response(
        self,
        context: TutoringContext,
        request: GradingRequest,
    ) -> GradingResult:
        """Route to math / non-math path based on ``request.is_math``.

        State-inconsistent guard (run-7 P1-3 root cause): when the
        ``open_question.rendered_stem`` is empty or whitespace-only,
        there is no question to ground against. The grader must NOT
        spend an LLM call trying to extract a canonical from "". Return
        WRONG with ``reasoning`` stamped ``state_inconsistent`` so the
        downstream engine flows through the wrong-verdict path and the
        student is asked to retry rather than the system silently
        affirming. The state_inconsistent reason_code preserves the
        observability signal.
        """
        stem = (request.open_question.rendered_stem or "").strip()
        if not stem:
            return GradingResult(
                verdict=Verdict.WRONG,
                reasoning=(
                    "state_inconsistent: open_question has no "
                    "rendered_stem to grade against"
                ),
                student_value=(request.student_input or "").strip(),
                bare_answer=False,
                reason_code="state_inconsistent",
            )
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
        """Two-LLM math grader.

        See design/tasks/two-llm-grader-implementation-plan.md.

        Pipeline:
          1. LLM-A (GRADER_MATH) extracts the canonical DSL from the
             QUESTION → Python executor produces canonical_value.
          2. LLM-B (GRADER_STUDENT_CLAIMS) parses the STUDENT response
             into {claims[], conclusion{}} and the Python comparator
             decides arithmetic vs conclusion error.

        (The legacy deterministic fast-path that skipped LLM-B for
        single-scalar inputs was deleted 2026-05-27 — it silently
        collapsed multi-slot answers to PARTIAL by extracting one
        scalar from a multi-value response. The latency saving did
        not justify the P1 risk.)

        Strict ternary contract (v2-prune-plan §4.1): every path
        returns CORRECT | PARTIAL | WRONG.
          - empty rendered_stem (state_inconsistent) → WRONG;
          - LLM-A extraction / validation failure → grounded
            fall-through (non-math path);
          - LLM-B extraction failure → grounded fall-through;
          - is_attempt=false (meta input) → WRONG so the engine
            asks for an attempt;
          - comparator outcomes → CORRECT / PARTIAL / WRONG.
        """
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

            # 3. LLM-B — parse the student's response into a structured
            # claim graph. Subject-agnostic; handles word-form numerics,
            # multi-slot prose, intermediate-vs-final values, meta input.
            student_extraction = self._extract_student_claims_dsl(
                problem_text=problem_text,
                student_response=request.student_input,
            )
            if not student_extraction.ok:
                # LLM-B failed (no client, JSON refused, schema invalid).
                # Fall through to the grounded / verifier path — the
                # same fail-soft escape valve we use for LLM-A.
                if span is not None:
                    span["payload"] = {
                        "verdict": "deferred",
                        "stage": "student_claims_extraction",
                        "fallthrough": "grounded",
                        "error": student_extraction.error or "",
                    }
                return self._grade_non_math(context, request)

            # 4. Meta input — the student didn't attempt an answer.
            # Ternary contract: route through WRONG so the engine asks
            # the student to attempt the question. reason_code preserves
            # the meta-input observability signal.
            if not student_extraction.conclusion.is_attempt:
                if span is not None:
                    span["payload"] = {
                        "verdict": Verdict.WRONG.value,
                        "reason_code": "meta_input",
                    }
                return GradingResult(
                    verdict=Verdict.WRONG,
                    private_canonical=canonical_str,
                    student_value=(request.student_input or "").strip(),
                    reasoning="math: student response is not an attempt",
                    reason_code="meta_input",
                    bare_answer=bare,
                )

            # 5. Comparator — deterministic Python over LLM-B's claims +
            # the canonical value(s).
            return self._compare_student_claims_to_canonical(
                span=span,
                canonical_value=canonical_value,
                canonical_str=canonical_str,
                extraction=student_extraction,
                bare=bare,
                student_input=request.student_input,
            )

    def _finalise_math_with_value(
        self,
        *,
        span,
        canonical_value: Any,
        canonical_str: str,
        student_value: Any,
        student_value_str: str,
        bare: bool,
        reasoning: str,
    ) -> GradingResult:
        """Fast-path finaliser: a single scalar student value vs. the
        canonical. Single- or multi-slot.

        Mirrors the legacy comparator's branching. Only used when the
        student input is unambiguously bare (regex extraction is
        equivalent to LLM-B for that shape) so we skip the LLM-B
        round-trip.
        """
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
                    "path": "fast",
                }
            return GradingResult(
                verdict=verdict_kind,
                private_canonical=canonical_str,
                student_safe_feedback=safe,
                student_value=student_value_str,
                reasoning=reasoning,
                bare_answer=bare,
            )

        # Single-slot.
        equivalent = values_equivalent(canonical_value, student_value)
        verdict_kind = Verdict.CORRECT if equivalent else Verdict.WRONG
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
                "path": "fast",
            }
        return GradingResult(
            verdict=verdict_kind,
            private_canonical=canonical_str,
            student_safe_feedback=safe,
            student_value=student_value_str,
            reasoning=reasoning,
            bare_answer=bare,
        )

    # ------------------------------------------------------------------
    # LLM-B — student-claims extractor (Two-LLM grader §2.2)
    # ------------------------------------------------------------------

    def _extract_student_claims_dsl(
        self,
        *,
        problem_text: str,
        student_response: str,
    ) -> _StudentClaimsExtraction:
        """Call ``GRADER_STUDENT_CLAIMS`` to produce LLM-B's claim graph."""
        client = self._resolve_student_claims_client()
        if client is None:
            return _StudentClaimsExtraction(
                variables={},
                claims=[],
                conclusion=_StudentConclusion(
                    statement="", answer_extracted_value=None,
                    answer_extracted_label="", is_attempt=False,
                ),
                domain_check_required=False,
                raw_text="",
                error="no GRADER_STUDENT_CLAIMS client available",
            )
        try:
            response = client.generate(
                messages=[
                    {
                        "role": "user",
                        "content": render_student_claims_user_prompt(
                            problem_text=problem_text,
                            student_response=student_response,
                        ),
                    },
                ],
                system_prompt=STUDENT_CLAIMS_SYSTEM,
                max_tokens=1200,
            )
            raw_text = response.content or ""
        except Exception as exc:
            return _StudentClaimsExtraction(
                variables={},
                claims=[],
                conclusion=_StudentConclusion(
                    statement="", answer_extracted_value=None,
                    answer_extracted_label="", is_attempt=False,
                ),
                domain_check_required=False,
                raw_text="",
                error=f"student-claims extraction raised: {type(exc).__name__}",
            )

        payload = _safe_json_loads(raw_text)
        if not isinstance(payload, dict):
            return _StudentClaimsExtraction(
                variables={},
                claims=[],
                conclusion=_StudentConclusion(
                    statement="", answer_extracted_value=None,
                    answer_extracted_label="", is_attempt=False,
                ),
                domain_check_required=False,
                raw_text=raw_text,
                error="LLM-B did not return a JSON object",
            )

        variables = payload.get("variables") or {}
        if not isinstance(variables, dict):
            variables = {}

        raw_claims = payload.get("claims") or []
        claims: list[_StudentClaim] = []
        if isinstance(raw_claims, list):
            for entry in raw_claims:
                if not isinstance(entry, dict):
                    continue
                claims.append(_StudentClaim(
                    description=str(entry.get("description", "")).strip(),
                    expression=entry.get("expression"),
                    asserted_value=entry.get("asserted_value"),
                ))

        raw_conclusion = payload.get("conclusion") or {}
        if not isinstance(raw_conclusion, dict):
            raw_conclusion = {}
        conclusion = _StudentConclusion(
            statement=str(raw_conclusion.get("statement", "")).strip(),
            answer_extracted_value=raw_conclusion.get(
                "answer_extracted_value",
            ),
            answer_extracted_label=str(
                raw_conclusion.get("answer_extracted_label", "") or ""
            ).strip(),
            is_attempt=bool(raw_conclusion.get("is_attempt", False)),
        )

        return _StudentClaimsExtraction(
            variables=variables,
            claims=claims,
            conclusion=conclusion,
            domain_check_required=bool(
                payload.get("domain_check_required", False)
            ),
            raw_text=raw_text,
        )

    # ------------------------------------------------------------------
    # Comparator — pure Python (Two-LLM grader §2.3)
    # ------------------------------------------------------------------

    def _compare_student_claims_to_canonical(
        self,
        *,
        span,
        canonical_value: Any,
        canonical_str: str,
        extraction: _StudentClaimsExtraction,
        bare: bool,
        student_input: str,
    ) -> GradingResult:
        """Deterministic comparator over LLM-B's claim graph.

        Step A — verify each claim's asserted_value against its
                 expression evaluated by the DSL interpreter. Any
                 mismatch → WRONG with reason_code=arithmetic_failed
                 and a redacted misconception that names the failing
                 step.
        Step B — compare the student's conclusion against the canonical:
                  * matches canonical → CORRECT
                  * matches partially (multi-slot, some slots matched)
                                       → PARTIAL
                  * doesn't match → WRONG with
                                    reason_code=conclusion_inconsistent_with_canonical
        """
        # Step A — arithmetic verification of each claim.
        for idx, claim in enumerate(extraction.claims):
            if claim.expression is None or claim.asserted_value is None:
                # Can't verify a missing-expression / missing-value
                # claim. Skip rather than crash; LLM-B is allowed to
                # omit either when not applicable.
                continue
            computed = _evaluate_claim_safely(
                claim.expression, extraction.variables,
            )
            if computed is None:
                continue
            if not values_equivalent(claim.asserted_value, computed):
                # Step-level arithmetic error. Name the slip without
                # leaking the canonical of the overall problem.
                desc = claim.description or f"step {idx + 1}"
                safe = StudentSafeFeedback(
                    first_misconception_redacted=(
                        f"the step \"{desc}\" doesn't add up — "
                        "re-check that calculation"
                    ),
                )
                if span is not None:
                    span["payload"] = {
                        "verdict": Verdict.WRONG.value,
                        "reason_code": "arithmetic_failed",
                        "failed_claim": desc[:120],
                        "path": "two_llm",
                    }
                return GradingResult(
                    verdict=Verdict.WRONG,
                    private_canonical=canonical_str,
                    student_safe_feedback=safe,
                    student_value=(student_input or "").strip(),
                    reasoning=(
                        "math: claim "
                        f"{idx + 1} failed arithmetic verification"
                    ),
                    reason_code="arithmetic_failed",
                    bare_answer=bare,
                )

        # Step B — conclusion vs canonical.
        conc = extraction.conclusion
        is_multi = isinstance(canonical_value, list) and canonical_value and \
            all(isinstance(e, dict) and "value" in e for e in canonical_value)

        # Build the candidate-value list from the conclusion. For Y/N,
        # T/F, and MCQ-letter canonicals, we may also fall back to the
        # extracted label.
        candidate_values: list[Any] = []
        raw_answer = conc.answer_extracted_value
        if isinstance(raw_answer, list):
            candidate_values.extend(raw_answer)
        elif raw_answer is not None:
            candidate_values.append(raw_answer)

        if is_multi:
            matched_slots: list[dict] = []
            for slot in canonical_value:
                slot_val = slot.get("value")
                if any(values_equivalent(slot_val, c) for c in candidate_values):
                    matched_slots.append(slot)
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
                student_value=(
                    candidate_values[0] if candidate_values else None
                ),
                bare_answer=bare,
            )
            reason_code = None
            if verdict_kind == Verdict.WRONG:
                reason_code = "conclusion_inconsistent_with_canonical"
            if span is not None:
                span["payload"] = {
                    "verdict": verdict_kind.value,
                    "multi_slot": True,
                    "matched_slot_count": len(matched_slots),
                    "total_slot_count": len(canonical_value),
                    "reason_code": reason_code or "",
                    "path": "two_llm",
                }
            return GradingResult(
                verdict=verdict_kind,
                private_canonical=canonical_str,
                student_safe_feedback=safe,
                student_value=conc.statement or (student_input or "").strip(),
                reasoning="math: two-llm comparator (multi-slot)",
                reason_code=reason_code,
                bare_answer=bare,
            )

        # Single-slot path.
        # Build candidate set from numeric answer(s) and the answer
        # label (yes/no/true/false map to bools for boolean canonicals;
        # MCQ letters compare string-to-string).
        match = False
        if candidate_values:
            for c in candidate_values:
                if values_equivalent(canonical_value, c):
                    match = True
                    break
        if not match and conc.answer_extracted_label:
            label = conc.answer_extracted_label.strip().lower()
            if isinstance(canonical_value, bool):
                if label in {"yes", "true"} and canonical_value is True:
                    match = True
                elif label in {"no", "false"} and canonical_value is False:
                    match = True
            elif isinstance(canonical_value, str):
                if label.upper() == canonical_value.strip().upper():
                    match = True

        if match:
            verdict_kind = Verdict.CORRECT
            safe = self._build_math_safe_feedback(
                verdict_kind=verdict_kind,
                canonical_value=canonical_value,
                student_value=(
                    candidate_values[0] if candidate_values else None
                ),
                bare_answer=bare,
            )
            if span is not None:
                span["payload"] = {
                    "verdict": verdict_kind.value,
                    "path": "two_llm",
                }
            return GradingResult(
                verdict=verdict_kind,
                private_canonical=canonical_str,
                student_safe_feedback=safe,
                student_value=conc.statement or (student_input or "").strip(),
                reasoning="math: two-llm comparator (single-slot)",
                bare_answer=bare,
            )

        # No match — but the student attempted an answer (is_attempt=True
        # already verified upstream). Distinguish "answered nothing
        # numeric" (no candidate value AND no label) from
        # "answered the wrong thing".
        if not candidate_values and not conc.answer_extracted_label:
            # Working shown without a stated final answer.
            if extraction.claims:
                verdict_kind = Verdict.PARTIAL
                safe = StudentSafeFeedback(
                    what_right="your working has the right pieces",
                    what_missing="state your final answer clearly",
                )
                if span is not None:
                    span["payload"] = {
                        "verdict": verdict_kind.value,
                        "path": "two_llm",
                        "reason_code": "no_conclusion_stated",
                    }
                return GradingResult(
                    verdict=verdict_kind,
                    private_canonical=canonical_str,
                    student_safe_feedback=safe,
                    student_value=conc.statement
                        or (student_input or "").strip(),
                    reasoning="math: working shown, no stated conclusion",
                    bare_answer=bare,
                )

        # The student stated something that doesn't match the canonical.
        verdict_kind = Verdict.WRONG
        safe = self._build_math_safe_feedback(
            verdict_kind=verdict_kind,
            canonical_value=canonical_value,
            student_value=(
                candidate_values[0] if candidate_values else None
            ),
            bare_answer=bare,
        )
        if span is not None:
            span["payload"] = {
                "verdict": verdict_kind.value,
                "reason_code": "conclusion_inconsistent_with_canonical",
                "path": "two_llm",
            }
        return GradingResult(
            verdict=verdict_kind,
            private_canonical=canonical_str,
            student_safe_feedback=safe,
            student_value=conc.statement or (student_input or "").strip(),
            reasoning="math: two-llm comparator — conclusion mismatch",
            reason_code="conclusion_inconsistent_with_canonical",
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
        """Two-LLM non-math grader.

        Companion to the math two-LLM grader. Same shape: a student
        parser (LLM-B) feeds a judge (LLM-C). Python pre/post-checks
        handle the structural cases the LLMs shouldn't decide:
        meta input, self-reported guesses, canonical leakage.

        Pipeline:
          1. Tier 0 — deterministic direct match for LessonStep
             answer_type ∈ {multiple_choice, true_false, short_numeric}.
             Round-trip-free fast path. Mirrors the math bare-numeric
             fast-path.
          2. Tier 1 — bank grader when the OpenQuestion resolves to a
             bank row with a canonical.
          3. LLM-B — STUDENT_RESPONSE_SYSTEM parses the student input
             into structured {is_attempt, hedge_marker, claims,
             conclusion}.
          4. Pre-check: ``is_attempt=false`` → WRONG reason_code=
             "meta_input". No LLM-C call needed.
          5. LLM-C — NON_MATH_JUDGE_SYSTEM reads question + KB + LLM-B
             output and emits the verdict + redacted feedback.
          6. Post-checks: hedge-marker downgrade, canonical-leak
             redaction guard, reason_code propagation.
        """
        with emit_span("audit", "grader.non_math") as span:
            # Tier 0 — deterministic direct match (LessonStep only).
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

            stem = request.open_question.rendered_stem or ""
            student_input = request.student_input or ""

            # LLM-B — parse the student response.
            response = self._extract_student_response_dsl(
                question_stem=stem, student_response=student_input,
            )
            if not response.ok:
                # Ternary contract: no client / parse refusal → WRONG so
                # the engine flows through the wrong-verdict path and
                # asks the student to retry. The grader_extraction_failed
                # reason_code preserves the observability signal.
                if span is not None:
                    span["payload"] = {
                        "verdict": Verdict.WRONG.value,
                        "stage": "student_response_extraction",
                        "error": response.error or "",
                    }
                return GradingResult(
                    verdict=Verdict.WRONG,
                    student_value=student_input.strip(),
                    reasoning=(
                        f"non-math: student-response extraction failed "
                        f"({response.error or 'unknown'})"
                    ),
                    reason_code="grader_extraction_failed",
                    bare_answer=False,
                )

            # Pre-check 1: meta input — student did not attempt an answer.
            # Ternary contract: route through WRONG so the engine asks
            # for an attempt.
            if not response.is_attempt:
                if span is not None:
                    span["payload"] = {
                        "verdict": Verdict.WRONG.value,
                        "reason_code": "meta_input",
                    }
                return GradingResult(
                    verdict=Verdict.WRONG,
                    student_value=student_input.strip(),
                    reasoning="non-math: student response is not an attempt",
                    reason_code="meta_input",
                    bare_answer=False,
                )

            # LLM-C — judge the structured student response.
            judgement = self._call_nonmath_judge(
                question_stem=stem,
                student_response_dsl=response.to_judge_payload(),
                sources=list(request.kb_chunks or []),
            )
            if not judgement.ok:
                # Ternary contract: judge failure → WRONG (no client /
                # raise). The plan biases ambiguous LLM output toward
                # PARTIAL, but a missing judge means we have no signal
                # at all — WRONG is the conservative engine-retry path.
                if span is not None:
                    span["payload"] = {
                        "verdict": Verdict.WRONG.value,
                        "stage": "non_math_judge",
                        "error": judgement.error or "",
                    }
                return GradingResult(
                    verdict=Verdict.WRONG,
                    student_value=student_input.strip(),
                    reasoning=(
                        f"non-math: judge call failed "
                        f"({judgement.error or 'unknown'})"
                    ),
                    reason_code="grader_extraction_failed",
                    bare_answer=False,
                )

            verdict_kind = judgement.verdict
            reason_code: Optional[str] = judgement.reason_code or None

            # Post-check 1: self-reported guess. A correct pick that the
            # student admits is a guess does NOT indicate mastery; the
            # move layer needs that signal to re-pose at the same
            # difficulty instead of advancing.
            if verdict_kind == Verdict.CORRECT and response.hedge_marker:
                verdict_kind = Verdict.PARTIAL
                reason_code = "self_reported_guess"

            # Post-check 2: programmatic canonical-leak redaction. The
            # LLM-C prompt instructs against putting the canonical in
            # the safe_feedback fields, but we belt-and-braces it.
            what_right, what_missing, first_misc = _redact_canonical_leak(
                private_canonical=judgement.private_canonical,
                what_right=judgement.what_right,
                what_missing=judgement.what_missing,
                first_misconception=judgement.first_misconception,
            )

            safe = StudentSafeFeedback(
                what_right=what_right,
                what_missing=what_missing,
                first_misconception_redacted=first_misc,
            )

            reasoning = "non-math: two-llm judge"
            if reason_code:
                reasoning = f"{reasoning} ({reason_code})"

            if span is not None:
                span["payload"] = {
                    "verdict": verdict_kind.value,
                    "tier": "two_llm",
                    "reason_code": reason_code or "",
                    "hedge_marker": response.hedge_marker,
                    "denies_canonical": response.conclusion.denies_canonical,
                }

            return GradingResult(
                verdict=verdict_kind,
                private_canonical=judgement.private_canonical,
                student_safe_feedback=safe,
                student_value=(
                    response.conclusion.stated_answer
                    or student_input.strip()
                ),
                reasoning=reasoning,
                citation=judgement.citation,
                reason_code=reason_code,
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
        work. Avoids the no-KB-grounding wrong-verdict trap (the
        dominant verdict regression observed in S1 + S5 evaluation
        sessions).
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
    # Non-math two-LLM pipeline (companion to math LLM-A + LLM-B)
    # ------------------------------------------------------------------

    def _extract_student_response_dsl(
        self,
        *,
        question_stem: str,
        student_response: str,
    ) -> _StudentResponseExtraction:
        """LLM-B for the non-math path. Parses student prose into a
        structured object the judge (LLM-C) consumes as data, not text."""
        with emit_span("audit", "grader.student_response_extractor") as span:
            client = self._resolve_student_response_client()
            if client is None:
                if span is not None:
                    span["payload"] = {"error": "no GRADER_STUDENT_RESPONSE client"}
                return _StudentResponseExtraction(
                    is_attempt=False, hedge_marker=False, claims=[],
                    conclusion=_StudentResponseConclusion(
                        stated_answer="", answer_label="",
                        denies_canonical=False,
                    ),
                    raw_text="",
                    error="no GRADER_STUDENT_RESPONSE client available",
                )
            try:
                resp = client.generate(
                    messages=[
                        {
                            "role": "user",
                            "content": render_student_response_user_prompt(
                                question_stem=question_stem,
                                student_response=student_response,
                            ),
                        },
                    ],
                    system_prompt=STUDENT_RESPONSE_SYSTEM,
                    max_tokens=1200,
                )
                raw_text = resp.content or ""
            except Exception as exc:
                if span is not None:
                    span["payload"] = {
                        "error": f"raise: {type(exc).__name__}",
                    }
                return _StudentResponseExtraction(
                    is_attempt=False, hedge_marker=False, claims=[],
                    conclusion=_StudentResponseConclusion(
                        stated_answer="", answer_label="",
                        denies_canonical=False,
                    ),
                    raw_text="",
                    error=f"student-response extraction raised: {type(exc).__name__}",
                )

            payload = _safe_json_loads(raw_text)
            if not isinstance(payload, dict):
                if span is not None:
                    span["payload"] = {"error": "non-dict JSON"}
                return _StudentResponseExtraction(
                    is_attempt=False, hedge_marker=False, claims=[],
                    conclusion=_StudentResponseConclusion(
                        stated_answer="", answer_label="",
                        denies_canonical=False,
                    ),
                    raw_text=raw_text,
                    error="LLM-B did not return a JSON object",
                )

            raw_claims = payload.get("claims") or []
            claims: list[dict] = []
            if isinstance(raw_claims, list):
                for entry in raw_claims:
                    if not isinstance(entry, dict):
                        continue
                    claims.append({
                        "id": str(entry.get("id", "")).strip(),
                        "text": str(entry.get("text", "")).strip(),
                    })

            raw_conc = payload.get("conclusion") or {}
            if not isinstance(raw_conc, dict):
                raw_conc = {}
            conclusion = _StudentResponseConclusion(
                stated_answer=str(raw_conc.get("stated_answer", "")).strip(),
                answer_label=str(
                    raw_conc.get("answer_label", "") or ""
                ).strip(),
                denies_canonical=bool(raw_conc.get("denies_canonical", False)),
            )

            extraction = _StudentResponseExtraction(
                is_attempt=bool(payload.get("is_attempt", False)),
                hedge_marker=bool(payload.get("hedge_marker", False)),
                claims=claims,
                conclusion=conclusion,
                raw_text=raw_text,
            )
            if span is not None:
                span["payload"] = {
                    "is_attempt": extraction.is_attempt,
                    "hedge_marker": extraction.hedge_marker,
                    "claim_count": len(extraction.claims),
                    "denies_canonical": conclusion.denies_canonical,
                }
            return extraction

    def _call_nonmath_judge(
        self,
        *,
        question_stem: str,
        student_response_dsl: dict,
        sources: list[str],
    ) -> _NonMathJudgement:
        """LLM-C for the non-math path. Judges the STRUCTURED student
        output (from LLM-B) against the question + KB sources.

        Reuses the GRADER_GROUNDED ModelConfig (Gemini-pinned for
        Google-grounding). The prompt is the new
        NON_MATH_JUDGE_SYSTEM, not the legacy adjudicator prompt.
        """
        with emit_span("audit", "grader.non_math_judge") as span:
            client = self._resolve_grounded_client()
            if client is None:
                if span is not None:
                    span["payload"] = {"error": "no GRADER_GROUNDED client"}
                # Surfaced to caller as not-ok → engine flows WRONG.
                return _NonMathJudgement(
                    verdict=Verdict.WRONG, private_canonical="",
                    what_right="", what_missing="", first_misconception="",
                    citation="", reason_code="", raw_text="",
                    error="no GRADER_GROUNDED client available",
                )
            try:
                resp = client.generate(
                    messages=[
                        {
                            "role": "user",
                            "content": render_non_math_judge_user_prompt(
                                question_stem=question_stem,
                                student_response_dsl=student_response_dsl,
                                sources=sources,
                            ),
                        },
                    ],
                    system_prompt=NON_MATH_JUDGE_SYSTEM,
                    max_tokens=2048,
                )
                raw_text = resp.content or ""
            except Exception as exc:
                logger.warning(
                    "[StudentGrader] non-math judge raised %s",
                    type(exc).__name__,
                )
                if span is not None:
                    span["payload"] = {"error": f"raise: {type(exc).__name__}"}
                return _NonMathJudgement(
                    verdict=Verdict.WRONG, private_canonical="",
                    what_right="", what_missing="", first_misconception="",
                    citation="", reason_code="", raw_text="",
                    error=f"non-math judge raised: {type(exc).__name__}",
                )

            payload = _safe_json_loads(raw_text)
            if not isinstance(payload, dict):
                # Surfaced as not-ok so the caller routes WRONG.
                if span is not None:
                    span["payload"] = {"error": "non-dict JSON"}
                return _NonMathJudgement(
                    verdict=Verdict.WRONG, private_canonical="",
                    what_right="", what_missing="", first_misconception="",
                    citation="", reason_code="", raw_text=raw_text,
                    error="LLM-C did not return a JSON object",
                )

            # Strict ternary — bias toward PARTIAL on ambiguous LLM
            # output per v2-prune-plan §4.1 ("if you cannot decide
            # between PARTIAL and WRONG, prefer PARTIAL").
            raw_verdict = str(payload.get("verdict", "partial")).strip().lower()
            if raw_verdict not in ("correct", "partial", "wrong"):
                raw_verdict = "partial"

            reason_code = str(payload.get("reason_code", "") or "").strip().lower()
            if reason_code not in (
                "", "known_misconception", "denies_canonical", "off_topic",
            ):
                reason_code = ""

            judgement = _NonMathJudgement(
                verdict=Verdict(raw_verdict),
                private_canonical=str(payload.get("private_canonical", "")).strip(),
                what_right=str(payload.get("what_right", "")).strip(),
                what_missing=str(payload.get("what_missing", "")).strip(),
                first_misconception=str(
                    payload.get("first_misconception", "")
                ).strip(),
                citation=str(payload.get("citation", "")).strip(),
                reason_code=reason_code,
                raw_text=raw_text,
            )
            if span is not None:
                span["payload"] = {
                    "verdict": judgement.verdict.value,
                    "reason_code": reason_code,
                }
            return judgement

    # ==================================================================
    # Tutor-claim adjudication
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

    def _resolve_student_claims_client(self):
        if self._student_claims_client_factory is not None:
            return self._student_claims_client_factory()
        return _build_client_for_purpose("grader_student_claims")

    def _resolve_student_response_client(self):
        if self._student_response_client_factory is not None:
            return self._student_response_client_factory()
        return _build_client_for_purpose("grader_student_response")


# ──────────────────────────────────────────────────────────────────────
# Module-private helpers
# ──────────────────────────────────────────────────────────────────────


def _build_client_for_purpose(purpose: str):
    """Resolve the ``ModelConfig`` for the purpose and return a client.

    Fail-soft on every error path — including the pytest-django "no
    database access" RuntimeError that triggers when grading tests
    omit a client factory injection. The caller treats a None return
    as "client unavailable" and degrades to the next tier.
    """
    try:
        from apps.llm.client import get_llm_client
        from apps.llm.models import ModelConfig
    except Exception:
        return None
    try:
        cfg = ModelConfig.get_for(purpose)
    except Exception as exc:
        logger.warning(
            "[StudentGrader] ModelConfig.get_for(%s) raised %s",
            purpose, type(exc).__name__,
        )
        return None
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


# Word-form numeric tokens — when ANY of these appears in the student
# input, the regex chain is unreliable (it cannot read "eight" as 8 and
# may pick a digit-form intermediate as the answer). Route to LLM-B in
# that case. Single-character tokens (a, i) deliberately excluded to
# avoid false positives.
_WORD_FORM_NUMERIC_RE = re.compile(
    r"\b("
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|"
    r"half|halves|quarter|third|thirds|fourth|fifths"
    r")\b",
    re.IGNORECASE,
)


def _has_word_form_numeric(text: str) -> bool:
    """True iff the student input contains a word-form numeric token.

    Such inputs (e.g. "the hidden variable is eight") are unreliable
    under regex extraction and must go through LLM-B so word-form
    answers and digit-form intermediates can be disambiguated.
    """
    return bool(_WORD_FORM_NUMERIC_RE.search(text or ""))


def _evaluate_claim_safely(expression: Any, variables: dict) -> Optional[Any]:
    """Evaluate a single LLM-B expression node via the math interpreter.

    Returns ``None`` when the expression is malformed (so the
    comparator skips the claim rather than crashing). Variables from
    LLM-B's ``variables`` block are passed through; the math
    interpreter dereferences ``{"var": "name"}`` against them.
    """
    if expression is None:
        return None
    try:
        return _evaluate_dsl_node(expression, variables or {}, MathTrace())
    except (DSLValidationError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


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
         prose-wrapped answer falls through to grounded grading.
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
    # Trailing bare number, optionally with a short unit/word suffix
    # ("9 SCR", "60%", "37 SCR?", "25 percent", "5 kg"). Anchored at
    # end-of-string so it picks up the FINAL number, ignoring earlier
    # numbers in the problem restatement. The optional suffix is at
    # most 8 alphanumeric / punctuation characters — long enough for
    # common units (SCR / cm / m³ / km / kg / percent / dollars) but
    # short enough that it won't accidentally swallow whole clauses.
    re.compile(
        r"(?:^|[^\w.])(-?\d+(?:\.\d+)?)"
        r"\s*[A-Za-z%³²]{0,8}"
        r"\s*[.!?]?\s*$"
    ),
)


_MCQ_PROSE_PATTERNS = (
    # "option B", "answer B", "answer: B", "answer is B", "choice B",
    # "pick B", "go with B", "guess B", "vote B".
    re.compile(
        r"(?i)\b(?:option|answer|choice|pick|guess|vote|select)"
        r"(?:\s+is)?\s*[:\-]?\s*([A-Da-d])\b"
    ),
    # "I pick B", "I choose B", "I'd choose B", "I'll go with B",
    # "I think it's B" (the apostrophe-s contraction observed in run-7),
    # "I'd say B", "I would go with B".
    re.compile(
        r"(?i)\bi(?:'?ll|'?d|\s+would)?\s+"
        r"(?:pick|choose|select|go\s+with|say|think|guess|vote)\s+"
        r"(?:it'?s\s+|it\s+is\s+|is\s+|the\s+answer\s+is\s+|that\s+)?([A-Da-d])\b"
    ),
    # "(B)", "[B]" — bracketed letter
    re.compile(r"(?i)^\s*[\(\[]([A-Da-d])[\)\]]"),
    # Bare letter alone or with terminal punctuation: "B", "b.", "B!"
    re.compile(r"(?i)^\s*([A-Da-d])\s*[\.\!]?\s*$"),
    # Letter at start of response followed by a delimiter (dash, comma,
    # space + "because" / "since" / etc.) — common rationale form
    # observed in MATHS-S1 / GEO-S5 transcripts ("B - it's …",
    # "b because …", "C, since …").
    re.compile(
        r"(?i)^\s*([A-Da-d])\s*(?:[\-,:;.]|\s+(?:because|since|as|for|—|–))"
    ),
    # "It's B", "it is B" — pronoun form at start
    re.compile(r"(?i)^\s*it'?s\s+([A-Da-d])\b"),
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


def _redact_canonical_leak(
    *,
    private_canonical: str,
    what_right: str,
    what_missing: str,
    first_misconception: str,
) -> tuple[str, str, str]:
    """Programmatic guard: if LLM-C's safe_feedback strings contain the
    canonical answer substring (case-insensitive), replace those fields
    with generic templates.

    The NON_MATH_JUDGE_SYSTEM prompt instructs the LLM not to put the
    canonical in safe_feedback, but we belt-and-braces it. Canonical
    leakage on wrong/partial verdicts is a Tier-1 confidentiality bug;
    we'd rather show a generic "you didn't quite get it" line than
    leak the answer.
    """
    canon = (private_canonical or "").strip().lower()
    if not canon:
        return what_right, what_missing, first_misconception
    def _scrub(field: str, replacement: str) -> str:
        if not field:
            return field
        if canon in field.lower():
            return replacement
        return field
    return (
        _scrub(what_right, "you have part of the answer"),
        _scrub(what_missing, "there's more to add"),
        _scrub(first_misconception, "the answer doesn't quite line up"),
    )
