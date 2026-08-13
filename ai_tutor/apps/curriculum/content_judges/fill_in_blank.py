"""POST-generation judge for fill-in-the-blank exit-ticket questions.

Mirrors `exit_question.py` (MCQ) shape but adapted for FIB schema:
each question has a `text_template` with `___` markers + a `blanks`
list of correct values + `accept_alternatives` (list of lists) for
acceptable variants per blank.

**Hooks at:** `apps/curriculum/content_generator.py` POST-gen of
ExitTicketQuestion (alongside the MCQ + short_answer + matching judges).
**Generator-side providers:** Anthropic (default for `generation`).
**Judge-side providers:** Gemini → Anthropic → OpenAI (cross-provider).

Five stable violation codes — each names a distinct fix path:

  FIB_WRONG_ANSWER (HARD) — at least one blank's marked correct value
    is factually wrong. Hard reject; the student would learn wrong info.

  FIB_AMBIGUOUS_BLANK (SOFT) — the surrounding context doesn't
    constrain the blank enough; multiple plausible answers would fit
    and `accept_alternatives` doesn't cover them. Soft signal.

  FIB_OFF_OBJECTIVE (HARD) — the blank tests a concept the lesson /
    enabling objective doesn't teach. Hard reject.

  FIB_MISSING_ALTERNATIVES (SOFT) — common semantically-equivalent
    answers (synonyms, abbreviations, alternate spellings, common
    variants) are missing from `accept_alternatives`. Soft signal —
    the question still tests the right thing, just risks marking valid
    answers wrong.

  FIB_TRICK_WORDING (SOFT) — the question relies on grammatical
    tricks (negations, awkward word order) rather than concept
    comprehension. Soft signal.

Verdict semantics:
  - passed=True, violations=[] → ship it
  - passed=False → at least one HARD code; should regen
  - passed=True, violations=[soft only] → surface to teacher review

Skips when: question_type != 'fill_in_blank' / required fields missing
/ providers fail. Skip returns passed=True so gen never blocks.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ai_tutor.apps.curriculum.content_judges import JudgeResult
from ai_tutor.apps.curriculum.content_judges._providers import (
    _grounding_enabled,
    call_judge_grounded_then_structured,
    call_judge_structured_with_fallback,
    get_judge_provider_chain,
)

logger = logging.getLogger(__name__)


# ─── Stable violation codes ────────────────────────────────────────────
VIOLATION_WRONG_ANSWER = "FIB_WRONG_ANSWER"
VIOLATION_AMBIGUOUS_BLANK = "FIB_AMBIGUOUS_BLANK"
VIOLATION_OFF_OBJECTIVE = "FIB_OFF_OBJECTIVE"
VIOLATION_MISSING_ALTERNATIVES = "FIB_MISSING_ALTERNATIVES"
VIOLATION_TRICK_WORDING = "FIB_TRICK_WORDING"

VIOLATION_CODES = (
    VIOLATION_WRONG_ANSWER,
    VIOLATION_AMBIGUOUS_BLANK,
    VIOLATION_OFF_OBJECTIVE,
    VIOLATION_MISSING_ALTERNATIVES,
    VIOLATION_TRICK_WORDING,
)

_HARD_CODES = frozenset({
    VIOLATION_WRONG_ANSWER,
    VIOLATION_OFF_OBJECTIVE,
})


_SYSTEM_INSTRUCTION = """\
Review one fill-in-the-blank exit-ticket question for use in a \
secondary-school lesson. Check the marked correct value(s) are right, \
the blank is unambiguously constrained by context, and the question \
tests the lesson's enabling objective.

Approve the question when ALL of these hold:
  - Every blank's marked correct value is factually correct.
  - The surrounding text constrains the blank enough that ONE value \
(or a small set of equivalents) is clearly expected.
  - The question tests what the enabling_objective specifies.
  - `accept_alternatives` captures common synonyms / abbreviations / \
spelling variants for the blank.
  - Wording is direct — student fails by not knowing the concept, \
not by parsing tricks.

Reject otherwise. Use ONLY these codes:

  FIB_WRONG_ANSWER
    The marked correct value for at least one blank is factually wrong.
    Example: "The capital of Seychelles is ___" with answer "Praslin" \
