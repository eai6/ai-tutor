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
    # For fill_in_blank: per-blank arrays so the grader can evaluate
    # each blank separately (synonyms, partial credit visibility) and
    # the frontend can colour each blank individually. Empty lists for
    # other q_types.
    expected_blanks: List[str] = field(default_factory=list)
    student_blanks: List[str] = field(default_factory=list)
    # For matching: per-pair arrays. Each entry is {"left": "...",
    # "right": "..."}. expected_pairs is the canonical pairing from
    # the question's answer_data; student_pairs is what the student
    # actually submitted (left_label → chosen_right). Same purpose as
    # the blank lists above: gives the grader the structure it needs
    # to emit per-pair verdicts.
    expected_pairs: List[dict] = field(default_factory=list)
    student_pairs: List[dict] = field(default_factory=list)


@dataclass
class PartVerdict:
    """Per-element verdict (one blank for fill_in_blank, one pair for
    matching). Same shape regardless of element type so the prompt
    schema can stay uniform."""
    is_correct: bool
    reasoning: str = ""


# Backwards-compat alias — earlier code imported BlankVerdict directly.
BlankVerdict = PartVerdict


@dataclass
class BatchGradeResult:
    """Verdict for one BatchGradeItem. Returned in input order."""
    index: int
    correct: bool
    reasoning: str = ""
    # For fill_in_blank: one PartVerdict per blank, in input order.
    # For matching: one PartVerdict per pair, in expected_pairs order.
    # Empty for other q_types or when the grader skipped per-element
    # detail — caller falls back to the question-level `correct` flag.
    parts: List[PartVerdict] = field(default_factory=list)

    @property
    def blanks(self) -> List[PartVerdict]:
        """Legacy alias — early callers expected `.blanks`. Use `.parts`."""
        return self.parts


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
    "  - For FILL_IN_BLANK: judge the WHOLE FILLED-IN SENTENCE, not "
    "individual blank words in isolation. The input gives you "
    "expected_blanks and student_blanks; mentally substitute each into "
    "the sentence template and ask 'does the student's completed "
    "sentence read correctly and convey the same meaning as the "
    "expected one?' Be GENEROUS with synonyms ('equal' = 'even' = "
    "'fair', 'poverty' = 'inequality' = 'low income', 'variety' = "
    "'wider range' = 'more options') and minor spelling slips. Reject "
    "only when a substituted word makes the sentence WRONG or "
    "MEANINGFULLY different — not when it's just phrased differently.\n"
    "  - For MATCHING: judge each pair IN THE CONTEXT OF THE WHOLE "
    "QUESTION, not in isolation. The input gives you expected_pairs "
    "(canonical pairing) and student_pairs (what the student chose, "
    "aligned to the same left-side order). For each student pair, ask "
    "'is this right-side choice a sensible match for this left-side "
    "item, given what the question is teaching and given the other "
    "options the student saw?'\n"
    "    * Accept SEMANTIC EQUIVALENTS on the right-side: "
    "'fish and seafood' ≈ 'seafood products' ≈ 'tuna and fish'; "
    "'tea and coffee' ≈ 'coffee, tea'; 'tropical fruits' ≈ "
    "'mangoes and bananas'. Word order doesn't matter, plurals "
    "don't matter, minor wording shifts don't matter.\n"
    "    * Accept PARTIALLY-CORRECT matches if they reflect a "
    "real attribute of the left-side item (e.g. Madagascar "
    "exports vanilla AND some petroleum — vanilla is the "
    "primary expected pairing but a petroleum answer shouldn't "
    "be marked as wildly wrong if both are true).\n"
    "    * REJECT pairings that are factually wrong or culturally "
    "incorrect (e.g. Mauritius → wheat: Mauritius doesn't "
    "produce significant wheat, this is wrong).\n"
    "    * The overall question is correct only when EVERY pair "
    "passes. But each pair's verdict should reflect its own "
    "merit, not the question's overall pass/fail.\n"
    "  - When in doubt and the student is in the right ballpark, "
    "favour CORRECT — students shouldn't be penalised for phrasing.\n"
    "\n"
    "Output JSON ARRAY ONLY (no prose, no code fence) — one object per "
    "input item, in the same order as input. For most items:\n"
    "[\n"
    '  {"index": <int from input>, "correct": <true|false>, '
    '"reasoning": "<short why, <=120 chars>"}\n'
    "]\n"
    "\n"
    "FOR FILL_IN_BLANK and MATCHING items, ALSO include a per-element "
    "breakdown so the frontend can colour each blank/pair "
    "individually. Use the `parts` field — same shape regardless of "
    "element type:\n"
    "[\n"
    '  {"index": 0, "correct": false, "reasoning": "1 of 2 wrong",\n'
    '   "parts": [\n'
    '     {"is_correct": true, "reasoning": "even ≈ equal"},\n'
    '     {"is_correct": false, "reasoning": "expected poverty, got X"}\n'
    '   ]}\n'
    "]\n"
    "Length of `parts` MUST equal the number of expected_blanks "
    "(fill_in_blank) or expected_pairs (matching) in the input. "
    "Order matches the input order."
)


