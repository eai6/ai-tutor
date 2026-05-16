"""POST-generation judge for short-answer exit-ticket questions.

Mirrors `exit_question.py` (MCQ) shape but adapted for SA schema:
each question has a `question_text` (open prompt), a `model_answer`
(rubric), `keywords` (list of expected terms), and `min_keywords`
(threshold for "correct" grading).

**Hooks at:** `apps/curriculum/content_generator.py` POST-gen of
ExitTicketQuestion (alongside the MCQ + fill_in_blank + matching judges).
**Generator-side providers:** Anthropic (default for `generation`).
**Judge-side providers:** Gemini → Anthropic → OpenAI (cross-provider).

Six stable violation codes — each names a distinct fix path:

  SA_WRONG_MODEL_ANSWER (HARD) — the model answer is factually wrong.
    Hard reject; the student is graded against a wrong key.

  SA_VAGUE_QUESTION (HARD) — the question is too open / ambiguous;
    multiple distinct answers would all be valid. Hard reject.

  SA_OFF_OBJECTIVE (HARD) — the question tests something the lesson
    / enabling objective doesn't teach. Hard reject.

  SA_KEYWORDS_INCOMPLETE (SOFT) — `keywords` list doesn't capture
    common acceptable answer variants; students with valid answers
    risk being marked wrong by the keyword-match grader.

  SA_KEYWORDS_TOO_PERMISSIVE (SOFT) — `keywords` would match
    unrelated answers (e.g. keyword = "the" or a common stopword);
    grader would mark off-topic answers correct.

  SA_RUBRIC_MISMATCH (SOFT) — model_answer doesn't actually use the
    keywords required by the grader, so a student matching the keyword
    threshold may not have given the model answer.

Verdict semantics:
  - passed=True, violations=[] → ship it
  - passed=False → at least one HARD code; should regen
  - passed=True, violations=[soft only] → surface to teacher review

Skips when: question_type != 'short_answer' / required fields missing
/ providers fail. Skip returns passed=True so gen never blocks.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from apps.curriculum.content_judges import JudgeResult
from apps.curriculum.content_judges._providers import (
    call_judge_structured_with_fallback,
    get_judge_provider_chain,
)

logger = logging.getLogger(__name__)


# ─── Stable violation codes ────────────────────────────────────────────
VIOLATION_WRONG_MODEL_ANSWER = "SA_WRONG_MODEL_ANSWER"
VIOLATION_VAGUE_QUESTION = "SA_VAGUE_QUESTION"
VIOLATION_OFF_OBJECTIVE = "SA_OFF_OBJECTIVE"
VIOLATION_KEYWORDS_INCOMPLETE = "SA_KEYWORDS_INCOMPLETE"
VIOLATION_KEYWORDS_TOO_PERMISSIVE = "SA_KEYWORDS_TOO_PERMISSIVE"
VIOLATION_RUBRIC_MISMATCH = "SA_RUBRIC_MISMATCH"

VIOLATION_CODES = (
    VIOLATION_WRONG_MODEL_ANSWER,
    VIOLATION_VAGUE_QUESTION,
    VIOLATION_OFF_OBJECTIVE,
    VIOLATION_KEYWORDS_INCOMPLETE,
    VIOLATION_KEYWORDS_TOO_PERMISSIVE,
    VIOLATION_RUBRIC_MISMATCH,
)

_HARD_CODES = frozenset({
    VIOLATION_WRONG_MODEL_ANSWER,
    VIOLATION_VAGUE_QUESTION,
    VIOLATION_OFF_OBJECTIVE,
})


_SYSTEM_INSTRUCTION = """\
Review one short-answer exit-ticket question for use in a \
secondary-school lesson. Check the model answer is correct, the \
question is well-scoped, and the keyword-grader rubric will mark \
valid answers correctly.

Approve the question when ALL of these hold:
  - The model_answer is factually correct and on-objective.
  - The question is specific enough that ONE focus is clearly expected \
(or a small set of equivalents the rubric captures).
  - The keywords list captures common acceptable answer variants \
without being so generic it would match unrelated answers.
  - The model_answer actually contains (or implies) the keywords the \
grader will require — otherwise a student matching the keyword \
threshold may not have given the model answer.

Reject otherwise. Use ONLY these codes:

  SA_WRONG_MODEL_ANSWER
    The model_answer is factually wrong or contradicts the curriculum.
    Example: "Why is Victoria the capital?", model_answer = "It has \
the largest population" — but Praslin has a larger population.

  SA_VAGUE_QUESTION
    Question is too open; multiple distinct valid answers exist. The \
