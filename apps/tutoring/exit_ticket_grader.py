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
from enum import Enum
from typing import List, Optional, Sequence, Union

logger = logging.getLogger(__name__)


class JudgmentType(str, Enum):
    """The unified grader supports multiple judgment kinds. Each shares
    the same LLM client + retry + structured-output infrastructure.

    Added 2026-05-17 per memory/hint_vs_reveal_guards_plan.md W6 —
    extracting a single grading codepath so exit-ticket grading,
    mid-lesson grading, leak detection, repeat detection, and intent
    classification all flow through one implementation.
    """
    GRADE_CORRECTNESS = "grade_correctness"  # existing — is student answer right?
    JUDGE_LEAK        = "judge_leak"         # W1 — did tutor reveal canonical answer?
    JUDGE_REPEAT      = "judge_repeat"       # W14 — did tutor re-ask an earlier question?
    CLASSIFY_INTENT   = "classify_intent"    # W9 — attempt / confusion / off_topic


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
    "ACCEPTABLE PARAPHRASES (mark correct):\n"
    "  Expected: 'Geography studies Earth and its inhabitants.'\n"
    "  Student:  'Geography is about Earth and the things that live on it.' ✓\n"
    "  Student:  'The study of our planet and its people.' ✓\n"
    "  Student:  'Earth and the people / animals on it.' ✓\n"
    "  Expected: 'Use it to determine which direction you need to travel.'\n"
    "  Student:  'It tells you which way to go.' ✓\n"
    "  Student:  'For figuring out direction.' ✓\n"
    "\n"
    "UNACCEPTABLE (mark wrong even if related):\n"
    "  Expected: 'Geography studies Earth and its inhabitants.'\n"
    "  Student:  'Geography is a school subject.' ✗ — too narrow\n"
    "  Student:  'It studies maps.' ✗ — partial / missing inhabitants\n"
    "  Student:  'The study of countries.' ✗ — too narrow\n"
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


# =============================================================================
# JUDGE_LEAK — did the tutor reveal the canonical answer?  (W1)
# =============================================================================

@dataclass
class BatchLeakItem:
    """One leak-detection input for the single LLM leak judge.

    For chat-authored questions where there's no canonical bank entry,
    pass the LLM grader's best understanding of the expected answer
    as `canonical_answer` (it becomes the reference the judge checks
    against).

    For MCQ items, supplying `correct_letter` (the A/B/C/D the question
    expects) makes the LLM judge explicit about WHAT to look for —
    catches "the correct answer was A" style leaks that ambiguous
    canonical_answer-only payloads miss."""
    index: int
    question_text: str
    canonical_answer: str
    response: str
    options: Optional[dict] = None  # {'A': '...', 'B': '...', ...} for MCQ
    correct_letter: Optional[str] = None  # 'A' | 'B' | 'C' | 'D' for MCQ


@dataclass
class BatchLeakResult:
    index: int
    leaked: bool
    reason: str = ""


