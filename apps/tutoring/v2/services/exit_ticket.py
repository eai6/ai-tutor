"""ExitTicketService — selects + grades exit-ticket questions.

Phase 2 §2.6:
  - Selects a subset of pre-authored ``ExitTicketQuestion`` rows from
    the lesson's ``ExitTicket`` bank, excluding recently-attempted
    ones via ``ExitTicketAttempt`` history.
  - Each response routes through ``StudentGrader`` — ``bank_grader``
    first (deterministic), grounded adjudication fallback for
    free-text rubric items.
  - Aggregate pass/fail is a derived count vs ``ExitTicket.passing_score``
    computed by this service. NO "exit-ticket hold gate" and NO
    "force-clear after N hold cycles" (dropped per R6).
  - ``BANK_PREPOSE_RECHECK=on`` by default — bank questions get
    derivability + repeat guards via the same Phase 1 tool-boundary
    pipeline. When the flag flips off post-MVP, ONLY the derivability
    check is skipped; both repeat guards still run (§4.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from apps.tutoring.v2.contracts import (
    GradingRequest,
    GradingResult,
    OpenQuestion,
    QuestionSource,
    TutoringContext,
    Verdict,
)

logger = logging.getLogger(__name__)


@dataclass
class ExitTicketBatch:
    """Bundle returned by ``select_questions``."""

    exit_ticket_id: int
    question_ids: List[int]
    passing_score: int
    instructions: str = ""


@dataclass
class ExitTicketAggregate:
    """Aggregate result over an entire exit-ticket attempt."""

    total: int
    correct: int
    passing_score: int
    passed: bool
    per_question: List[dict]


class ExitTicketService:
    """Stateless. Per-attempt instance is fine — no shared state."""

    def __init__(self, grader=None) -> None:
        """``grader`` is a ``StudentGrader``. None → resolve lazily."""
        self._grader = grader

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select_questions(
        self,
        *,
        lesson_id: int,
        student_id: int,
        institution_id: int,
        max_questions: Optional[int] = None,
    ) -> Optional[ExitTicketBatch]:
        """Pick a subset from the lesson's bank, excluding recent attempts.

        Multi-tenancy-safe: filters by the lesson's exit ticket; the
        attempt-history exclusion is per-student. Returns ``None``
        when the lesson has no exit ticket configured.
        """
        try:
            from apps.tutoring.models import (
                ExitTicket,
                ExitTicketAttempt,
                ExitTicketQuestion,
            )
        except Exception:
            return None

        try:
            exit_ticket = (
                ExitTicket.objects
                .filter(lesson_id=lesson_id)
                .first()
            )
        except Exception:
            exit_ticket = None
        if exit_ticket is None:
            return None

        # Recently-attempted question IDs for this student — exclude
        # from the new pick.
        recent_qs_ids: set[int] = set()
        try:
            for attempt in ExitTicketAttempt.objects.filter(
                exit_ticket=exit_ticket,
                student_id=student_id,
            ).order_by("-pk")[:5]:
                resp = getattr(attempt, "responses", None) or {}
                if isinstance(resp, dict):
                    for raw_id in resp.keys():
                        try:
                            recent_qs_ids.add(int(raw_id))
                        except (TypeError, ValueError):
                            continue
        except Exception as exc:
            logger.warning(
                "[ExitTicketService] recent-attempts lookup raised %s",
                type(exc).__name__,
            )

        cap = max_questions or getattr(
            exit_ticket, "questions_per_attempt", 10,
        )

        try:
            picked_ids = list(
                ExitTicketQuestion.objects
                .filter(exit_ticket=exit_ticket)
                .exclude(pk__in=recent_qs_ids)
                .order_by("?")[: cap]
                .values_list("pk", flat=True)
            )
        except Exception as exc:
            logger.warning(
                "[ExitTicketService] selection query raised %s",
                type(exc).__name__,
            )
            return None

        if not picked_ids:
            # Fallback — if every question has been attempted, allow
            # repeats so the student isn't blocked.
            try:
                picked_ids = list(
                    ExitTicketQuestion.objects
                    .filter(exit_ticket=exit_ticket)
                    .order_by("?")[: cap]
                    .values_list("pk", flat=True)
                )
            except Exception:
                picked_ids = []

        return ExitTicketBatch(
            exit_ticket_id=exit_ticket.pk,
            question_ids=picked_ids,
            passing_score=exit_ticket.passing_score,
            instructions=getattr(exit_ticket, "instructions", "") or "",
        )

    # ------------------------------------------------------------------
    # Per-question grading
    # ------------------------------------------------------------------

    def grade_response(
        self,
        *,
        context: TutoringContext,
        exit_ticket_question_id: int,
        student_input: str,
        is_math: bool = False,
    ) -> GradingResult:
        """Route a single exit-ticket response through ``StudentGrader``.

        The grader's non-math path tries ``bank_grader`` first
        (deterministic match) and falls back to grounded adjudication
        for rubric-graded free-text. Math questions take the math
        path. Verdict shape is identical to conversational grading.
        """
        grader = self._resolve_grader()
        if grader is None:
            return GradingResult(verdict=Verdict.UNVERIFIED)

        open_q = OpenQuestion(
            source=QuestionSource.EXIT_TICKET_QUESTION,
            id=exit_ticket_question_id,
            rendered_stem=self._lookup_stem(exit_ticket_question_id),
        )
        return grader.grade_student_response(
            context,
            GradingRequest(
                open_question=open_q,
                student_input=student_input,
                is_math=is_math,
            ),
        )

    # ------------------------------------------------------------------
    # Aggregate scoring
    # ------------------------------------------------------------------

    def aggregate(
        self,
        *,
        per_question_results: List[GradingResult],
        passing_score: int,
    ) -> ExitTicketAggregate:
        """Derive pass/fail from the per-question verdicts.

        Per analysis §3: the "did the student pass" is a derived
        count, NOT a separate grader output. Only ``correct``
        verdicts count.
        """
        correct = sum(
            1 for r in per_question_results
            if r.verdict == Verdict.CORRECT
        )
        total = len(per_question_results)
        return ExitTicketAggregate(
            total=total,
            correct=correct,
            passing_score=passing_score,
            passed=(correct >= passing_score),
            per_question=[
                {
                    "verdict": r.verdict.value,
                    "student_value": r.student_value,
                }
                for r in per_question_results
            ],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_grader(self):
        if self._grader is not None:
            return self._grader
        try:
            from apps.tutoring.v2.services.student_grader import StudentGrader
            self._grader = StudentGrader()
            return self._grader
        except Exception as exc:
            logger.warning(
                "[ExitTicketService] grader resolution failed: %s",
                type(exc).__name__,
            )
            return None

    def _lookup_stem(self, question_id: int) -> str:
        try:
            from apps.tutoring.models import ExitTicketQuestion
            q = ExitTicketQuestion.objects.filter(pk=question_id).first()
            if q is None:
                return ""
            return getattr(q, "question_text", "") or ""
        except Exception:
            return ""
