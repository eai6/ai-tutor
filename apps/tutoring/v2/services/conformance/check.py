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
    run_open_question_stickiness_check,
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


# ──────────────────────────────────────────────────────────────────────
# Conformance-failure classification — Fix 3 (pose-question two-phase
# commit). On a first-attempt conformance failure, TutorEngine asks
# whether the violations are PROSE_ONLY (re-render prose; hold the
# first attempt's PendingPose; Phase A does not re-run) or POSE_RELATED
# / MIXED (full retry; Phase A may pick a different slot).
#
# Each set holds the *rule-name prefix* that violations carry on
# ``ConformanceResult.violations`` (entries are formatted as
# ``"<rule>: <detail>"`` — see ``ConformanceResult.add_violation``).
# ──────────────────────────────────────────────────────────────────────

PROSE_ONLY_VIOLATIONS: frozenset[str] = frozenset({
    # Deterministic gates whose subject is prose shape, not the pose
    # itself. ``state_coherence`` is engine-internal sanity; the gate's
    # failure modes are unrelated to which slot was posed. ``safety``,
    # ``figure_ref``, ``rule_check``, ``praise_filter``, ``answer_leak``
    # all scan the visible response text.
    "state_coherence",
    "safety",
    "figure_ref",
    "rule_check",
    "praise_filter",
    "answer_leak",
    # Tutor-claim adjudication — the prose makes a factual claim that
    # the grounded adjudicator could not verify. Holding the pose and
    # re-rendering prose is the correct retry: drop / cite the claim.
    "tutor_claim_contradicted",
    "tutor_claim_unverified",
    # Verdict-keyed matrix rules — every one is a shape constraint on
    # the prose response (affirm / refute / hand-floor-back / partial
    # feedback shape / uncertainty surfacing). The pose itself is not
    # what triggers any of these.
    "correct__no_refutation",
    "correct__hands_floor_back",
    "wrong__no_affirmation",
    "wrong__hands_floor_back",
    "partial__no_bare_affirm",
    "partial__no_bare_refute",
    "partial__feedback_shape",
    "partial__hands_floor_back",
    "unverified__no_affirm",
    "unverified__no_refute",
    "unverified__surfaces_uncertainty",
    "unverified__hands_floor_back",
    "no_verdict_claim__no_affirm",
    "no_verdict_claim__no_refute",
    "no_verdict_claim__hands_floor_back",
})

POSE_RELATED_VIOLATIONS: frozenset[str] = frozenset({
    # The candidate posed a question in prose despite the pose tool
    # being available — the LLM must instead emit a tool_use block. A
    # held pose is moot here (there is none); the retry must re-run
    # Phase A so the tool path can be taken.
    "all__no_assessment_in_prose",
    # Drift: the candidate's pending_pose targets a NEW slot while the
    # original open question is still live. Holding the drifting pose
    # would re-fail the same gate; the retry must let Phase A pick the
    # right slot (or no slot).
    "open_question_stickiness",
    # Extractor-derived violations on prose-stacked questions / no
    # active end. Both are emitted by the question_extractor (see
    # ``TutorEngine._extractor_violations``) and indicate the LLM
    # emitted assessment-shaped prose; the retry needs Phase A so a
    # legitimate tool-routed pose can replace the prose questions.
    "one_question_per_turn",
    "active_end_required",
})


def _violation_rule_name(violation: str) -> str:
    """Extract the rule-name prefix from a violation string.

    Violations are written as ``"<rule>: <detail>"`` by
    ``ConformanceResult.add_violation``. When no ``:`` separator is
    present the whole string is the rule name.
    """
    head, _sep, _rest = violation.partition(":")
    return head.strip()


def classify_conformance_failure(
    violations: list[str],
    *,
    pending_pose=None,
) -> str:
    """Return one of ``'prose_only'``, ``'pose_related'``, ``'mixed'``.

    Sub-case B of §2.3.1: ``all__no_assessment_in_prose`` flips to
    ``prose_only`` when the candidate already committed a valid
    ``PendingPose`` (sub-case B) — the prose question is a duplicate
    of an over-eager LLM that ALSO emitted a tool_use; re-rendering
    prose with the held pose is the right behaviour.
    """
    has_prose = False
    has_pose = False
    for v in violations:
        name = _violation_rule_name(v)
        if not name:
            continue
        if name == "all__no_assessment_in_prose" and pending_pose is not None:
            # Sub-case B — over-eager LLM emitted BOTH a tool_use and a
            # prose assessment question. The held pose is valid; the
            # prose is the duplicate.
            has_prose = True
            continue
        if name in POSE_RELATED_VIOLATIONS:
            has_pose = True
        elif name in PROSE_ONLY_VIOLATIONS:
            has_prose = True
        else:
            # Unknown rule names default to mixed so the safe path
            # (full retry) runs. New conformance rules must be added
            # to one of the two sets to be classified.
            has_pose = True
            has_prose = True
    if has_pose and not has_prose:
        return "pose_related"
    if has_prose and not has_pose:
        return "prose_only"
    if not has_pose and not has_prose:
        # Empty / unknown — be conservative and run the full retry.
        return "pose_related"
    return "mixed"


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
        lesson_has_media: bool = True,
        pending_pose=None,  # PendingPose | None — for open-question stickiness
        pose_tool_available: bool = True,
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
        # When the lesson has zero media available AND the candidate
        # was posed via the tool, any deictic phrase ("the diagram")
        # is in the curriculum-authored bank stem — not LLM-authored.
        # Failing conformance there strands the student on a question
        # whose visual support the curriculum never published; the
        # right scope of the figure_ref gate is LLM authorship, not
        # curriculum content. The Layer-2 quantitative-claim check
        # only fires when ``attached_media_count > 0``, so the
        # short-circuit here is safe.
        if posed_via_tool and not lesson_has_media:
            gr = GateResult(
                passed=True,
                name="figure_ref",
                skipped=True,
                reason="bank_stem_deictic_no_media",
            )
        else:
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

        # 4b. Open-question stickiness — safety floor that catches
        # scaffold/probe moves drifting onto a new bank item while
        # the original open question is still live. Cheap; only
        # active for the probe-shaped moves. Rejection routes through
        # the standard retry path; on second failure the per-move
        # terminal restates the open question (the intended recovery).
        gr = run_open_question_stickiness_check(
            selected_move=selected_move,
            runtime_state=runtime_state,
            pending_pose=pending_pose,
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
            labels=labels,
            verdict=verdict,
            posed_via_tool=posed_via_tool,
            selected_move=selected_move,
            pose_tool_available=pose_tool_available,
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
        #
        # Scope: the adjudicator is designed to catch *stray* factual
        # claims in moves where prose teaching is incidental (a hint, a
        # pose, a confirmation). It is NOT designed to block teaching
        # moves whose entire purpose is to make factual claims:
        # ``explain`` and ``worked_example`` are explicitly invited to
        # define terms, state rules, walk arithmetic steps. Running
        # the adjudicator on every claim they make would reject every
        # legitimate explanation when KB coverage is sparse — exactly
        # the failure mode the MATHS-S1 / GEO-S5 evals surfaced. The
        # safety floor for these moves stays: the answer-leak gate,
        # the figure-ref gate, and the rule_check (numeric mutation)
        # still run. The adjudicator-bypass narrows from "every
        # factual-claim response" to "factual-claim responses outside
        # the teaching moves" — still a safety net, but matched to
        # where stray claims actually live.
        _teaching_moves = {"explain", "worked_example"}
        if selected_move in _teaching_moves:
            return result

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
