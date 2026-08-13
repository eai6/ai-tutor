"""Deterministic grader for bank-pulled questions (P3 of
memory/curriculum_tutor_v2_plan.md).

The platform-wide architectural rule: the LLM never calculates correct
answers. It only compares a student's answer to a verified, approved
schema answer that already exists on the question record. This module
implements the comparison.

For every question type the bank can pull, this module knows how to
grade it from data alone — no LLM call:

  question_type    | grade signal source
  ─────────────────|─────────────────────────────────────────────
  mcq              | ExitTicketQuestion.correct_answer (letter)
                   | OR option text match
  short_numeric    | ExitTicketQuestion.answer_data['computed']
                   | OR LessonStep.expected_answer
  fill_in_blank    | answer_data['blanks'] (per-blank list)
  matching         | answer_data['pairs']  (left → right map)
  short_answer     | answer_data['model_answer'] (final-answer field)

Returns a `BankGradeResult` with `is_correct` (True/False/None),
`expected` (what the bank says), `student_parsed` (normalised
student input), and `detail` (per-blank or per-pair breakdown).

`is_correct=None` means the comparison could not be performed
confidently — caller falls through to the existing LLM evaluator
for short_answer working review or for unknown question shapes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_tutor.apps.tutoring.student_working_analyzer import safe_eval_arithmetic

logger = logging.getLogger(__name__)


@dataclass
class BankGradeResult:
    is_correct: Optional[bool]
    expected: Any = None
    student_parsed: Any = None
    detail: Dict = field(default_factory=dict)
    skip_reason: str = ""

    def to_metadata(self) -> Dict:
        return {
            "is_correct": self.is_correct,
            "expected": self.expected,
            "student_parsed": self.student_parsed,
            "detail": self.detail,
            "skip_reason": self.skip_reason,
        }


def _norm(s: str) -> str:
    """Normalise a student-typed string for comparison: lowercase,
    collapse whitespace, strip punctuation, drop unit symbols."""
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\.,;:!?]+$", "", s)
    # Drop common unit suffixes — the schema may store "180°" while
    # the student typed "180". ORDER MATTERS: " degrees" before
    # " deg" so the latter doesn't clip inside the former
    # ("210 degrees".replace(" deg", "") used to produce "210rees").
    s = s.replace("°", "").replace(" degrees", "").replace(" deg", "")
    s = s.replace(" m²", "").replace(" m2", "").replace(" sq m", "")
    s = s.replace(" kg", "").replace(" %", "").replace("%", "")
    return s.strip()


def _numeric_equal(a: str, b: str, eps: float = 0.01) -> bool:
    """Compare two strings as numbers when possible; fall back to
    normalised string equality when either side isn't numeric."""
    av = safe_eval_arithmetic(a)
    bv = safe_eval_arithmetic(b)
    if av is not None and bv is not None:
        return abs(av - bv) <= eps
    return _norm(a) == _norm(b)


