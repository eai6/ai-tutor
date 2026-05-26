"""Conformance check orchestrator — Phase 2 §2.4.

Runs:
  1. Deterministic gates (cheapest, can short-circuit) — state
     coherence, safety, figure-ref, rule check, praise filter,
     answer leak.
  2. Fast-LLM classifier (the nine binary labels).
  3. Verdict-keyed rule matrix on the labels.
  4. Tutor-claim adjudication routing when the classifier flags a
     factual / arithmetic claim.

Returns a ``ConformanceResult`` with ``passed`` + violated rules
+ the classifier labels. The CALLER (TutorEngine) decides whether to
retry; the caller invokes ``ConformanceCheck.run`` a second time
after the rewrite and falls back to the safe terminal template if
the second pass also fails. Keeping the retry decision OUT of this
class keeps each `run()` call pure-w.r.t. a given candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from apps.tutoring.v2.contracts import GradingResult
from apps.tutoring.v2.services.conformance.classifier import (
    ClassifierLabels,
    run_conformance_classifier,
)
from apps.tutoring.v2.services.conformance.gates import (
    GateResult,
    run_answer_leak_check,
    run_figure_ref_check,
    run_praise_filter,
    run_rule_check,
    run_safety_check,
    run_state_coherence_check,
)
from apps.tutoring.v2.services.conformance.verdict_matrix import (
    apply_verdict_matrix,
)
from apps.tutoring.v2.services.move_prompts import MOVE_PROMPTS

logger = logging.getLogger(__name__)


@dataclass
class ConformanceResult:
    """End-state of one conformance pass over a candidate response."""

    passed: bool
    violations: List[str] = field(default_factory=list)
    labels: Optional[ClassifierLabels] = None
    fallback_used: bool = False  # set by TutorEngine when it routes to template
    retry_used: bool = False     # set by TutorEngine when this was a second pass
    payload: dict = field(default_factory=dict)

    def add_violation(self, rule: str, detail: str = "") -> None:
        entry = rule if not detail else f"{rule}: {detail}"
        self.violations.append(entry)


class ConformanceCheck:
    """Orchestrator. Stateless — instantiate per turn or per session."""

    def __init__(
        self,
        *,
        grader=None,
        classifier_client_factory=None,
        safety_llm_client=None,
        answer_leak_llm_client=None,
    ) -> None:
        """Optional injection seams for tests.

        ``grader`` is a ``StudentGrader`` used for tutor-claim
        adjudication. The classifier client factory is consumed by
        ``run_conformance_classifier``; the safety / answer-leak
        clients are passed through to the legacy judges they wrap.
        """
        self.grader = grader
        self._classifier_client_factory = classifier_client_factory
        self._safety_llm_client = safety_llm_client
        self._answer_leak_llm_client = answer_leak_llm_client

    def run(
        self,
        *,
        candidate_response: str,
        verdict: Optional[GradingResult],
        runtime_state,
        selected_move: str,
        prior_student_turn: str = "",
        open_question_stem: str = "",
        attached_media_count: int = 0,
        figure_facts: Optional[List[str]] = None,
        bank_stems: Optional[List[str]] = None,
        recent_student_turns: Optional[List[str]] = None,
        private_canonical: str = "",
        context=None,  # TutoringContext — for tutor-claim adjudication
        posed_via_tool: bool = False,
    ) -> ConformanceResult:
        """Run one full conformance pass over the candidate.

        Order matters — deterministic gates run FIRST and short-circuit
        cheaply, then the LLM classifier, then the matrix on its
        labels, then the tutor-claim adjudication route.
        """
        result = ConformanceResult(passed=True)
        allowed_moves = list(MOVE_PROMPTS.keys())

        # 1. State coherence — sanity check on engine-set values.
        gr = run_state_coherence_check(
            runtime_state=runtime_state,
            selected_move=selected_move,
            verdict=verdict,
            allowed_moves=allowed_moves,
        )
        if not self._record(gr, result):
            return result

        # 2. Safety pre-screen — child-safety floor.
        gr = run_safety_check(
            candidate_response,
            llm_client=self._safety_llm_client,
        )
        if not self._record(gr, result):
            return result

        # 3. Figure-ref (deictic + figure_facts quantitative claim guard).
        gr = run_figure_ref_check(
            candidate_response,
            attached_media_count=attached_media_count,
            figure_facts=figure_facts,
        )
        if not self._record(gr, result):
            return result

        # 4. Rule check (numeric mutation + authored-example provenance).
        gr = run_rule_check(
            candidate_response,
            open_question_stem=open_question_stem,
            bank_stems=bank_stems,
            recent_student_turns=recent_student_turns,
        )
        if not self._record(gr, result):
            return result

        # 5. Praise filter — strip bare-praise openers on non-correct.
        gr = run_praise_filter(candidate_response, verdict=verdict)
        if not self._record(gr, result):
            return result

        # 6. Answer-leak — scoped to wrong/partial + unanswered-open turns.
        gr = run_answer_leak_check(
            candidate_response,
            verdict=verdict,
            open_question_stem=open_question_stem,
            private_canonical=private_canonical,
            llm_client=self._answer_leak_llm_client,
        )
        if not self._record(gr, result):
            return result

        # 7. Conformance classifier — the nine binary labels.
        classifier_client = None
        if self._classifier_client_factory is not None:
            classifier_client = self._classifier_client_factory()
        labels = run_conformance_classifier(
            candidate_response=candidate_response,
            prior_student_turn=prior_student_turn,
            llm_client=classifier_client,
        )
        result.labels = labels

        # 8. Verdict-keyed rule matrix.
        matrix_violations = apply_verdict_matrix(
            labels=labels, verdict=verdict, posed_via_tool=posed_via_tool,
        )
        if matrix_violations:
            for mv in matrix_violations:
                result.add_violation(mv.rule, mv.description)
            result.passed = False
            return result

        # 9. Tutor-claim adjudication routing — when the classifier
        #    flagged a factual or arithmetic claim, route the response
        #    through the grader's grounded adjudicator. A
        #    ``contradicted`` or persistent ``unverified`` rejects the
        #    candidate.
        if (labels.contains_factual_claim or labels.contains_arithmetic_claim) \
                and self.grader is not None and context is not None:
            try:
                outcome = self.grader.adjudicate_tutor_claim(
                    context, candidate_response,
                )
            except Exception as exc:
                logger.warning(
                    "[ConformanceCheck] tutor-claim adjudication raised %s",
                    type(exc).__name__,
                )
                outcome = {"status": "unverified", "citation": ""}
            status = (outcome or {}).get("status", "unverified")
            if status == "contradicted":
                result.add_violation(
                    "tutor_claim_contradicted",
                    "tutor prose claim contradicted by grounded adjudication",
                )
                result.passed = False
                return result
            if status == "unverified":
                # Persistent unverified rejects: per analysis §3,
                # "contradicted OR persistent unverified → reject".
                # Conservative single-pass: any unverified rejects so
                # the rewrite can ground the claim or drop it.
                result.add_violation(
                    "tutor_claim_unverified",
                    "tutor prose claim could not be grounded — drop or cite",
                )
                result.passed = False
                return result

        return result

    # ------------------------------------------------------------------

    def _record(self, gate: GateResult, result: ConformanceResult) -> bool:
        """Append a failing gate to ``result``; return True iff passed."""
        if gate.passed or gate.skipped:
            return True
        result.add_violation(gate.name, gate.reason)
        result.passed = False
        return False
