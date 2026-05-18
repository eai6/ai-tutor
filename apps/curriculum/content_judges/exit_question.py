"""POST-generation judge for MCQ exit-ticket questions.

Runs after `apps/curriculum/pipeline.py::generate_exit_ticket_questions()`
saves each ExitTicketQuestion. Verifies the question has:
  - a defensibly-correct marked answer
  - distractors that are wrong but plausible (not obviously wrong, not
    secretly correct)
  - alignment with the lesson + step learning objective
  - no trick wording

Catches the failure modes that hurt students most:
  - Wrong answer keys (the marked correct option is actually wrong)
  - Multiple defensibly-correct options (ambiguous)
  - "Trick questions" that test grammar parsing rather than concepts
  - Off-objective drift (question tests a different concept than the
    step actually taught)

**Hooks at:** `apps/curriculum/pipeline.py` POST-gen of exit-ticket
questions, AND `apps/dashboard/views.py::exit_question_regenerate`
auto_review mode (Q3.2 wired this in but routed to a 400 placeholder
until Q4 — this judge unlocks it).

**Generator-side providers:** Anthropic (default for `generation`).
**Judge-side providers:** Gemini → Anthropic → OpenAI (cross-provider;
generator's provider is excluded). See `_providers.py`.

Five stable violation codes — each names a distinct fix path:

  - EXITQ_WRONG_ANSWER_KEY — the marked correct option is wrong.
    HARD reject; passed=False. The student would learn the wrong fact.
  - EXITQ_MULTIPLE_CORRECT — more than one option is defensibly
    correct. HARD reject; passed=False. The student is graded
    against an arbitrary "right" answer.
  - EXITQ_AMBIGUOUS_DISTRACTOR — at least one distractor is plausible
    enough that a student who knows the material could pick it. SOFT;
    passed=True with violation surfaced for teacher review.
  - EXITQ_OFF_OBJECTIVE — question tests something the step/lesson
    doesn't teach. HARD reject; passed=False. Either replace the
    question or rescope the lesson.
  - EXITQ_TRICK_WORDING — the question relies on grammatical tricks
    (negations, "EXCEPT", word order) rather than testing the
    concept. SOFT; passed=True with violation surfaced.

Verdict semantics:
  - passed=True, violations=[] → question is good, ship it
  - passed=False → at least one HARD code present; should regen
  - passed=True, violations=[soft codes only] → surface to teacher
    review but don't auto-trigger regen

Skips when: question_type != 'mcq' (other types not in v1 scope) /
required fields missing / providers fail / verdict unparseable. All
skips return passed=True so the gen pipeline never blocks.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from apps.curriculum.content_judges import JudgeResult
from apps.curriculum.content_judges._providers import (
    _grounding_enabled,
    call_judge_grounded_then_structured,
    call_judge_structured_with_fallback,
    get_judge_provider_chain,
)

logger = logging.getLogger(__name__)


# ─── Stable violation codes ────────────────────────────────────────────
VIOLATION_WRONG_ANSWER_KEY = "EXITQ_WRONG_ANSWER_KEY"
VIOLATION_MULTIPLE_CORRECT = "EXITQ_MULTIPLE_CORRECT"
VIOLATION_AMBIGUOUS_DISTRACTOR = "EXITQ_AMBIGUOUS_DISTRACTOR"
VIOLATION_OFF_OBJECTIVE = "EXITQ_OFF_OBJECTIVE"
VIOLATION_TRICK_WORDING = "EXITQ_TRICK_WORDING"

VIOLATION_CODES = (
    VIOLATION_WRONG_ANSWER_KEY,
    VIOLATION_MULTIPLE_CORRECT,
    VIOLATION_AMBIGUOUS_DISTRACTOR,
    VIOLATION_OFF_OBJECTIVE,
    VIOLATION_TRICK_WORDING,
)

# Codes that force passed=False. Anything else is a soft warning.
_HARD_CODES = frozenset({
    VIOLATION_WRONG_ANSWER_KEY,
    VIOLATION_MULTIPLE_CORRECT,
    VIOLATION_OFF_OBJECTIVE,
})


# ─── System instruction ────────────────────────────────────────────────
# Direct task statement (Gemini 3 anti-flowery rule). Closed violation
# enum; reason-in-prose then emit JSON wrapper. Each code is paired
# with a worked example so the model has an anchor for the boundary.
_SYSTEM_INSTRUCTION = """\
Review one multiple-choice exit-ticket question for use in a \
secondary-school lesson. Check the marked correct answer is truly \
correct, the distractors are wrong but plausible, and the question \
tests the lesson's learning objective.

