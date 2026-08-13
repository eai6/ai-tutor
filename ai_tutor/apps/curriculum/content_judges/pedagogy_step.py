"""POST-generation pedagogical-soundness judge for lesson steps.

Runs alongside factual_step (Q1) on every newly-generated LessonStep.
While factual_step asks "is what this says TRUE?", pedagogy_step asks
"is the way it's TAUGHT effective for this grade band?"

**Hooks at:** `apps/curriculum/content_generator.py` POST-gen of
LessonStep, in the same fan-out as factual_step.
**Generator-side providers:** Anthropic (default for `generation`).
**Judge-side providers:** Gemini → Anthropic → OpenAI (cross-provider).

Five stable violation codes — each a distinct pedagogical failure:

  - PEDAGOGY_GRADE_MISMATCH (HARD) — vocabulary, abstraction, or
    sentence complexity is wrong for the lesson's grade band. A
    Form 1 lesson reading like a university lecture, or a Form 4
    lesson reading like primary school.
  - PEDAGOGY_NO_LEARNING_PROMPT (HARD) — the step is pure exposition
    without any retrieval-practice question, "what do you think?",
    or call-to-action that gets the student thinking. Tutoring is
    not lecturing.
  - PEDAGOGY_DOK_MISMATCH (SOFT) — the step's cognitive demand
    doesn't match the step's stated objective (e.g. objective says
    "analyze" but the step only asks for recall).
  - PEDAGOGY_OFF_OBJECTIVE (SOFT) — the step's content drifts from
    its stated objective. Less severe than factual_step OFF — this
    catches drift in HOW the concept is presented, not whether
    facts are wrong.
  - PEDAGOGY_OVERLOAD (SOFT) — the step packs in too many distinct
    concepts at once. Cognitive load too high for the grade band.

Verdict semantics:
  - passed=True, violations=[] → step is pedagogically sound
  - passed=False (HARD violations) → must regen before shipping
  - passed=True with SOFT codes → surface to teacher review

Skips when: empty step text / no objective context / providers fail.
All skips return passed=True so the gen pipeline never blocks.
"""

from __future__ import annotations

import json
import logging
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from ai_tutor.apps.curriculum.content_judges import JudgeResult
from ai_tutor.apps.curriculum.content_judges._providers import (
    call_judge_structured_with_fallback,
    get_judge_provider_chain,
)

logger = logging.getLogger(__name__)


# ─── Stable violation codes ────────────────────────────────────────────
VIOLATION_GRADE_MISMATCH = "PEDAGOGY_GRADE_MISMATCH"
VIOLATION_NO_LEARNING_PROMPT = "PEDAGOGY_NO_LEARNING_PROMPT"
VIOLATION_DOK_MISMATCH = "PEDAGOGY_DOK_MISMATCH"
VIOLATION_OFF_OBJECTIVE = "PEDAGOGY_OFF_OBJECTIVE"
VIOLATION_OVERLOAD = "PEDAGOGY_OVERLOAD"

VIOLATION_CODES = (
    VIOLATION_GRADE_MISMATCH,
    VIOLATION_NO_LEARNING_PROMPT,
    VIOLATION_DOK_MISMATCH,
    VIOLATION_OFF_OBJECTIVE,
    VIOLATION_OVERLOAD,
)

_HARD_CODES = frozenset({
    VIOLATION_GRADE_MISMATCH,
    VIOLATION_NO_LEARNING_PROMPT,
})


