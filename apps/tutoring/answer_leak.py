"""Answer-leak detector — single LLM judge.

When the student answers wrong, the tutor should give a HINT and let
them try again. Sometimes the tutor soft-reveals by paraphrasing the
canonical answer or naming the correct option letter.

Architecture (simplified 2026-05-17 — see
memory/tutor_state_drift_and_leak_simplification_plan.md): one LLM judge
running concurrently inside `run_all_judges` via `_run_leak_inline`.
Verdict surfaces as `CombinedJudgeResult.answer_leaked` and feeds
`apps/tutoring/regen/score.py` like every other judge signal.

Replaced the prior three-layer system (deterministic regex + LLM judge +
arbiter, ~525 LOC) after prod session 265 turn 8 showed the arbiter
overruling a correct LLM `leak=True` verdict on the literal phrase "the
answer is actually C". The deterministic / arbiter layers added more
noise than signal — see the plan file for evidence.

Skip cases:
  - empty response → nothing to scan
  - no question reference (neither bank Q nor chat-authored Q) → can't
    judge what's being leaked
  - upstream caller gates by `answer_was_wrong` in
    `apps/tutoring/judges/__init__.py::_run_leak_inline` so a tutor
    confirming a correct answer is not flagged

Returns None when no leak; otherwise a LeakVerdict with leaked=True so
the regen layer can suppress the canonical from its context (the
leak-aware regen path).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LeakVerdict:
    leaked: bool
    reason: str
    sources: List[str] = field(default_factory=list)
    llm_said: Optional[bool] = None
    elapsed_ms: int = 0


def detect_answer_leak(
    response: str,
    bank_question,                       # ExitTicketQuestion | LessonStep | None
    chat_authored_q: Optional[str],      # last tutor question text when chat-authored
    wrong_attempts: int,
    llm_client,                          # required — single LLM judge
    reveal_threshold: int = 3,           # kept for caller signature stability; intentionally unused
) -> Optional[LeakVerdict]:
    """Single LLM judge over the tutor response.

    `wrong_attempts` + `reveal_threshold` are accepted for signature
    stability with callers, but the policy after pilot directive
    2026-05-17 is: reveal is NEVER allowed. The tutor MOVES ON
    (pivots to a different question on the same concept) past the
    threshold rather than revealing.

    Returns None on no leak / skip; LeakVerdict on detected leak.
    """
    _ = wrong_attempts, reveal_threshold  # noqa: F841 — see docstring
    if not response or not response.strip():
        return None
    if bank_question is None and not chat_authored_q:
        return None

    t0 = time.monotonic()
    llm_leaked, llm_reason = _llm_check(
        response=response,
        bank_question=bank_question,
        chat_authored_q=chat_authored_q,
        llm_client=llm_client,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    logger.info(
        "[LeakDetect] llm=%s reason=%r — %d ms",
        llm_leaked, (llm_reason or '')[:120], elapsed,
    )
    if not llm_leaked:
        return None
    return LeakVerdict(
        leaked=True,
        reason=llm_reason or 'llm judge flagged leak',
        sources=['llm'],
        llm_said=True,
        elapsed_ms=elapsed,
    )


def _llm_check(
    response: str,
    bank_question,
    chat_authored_q: Optional[str],
    llm_client,
) -> (bool, str):
    """Call the unified grader's JUDGE_LEAK path. Returns (leaked, reason)."""
    if llm_client is None:
        return False, "no_llm_client"

    from apps.tutoring.exit_ticket_grader import (
        BatchLeakItem, JudgmentType, run_grading_batch,
    )

    correct_letter_for_judge: Optional[str] = None
    if bank_question is not None:
        q_type = (getattr(bank_question, 'question_type', None) or '').lower()
        stem = (getattr(bank_question, 'question_text', None)
                or getattr(bank_question, 'question', None)
                or getattr(bank_question, 'teacher_script', '')
                or '')
        if q_type == 'mcq':
            correct_letter = (getattr(bank_question, 'correct_answer', '') or '').strip().upper()
            options = {
                'A': getattr(bank_question, 'option_a', '') or '',
                'B': getattr(bank_question, 'option_b', '') or '',
                'C': getattr(bank_question, 'option_c', '') or '',
                'D': getattr(bank_question, 'option_d', '') or '',
            }
            canonical = options.get(correct_letter, '')
            if correct_letter in ('A', 'B', 'C', 'D'):
                correct_letter_for_judge = correct_letter
        else:
            options = None
            answer_data = getattr(bank_question, 'answer_data', None) or {}
            canonical = (
                getattr(bank_question, 'expected_answer', None)
                or getattr(bank_question, 'correct_answer', None)
                or (answer_data.get('model_answer') if isinstance(answer_data, dict) else None)
                or ''
            )
    else:
        # Chat-authored: question came from prior tutor turn; no canonical
        # so we pass the chat-authored question as both stem and canonical
        # — the judge's job becomes "did the response give away the
        # expected answer to its own question?"
        stem = chat_authored_q or ''
        canonical = chat_authored_q or ''
        options = None

    item = BatchLeakItem(
        index=0,
        question_text=stem,
        canonical_answer=canonical,
        response=response,
        options=options,
        correct_letter=correct_letter_for_judge,
    )
    try:
        results = run_grading_batch(
            [item], judgment_type=JudgmentType.JUDGE_LEAK, llm_client=llm_client,
        )
    except Exception as exc:
        logger.warning("[LeakDetect] LLM judge crash: %s", exc)
        return False, f"llm_crash: {exc}"

    if not results:
        return False, "llm_no_result"
    r = results[0]
    return bool(r.leaked), r.reason or ''