def grade_bank_response(
    question,
    student_input,
    *,
    llm_client=None,
    is_math: bool = False,
) -> BankGradeResult:
    """Grade a student's input against the bank's stored answer.

    `question` is an ExitTicketQuestion model instance OR a
    duck-typed object with the same attributes (question_type,
    correct_answer, option_a/b/c/d, answer_data).

    `student_input` accepts:
      - str  — typed answer (any question type)
      - list — for fill_in_blank (per-blank list) or matching
               (list of {left, right} dicts)
      - dict — for short_answer (with `final_answer` + `working`)

    `llm_client` — optional. When provided, text-content question
    types (short_answer, non-numeric fill_in_blank, matching when
    exact match fails) are graded via the SAME LLM batch grader
    used by the exit ticket (`grade_written_responses_batch`,
    single-item batch). Without it, all types fall through to the
    deterministic path. Pilot directive 2026-05-16: mid-lesson
    grading must mirror exit-ticket grading exactly — same prompt,
    same rubric, same generosity around synonyms / phrasings — so
    students don't get false-NEGs during tutoring that they wouldn't
    get on the same question at submission time.

    `is_math` — propagated into the BatchGradeItem so the LLM grader
    knows to focus on numerical correctness over phrasing.

    Returns BankGradeResult. Never raises; on unrecoverable
    inputs returns is_correct=None with skip_reason set.
    """
    if question is None:
        return BankGradeResult(is_correct=None, skip_reason="no_question")
    if student_input is None:
        return BankGradeResult(is_correct=None, skip_reason="no_student_input")

    qt = (getattr(question, "question_type", "") or "mcq").lower()
    # Strict empty check: empty string only (lists / dicts pass through).
    if isinstance(student_input, str):
        raw = student_input.strip()
        if not raw:
            return BankGradeResult(is_correct=None, skip_reason="empty_student_input")
    else:
        raw = student_input

    if qt == "mcq":
        det = _grade_mcq(question, raw)
        # If the student typed free-form prose (not a letter, no option
        # text match), the deterministic check returns is_correct=False
        # with detail.reason='no_option_matched'. Route to the LLM
        # grader using the CORRECT OPTION'S TEXT as the model answer,
        # so a semantically equivalent free-form answer is still
        # accepted as correct. Pilot directive 2026-05-16: bank MCQs
        # sometimes render in chat without their A/B/C/D options
        # (intermittent rendering bug), and the student answers in
        # prose. Grading those prose answers against the bare letter
        # produces false-negatives that strand the lesson.
        if (
            llm_client is not None
            and det.is_correct is False
            and isinstance(det.detail, dict)
            and det.detail.get("reason") == "no_option_matched"
        ):
            return _grade_mcq_with_llm(
                question, raw,
                llm_client=llm_client,
                deterministic_fallback=det,
            )
        return det
    if qt == "short_numeric":
        # Numeric fast-path; on miss + LLM available, fall through to
        # batch grader (catches "two hundred ten" / "210 deg" / etc).
        det = _grade_numeric(question, raw)
        if det.is_correct or llm_client is None:
            return det
        return _grade_with_llm_batch(
            question, raw, llm_client=llm_client, is_math=True,
            deterministic_fallback=det,
        )
    if qt == "fill_in_blank":
        # If LLM client provided, route ALL FIB through the batch
        # grader — the exit-ticket grader handles numeric blanks via
        # its own fast-path inside grade_written_responses_batch's
        # caller path. Without client, deterministic only.
        if llm_client is not None:
            return _grade_with_llm_batch(
                question, raw, llm_client=llm_client, is_math=is_math,
            )
        return _grade_fill_blank(question, raw)
    if qt == "matching":
        det = _grade_matching(question, raw)
        if det.is_correct or llm_client is None:
            return det
        return _grade_with_llm_batch(
            question, raw, llm_client=llm_client, is_math=is_math,
            deterministic_fallback=det,
        )
    if qt == "short_answer":
        if llm_client is not None:
            return _grade_with_llm_batch(
                question, raw, llm_client=llm_client, is_math=is_math,
            )
        return _grade_short_answer(question, raw)
    return BankGradeResult(is_correct=None, skip_reason=f"unknown_type:{qt}")