Approve the question when ALL of these hold:
  - The marked correct option is unambiguously the right answer.
  - Exactly one option is defensibly correct.
  - Distractors are wrong, but plausible enough that a student who \
hasn't grasped the concept might pick them.
  - The question tests what the step / lesson actually teaches.
  - The question's wording is direct — students fail by not knowing \
the concept, not by misreading the grammar.

Reject otherwise. Use ONLY these codes:

  EXITQ_WRONG_ANSWER_KEY
    The marked correct option is actually wrong, or another option is \
clearly more correct.
    Example: question "Capital of Seychelles?", marked B="Praslin"; \
the right answer is "Victoria" (option A).

  EXITQ_MULTIPLE_CORRECT
    Two or more options are defensibly correct under any reasonable \
reading. Even if one is "more" correct, the alternatives aren't \
unambiguously wrong.
    Example: "What language is spoken in Seychelles?" with both \
"Creole" and "English" as options — both are official languages.

  EXITQ_AMBIGUOUS_DISTRACTOR
    A specific distractor is so plausible that a student who knows \
the material could legitimately pick it. (Different from \
MULTIPLE_CORRECT — here the marked answer IS unambiguously correct, \
but a distractor is too tempting because of a real-world overlap.)

  EXITQ_OFF_OBJECTIVE
    The question tests a concept the lesson does not teach. Either \
the question is mis-scoped, or it belongs in a different lesson.
    Example: lesson is "five themes of geography", question asks \
about plate tectonics.

  EXITQ_TRICK_WORDING
    The question relies on parsing tricks (negations, double-negatives, \
"EXCEPT", "NOT", clever word order) rather than concept comprehension. \
Students who understand the material can still get it wrong by \
misreading.
    Example: "Which of the following is NOT a feature of geography \
EXCEPT..." — students fail on grammar, not concept.

When rejecting, write a `recommended_fix` (≤120 words) that gives \
the regen layer a concrete, self-contained instruction — name the \
specific edit (which option to fix, which to relabel as correct, \
what to rephrase). No "make it better" comments.

In `reasoning`, write 3-5 short sentences. Walk through whether the \
marked answer is correct, whether each distractor is unambiguously \
wrong, whether the question is on-objective, and whether the wording \
is fair. Cite the specific options when justifying.
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_ALLOWED_VIOLATIONS = Literal[
    "EXITQ_WRONG_ANSWER_KEY",
    "EXITQ_MULTIPLE_CORRECT",
    "EXITQ_AMBIGUOUS_DISTRACTOR",
    "EXITQ_OFF_OBJECTIVE",
    "EXITQ_TRICK_WORDING",
]


class ExitQuestionVerdict(BaseModel):
    """Structured output for the exit_question judge.

    Instructor enforces this schema via each provider's native
    structured-output API — eliminates the unparseable_json failure
    class entirely.
    """
    reasoning: str = Field(
        description=(
            "3-5 short sentences walking through whether the marked "
            "answer is correct, whether distractors are unambiguously "
            "wrong, whether the question is on-objective, and whether "
            "the wording is fair. Cite specific options."
        ),
        max_length=2000,
    )
    passed: bool = Field(
        description=(
            "True iff the question is approved for use. False if any "
            "HARD violation is present (WRONG_ANSWER_KEY / "
            "MULTIPLE_CORRECT / OFF_OBJECTIVE)."
        ),
    )
    violations: List[_ALLOWED_VIOLATIONS] = Field(
        default_factory=list,
        description="Zero or more violation codes from the enum.",
    )
    recommended_fix: str = Field(
        default="",
        description=(
            "When rejecting: ≤120-word concrete instruction the "
            "regen layer can act on (which option to fix, which to "
            "relabel as correct, what to rephrase). Empty when passing."
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
    option_a: str,
    option_b: str,
    option_c: str,
    option_d: str,
    correct_answer: str,
    explanation: str,
    lesson_subject: str,
    lesson_grade: str,
    lesson_title: str,
    lesson_objective: str,
    step_concept_tag: str,
    enabling_objective: str,
) -> str:
    """Compose the user message — input goes LAST (long-context
    query-last). XML context blocks for cross-provider portability."""
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
            "option_a": (option_a or "").strip()[:300],
            "option_b": (option_b or "").strip()[:300],
            "option_c": (option_c or "").strip()[:300],
            "option_d": (option_d or "").strip()[:300],
            "marked_correct": (correct_answer or "").strip().upper()[:1],
            "explanation": (explanation or "").strip()[:500],
        },
    }
    return (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Based on the lesson context and the question above, decide "
        "whether this MCQ is approved for use. Apply each rejection "
        "code definition strictly. Cite specific options when "
        "justifying."
    )


