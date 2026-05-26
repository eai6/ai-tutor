"""Standalone repeat guards — run at the tool boundary, independently
of ``BANK_PREPOSE_RECHECK``.

Per Phase 1 §4.3 (and Phase 2 §2.1.1):
  - ``in_session_repeat_guard()`` — canonicalizes the visible stem,
    computes a Jaccard signature via the lifted-forward
    ``apps.tutoring.repeated_question``, compares against
    ``SessionRuntimeState.posed_question_ledger``. Match → refuse.
  - ``cross_session_repeat_guard()`` — checks
    ``StudentProfile.asked_questions`` for the composite
    ``"{source}:{id}"`` key with ``last_asked_at`` inside the
    avoidance window (default 14 days). Match → refuse.

Both run on EVERY tool-posed assessment question regardless of
``BANK_PREPOSE_RECHECK``. The derivability check is the only
validation that flag can disable; repeat prevention is always on.

Reading an empty ``asked_questions`` is a no-op until Phase 3 wires
the profiler write side.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from apps.tutoring.v2.contracts import (
    PosedQuestionLedgerEntry,
    QuestionRef,
)


DEFAULT_CROSS_SESSION_WINDOW_DAYS = 14
DEFAULT_JACCARD_THRESHOLD = 0.85


@dataclass(frozen=True)
class GuardResult:
    """Result of a repeat-guard check."""

    refused: bool
    reason: str = ""


# ----------------------------------------------------------------------
# Stem canonicalization + Jaccard
# ----------------------------------------------------------------------


def canonicalize_stem(text: str) -> str:
    """Canonical signature of a question stem, used for in-session
    repeat comparison.

    Delegates to ``apps.tutoring.repeated_question.normalise_question_signature``
    so the signature shape stays consistent with the legacy engine's
    repeat detector.
    """
    from apps.tutoring.repeated_question import normalise_question_signature

    return normalise_question_signature(text or "")


def _jaccard(a: str, b: str) -> float:
    from apps.tutoring.repeated_question import _jaccard_tokens

    return _jaccard_tokens(a, b)


# ----------------------------------------------------------------------
# In-session repeat guard
# ----------------------------------------------------------------------


def in_session_repeat_guard(
    candidate_signature: str,
    ledger: Iterable[PosedQuestionLedgerEntry],
    candidate_ref: Optional[QuestionRef] = None,
    threshold: float = DEFAULT_JACCARD_THRESHOLD,
) -> GuardResult:
    """Refuse if ``candidate_signature`` matches anything in the ledger.

    Matches on:
      - Exact source/id when ``candidate_ref`` is supplied (catches
        bank reposts that paraphrase).
      - Jaccard similarity above ``threshold`` on the canonicalized
        stem (catches paraphrases of an already-posed question).
    """
    if not candidate_signature:
        return GuardResult(refused=False)

    for entry in ledger:
        if (
            candidate_ref is not None
            and entry.source == candidate_ref.source
            and entry.id == candidate_ref.id
        ):
            return GuardResult(
                refused=True,
                reason=f"repeat:same_ref:{candidate_ref.composite_key()}",
            )
        sim = _jaccard(candidate_signature, entry.jaccard_signature)
        if sim >= threshold:
            return GuardResult(
                refused=True,
                reason=f"repeat:jaccard:{sim:.2f}",
            )
    return GuardResult(refused=False)


# ----------------------------------------------------------------------
# Cross-session repeat guard
# ----------------------------------------------------------------------


def cross_session_repeat_guard(
    candidate_ref: QuestionRef,
    asked_questions: Optional[dict],
    window_days: int = DEFAULT_CROSS_SESSION_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> GuardResult:
    """Refuse if this ref was asked of this student inside the
    avoidance window.

    Tolerates empty / None ``asked_questions`` (pre-cutover students
    have an empty map until Phase 3 writes to it).
    """
    if not asked_questions:
        return GuardResult(refused=False)

    key = candidate_ref.composite_key()
    entry = asked_questions.get(key)
    if not entry:
        return GuardResult(refused=False)

    last_asked_raw = entry.get("last_asked_at") if isinstance(entry, dict) else None
    if not last_asked_raw:
        return GuardResult(refused=False)

    try:
        last_asked = datetime.fromisoformat(last_asked_raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return GuardResult(refused=False)

    now = now or datetime.now(timezone.utc)
    if last_asked.tzinfo is None:
        last_asked = last_asked.replace(tzinfo=timezone.utc)
    age = now - last_asked
    if age < timedelta(days=window_days):
        return GuardResult(
            refused=True,
            reason=f"cross_session_repeat:{key}:age_days={age.days}",
        )
    return GuardResult(refused=False)