def grade_chat_authored_question(
    question_text: str,
    student_response: str,
    *,
    llm_client,
    is_math: bool = False,
    kb_context: str = '',
    use_grounding: bool = True,
    committed_canonical=None,
) -> BankGradeResult:
    """LLM grades a tutor-authored question with NO answer key.

    Used as a fallback when the tutor authored a question in chat
    narrative without calling pose_inline_question (which would carry
    an answer key).

    **Two-layer grounding** (pilot directive 2026-05-16):
      1. `kb_context` — relevant chunks pulled from the lesson's
         knowledge base. Anchors the verdict to the curriculum
         (vs. the LLM's parametric knowledge which may be wrong on
         local / niche topics like Seychelles geography facts).
      2. Google search grounding via the two-call pattern in
         `call_judge_grounded_then_structured` — when the picked
         provider is Gemini, fact-checks against live web sources.
         Especially useful for current events, place facts, named
         entities.

    Falls back to the simple `grade_written_responses_batch` path
    when grounding is disabled / unavailable.

    PR4 (2026-05-25): when ``committed_canonical`` is a trustworthy
    ``CanonicalCommitment``, skip the grounded derive-at-grade-time
    path entirely and grade against the frozen canonical. This
    eliminates the v3 benchmark failure mode where the LLM derived a
    canonical biased by the student's response and graded wrong
    answers as correct.

    Returns BankGradeResult. Never raises; on LLM crash returns
    is_correct=None with skip_reason set.
    """
    if not question_text or not student_response or llm_client is None:
        return BankGradeResult(
            is_correct=None,
            skip_reason="missing_input_or_client",
        )

    # PR4: pre-committed canonical short-circuit. When trustworthy,
    # grade against the frozen answer; falls through to the legacy
    # paths when the commitment was skipped / low-confidence.
    if committed_canonical is not None and getattr(committed_canonical, 'trustworthy', False):
        return _grade_against_committed_canonical(
            question_text=question_text,
            student_response=student_response,
            committed=committed_canonical,
            llm_client=llm_client,
            is_math=is_math,
        )

    # Try the grounded path first (KB context + Google search).
    if use_grounding:
        try:
            return _grade_chat_authored_grounded(
                question_text=question_text,
                student_response=student_response,
                kb_context=kb_context,
                is_math=is_math,
            )
        except Exception as exc:
            logger.warning(
                "[BankGrader] grounded chat_authored failed (%s: %s) — "
                "falling back to batch grader without grounding",
                type(exc).__name__, str(exc)[:120],
            )
            # Fall through to batch path below.

    # Fallback: non-grounded batch grader with KB context spliced
    # into the question text. Still uses the LLM, just without web
    # search verification.
    from ai_tutor.apps.tutoring.exit_ticket_grader import (
        BatchGradeItem,
        grade_written_responses_batch,
    )

    item_question = (
        f"[NO ANSWER KEY PROVIDED — derive the correct answer "
        f"from your domain knowledge"
        + (
            f" + the following curriculum context, then judge whether "
            f"the student's response is correct.\n\nCURRICULUM CONTEXT:\n"
            f"{kb_context[:2000]}\n\nQUESTION: {question_text}"
            if kb_context.strip()
            else f", then judge whether the student's response is correct.] {question_text}"
        )
    )
    item = BatchGradeItem(
        index=0,
        question_text=item_question,
        q_type='short_answer',
        expected='',
        keywords=[],
        student_answer=student_response,
        is_math=is_math,
    )
    try:
        results = grade_written_responses_batch(
            [item], llm_client=llm_client,
        )
    except Exception as exc:
        logger.warning(
            "[BankGrader] chat_authored batch fallback crashed: %s: %s",
            type(exc).__name__, exc,
        )
        return BankGradeResult(
            is_correct=None,
            skip_reason=f"llm_crash:{type(exc).__name__}",
        )

    if not results:
        return BankGradeResult(
            is_correct=None,
            skip_reason="llm_no_results",
        )

    r = results[0]
    return BankGradeResult(
        is_correct=bool(r.correct),
        expected="(derived by LLM)",
        student_parsed=student_response[:200],
        detail={
            'source': 'llm_chat_authored_batch',
            'reasoning': (r.reasoning or '')[:300],
        },
    )


def _grade_against_committed_canonical(
    *,
    question_text: str,
    student_response: str,
    committed,
    llm_client,
    is_math: bool = False,
) -> BankGradeResult:
    """Grade a student response against a pre-committed canonical
    (``apps.tutoring.canonical_committer.CanonicalCommitment``).

    Two-stage match:
      1. Deterministic — normalised string match against the canonical
         and each acceptable variant. Catches the easy cases without
         spending an LLM call.
      2. LLM batch judge — when deterministic doesn't match, ask the
         grader whether the response is semantically equivalent to
         the canonical (NOT whether it's "correct in general").

    Returns BankGradeResult with detail.source='committed_canonical'.
    Never raises.
    """
    canonical = committed.canonical.strip()
    variants = [v.strip() for v in (committed.acceptable_variants or []) if v.strip()]
    student_norm = _norm(student_response or '')

    # 1. Deterministic match
    candidates = [canonical] + variants
    for cand in candidates:
        if _norm(cand) == student_norm and student_norm:
            return BankGradeResult(
                is_correct=True,
                expected=canonical,
                student_parsed=student_response[:200],
                detail={
                    'source': 'committed_canonical_deterministic',
                    'matched_variant': cand,
                    'confidence': committed.confidence,
                    'committer_reasoning': committed.reasoning,
                },
            )

    # 2. LLM equivalence judge — phrase the grading question as
    # "is the response equivalent to the canonical?" not "is the
    # response correct?". This avoids the LLM smuggling its own
    # opinion about correctness back in.
    from ai_tutor.apps.tutoring.exit_ticket_grader import (
        BatchGradeItem,
        grade_written_responses_batch,
    )
    framed_question = (
        f"Did the student give an answer equivalent to: \"{canonical}\""
        + (f" (also acceptable: {', '.join(variants)})" if variants else "")
        + f"?\n\nORIGINAL QUESTION: {question_text.strip()[:600]}"
    )
    item = BatchGradeItem(
        index=0,
        question_text=framed_question,
        q_type='short_answer',
        expected=canonical,
        keywords=variants[:5],
        student_answer=student_response,
        is_math=is_math,
    )
    try:
        results = grade_written_responses_batch([item], llm_client=llm_client)
    except Exception as exc:
        logger.warning(
            "[BankGrader] committed_canonical LLM equivalence judge crashed: %s",
            exc,
        )
        return BankGradeResult(
            is_correct=None,
            skip_reason=f"llm_crash:{type(exc).__name__}",
            expected=canonical,
            detail={
                'source': 'committed_canonical_llm_failed',
                'confidence': committed.confidence,
            },
        )

    if not results:
        return BankGradeResult(
            is_correct=None,
            skip_reason="llm_no_results",
            expected=canonical,
            detail={'source': 'committed_canonical_llm_empty'},
        )

    r = results[0]
    return BankGradeResult(
        is_correct=bool(r.correct),
        expected=canonical,
        student_parsed=student_response[:200],
        detail={
            'source': 'committed_canonical_llm',
            'reasoning': (r.reasoning or '')[:300],
            'confidence': committed.confidence,
            'committer_reasoning': committed.reasoning,
        },
    )