def _build_user_prompt(items: List[BatchGradeItem]) -> str:
    payload = []
    for it in items:
        entry = {
            "index": it.index,
            "question": (it.question_text or "")[:500],
            "q_type": it.q_type,
            "expected": (it.expected or "")[:400],
            "keywords": list(it.keywords or [])[:8],
            "student_answer": (it.student_answer or "")[:400],
            "is_math": bool(it.is_math),
        }
        # Surface per-element arrays so the grader can evaluate each
        # blank / pair independently. Capped to keep the prompt
        # compact even on long-element questions.
        if it.q_type == 'fill_in_blank' and it.expected_blanks:
            entry["expected_blanks"] = [
                str(b)[:120] for b in it.expected_blanks
            ]
            entry["student_blanks"] = [
                str(b)[:120] for b in (it.student_blanks or [])
            ]
        if it.q_type == 'matching' and it.expected_pairs:
            entry["expected_pairs"] = [
                {"left": str(p.get("left", ""))[:80],
                 "right": str(p.get("right", ""))[:120]}
                for p in it.expected_pairs
            ]
            entry["student_pairs"] = [
                {"left": str(p.get("left", ""))[:80],
                 "right": str(p.get("right", ""))[:120]}
                for p in (it.student_pairs or [])
            ]
        payload.append(entry)
    return (
        "Grade each item below. Reply with ONLY the JSON array specified "
        "— no prose, no code fence.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def build_batch_grade_item(
    index: int,
    question,
    student_answer,
    *,
    is_math: bool = False,
) -> BatchGradeItem:
    """Serialise one ExitTicketQuestion into a BatchGradeItem.

    Extracted from ConversationalTutor._build_batch_grade_item so the
    same builder can be reused by mid-lesson artifact grading
    (bank_grader.grade_bank_response → grade_with_llm_single) without
    duplicating the per-q_type serialisation logic. Per pilot directive
    2026-05-16: mid-lesson grading must mirror exit-ticket grading
    exactly, no separate grader path.
    """
    q_type = getattr(question, 'question_type', 'mcq') or 'mcq'
    data = question.answer_data or {}

    extra_kwargs: dict = {}
    if q_type == 'fill_in_blank':
        blanks = data.get('blanks', []) or []
        student_blanks = (
            student_answer if isinstance(student_answer, list)
            else [student_answer]
        )
        expected = "; ".join(str(b) for b in blanks)
        student_str = "; ".join(str(b) for b in student_blanks)
        extra_kwargs = {
            'expected_blanks': [str(b) for b in blanks],
            'student_blanks': [str(b) for b in student_blanks],
        }
    elif q_type == 'matching':
        pairs = data.get('pairs', []) or []
        expected = "; ".join(
            f"{p.get('left', '')} → {p.get('right', '')}"
            for p in pairs
        )
        student_map = student_answer if isinstance(student_answer, dict) else {}
        student_str = "; ".join(
            f"{k} → {v}" for k, v in student_map.items()
        )
        extra_kwargs = {
            'expected_pairs': [
                {'left': str(p.get('left', '')),
                 'right': str(p.get('right', ''))}
                for p in pairs
            ],
            'student_pairs': [
                {
                    'left': str(p.get('left', '')),
                    'right': str(
                        student_map.get(str(p.get('left', '')), ''),
                    ),
                }
                for p in pairs
            ],
        }
    else:  # short_answer / data_interpretation / short_numeric
        expected = str(
            data.get('model_answer', '')
            or getattr(question, 'correct_answer', '')
            or '',
        )
        student_str = str(student_answer or '')

    keywords = list(data.get('keywords', []) or [])
    return BatchGradeItem(
        index=index,
        question_text=getattr(question, 'question_text', '') or '',
        q_type=q_type,
        expected=expected,
        keywords=keywords,
        student_answer=student_str,
        is_math=is_math,
        **extra_kwargs,
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
        # Optional per-element breakdown. The grader is INSTRUCTED to
        # emit this for fill_in_blank AND matching items under the
        # `parts` key; tolerate the legacy `blanks` key for the same
        # shape so an in-flight prompt change doesn't break parsing.
        parts_raw = entry.get("parts")
        if not isinstance(parts_raw, list):
            parts_raw = entry.get("blanks")  # legacy
        parts: List[PartVerdict] = []
        if isinstance(parts_raw, list):
            for b in parts_raw:
                if not isinstance(b, dict):
                    continue
                parts.append(PartVerdict(
                    is_correct=bool(b.get("is_correct", False)),
                    reasoning=str(b.get("reasoning") or "")[:200],
                ))
        by_index[idx] = BatchGradeResult(
            index=idx, correct=correct, reasoning=reasoning, parts=parts,
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