_LEAK_SYSTEM = (
    "You judge whether a tutor's response REVEALED the correct answer "
    "to a student during a tutoring session. Works for MCQ AND non-MCQ "
    "(short-answer, fill-in-blank, numeric, conceptual). You are the "
    "ONLY guard against the tutor giving the answer away — there is no "
    "arbiter, no second opinion. Get it right.\n"
    "\n"
    "Each input gives you:\n"
    "  - question: the question stem the student is trying to answer\n"
    "  - correct_answer_value: the actual answer text the student must\n"
    "    produce / pick. For MCQ this is the text of the correct\n"
    "    option (NOT just the letter). For short-answer / numeric, it's\n"
    "    the canonical expected text. THE TUTOR MUST NOT STATE THIS\n"
    "    VALUE OR A CLOSE PARAPHRASE.\n"
    "  - correct_letter: (MCQ only) the option letter A/B/C/D that\n"
    "    holds the canonical answer. The tutor must also not state\n"
    "    THIS letter. Absent for non-MCQ questions.\n"
    "  - options: (MCQ only) all four options A/B/C/D for context\n"
    "  - tutor_response: the message about to be sent to the student\n"
    "\n"
    "Your single job: read tutor_response and decide whether it would\n"
    "let the student copy/pick the correct answer without thinking.\n"
    "\n"
    "REVEAL (leaked=true) — flag ALL of these:\n"
    "  (a) Tutor states the correct_letter (MCQ), in ANY tense or\n"
    "      framing — even when wrapped in a conceptual explanation:\n"
    "      'the answer is B', 'the correct answer was A', 'it would\n"
    "      be C', 'should be D', 'A) True is correct', 'choose C',\n"
    "      'C is correct because...', 'the right one is B'. NAMING\n"
    "      THE LETTER IS A LEAK regardless of the WHY framing.\n"
    "  (b) Tutor states the correct_answer_value verbatim or with\n"
    "      trivial reordering. E.g. value='Readers would not know\n"
    "      what area the map represents' and tutor says 'Without it,\n"
    "      readers don't know what the map represents' → LEAK.\n"
    "      For short-answer: value='physical geography' and tutor\n"
    "      says 'this branch is physical geography' → LEAK.\n"
    "      For numeric: value='240°' and tutor says 'the answer\n"
    "      comes out to 240 degrees' → LEAK.\n"
    "  (c) Tutor PARAPHRASES correct_answer_value in different words\n"
    "      so the student can copy/pick it. E.g. value='Use it to\n"
    "      determine which direction you need to travel' and tutor\n"
    "      says 'It helps you figure out which direction to travel'\n"
    "      → LEAK.\n"
    "  (d) Tutor states the answer as a fact in a teach-back, even\n"
    "      while explaining. 'The correct answer was A because...'\n"
    "      and 'X is the answer here' both → LEAK.\n"
    "\n"
    "WORKED EXAMPLE (prod session 265 — the LLM judge correctly\n"
    "flagged this, then an arbiter wrongly overruled it. The arbiter\n"
    "no longer exists; your verdict is final.):\n"
    "  question: 'A street map of Victoria (1:5,000) and a national\n"
    "    map (1:500,000) differ in detail. Which statement is true?'\n"
    "  correct_letter: 'C'\n"
    "  correct_answer_value: 'The street map shows more detail because\n"
    "    it uses a larger scale'\n"
    "  tutor_response: 'Not quite, Edward. The answer is actually C)\n"
    "    The street map shows more detail because it uses a larger\n"
    "    scale. Here's the key idea: a scale of 1:5,000 means...'\n"
    "  CORRECT VERDICT: leaked=true. The tutor names both the letter\n"
    "  AND the option text. The conceptual explanation that follows\n"
    "  does NOT redeem the leak — by the time the student reads it\n"
    "  they already know the answer.\n"
    "\n"
    "NOT a reveal (leaked=false) — concept-level hints are OK:\n"
    "  - Names what the question is testing without stating the answer\n"
    "    OR the correct letter. 'Think about what a compass rose\n"
    "    actually shows on a map.' ✓\n"
    "  - Asks a Socratic question that narrows the option space\n"
    "    without giving the answer. 'What's the key thing you need to\n"
    "    know about your route?' ✓\n"
    "  - Eliminates wrong options without naming the right one.\n"
    "    'Two of these options are about distance, not direction.' ✓\n"
    "  - Explains the underlying CONCEPT (the rule, the mechanism)\n"
    "    without referring to a specific option letter or quoting the\n"
    "    canonical text. 'Larger scales mean smaller denominators,\n"
    "    which means each unit of map covers less ground.' ✓ — even\n"
    "    though this primes the student toward C, it never names C\n"
    "    or quotes C's exact wording.\n"
    "\n"
    "WHEN IN DOUBT: lean leaked=true. False positives just trigger a\n"
    "regen; false negatives ship the answer to the student.\n"
    "\n"
    "Output JSON ARRAY ONLY — one object per input item, in input order:\n"
    "[\n"
    '  {"index": <int>, "leaked": <true|false>, '
    '"reason": "<short why, <=200 chars>"}\n'
    "]\n"
)


def _build_leak_user_prompt(items: Sequence['BatchLeakItem']) -> str:
    payload = []
    for it in items:
        # `correct_answer_value` is the explicit text the tutor MUST
        # NOT state or paraphrase. Works for both MCQ (text of the
        # correct option) and non-MCQ (canonical expected answer).
        # `correct_letter` is MCQ-only and signals the letter that
        # also must not be stated.
        entry = {
            "index": it.index,
            "question": (it.question_text or "")[:500],
            "correct_answer_value": (it.canonical_answer or "")[:400],
            "tutor_response": (it.response or "")[:1500],
        }
        if it.options:
            entry["options"] = {
                k: str(v)[:200] for k, v in it.options.items() if v
            }
        if it.correct_letter:
            entry["correct_letter"] = it.correct_letter
        payload.append(entry)
    return (
        "Judge each item below. Reply with ONLY the JSON array — no "
        "prose, no code fence.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
    )