# ─── Public entry point ────────────────────────────────────────────────
def run_exit_question_judge(
    *,
    question_text: str,
    option_a: str = "",
    option_b: str = "",
    option_c: str = "",
    option_d: str = "",
    correct_answer: str = "",
    explanation: str = "",
    lesson=None,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
    exclude_provider: Optional[str] = None,
    judge_purpose: str = "content_judge_exit_question",
    max_tokens: int = 3500,
    force_model_config=None,
) -> JudgeResult:
    """Verify an MCQ exit-ticket question.

    Args:
        question_text: The question stem.
        option_a..d: The four answer choices.
        correct_answer: A/B/C/D — the marked correct option.
        explanation: The post-answer explanation (helps the judge
            understand the intended reasoning).
        lesson: Lesson instance for context derivation. Optional —
            when None, the lesson_* fields below must be supplied
            directly.
        lesson_*/step_*: Lesson + step context. Auto-derived from
            `lesson` when not supplied.
        exclude_provider: Provider that produced the question.
            Judge chain skips this provider for cross-provider review.
        judge_purpose: ModelConfig purpose to consult first.
        max_tokens: Cap on judge output.

    Returns:
        JudgeResult. On infrastructure failure: passed=True+skipped=True
        with skip_reason — generation never blocks on judge outage.
    """
    result = JudgeResult()

    # Pre-gates
    if not (question_text or '').strip():
        result.skipped = True
        result.skip_reason = "empty_question_text"
        return result
    options = [option_a, option_b, option_c, option_d]
    if not all((o or '').strip() for o in options):
        result.skipped = True
        result.skip_reason = "missing_options"
        return result
    correct = (correct_answer or '').strip().upper()[:1]
    if correct not in {'A', 'B', 'C', 'D'}:
        result.skipped = True
        result.skip_reason = "invalid_correct_answer"
        return result

    # Auto-derive lesson context
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
            "[ExitQuestionJudge] no providers available "
            f"(purpose={judge_purpose}, exclude={exclude_provider})"
        )
        result.skipped = True
        result.skip_reason = "no_providers_available"
        return result

    user_prompt = _build_user_prompt(
        question_text=question_text,
        option_a=option_a, option_b=option_b,
        option_c=option_c, option_d=option_d,
        correct_answer=correct, explanation=explanation,
        lesson_subject=lesson_subject, lesson_grade=lesson_grade,
        lesson_title=lesson_title, lesson_objective=lesson_objective,
        step_concept_tag=step_concept_tag,
        enabling_objective=enabling_objective,
    )

    # MCQ correctness IS factual — use Gemini search-grounded two-call
    # pattern when available so wrong-answer-key claims get caught
    # against live web sources, not just curriculum.
    if _grounding_enabled():
        call = call_judge_grounded_then_structured(
            user_prompt,
            providers,
            ExitQuestionVerdict,
            system_prompt=_SYSTEM_INSTRUCTION,
            max_tokens=max_tokens,
        )
    else:
        call = call_judge_structured_with_fallback(
            user_prompt,
            providers,
            ExitQuestionVerdict,
            system_prompt=_SYSTEM_INSTRUCTION,
            max_tokens=max_tokens,
        )
    if not call.success:
        logger.warning(
            f"[ExitQuestionJudge] all providers failed: "
            f"{call.error_class}: {call.error_detail}"
        )
        result.skipped = True
        result.skip_reason = f"all_providers_failed: {call.error_class}"
        return result

    result.provider = call.provider
    result.model_name = call.model_name

    verdict: ExitQuestionVerdict = call.verdict
    result.reasoning = (verdict.reasoning or "").strip()[:300]

    violations = _dedupe_violations(list(verdict.violations or []))
    fix = (verdict.recommended_fix or "").strip()[:600]

    # Pass policy: any HARD code → fail; only SOFT codes → pass with
    # warning; respect model's passed=false even if codes are all soft
    # (model may see something we don't enumerate).
    hard_violations = [v for v in violations if v in _HARD_CODES]
    if hard_violations:
        passed = False
    else:
        passed = bool(verdict.passed) or not violations

    result.passed = passed
    result.violations = violations
    result.recommended_fix = fix if not passed or violations else ""

    logger.info(
        f"[ExitQuestionJudge] {'PASS' if result.passed else 'REJECT'} via "
        f"{call.provider}/{call.model_name} violations={violations}"
    )

    return result


__all__ = [
    "VIOLATION_CODES",
    "VIOLATION_WRONG_ANSWER_KEY",
    "VIOLATION_MULTIPLE_CORRECT",
    "VIOLATION_AMBIGUOUS_DISTRACTOR",
    "VIOLATION_OFF_OBJECTIVE",
    "VIOLATION_TRICK_WORDING",
    "run_exit_question_judge",
]