# Pydantic schema for the grounded chat-authored verdict (two-call
# pattern: first call is grounded free-form Gemini, second is
# structured-output extraction into this shape).
def _build_chat_authored_verdict_schema():
    """Lazy-build the verdict Pydantic class so importing bank_grader
    doesn't require pydantic at module load."""
    from pydantic import BaseModel, Field

    class ChatAuthoredVerdict(BaseModel):
        is_correct: bool = Field(
            description=(
                "True if the student's response correctly answers the "
                "question. False otherwise. Be generous on phrasing — "
                "what matters is whether they convey the correct idea."
            ),
        )
        reasoning: str = Field(
            description=(
                "One short sentence (<= 200 chars) explaining the "
                "verdict. Cite the curriculum context or web source "
                "if used."
            ),
            max_length=300,
        )
        derived_answer: str = Field(
            default='',
            description=(
                "What the correct answer is, in your judgment. "
                "Used for downstream tutor scaffolding."
            ),
            max_length=300,
        )

    return ChatAuthoredVerdict


def _grade_chat_authored_grounded(
    *,
    question_text: str,
    student_response: str,
    kb_context: str,
    is_math: bool,
) -> BankGradeResult:
    """Grounded grading path — uses `call_judge_grounded_then_structured`.

    Builds a judge_provider chain, runs the two-call pattern (Gemini
    grounded free-form → structured-output extraction), returns a
    BankGradeResult. Raises on infrastructure failure so the caller
    can fall back to the non-grounded batch path.
    """
    from ai_tutor.apps.curriculum.content_judges._providers import (
        call_judge_grounded_then_structured,
        get_judge_provider_chain,
    )

    verdict_schema = _build_chat_authored_verdict_schema()

    # judge_purpose is a positional arg, not keyword. 'judge' is the
    # standard purpose; chain helper falls through to other purposes
    # if no active 'judge' config exists.
    providers = get_judge_provider_chain('judge')
    if not providers:
        raise RuntimeError("no judge providers available")

    # Build the user prompt. KB context first so it grounds the
    # subsequent question + response evaluation.
    parts = []
    if kb_context.strip():
        parts.append("## CURRICULUM CONTEXT (authoritative for this lesson)")
        parts.append(kb_context.strip()[:2500])
        parts.append("")
    parts.append("## QUESTION (no canonical answer key was provided)")
    parts.append(question_text.strip()[:1000])
    parts.append("")
    parts.append("## STUDENT_RESPONSE")
    parts.append(student_response.strip()[:1000])
    parts.append("")
    parts.append("## YOUR JOB")
    parts.append(
        "Determine whether the student's response correctly answers "
        "the question.\n"
        "Priority order for ground truth:\n"
        "  1. The CURRICULUM CONTEXT above (this is what the lesson "
        "teaches).\n"
        "  2. Live web search results (use Google Search to verify "
        "any factual claim about places, dates, numbers, named "
        "entities — especially local or regional facts that may be "
        "outside your training data).\n"
        "  3. Your own domain knowledge (last resort).\n\n"
        "Be GENEROUS on phrasing — what matters is whether the "
        "student conveys the correct idea, not whether they used the "
        "exact words from the curriculum."
    )
    user_prompt = "\n".join(parts)

    system_prompt = (
        "You are an expert grader for school exit-ticket-style "
        "questions. You evaluate written student responses against "
        "the curriculum + verifiable facts. Output structured "
        "JSON per the verdict schema."
    )

    grounded_result = call_judge_grounded_then_structured(
        user_prompt=user_prompt,
        providers=providers,
        response_model=verdict_schema,
        system_prompt=system_prompt,
        max_tokens=2000,
        max_retries=1,
        grounding_instruction=(
            "Use Google Search to verify any place name, date, "
            "number, or named entity in the question or student "
            "response. This matters most for local or regional facts — "
            "place names, dates, populations, geography — that may be "
            "missing from your training data. "
            "Cite the sources you used inline."
        ),
    )

    if not grounded_result.success or grounded_result.verdict is None:
        raise RuntimeError(
            f"grounded judge failed: "
            f"{grounded_result.error_class}: "
            f"{grounded_result.error_detail[:150]}"
        )

    v = grounded_result.verdict
    return BankGradeResult(
        is_correct=bool(v.is_correct),
        expected=(getattr(v, 'derived_answer', '') or "(derived by LLM)")[:200],
        student_parsed=student_response[:200],
        detail={
            'source': 'llm_chat_authored_grounded',
            'reasoning': (v.reasoning or '')[:300],
            'provider': grounded_result.provider,
            'model_name': grounded_result.model_name,
            'kb_context_used': bool(kb_context.strip()),
        },
    )