_SYSTEM_INSTRUCTION = """\
Review one lesson-step's narrative for pedagogical soundness. The \
step is read by a tutor to a secondary-school student.

Approve when ALL of these hold:
  - Vocabulary, sentence complexity, and abstraction match the \
lesson's grade band.
  - The step ends with (or contains) something that gets the student \
thinking — a question, a "what do you notice?", a small task, an \
invitation to respond. Pure exposition is rejected.
  - The cognitive demand matches what the step's objective requires \
(recall vs apply vs analyze).
  - The content stays on the step's stated objective.
  - The step doesn't pack in too many distinct concepts (cognitive \
overload).

Reject otherwise. Use ONLY these codes:

  PEDAGOGY_GRADE_MISMATCH
    Vocabulary or complexity wrong for the lesson grade. Either too \
abstract for the grade, or too childish for the grade.
    Example: a Form 1 (Year 7) lesson uses "epistemological \
underpinnings"; a Form 4 lesson reads like "We're going to learn \
about big mountains!"

  PEDAGOGY_NO_LEARNING_PROMPT
    The step is pure exposition with no question, prompt, or \
invitation to think. Students sit passively.
    Example: three paragraphs of "geography is the study of...", \
then ends with "Now we'll move on to the next concept."

  PEDAGOGY_DOK_MISMATCH
    The cognitive demand doesn't match the objective. Objective \
says "analyze" but step only asks for recall; objective says \
"identify" but step demands synthesis.

  PEDAGOGY_OFF_OBJECTIVE
    The narrative drifts from the step's stated objective. Different \
from factual error — the content may be true but it's teaching the \
wrong thing for THIS step.

  PEDAGOGY_OVERLOAD
    The step covers more distinct concepts than a student at this \
grade can absorb in one step. Should be split.

When rejecting, write a `recommended_fix` (≤120 words) — a concrete \
edit instruction the regen layer can apply. No "make it better" \
comments.

In `reasoning`, write 3-5 short sentences. Walk through \
grade-appropriateness, presence of a learning prompt, DOK alignment, \
on-objective focus, and load. Cite specific phrases when justifying.
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_ALLOWED_VIOLATIONS = Literal[
    "PEDAGOGY_GRADE_MISMATCH",
    "PEDAGOGY_NO_LEARNING_PROMPT",
    "PEDAGOGY_DOK_MISMATCH",
    "PEDAGOGY_OFF_OBJECTIVE",
    "PEDAGOGY_OVERLOAD",
]


class PedagogyStepVerdict(BaseModel):
    """Structured output for the pedagogy_step judge."""
    reasoning: str = Field(
        description=(
            "3-5 short sentences covering grade-appropriateness, "
            "presence of a learning prompt, DOK alignment, "
            "on-objective focus, and load. Cite specific phrases."
        ),
        max_length=2000,
    )
    passed: bool = Field(
        description=(
            "True iff the step is pedagogically sound. False when any "
            "HARD violation (GRADE_MISMATCH / NO_LEARNING_PROMPT) is "
            "present."
        ),
    )
    violations: List[_ALLOWED_VIOLATIONS] = Field(
        default_factory=list,
        description="Zero or more violation codes from the enum.",
    )
    recommended_fix: str = Field(
        default="",
        description=(
            "When rejecting: ≤120-word concrete edit the regen layer "
            "can act on. Empty when passing clean."
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


def run_pedagogy_step_judge(
    step_text: str,
    *,
    lesson=None,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_objective: str = "",
    step_concept_tag: str = "",
    exclude_provider: Optional[str] = None,
    judge_purpose: str = "content_judge_pedagogy_step",
    max_tokens: int = 3500,
    force_model_config=None,
) -> JudgeResult:
    """Pedagogical-soundness verdict on a generated lesson-step text.

    Args mirror run_factual_step_judge for orchestrator consistency.
    Returns JudgeResult; passed=True+skipped=True on infra failure.
    """
    result = JudgeResult()

    text = (step_text or "").strip()
    if not text:
        result.skipped = True
        result.skip_reason = "empty_step_text"
        return result
    if len(text) < 30:
        result.skipped = True
        result.skip_reason = "step_text_below_min_length"
        return result

    # Auto-derive lesson context from the Lesson instance
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
            "[PedagogyStepJudge] no providers available "
            f"(purpose={judge_purpose}, exclude={exclude_provider})"
        )
        result.skipped = True
        result.skip_reason = "no_providers_available"
        return result

    payload = {
        "step_text": text[:2500],
        "lesson": {
            "subject": (lesson_subject or "")[:120],
            "grade": (lesson_grade or "(unspecified)")[:80],
            "title": (lesson_title or "")[:200],
            "overall_objective": (lesson_objective or "")[:400],
        },
        "step": {
            "concept": (step_concept_tag or "")[:200],
            "objective": (step_objective or "")[:400],
        },
    }
    user_prompt = (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Based on the input above, judge whether this lesson-step "
        "narrative is pedagogically sound for the stated grade and "
        "objective."
    )

    call = call_judge_structured_with_fallback(
        user_prompt,
        providers,
        PedagogyStepVerdict,
        system_prompt=_SYSTEM_INSTRUCTION,
        max_tokens=max_tokens,
    )
    if not call.success:
        logger.warning(
            f"[PedagogyStepJudge] all providers failed: "
            f"{call.error_class}: {call.error_detail}"
        )
        result.skipped = True
        result.skip_reason = f"all_providers_failed: {call.error_class}"
        return result

    result.provider = call.provider
    result.model_name = call.model_name

    verdict: PedagogyStepVerdict = call.verdict
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
        f"[PedagogyStepJudge] {'PASS' if result.passed else 'REJECT'} via "
        f"{call.provider}/{call.model_name} violations={violations}"
    )

    return result


__all__ = [
    "VIOLATION_CODES",
    "VIOLATION_GRADE_MISMATCH",
    "VIOLATION_NO_LEARNING_PROMPT",
    "VIOLATION_DOK_MISMATCH",
    "VIOLATION_OFF_OBJECTIVE",
    "VIOLATION_OVERLOAD",
    "run_pedagogy_step_judge",
]
