"""Batched grader for open-ended exit-ticket answers.

Replaces the per-question LLM call in _llm_grade_exit_question with a
single batched LLM call that grades all written / non-deterministic
answers at once. Cuts exit-ticket submission cost by 5–10x on a
typical 10-question ticket where ~half need LLM grading.

Design mirrors combined_judge.py:
  - Structured JSON output (one entry per input item)
  - Runs on the dedicated judge_client (Sonnet 4) — separate from the
    tutoring model so submission grading doesn't compete for tutor
    bandwidth.
  - Deterministic results (MCQ letter match, numeric fast-path) are
    handled by the caller; only items needing semantic comparison
    reach the batched call.
  - Fail-open: if the batch call errors, individual items can fall
    back to the per-question grader.

Pre-grading filtering already done by the caller:
  - MCQ → letter match in _grade_exit_question (deterministic)
  - short_numeric / fill_in_blank with all-numeric blanks →
    numeric tolerance compare in _llm_grade_exit_question's
    fast-path (deterministic)

What reaches this batch:
  - short_answer / data_interpretation written responses
  - fill_in_blank with non-numeric blanks (e.g. concept names)
  - matching pairs that didn't land cleanly on deterministic match
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class BatchGradeItem:
    """One entry in the batch — represents a single open-ended answer
    to be graded against an expected solution."""
    index: int
    question_text: str
    q_type: str  # short_answer / data_interpretation / fill_in_blank / matching
    expected: str  # model_answer / blanks-joined / pairs-formatted (caller serialises)
    keywords: List[str] = field(default_factory=list)
    student_answer: str = ""  # caller pre-serialises lists/dicts to a readable string
    is_math: bool = False


@dataclass
class BatchGradeResult:
    """Verdict for one BatchGradeItem. Returned in input order."""
    index: int
    correct: bool
    reasoning: str = ""


_GRADE_SYSTEM = (
    "You are a strict-but-fair grader for student answers on a school "
    "exit ticket. For each item in the input array, decide whether the "
    "student's answer is CORRECT.\n"
    "\n"
    "Grading rules:\n"
    "  - For MATH answers (is_math=true): focus on the NUMERICAL RESULT. "
    "Working/method matters less than getting the right number. "
    "Accept equivalent forms (60/10 = 6, 30÷3 = 10).\n"
    "  - For SHORT_ANSWER / DATA_INTERPRETATION: accept paraphrases that "
    "demonstrate understanding of the key concepts AND match the model "
    "answer's main numerical or conceptual claim.\n"
    "  - For FILL_IN_BLANK: accept correct meaning even with minor "
    "spelling differences. Each blank should be approximately correct.\n"
    "  - For MATCHING: accept if the MAJORITY of pairs are correctly "
    "matched (>50%).\n"
    "  - When in doubt and the student is in the right ballpark, "
    "favour CORRECT — students shouldn't be penalised for phrasing.\n"
    "\n"
    "Output JSON ARRAY ONLY (no prose, no code fence) — one object per "
    "input item, in the same order as input:\n"
    "[\n"
    '  {"index": <int from input>, "correct": <true|false>, '
    '"reasoning": "<short why, <=120 chars>"}\n'
    "]"
)


def _build_user_prompt(items: List[BatchGradeItem]) -> str:
    payload = []
    for it in items:
        payload.append({
            "index": it.index,
            "question": (it.question_text or "")[:500],
            "q_type": it.q_type,
            "expected": (it.expected or "")[:400],
            "keywords": list(it.keywords or [])[:8],
            "student_answer": (it.student_answer or "")[:400],
            "is_math": bool(it.is_math),
        })
    return (
        "Grade each item below. Reply with ONLY the JSON array specified "
        "— no prose, no code fence.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def grade_written_responses_batch(
    items: List[BatchGradeItem],
    *,
    llm_client,
    max_tokens: int = 1024,
) -> List[BatchGradeResult]:
    """Grade multiple open-ended exit-ticket answers in ONE LLM call.

    Returns one BatchGradeResult per input item, ordered by input index.
    On any failure (no client, malformed JSON, network), every item
    defaults to ``correct=False`` so the caller can decide whether to
    fall back to the per-question grader.
    """
    if not items:
        return []
    if llm_client is None:
        logger.info("[ExitTicketGrader] no llm_client — defaulting all to False")
        return [BatchGradeResult(index=it.index, correct=False) for it in items]

    user_prompt = _build_user_prompt(items)
    logger.info(
        "[ExitTicketGrader] batch_grade: %d items (one LLM call)", len(items),
    )

    try:
        resp = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_GRADE_SYSTEM,
            max_tokens=max_tokens,
        )
        raw = (resp.content or "").strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("expected JSON array")
    except Exception as e:
        logger.warning("[ExitTicketGrader] batch call failed: %s", e)
        return [BatchGradeResult(index=it.index, correct=False) for it in items]

    by_index: dict[int, BatchGradeResult] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        correct = bool(entry.get("correct", False))
        reasoning = str(entry.get("reasoning") or "")[:200]
        by_index[idx] = BatchGradeResult(
            index=idx, correct=correct, reasoning=reasoning,
        )

    out: List[BatchGradeResult] = []
    for it in items:
        r = by_index.get(it.index)
        if r is not None:
            out.append(r)
        else:
            # Missing entry — default to wrong (conservative; caller
            # can choose to fall back to single-question grader).
            logger.info(
                "[ExitTicketGrader] batch missing index=%d — defaulting False",
                it.index,
            )
            out.append(BatchGradeResult(index=it.index, correct=False))

    correct_count = sum(1 for r in out if r.correct)
    logger.info(
        "[ExitTicketGrader] batch done: %d/%d correct",
        correct_count, len(out),
    )
    return out
