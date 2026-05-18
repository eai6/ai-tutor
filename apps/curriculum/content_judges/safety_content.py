"""POST-generation child-safety + cultural-appropriateness judge for
generated content.

Smaller-stakes companion to `apps/tutoring/judges/safety.py` (which
runs on every live tutor turn and student message). This judge runs
once per generated lesson step, not per turn — so it can afford a
slightly more thorough review without the runtime budget pressure.

**Hooks at:** `apps/curriculum/content_generator.py` POST-gen of
LessonStep, in the same fan-out as factual_step + pedagogy_step.
**Generator-side providers:** Anthropic (default for `generation`).
**Judge-side providers:** Gemini → Anthropic → OpenAI (cross-provider).

Four stable violation codes — each names a specific risk class:

  - SAFETY_HARMFUL_CONTENT (HARD) — content describes or implies harm
    to self or others, depicts violence, or encourages dangerous
    behaviour. Hard reject.
  - SAFETY_AGE_INAPPROPRIATE (HARD) — sexual / mature content not
    suitable for the lesson grade band. Hard reject.
  - SAFETY_CULTURAL_MISFIT (SOFT) — content uses examples or
    references that are insensitive to the Seychelles pilot context
    (or the lesson's stated cultural setting). Doesn't auto-block,
    surfaces for teacher review.
  - SAFETY_BIASED_FRAMING (SOFT) — content presents stereotypes or
    one-sided framing of a group / region / identity that should be
    re-balanced. Surfaces for teacher review.

Verdict semantics:
  - passed=True, violations=[] → safe
  - passed=False (HARD codes) → must NOT ship; auto_flagged status
  - passed=True with SOFT codes → surface to teacher for review

Skips when: empty step / providers fail / verdict unparseable. All
skips return passed=True so the gen pipeline never blocks. (Note:
this is conservative — a safety judge skip means we're SHIPPING
unreviewed content. The safety net here is the live tutor-side
safety judge in apps/tutoring/judges/safety.py which runs at
runtime on every tutor turn.)
"""

from __future__ import annotations

import json
import logging
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from apps.curriculum.content_judges import JudgeResult
from apps.curriculum.content_judges._providers import (
    call_judge_structured_with_fallback,
    get_judge_provider_chain,
)

logger = logging.getLogger(__name__)


# ─── Stable violation codes ────────────────────────────────────────────
VIOLATION_HARMFUL_CONTENT = "SAFETY_HARMFUL_CONTENT"
VIOLATION_AGE_INAPPROPRIATE = "SAFETY_AGE_INAPPROPRIATE"
VIOLATION_CULTURAL_MISFIT = "SAFETY_CULTURAL_MISFIT"
VIOLATION_BIASED_FRAMING = "SAFETY_BIASED_FRAMING"

VIOLATION_CODES = (
    VIOLATION_HARMFUL_CONTENT,
    VIOLATION_AGE_INAPPROPRIATE,
    VIOLATION_CULTURAL_MISFIT,
    VIOLATION_BIASED_FRAMING,
)

_HARD_CODES = frozenset({
    VIOLATION_HARMFUL_CONTENT,
    VIOLATION_AGE_INAPPROPRIATE,
})