def _grade_with_llm_batch(
    question,
    student_input,
    *,
    llm_client,
    is_math: bool = False,
    deterministic_fallback: 'Optional[BankGradeResult]' = None,
) -> BankGradeResult:
    """Single-item wrapper around grade_written_responses_batch.

    Builds one BatchGradeItem via the shared
    exit_ticket_grader.build_batch_grade_item, runs the batch
    grader (batch size = 1), and maps the verdict back to a
    BankGradeResult so callers get a uniform return shape regardless
    of which path graded.

    `deterministic_fallback` — when set, returned if the LLM call
    fails or returns no entry. Keeps callers safe in network outages.
    """
    from ai_tutor.apps.tutoring.exit_ticket_grader import (
        build_batch_grade_item,
        grade_written_responses_batch,
    )

    try:
        item = build_batch_grade_item(
            index=0,
            question=question,
            student_answer=student_input,
            is_math=is_math,
        )
        results = grade_written_responses_batch(
            [item], llm_client=llm_client,
        )
    except Exception as exc:
        logger.warning(
            "[BankGrader] LLM batch call crashed: %s: %s",
            type(exc).__name__, exc,
        )
        return deterministic_fallback or BankGradeResult(
            is_correct=None,
            skip_reason=f"llm_crash:{type(exc).__name__}",
        )

    if not results:
        return deterministic_fallback or BankGradeResult(
            is_correct=None, skip_reason="llm_no_results",
        )

    r = results[0]
    return BankGradeResult(
        is_correct=bool(r.correct),
        expected=str(item.expected)[:200],
        student_parsed=str(item.student_answer)[:200],
        detail={
            'source': 'llm_batch',
            'reasoning': (r.reasoning or '')[:300],
            'parts': [
                {'is_correct': p.is_correct, 'reasoning': p.reasoning}
                for p in (r.parts or [])
            ],
        },
    )


# ─── MCQ ───────────────────────────────────────────────────────────


def _grade_mcq(question, raw: str) -> BankGradeResult:
    correct_letter = (getattr(question, "correct_answer", "") or "").strip().upper()
    if not correct_letter:
        return BankGradeResult(
            is_correct=None, skip_reason="mcq_no_correct_answer",
        )

    norm = _norm(raw)
    norm_upper = norm.upper().strip()

    # Direct letter answer ("A" / "a" / "A." / "(A)" / "(B).")
    letter_match = re.match(r"^[\(\[]?\s*([A-D])\s*[\)\]\.]*$", str(raw).strip(), re.IGNORECASE)
    if letter_match:
        student_letter = letter_match.group(1).upper()
        return BankGradeResult(
            is_correct=(student_letter == correct_letter),
            expected=correct_letter,
            student_parsed=student_letter,
        )

    # Full-text match: compare against each option, find which
    # option the student picked, then compare to correct_letter.
    options = {
        "A": getattr(question, "option_a", "") or "",
        "B": getattr(question, "option_b", "") or "",
        "C": getattr(question, "option_c", "") or "",
        "D": getattr(question, "option_d", "") or "",
    }
    for letter, text in options.items():
        if not text:
            continue
        if _norm(text) == norm or _numeric_equal(text, raw):
            return BankGradeResult(
                is_correct=(letter == correct_letter),
                expected=correct_letter,
                student_parsed=letter,
                detail={"matched_option_text": text},
            )

    return BankGradeResult(
        is_correct=False,
        expected=correct_letter,
        student_parsed=raw[:60],
        detail={"reason": "no_option_matched"},
    )