# =============================================================================
# JUDGE_REPEAT — did the tutor re-ask a question already asked?  (W14)
# =============================================================================

@dataclass
class BatchRepeatItem:
    """One repeat-detection input. Used by W14's optional LLM judge layer
    on borderline Jaccard cases (0.45–0.7)."""
    index: int
    new_question: str
    previous_questions: List[str] = field(default_factory=list)


@dataclass
class BatchRepeatResult:
    index: int
    repeated: bool
    reason: str = ""


_REPEAT_SYSTEM = (
    "You judge whether a tutor's NEW question is a REPEAT of a question "
    "they (or the bank) already asked the student.\n"
    "\n"
    "REPEAT = the NEW question is substantively the same as one in the\n"
    "previous list — asking literally the same thing in different words.\n"
    "Examples: 'What does a compass rose show?' and 'What is the\n"
    "function of a compass rose?' are repeats.\n"
    "\n"
    "NOT a repeat: same topic but DIFFERENT angle. Examples:\n"
    "  - 'What does a compass rose show?' vs 'Why is a compass rose\n"
    "    essential on a navigation map?' — different angles, not repeat.\n"
    "  - 'What is a legend?' vs 'How would you use a legend to find\n"
    "    a forest on a map?' — different angles, not repeat.\n"
    "\n"
    "Output JSON ARRAY ONLY:\n"
    "[\n"
    '  {"index": <int>, "repeated": <true|false>, '
    '"reason": "<short why, <=200 chars>"}\n'
    "]\n"
)


def _build_repeat_user_prompt(items: Sequence['BatchRepeatItem']) -> str:
    payload = []
    for it in items:
        payload.append({
            "index": it.index,
            "new_question": (it.new_question or "")[:400],
            "previous_questions": [
                (q or "")[:300] for q in (it.previous_questions or [])[:10]
            ],
        })
    return (
        "Judge each item below. Reply with ONLY the JSON array — no "
        "prose, no code fence.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
    )


# =============================================================================
# CLASSIFY_INTENT — attempt / confusion / off_topic  (W9)
# =============================================================================

@dataclass
class BatchIntentItem:
    """One intent-classification input. Used by W9 to decide whether a
    student reply was an attempt vs a confusion signal vs off-topic."""
    index: int
    question_text: str
    student_input: str


@dataclass
class BatchIntentResult:
    index: int
    intent: str  # 'attempt' | 'confusion' | 'off_topic'
    reason: str = ""


_INTENT_SYSTEM = (
    "You classify a student's reply during a tutoring session. The "
    "student was just asked a question by the tutor.\n"
    "\n"
    "Categories:\n"
    "  - attempt: they tried to answer. Right OR wrong both count.\n"
    "    Even a half-answer ('I think it's A but not sure') is an\n"
    "    attempt.\n"
    "  - confusion: they explicitly signalled they don't know / are\n"
    "    stuck / asking for help WITHOUT attempting. Examples:\n"
    "    'I don't know', 'no clue', 'help me', 'what's this about?',\n"
    "    'I'm not sure', 'stuck'.\n"
    "  - off_topic: their reply is unrelated to the question (chit-chat,\n"
    "    a different question, a meta comment about the lesson).\n"
    "\n"
    "Output JSON ARRAY ONLY:\n"
    "[\n"
    '  {"index": <int>, "intent": "<attempt|confusion|off_topic>", '
    '"reason": "<short why, <=80 chars>"}\n'
    "]\n"
)


def _build_intent_user_prompt(items: Sequence['BatchIntentItem']) -> str:
    payload = []
    for it in items:
        payload.append({
            "index": it.index,
            "question": (it.question_text or "")[:400],
            "student_reply": (it.student_input or "")[:400],
        })
    return (
        "Classify each item. Reply with ONLY the JSON array — no "
        "prose, no code fence.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
    )


# =============================================================================
# Unified dispatcher
# =============================================================================

# Map each judgment type to its (system_prompt, user_builder, result_parser).
_JUDGMENT_CONFIG = {
    # GRADE_CORRECTNESS uses the existing infrastructure — handled inline
    # in run_grading_batch (kept here as a sentinel so the dispatcher
    # can recognise it).
    JudgmentType.GRADE_CORRECTNESS: None,
    JudgmentType.JUDGE_LEAK:        (_LEAK_SYSTEM, _build_leak_user_prompt, 'leak'),
    JudgmentType.JUDGE_REPEAT:      (_REPEAT_SYSTEM, _build_repeat_user_prompt, 'repeat'),
    JudgmentType.CLASSIFY_INTENT:   (_INTENT_SYSTEM, _build_intent_user_prompt, 'intent'),
}