keyword grader can't distinguish between them.
    Example: "What is geography?" — no constraint, many right answers.

  SA_OFF_OBJECTIVE
    Question tests a concept the lesson / enabling objective doesn't \
teach.

  SA_KEYWORDS_INCOMPLETE
    Common acceptable answer variants (synonyms, alternate phrasings) \
are missing from `keywords`. Students with valid answers risk being \
marked wrong.
    Example: model_answer about "evaporation" but keywords only has \
"evaporate" — student writing "evaporated water" wouldn't match.

  SA_KEYWORDS_TOO_PERMISSIVE
    keywords would match unrelated answers (a generic word, a stopword, \
or a term too broad). Grader marks off-topic answers correct.
    Example: keywords = ["the", "is"] — meaningless match.

  SA_RUBRIC_MISMATCH
    model_answer doesn't itself contain the keywords the grader \
requires. Students matching the keyword threshold haven't given the \
model answer.

When rejecting, write a `recommended_fix` (≤120 words) — a concrete \
edit naming the specific change (rephrase the stem, fix the model \
answer, add or replace specific keywords). No "make it better" \
comments.

In `reasoning`, write 3-5 short sentences. Walk through whether the \
model answer is right, whether the question is well-scoped, whether \
the keywords capture common variants without being too broad, and \
whether the rubric is internally consistent.
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_ALLOWED_VIOLATIONS = Literal[
    "SA_WRONG_MODEL_ANSWER",
    "SA_VAGUE_QUESTION",
    "SA_OFF_OBJECTIVE",
    "SA_KEYWORDS_INCOMPLETE",
    "SA_KEYWORDS_TOO_PERMISSIVE",
    "SA_RUBRIC_MISMATCH",
]


class ShortAnswerVerdict(BaseModel):
    """Structured output for the short_answer judge."""
    reasoning: str = Field(
        description=(
            "3-5 short sentences covering model-answer correctness, "
            "question scope, keyword coverage, and rubric consistency."
        ),
        max_length=2000,
    )
    passed: bool = Field(
        description=(
            "True iff the question is approved for use. False when "
            "any HARD violation (WRONG_MODEL_ANSWER / VAGUE_QUESTION "
            "/ OFF_OBJECTIVE) is present."
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
            "model_answer": str(answer_data.get('model_answer') or '').strip()[:800],
            "keywords": [
                str(k)[:120] for k in (answer_data.get('keywords') or [])[:15]
            ],
            "min_keywords": int(answer_data.get('min_keywords') or 1),
            "explanation": (explanation or "").strip()[:500],
        },
    }
    return (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Based on the lesson context and the short-answer question above, "
        "decide whether this question is approved for use. Apply each "
        "rejection code definition strictly."
    )


def run_short_answer_judge(
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
    judge_purpose: str = "content_judge_short_answer",
    max_tokens: int = 3500,
) -> JudgeResult:
    """Verify a short-answer exit-ticket question."""
    result = JudgeResult()
    answer_data = answer_data or {}

    if not (question_text or '').strip():
        result.skipped = True
        result.skip_reason = "empty_question_text"
        return result
    if not str(answer_data.get('model_answer') or '').strip():
        result.skipped = True
        result.skip_reason = "no_model_answer"
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
    )
    if not providers:
        logger.warning(
            "[ShortAnswerJudge] no providers available "
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

    call = call_judge_structured_with_fallback(
        user_prompt,
        providers,
        ShortAnswerVerdict,
        system_prompt=_SYSTEM_INSTRUCTION,
        max_tokens=max_tokens,
    )
    if not call.success:
        logger.warning(
            f"[ShortAnswerJudge] all providers failed: "
            f"{call.error_class}: {call.error_detail}"
        )
        result.skipped = True
        result.skip_reason = f"all_providers_failed: {call.error_class}"
        return result

    result.provider = call.provider
    result.model_name = call.model_name

    verdict: ShortAnswerVerdict = call.verdict
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
        f"[ShortAnswerJudge] {'PASS' if result.passed else 'REJECT'} via "
        f"{call.provider}/{call.model_name} violations={violations}"
    )

    return result


__all__ = [
    "VIOLATION_CODES",
    "VIOLATION_WRONG_MODEL_ANSWER",
    "VIOLATION_VAGUE_QUESTION",
    "VIOLATION_OFF_OBJECTIVE",
    "VIOLATION_KEYWORDS_INCOMPLETE",
    "VIOLATION_KEYWORDS_TOO_PERMISSIVE",
    "VIOLATION_RUBRIC_MISMATCH",
    "ShortAnswerVerdict",
    "run_short_answer_judge",
]