def _grade_mcq_with_llm(
    question,
    raw: str,
    *,
    llm_client,
    deterministic_fallback: 'Optional[BankGradeResult]' = None,
) -> BankGradeResult:
    """Free-form MCQ fallback. Uses the CORRECT OPTION'S text as the
    model answer so a prose answer can be matched conceptually.

    Builds a one-item batch by constructing a duck-typed question whose
    `correct_answer` is the correct option's text (not the letter), so
    `build_batch_grade_item` produces an `expected` string the batch
    grader can compare against the student's prose. Falls back to the
    deterministic verdict on LLM failure.
    """
    from ai_tutor.apps.tutoring.exit_ticket_grader import (
        build_batch_grade_item,
        grade_written_responses_batch,
    )

    correct_letter = (getattr(question, "correct_answer", "") or "").strip().upper()
    correct_text = (
        getattr(question, f"option_{correct_letter.lower()}", "") or ""
    ).strip()
    if not correct_text:
        return deterministic_fallback or BankGradeResult(
            is_correct=False,
            expected=correct_letter,
            student_parsed=raw[:60],
            detail={"reason": "no_correct_option_text_for_llm"},
        )

    # Render full stem + options so the LLM has the same context the
    # student would have seen in a properly-rendered MCQ.
    options_block_lines = []
    for letter in ("A", "B", "C", "D"):
        opt = (getattr(question, f"option_{letter.lower()}", "") or "").strip()
        if opt:
            options_block_lines.append(f"{letter}) {opt}")
    options_block = "\n".join(options_block_lines)
    full_stem = (getattr(question, "question_text", "") or "").strip()
    if options_block:
        full_stem = f"{full_stem}\n\n{options_block}".strip()

    # Build a minimal duck that build_batch_grade_item can serialise as
    # a "short_answer"-style item with the correct option's text as the
    # expected model answer.
    class _MCQDuck:
        question_type = "short_answer"
        question_text = full_stem
        correct_answer = correct_text
        answer_data = {"model_answer": correct_text}

    try:
        item = build_batch_grade_item(
            index=0,
            question=_MCQDuck(),
            student_answer=raw,
            is_math=False,
        )
        results = grade_written_responses_batch(
            [item], llm_client=llm_client,
        )
    except Exception as exc:
        logger.warning(
            "[BankGrader/MCQ-LLM] batch call crashed: %s: %s",
            type(exc).__name__, exc,
        )
        return deterministic_fallback or BankGradeResult(
            is_correct=False,
            expected=correct_letter,
            student_parsed=raw[:60],
            detail={"reason": f"llm_crash:{type(exc).__name__}"},
        )

    if not results:
        return deterministic_fallback or BankGradeResult(
            is_correct=False,
            expected=correct_letter,
            student_parsed=raw[:60],
            detail={"reason": "llm_no_results"},
        )

    r = results[0]
    return BankGradeResult(
        is_correct=bool(r.correct),
        expected=correct_letter,
        student_parsed=raw[:200],
        detail={
            "source": "mcq_llm_fallback",
            "correct_option_text": correct_text[:160],
            "reasoning": (r.reasoning or "")[:300],
        },
    )


# ─── short_numeric ─────────────────────────────────────────────────


def _grade_numeric(question, raw: str) -> BankGradeResult:
    ad = getattr(question, "answer_data", None) or {}
    if not isinstance(ad, dict):
        ad = {}

    # Preferred: computed numeric value from the renderer
    computed = ad.get("computed")
    expected_str = ad.get("model_answer") or ""

    student_v = safe_eval_arithmetic(raw)
    if computed is None and not expected_str:
        # Fall back to LessonStep.expected_answer if attribute exists
        ea = getattr(question, "expected_answer", None) or ""
        if ea:
            return BankGradeResult(
                is_correct=_numeric_equal(raw, ea),
                expected=ea,
                student_parsed=raw[:60],
            )
        return BankGradeResult(is_correct=None, skip_reason="numeric_no_expected")

    if computed is not None and student_v is not None:
        ok = abs(student_v - float(computed)) <= 0.01
        return BankGradeResult(
            is_correct=ok,
            expected=expected_str or computed,
            student_parsed=student_v,
        )
    if expected_str:
        return BankGradeResult(
            is_correct=_numeric_equal(raw, expected_str),
            expected=expected_str,
            student_parsed=raw[:60],
        )
    return BankGradeResult(is_correct=None, skip_reason="numeric_unparseable")


