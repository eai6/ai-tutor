"""Conformance check orchestrator — Phase 2 §2.4.

Runs the deterministic gates + the fast-LLM classifier + the
verdict-keyed rule matrix + the optional tutor-claim adjudication
route. Single retry on failure; on second failure → safe terminal
template (handled by the caller — see ``TutorEngine``).

Phase 2 §2.4 / Task #2 lands the foundations (gates + matrix). Phase 2
§2.4 / Task #8 lands the LLM classifier + the retry loop + the
tutor-claim adjudication routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from apps.tutoring.v2.contracts import GradingResult
from apps.tutoring.v2.services.conformance.classifier import ClassifierLabels


@dataclass
class ConformanceResult:
    """End-state of one conformance pass over a candidate response."""

    passed: bool
    violations: List[str] = field(default_factory=list)
    labels: Optional[ClassifierLabels] = None
    retry_used: bool = False
    fallback_used: bool = False
    payload: dict = field(default_factory=dict)


class ConformanceCheck:
    """Orchestrator stub — full impl in Phase 2 Task #8."""

    def __init__(self, llm_client_factory=None) -> None:
        """``llm_client_factory`` lets tests inject a fake LLM client."""
        self.llm_client_factory = llm_client_factory

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
    ) -> ConformanceResult:
        """Run the full conformance check.

        Phase 2 Task #8 wires this up. The implementation order is:
        deterministic gates first (cheapest, can short-circuit) → LLM
        classifier → verdict matrix → tutor-claim adjudication route
        → return.
        """
        raise NotImplementedError(
            "ConformanceCheck.run — Phase 2 Task #8"
        )
