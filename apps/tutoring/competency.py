"""Shared helpers for exit-ticket-driven lesson competency.

Used by:
  - The student exit-ticket submission endpoint
  - The new /api/lessons/<id>/competency/ endpoint
  - The teacher dashboard per-concept heatmap
  - The catalog competency indicator

See memory/lesson_competency_plan.md.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from django.contrib.auth.models import User
from django.db.models import Max

from apps.curriculum.models import Lesson
from apps.tutoring.models import (
    ExitTicket,
    ExitTicketAttempt,
    StudentLessonProgress,
)


def compute_passing_threshold_pct(exit_ticket: Optional[ExitTicket]) -> float:
    """Return the passing threshold as a 0.0-1.0 fraction.

    The denominator is questions_per_attempt (what the student sees in one
    sitting — default 10), NOT the bank size. Using bank size produces
    misleading thresholds like 8/35 ≈ 23% instead of the real 8/10 = 80%.
    """
    if not exit_ticket:
        return 0.8
    per_attempt = exit_ticket.questions_per_attempt or 10
    return round((exit_ticket.passing_score or 8) / per_attempt, 4)


def latest_attempt(student: User, lesson: Lesson) -> Optional[ExitTicketAttempt]:
    """Return the MOST RECENT ExitTicketAttempt for this student+lesson,
    or None if none exist.

    Competency / score reads use "latest" semantics — we want to know
    where the student is RIGHT NOW, not where they were at their best.
    A student who passed once and is failing now should show as
    failing, not as mastered. The historical promotion is captured by
    StudentCompetencyRecord (permanent transcript) instead.
    """
    return (
        ExitTicketAttempt.objects.filter(
            student=student,
            exit_ticket__lesson=lesson,
            completed_at__isnull=False,
        )
        .order_by("-completed_at", "-started_at")
        .first()
    )


# Back-compat alias. Older callers used `best_attempt`; the semantics
# now are "latest", not "best". Imports that still reference the old
# name continue to work but get the new behaviour.
best_attempt = latest_attempt


def per_concept_breakdown(attempt: ExitTicketAttempt) -> List[Dict]:
    """Compute per-concept competency from a single attempt's `answers`.

    Each answer is expected to have `{concept_tag, correct}`. Concepts with
    empty tags are grouped under '(untagged)'.

    Returns a list of dicts sorted by pct ascending (weakest first):
      [{ "concept": str, "correct": int, "total": int, "pct": float }, ...]
    """
    answers = attempt.answers or []
    by_concept: Dict[str, Dict[str, int]] = {}
    for a in answers:
        tag = (a.get("concept_tag") or "").strip() or "(untagged)"
        bucket = by_concept.setdefault(tag, {"correct": 0, "total": 0})
        bucket["total"] += 1
        if a.get("correct"):
            bucket["correct"] += 1
    results = []
    for concept, bucket in by_concept.items():
        total = bucket["total"] or 1
        pct = bucket["correct"] / total
        results.append(
            {
                "concept": concept,
                "correct": bucket["correct"],
                "total": bucket["total"],
                "pct": round(pct, 4),
            }
        )
    results.sort(key=lambda r: (r["pct"], r["concept"]))
    return results


def weak_concepts(per_concept: List[Dict], threshold: float = 0.7) -> List[str]:
    return [r["concept"] for r in per_concept if r["pct"] < threshold]


def competency_snapshot(student: User, lesson: Lesson) -> Dict:
    """Build a full competency snapshot for a student on a lesson.

    Returns a dict safe to serialize as JSON. Empty / all-zero when the
    student has no attempts yet.
    """
    exit_ticket = ExitTicket.objects.filter(lesson=lesson).first()
    threshold_pct = compute_passing_threshold_pct(exit_ticket)
    progress = StudentLessonProgress.objects.filter(
        student=student, lesson=lesson,
    ).first()

    attempt = latest_attempt(student, lesson)

    # Note on field names: ``best_score_pct`` is kept as a key for
    # back-compat with downstream consumers, but its VALUE is now the
    # latest-attempt score (StudentLessonProgress.best_score is
    # overwritten on every attempt instead of monotonically tracked).
    # Same for ``best_attempt`` — populated from the most recent
    # ExitTicketAttempt.
    snapshot = {
        "lesson_id": lesson.id,
        "lesson_title": lesson.title,
        "mastery_level": progress.mastery_level if progress else "not_started",
        "best_score_pct": progress.best_score if progress and progress.best_score is not None else None,
        "latest_score_pct": progress.best_score if progress and progress.best_score is not None else None,
        "passing_threshold_pct": threshold_pct,
        "attempts_count": progress.attempts_count if progress else 0,
        "last_attempt_at": progress.last_attempt_at.isoformat() if progress and progress.last_attempt_at else None,
        "best_attempt": None,
        "latest_attempt": None,
        "per_concept": [],
        "weak_concepts": [],
    }

    if attempt is not None:
        per_concept = per_concept_breakdown(attempt)
        attempt_payload = {
            "attempt_id": attempt.id,
            "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
            "score": attempt.score,
            "total": len(attempt.answers or []),
            "passed": attempt.passed,
        }
        snapshot["best_attempt"] = attempt_payload  # back-compat alias
        snapshot["latest_attempt"] = attempt_payload
        snapshot["per_concept"] = per_concept
        snapshot["weak_concepts"] = weak_concepts(per_concept)

    return snapshot


def attempt_response_block(
    attempt_score: int,
    attempt_results: List[Dict],
    exit_ticket: Optional[ExitTicket],
    progress: Optional[StudentLessonProgress],
) -> Dict:
    """Build the per-attempt block added to the exit-ticket submission
    response so the student UI can show score + threshold + per-concept."""
    total = len(attempt_results) or 1
    score_pct = round(attempt_score / total, 4)
    threshold_pct = compute_passing_threshold_pct(exit_ticket)
    per_concept = []
    by_concept: Dict[str, Dict[str, int]] = {}
    for r in attempt_results:
        tag = (r.get("concept_tag") or "").strip() or "(untagged)"
        bucket = by_concept.setdefault(tag, {"correct": 0, "total": 0})
        bucket["total"] += 1
        if r.get("is_correct"):
            bucket["correct"] += 1
    for concept, bucket in by_concept.items():
        pct = bucket["correct"] / (bucket["total"] or 1)
        per_concept.append(
            {
                "concept": concept,
                "correct": bucket["correct"],
                "total": bucket["total"],
                "pct": round(pct, 4),
            }
        )
    per_concept.sort(key=lambda r: (r["pct"], r["concept"]))
    return {
        "score": attempt_score,
        "total": total,
        "score_pct": score_pct,
        "passing_threshold": exit_ticket.passing_score if exit_ticket else 8,
        "passing_threshold_pct": threshold_pct,
        "per_concept": per_concept,
        "weak_concepts": [r["concept"] for r in per_concept if r["pct"] < 0.7],
        "best_score_pct": progress.best_score if progress and progress.best_score is not None else None,
        "attempts_count": progress.attempts_count if progress else 0,
        "mastery_level": progress.mastery_level if progress else "not_started",
    }