# ─── fill_in_blank ─────────────────────────────────────────────────


def _grade_fill_blank(question, raw: str) -> BankGradeResult:
    """Student input expected as a list-like value: either a Python
    list (when posted as JSON), a comma/newline-separated string, or
    a single value when there's only one blank.
    """
    ad = getattr(question, "answer_data", None) or {}
    blanks = ad.get("blanks") or []
    if not blanks:
        return BankGradeResult(is_correct=None, skip_reason="fill_no_blanks")

    # Tolerant input parsing: list passes through; string splits on
    # ',' or newline.
    if isinstance(raw, list):
        student_blanks = [str(x).strip() for x in raw]
    else:
        student_blanks = [s.strip() for s in re.split(r"[,;\n]", str(raw)) if s.strip()]

    # If the student gave fewer blanks than expected, count missing
    # as wrong rather than skipping.
    per_blank = []
    all_correct = True
    for i, expected in enumerate(blanks):
        student = student_blanks[i] if i < len(student_blanks) else ""
        ok = bool(student) and _numeric_equal(student, str(expected))
        per_blank.append({"expected": str(expected), "student": student, "is_correct": ok})
        if not ok:
            all_correct = False

    return BankGradeResult(
        is_correct=all_correct,
        expected=list(blanks),
        student_parsed=student_blanks,
        detail={"per_blank": per_blank},
    )


# ─── matching ──────────────────────────────────────────────────────


def _grade_matching(question, raw) -> BankGradeResult:
    """Student input expected as either a list of {left, right}
    dicts (preferred — the UI submits structured) or a string with
    'left → right' lines.
    """
    ad = getattr(question, "answer_data", None) or {}
    pairs = ad.get("pairs") or []
    if not pairs:
        return BankGradeResult(is_correct=None, skip_reason="matching_no_pairs")

    expected_map = {_norm(p["left"]): _norm(str(p["right"])) for p in pairs if "left" in p and "right" in p}

    # Parse student answer
    student_pairs: List[Dict] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "left" in item and "right" in item:
                student_pairs.append({"left": str(item["left"]), "right": str(item["right"])})
    else:
        # Parse "left → right" / "left -> right" / "left = right" lines
        for line in re.split(r"\n+", str(raw).strip()):
            m = re.split(r"\s*(?:→|->|=>|=|:)\s*", line, maxsplit=1)
            if len(m) == 2 and m[0].strip() and m[1].strip():
                student_pairs.append({"left": m[0].strip(), "right": m[1].strip()})

    if not student_pairs:
        return BankGradeResult(is_correct=False, expected=expected_map, student_parsed=raw[:80] if isinstance(raw, str) else raw, detail={"reason": "unparseable_pairs"})

    per_pair = []
    all_correct = True
    for sp in student_pairs:
        left_n = _norm(sp["left"])
        expected_right = expected_map.get(left_n)
        ok = expected_right is not None and _numeric_equal(sp["right"], expected_right)
        per_pair.append({"left": sp["left"], "student_right": sp["right"], "expected_right": expected_right, "is_correct": ok})
        if not ok:
            all_correct = False

    # Any expected-left the student didn't address → wrong
    student_lefts = {_norm(sp["left"]) for sp in student_pairs}
    for el in expected_map:
        if el not in student_lefts:
            all_correct = False
            per_pair.append({"left": el, "student_right": None, "expected_right": expected_map[el], "is_correct": False})

    return BankGradeResult(
        is_correct=all_correct,
        expected=expected_map,
        student_parsed=student_pairs,
        detail={"per_pair": per_pair},
    )


# ─── LessonStep (slot 0 in the bank) ──────────────────────────────