_SYSTEM_INSTRUCTION = """\
Review one lesson-step's narrative for child safety and cultural \
appropriateness. The step is read by a tutor to a secondary-school \
student in Seychelles (or the lesson's stated context).

Approve when the content is:
  - Free of harm depictions (violence, self-harm, dangerous \
behaviour) appropriate to the grade band.
  - Free of sexual / mature content beyond what the grade band's \
curriculum intentionally covers.
  - Culturally appropriate for the lesson's stated context — uses \
examples that resonate with the student population, doesn't lean \
on stereotypes.
  - Balanced in framing — doesn't present one-sided portrayals of \
groups, regions, or identities.

Reject otherwise. Use ONLY these codes:

  SAFETY_HARMFUL_CONTENT
    Depicts or encourages physical harm, self-harm, dangerous \
behaviour, or violence beyond what the curriculum context warrants.
    Example: a science lesson includes "you can also try this at \
home with bleach and ammonia".

  SAFETY_AGE_INAPPROPRIATE
    Sexual or mature content not warranted by the curriculum at this \
grade band.
    Example: a Form 1 geography lesson goes into graphic detail \
about ethnic conflict casualties.

  SAFETY_CULTURAL_MISFIT
    Examples / references that are insensitive or jarring for the \
lesson's stated context (e.g. uses American suburban examples for a \
Seychelles geography lesson, ignores the local marine / island \
context).

  SAFETY_BIASED_FRAMING
    Presents stereotypes or one-sided framings of groups, regions, \
or identities that should be re-balanced. Includes gendered \
assumptions, regional stereotypes, or politically loaded framings.

When rejecting, write a `recommended_fix` (≤120 words) — a concrete \
edit instruction. No "make it better" comments.

In `reasoning`, write 3-5 short sentences covering harm-content \
check, age-appropriateness, cultural fit, framing balance. Cite \
specific phrases when justifying.
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_ALLOWED_VIOLATIONS = Literal[
    "SAFETY_HARMFUL_CONTENT",
    "SAFETY_AGE_INAPPROPRIATE",
    "SAFETY_CULTURAL_MISFIT",
    "SAFETY_BIASED_FRAMING",
]


class SafetyContentVerdict(BaseModel):
    """Structured output for the safety_content judge."""
    reasoning: str = Field(
        description=(
            "3-5 short sentences covering harm check, "
            "age-appropriateness, cultural fit, and framing balance. "
            "Cite specific phrases."
        ),
        max_length=2000,
    )
    passed: bool = Field(
        description=(
            "True iff the content is safe. False when any HARD "
            "violation (HARMFUL_CONTENT / AGE_INAPPROPRIATE) is present."
        ),
    )
    violations: List[_ALLOWED_VIOLATIONS] = Field(
        default_factory=list,
        description="Zero or more violation codes from the enum.",
    )
    recommended_fix: str = Field(
        default="",
        description=(
            "When rejecting: ≤120-word concrete edit. Empty when "
            "passing clean."
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


def run_safety_content_judge(
    step_text: str,
    *,
    lesson=None,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    cultural_context: str = "Seychelles secondary school",
    exclude_provider: Optional[str] = None,
    judge_purpose: str = "content_judge_safety_content",
    max_tokens: int = 3500,
    force_model_config=None,
) -> JudgeResult:
    """Safety + cultural-appropriateness verdict on generated step text.

    `cultural_context` defaults to the Seychelles pilot setting; pass
    a different string when the lesson targets another context.

    Returns JudgeResult; passed=True+skipped=True on infra failure.
    The skip is intentionally conservative — see module docstring on
    why we don't block generation on safety-judge outages (the live
    tutor-side safety judge is the second line of defence).
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
            "[SafetyContentJudge] no providers available "
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
        "cultural_context": (cultural_context or "")[:200],
    }
    user_prompt = (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Based on the input above, judge whether this lesson-step "
        "narrative is safe and culturally appropriate."
    )

    call = call_judge_structured_with_fallback(
        user_prompt,
        providers,
        SafetyContentVerdict,
        system_prompt=_SYSTEM_INSTRUCTION,
        max_tokens=max_tokens,
    )
    if not call.success:
        logger.warning(
            f"[SafetyContentJudge] all providers failed: "
            f"{call.error_class}: {call.error_detail}"
        )
        result.skipped = True
        result.skip_reason = f"all_providers_failed: {call.error_class}"
        return result

    result.provider = call.provider
    result.model_name = call.model_name

    verdict: SafetyContentVerdict = call.verdict
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
        f"[SafetyContentJudge] {'PASS' if result.passed else 'REJECT'} via "
        f"{call.provider}/{call.model_name} violations={violations}"
    )

    return result


__all__ = [
    "VIOLATION_CODES",
    "VIOLATION_HARMFUL_CONTENT",
    "VIOLATION_AGE_INAPPROPRIATE",
    "VIOLATION_CULTURAL_MISFIT",
    "VIOLATION_BIASED_FRAMING",
    "run_safety_content_judge",
]