def _parse_leak_results(items, data) -> List[BatchLeakResult]:
    by_idx = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        by_idx[idx] = BatchLeakResult(
            index=idx,
            leaked=bool(entry.get("leaked", False)),
            reason=str(entry.get("reason") or "")[:400],
        )
    return [
        by_idx.get(it.index, BatchLeakResult(index=it.index, leaked=False))
        for it in items
    ]


def _parse_repeat_results(items, data) -> List[BatchRepeatResult]:
    by_idx = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        by_idx[idx] = BatchRepeatResult(
            index=idx,
            repeated=bool(entry.get("repeated", False)),
            reason=str(entry.get("reason") or "")[:400],
        )
    return [
        by_idx.get(it.index, BatchRepeatResult(index=it.index, repeated=False))
        for it in items
    ]


def _parse_intent_results(items, data) -> List[BatchIntentResult]:
    by_idx = {}
    valid_intents = {'attempt', 'confusion', 'off_topic'}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        raw_intent = str(entry.get("intent", "")).strip().lower()
        intent = raw_intent if raw_intent in valid_intents else 'attempt'
        by_idx[idx] = BatchIntentResult(
            index=idx,
            intent=intent,
            reason=str(entry.get("reason") or "")[:200],
        )
    return [
        by_idx.get(it.index, BatchIntentResult(index=it.index, intent='attempt'))
        for it in items
    ]


def run_grading_batch(
    items: Sequence,
    *,
    judgment_type: JudgmentType,
    llm_client,
    max_tokens: int = 1024,
) -> List:
    """Unified entry point for batched LLM judgments.

    Dispatches on judgment_type:
      GRADE_CORRECTNESS → delegates to grade_written_responses_batch
                          (preserves existing behavior + back-compat).
      JUDGE_LEAK        → answer-leak detector (W1). Single LLM judge,
                          no arbiter (removed 2026-05-17 — see
                          memory/tutor_state_drift_and_leak_simplification_plan.md).
      JUDGE_REPEAT      → repeated-question detector (W14).
      CLASSIFY_INTENT   → attempt / confusion / off_topic (W9).

    Returns a list of the appropriate result type, in input order.
    Fail-soft: on LLM error, returns conservative defaults (not leaked,
    not repeated, intent='attempt').
    """
    if not items:
        return []

    # GRADE_CORRECTNESS goes through the legacy path (it's the most
    # complex — per-element parts, fill_in_blank/matching breakdowns).
    # Keeping it separate avoids re-implementing.
    if judgment_type == JudgmentType.GRADE_CORRECTNESS:
        return grade_written_responses_batch(
            list(items), llm_client=llm_client, max_tokens=max_tokens,
        )

    config = _JUDGMENT_CONFIG.get(judgment_type)
    if config is None:
        logger.warning(
            "[Grader] unknown judgment_type=%s — returning empty",
            judgment_type,
        )
        return []
    system_prompt, build_user, result_kind = config

    if llm_client is None:
        logger.info(
            "[Grader] no llm_client for judgment_type=%s — returning defaults",
            judgment_type.value,
        )
        return _default_results(items, result_kind)

    user_prompt = build_user(items)
    logger.info(
        "[Grader] %s: %d items", judgment_type.value, len(items),
    )

    try:
        resp = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        raw = (resp.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("expected JSON array")
    except Exception as e:
        logger.warning(
            "[Grader] %s call failed: %s — returning defaults",
            judgment_type.value, e,
        )
        return _default_results(items, result_kind)

    if result_kind == 'leak':
        return _parse_leak_results(items, data)
    if result_kind == 'repeat':
        return _parse_repeat_results(items, data)
    if result_kind == 'intent':
        return _parse_intent_results(items, data)
    return []


def _default_results(items, kind: str) -> List:
    """Conservative defaults when the LLM call fails. Defaults are
    chosen so a failed call doesn't TRIGGER regen — better to miss a
    leak than to fire regen on every wrong turn during an outage."""
    if kind == 'leak':
        return [BatchLeakResult(index=it.index, leaked=False) for it in items]
    if kind == 'repeat':
        return [BatchRepeatResult(index=it.index, repeated=False) for it in items]
    if kind == 'intent':
        return [BatchIntentResult(index=it.index, intent='attempt') for it in items]
    return []