def grade_lesson_step_response(step, student_input) -> BankGradeResult:
    """Grade a student's reply against a LessonStep (the canonical
    question for slot 0 of the bank).

    LessonStep has a different field shape than ExitTicketQuestion —
    no `question_type`, no `correct_answer`, no `option_a/b/c/d`.
    Calling `grade_bank_response` on a LessonStep silently fell into
    the MCQ default and returned `is_correct=None expected=None`
    every time (this is what Martin saw in the logs). Dispatch by
    `step.answer_type` and grade against `step.expected_answer`.
    """
    if step is None:
        return BankGradeResult(is_correct=None, skip_reason="no_step")
    if student_input is None:
        return BankGradeResult(is_correct=None, skip_reason="no_student_input")
    if isinstance(student_input, str) and not student_input.strip():
        return BankGradeResult(is_correct=None, skip_reason="empty_student_input")

    answer_type = (getattr(step, "answer_type", "") or "").lower()
    expected = (getattr(step, "expected_answer", "") or "").strip()

    if answer_type in ("", "none"):
        return BankGradeResult(is_correct=None, skip_reason="lesson_step_no_answer_type")
    if not expected:
        return BankGradeResult(is_correct=None, skip_reason="lesson_step_no_expected")

    raw = student_input if not isinstance(student_input, str) else student_input.strip()

    if answer_type == "short_numeric":
        return BankGradeResult(
            is_correct=_numeric_equal(str(raw), expected),
            expected=expected,
            student_parsed=str(raw)[:60],
        )

    if answer_type == "true_false":
        return BankGradeResult(
            is_correct=(_norm(str(raw)) == _norm(expected)),
            expected=expected,
            student_parsed=_norm(str(raw)),
        )

    if answer_type == "multiple_choice":
        # LessonStep stores `choices` as a JSON list; expected_answer
        # is the canonical answer (letter, index, or option text).
        choices = list(getattr(step, "choices", None) or [])

        # Direct letter answer ("A" / "(B)" / "C.")
        letter_match = re.match(
            r"^[\(\[]?\s*([A-D])\s*[\)\]\.]*$", str(raw).strip(), re.IGNORECASE,
        )
        if letter_match:
            student_letter = letter_match.group(1).upper()
            expected_letter = expected.upper().strip()
            if expected_letter in {"A", "B", "C", "D"}:
                return BankGradeResult(
                    is_correct=(student_letter == expected_letter),
                    expected=expected_letter,
                    student_parsed=student_letter,
                )
            # expected is option text or index — map student's letter
            # to the indexed choice and compare.
            idx = ord(student_letter) - ord("A")
            if 0 <= idx < len(choices):
                picked = str(choices[idx])
                return BankGradeResult(
                    is_correct=_numeric_equal(picked, expected),
                    expected=expected,
                    student_parsed=picked,
                    detail={"matched_letter": student_letter},
                )

        # Full-text match against expected (handles both letter-form
        # and option-text-form expected_answer values).
        return BankGradeResult(
            is_correct=_numeric_equal(str(raw), expected),
            expected=expected,
            student_parsed=str(raw)[:60],
        )

    if answer_type == "free_text":
        # Free-text working can't be deterministically graded — defer
        # to the LLM evaluator. Caller treats is_correct=None as "no
        # signal" and falls through to the next layer.
        return BankGradeResult(
            is_correct=None,
            skip_reason="lesson_step_free_text_defer_to_llm",
            expected=expected,
        )

    return BankGradeResult(
        is_correct=None,
        skip_reason=f"lesson_step_unknown_answer_type:{answer_type}",
    )


# ─── short_answer (two-field design) ───────────────────────────────


def _grade_short_answer(question, raw) -> BankGradeResult:
    """Two-field design (per the v2 plan):
      - final_answer field is graded deterministically
      - working field is reviewed by an LLM later (this grader does
        NOT touch working). The runtime tutor passes working through
        a separate review step.

    Student input shapes accepted:
      - dict {final_answer, working} — fully structured
      - str — treated as final_answer only (working blank)
    """
    ad = getattr(question, "answer_data", None) or {}
    expected_final = ad.get("model_answer") or ""
    if not expected_final:
        return BankGradeResult(
            is_correct=None, skip_reason="short_answer_no_model_answer",
        )

    if isinstance(raw, dict):
        student_final = (raw.get("final_answer") or raw.get("answer") or "").strip()
    else:
        student_final = str(raw).strip()

    if not student_final:
        return BankGradeResult(
            is_correct=False,
            expected=expected_final,
            student_parsed="",
            detail={"reason": "no_final_answer"},
        )

    return BankGradeResult(
        is_correct=_numeric_equal(student_final, expected_final),
        expected=expected_final,
        student_parsed=student_final,
        detail={"working_grading": "deferred_to_llm_review"},
    )