(the right answer is "Victoria").

  FIB_AMBIGUOUS_BLANK
    Multiple plausible answers fit the blank because context doesn't \
constrain it. Distinct from MISSING_ALTERNATIVES — here the marked \
answer is RIGHT but the blank is too open.
    Example: "Seychelles has a ___ climate" — "tropical", "humid", \
"warm" all fit.

  FIB_OFF_OBJECTIVE
    The blank tests a concept the lesson / enabling objective does \
not teach.
    Example: lesson is "naming tectonic plates", blank asks about \
volcano formation depth.

  FIB_MISSING_ALTERNATIVES
    The marked answer is right and the blank is well-constrained, but \
common variants (synonyms, abbreviations, alternate spellings) are \
missing from accept_alternatives. Soft signal — students with valid \
answers may be marked wrong.
    Example: blank answer "GNP" but accept_alternatives doesn't \
include "gross national product".

  FIB_TRICK_WORDING
    Question relies on grammatical tricks (negations, "NOT", awkward \
word order) rather than concept comprehension.
    Example: "Which item is NOT not a feature of erosion is ___".

When rejecting, write a `recommended_fix` (≤120 words) — a concrete \
edit naming the specific change (which blank to fix, what alternates \
to add, what to rephrase). No "make it better" comments.

In `reasoning`, write 3-5 short sentences. Walk through whether each \
blank's value is right, whether the blank is well-constrained, \
whether the question is on-objective, and whether alternates are \
adequate.
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_ALLOWED_VIOLATIONS = Literal[
    "FIB_WRONG_ANSWER",
    "FIB_AMBIGUOUS_BLANK",
    "FIB_OFF_OBJECTIVE",
    "FIB_MISSING_ALTERNATIVES",
    "FIB_TRICK_WORDING",
]


class FillInBlankVerdict(BaseModel):
    """Structured output for the fill_in_blank judge."""
    reasoning: str = Field(
        description=(
            "3-5 short sentences covering whether each blank's value "
            "is correct, whether the blank is well-constrained by "
            "context, whether the question is on-objective, and "
            "whether accept_alternatives is adequate."
        ),
        max_length=2000,
    )
    passed: bool = Field(
        description=(
            "True iff the question is approved for use. False when "
            "any HARD violation (WRONG_ANSWER / OFF_OBJECTIVE) present."
        ),
    )
    violations: List[_ALLOWED_VIOLATIONS] = Field(
        default_factory=list,
        description="Zero or more violation codes from the enum.",
    )
    recommended_fix: str = Field(
        default="",
        description=(
            "When rejecting: ≤120-word concrete instruction the regen "
            "layer can act on. Empty when passing clean."
        ),
        max_length=800,
    )


def _dedupe_violations(codes: List[str]) -> List[str]:
    out: List[str] = []
    for code in codes:
        s = str(code or "").strip().upper()
        if s in VIOLATION_CODES and s not in out:
            out.append(s)
    return out


def _build_user_prompt(
    *,
    question_text: str,
    answer_data: Dict[str, Any],
    explanation: str,
    lesson_subject: str,
    lesson_grade: str,
    lesson_title: str,
    lesson_objective: str,
    step_concept_tag: str,
    enabling_objective: str,
) -> str:
    text_template = str(answer_data.get('text_template') or '')[:1200]
    blanks = answer_data.get('blanks') or []
    accept_alternatives = answer_data.get('accept_alternatives') or []
    payload = {
        "lesson": {
            "subject": (lesson_subject or "")[:120],
            "grade": (lesson_grade or "")[:80],
            "title": (lesson_title or "")[:200],
            "overall_objective": (lesson_objective or "")[:400],
        },
        "question_context": {
            "concept": (step_concept_tag or "")[:200],
            "enabling_objective": (enabling_objective or "")[:400],
        },
        "question": {
            "stem": (question_text or "").strip()[:1200],
            "text_template": text_template,
            "blanks": [str(b)[:200] for b in blanks[:6]],
            "accept_alternatives": [
                [str(a)[:200] for a in (alts or [])[:6]]
                for alts in accept_alternatives[:6]
            ],
            "explanation": (explanation or "").strip()[:500],
        },
    }
    return (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Based on the lesson context and the fill-in-the-blank question "
        "above, decide whether this question is approved for use. Apply "
        "each rejection code definition strictly."
    )


def run_fill_in_blank_judge(
    *,
    question_text: str,
    answer_data: Optional[Dict[str, Any]] = None,
    explanation: str = "",
    lesson=None,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
    exclude_provider: Optional[str] = None,
    judge_purpose: str = "content_judge_fill_in_blank",
    max_tokens: int = 3500,
    force_model_config=None,
) -> JudgeResult:
    """Verify a fill-in-the-blank exit-ticket question.

    Returns JudgeResult. On infrastructure failure:
    passed=True+skipped=True with skip_reason — generation never
    blocks on judge outage.
    """
    result = JudgeResult()
    answer_data = answer_data or {}

    if not (question_text or '').strip():
        result.skipped = True
        result.skip_reason = "empty_question_text"
        return result
    blanks = answer_data.get('blanks') or []
    if not blanks:
        result.skipped = True
        result.skip_reason = "no_blanks_specified"
        return result
    if not str(answer_data.get('text_template') or '').strip():
        result.skipped = True
        result.skip_reason = "no_text_template"
        return result

    if lesson is not None and not (lesson_subject and lesson_title):
        try:
            lesson_title = lesson_title or str(getattr(lesson, 'title', '') or '')
            lesson_objective = lesson_objective or str(
                getattr(lesson, 'objective', '') or ''
            )
            unit = getattr(lesson, 'unit', None)
            course = getattr(unit, 'course', None) if unit else None
            if course is not None and not lesson_subject:
                subj_type = getattr(course, 'subject_type', '') or ''
                course_name = getattr(course, 'name', '') or ''
                lesson_subject = str(subj_type or course_name)
            if course is not None and not lesson_grade:
                grades = getattr(course, 'grade_levels', None) or []
                if isinstance(grades, list) and grades:
                    lesson_grade = ", ".join(str(g) for g in grades[:3])
                else:
                    lesson_grade = str(getattr(course, 'grade_level', '') or '')
        except Exception:
            pass

    providers = get_judge_provider_chain(
        judge_purpose, exclude_provider=exclude_provider,
        force_model_config=force_model_config,
    )
    if not providers:
        logger.warning(
            "[FillInBlankJudge] no providers available "
            f"(purpose={judge_purpose}, exclude={exclude_provider})"
        )
        result.skipped = True
        result.skip_reason = "no_providers_available"
        return result

    user_prompt = _build_user_prompt(
        question_text=question_text,
        answer_data=answer_data,
        explanation=explanation,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        step_concept_tag=step_concept_tag,
        enabling_objective=enabling_objective,
    )

    # FIB correctness IS factual — Gemini search grounding catches
    # wrong blank values that the curriculum KB might miss.
    if _grounding_enabled():
        call = call_judge_grounded_then_structured(
            user_prompt,
            providers,
            FillInBlankVerdict,
            system_prompt=_SYSTEM_INSTRUCTION,
            max_tokens=max_tokens,
        )
    else:
        call = call_judge_structured_with_fallback(
            user_prompt,
            providers,
            FillInBlankVerdict,
            system_prompt=_SYSTEM_INSTRUCTION,
            max_tokens=max_tokens,
        )
    if not call.success:
        logger.warning(
            f"[FillInBlankJudge] all providers failed: "
            f"{call.error_class}: {call.error_detail}"
        )
        result.skipped = True
        result.skip_reason = f"all_providers_failed: {call.error_class}"
        return result

    result.provider = call.provider
    result.model_name = call.model_name

    verdict: FillInBlankVerdict = call.verdict
    result.reasoning = (verdict.reasoning or "").strip()[:300]

    violations = _dedupe_violations(list(verdict.violations or []))
    fix = (verdict.recommended_fix or "").strip()[:600]

    hard = [v for v in violations if v in _HARD_CODES]
    if hard:
        passed = False
    else:
        passed = bool(verdict.passed) or not violations

    result.passed = passed
    result.violations = violations
    result.recommended_fix = fix if not passed or violations else ""

    logger.info(
        f"[FillInBlankJudge] {'PASS' if result.passed else 'REJECT'} via "
        f"{call.provider}/{call.model_name} violations={violations}"
    )

    return result


__all__ = [
    "VIOLATION_CODES",
    "VIOLATION_WRONG_ANSWER",
    "VIOLATION_AMBIGUOUS_BLANK",
    "VIOLATION_OFF_OBJECTIVE",
    "VIOLATION_MISSING_ALTERNATIVES",
    "VIOLATION_TRICK_WORDING",
    "FillInBlankVerdict",
    "run_fill_in_blank_judge",
]
