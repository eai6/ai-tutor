"""POST-generation judge for matching exit-ticket questions.

Mirrors `exit_question.py` (MCQ) shape but adapted for matching schema:
each question has `pairs` (list of {"left": "A", "right": "1"} dicts
representing the canonical left↔right mappings) and `distractor_rights`
(extra unmatched right-side options to make the question non-trivial).

**Hooks at:** `apps/curriculum/content_generator.py` POST-gen of
ExitTicketQuestion (alongside the MCQ + fill_in_blank + short_answer
judges).
**Generator-side providers:** Anthropic (default for `generation`).
**Judge-side providers:** Gemini → Anthropic → OpenAI (cross-provider).

Six stable violation codes — each names a distinct fix path:

  MATCH_WRONG_PAIR (HARD) — at least one canonical left↔right pair
    is factually wrong. Hard reject.

  MATCH_AMBIGUOUS_PAIRING (HARD) — multiple valid mappings exist
    between the left and right items; the marked pairing isn't
    uniquely correct. Hard reject.

  MATCH_DISTRACTOR_VALID (HARD) — one of the `distractor_rights`
    items is actually a valid right-side for some left item not in
    the canonical pairs. Hard reject — student would be marked wrong
    for a valid answer.

  MATCH_OFF_OBJECTIVE (HARD) — the matching exercise tests a concept
    the lesson / enabling objective doesn't teach. Hard reject.

  MATCH_UNEVEN_DIFFICULTY (SOFT) — pairs differ wildly in difficulty
    (some trivially obvious, some require deep recall); reduces
    discriminatory power. Soft signal.

  MATCH_TRIVIAL_DISTRACTORS (SOFT) — distractor_rights are so
    obviously wrong they don't add any challenge. Soft signal.

Verdict semantics:
  - passed=True, violations=[] → ship it
  - passed=False → at least one HARD code; should regen
  - passed=True, violations=[soft only] → surface to teacher review

Skips when: question_type != 'matching' / pairs missing / providers
fail. Skip returns passed=True so gen never blocks.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

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
VIOLATION_WRONG_PAIR = "MATCH_WRONG_PAIR"
VIOLATION_AMBIGUOUS_PAIRING = "MATCH_AMBIGUOUS_PAIRING"
VIOLATION_DISTRACTOR_VALID = "MATCH_DISTRACTOR_VALID"
VIOLATION_OFF_OBJECTIVE = "MATCH_OFF_OBJECTIVE"
VIOLATION_UNEVEN_DIFFICULTY = "MATCH_UNEVEN_DIFFICULTY"
VIOLATION_TRIVIAL_DISTRACTORS = "MATCH_TRIVIAL_DISTRACTORS"

VIOLATION_CODES = (
    VIOLATION_WRONG_PAIR,
    VIOLATION_AMBIGUOUS_PAIRING,
    VIOLATION_DISTRACTOR_VALID,
    VIOLATION_OFF_OBJECTIVE,
    VIOLATION_UNEVEN_DIFFICULTY,
    VIOLATION_TRIVIAL_DISTRACTORS,
)

_HARD_CODES = frozenset({
    VIOLATION_WRONG_PAIR,
    VIOLATION_AMBIGUOUS_PAIRING,
    VIOLATION_DISTRACTOR_VALID,
    VIOLATION_OFF_OBJECTIVE,
})


_SYSTEM_INSTRUCTION = """\
Review one matching exit-ticket question for use in a secondary-school \
lesson. Check every canonical left↔right pair is correct, the mapping \
is unambiguous, and the distractor_rights are plausibly wrong but not \
secretly valid.

Approve the question when ALL of these hold:
  - Every canonical pair (left, right) is factually correct.
  - The mapping is unique — no left item has more than one valid right \
side under any reasonable reading.
  - No distractor_rights item is a valid match for any left item.
  - The question tests what the enabling_objective specifies.
  - Pairs are similar in difficulty; distractors are plausible-but-wrong, \
not obviously absurd.

Reject otherwise. Use ONLY these codes:

  MATCH_WRONG_PAIR
    At least one canonical pair is factually wrong.
    Example: "Capital ↔ Country" pair "Praslin ↔ Seychelles" — the \
capital is Victoria, not Praslin.

  MATCH_AMBIGUOUS_PAIRING
    Multiple valid right-sides exist for some left under any \
reasonable reading. Student answering with an alternative valid \
mapping is marked wrong.
    Example: pairs include "Rainy season ↔ December" but also "Dry \
season ↔ June"; in Seychelles both seasons span multiple months — \
any single month is ambiguous.

  MATCH_DISTRACTOR_VALID
    One distractor_rights item is actually a valid match for some \
left item (whether listed in pairs or not).
    Example: lefts include "Inner Islands"; rights include "Mahé" \
(correct pair) but distractor_rights has "Praslin" — Praslin is also \
an Inner Island, so it's a valid match.

  MATCH_OFF_OBJECTIVE
    The matching exercise tests a concept the lesson / enabling \
objective does not teach.

  MATCH_UNEVEN_DIFFICULTY
    Some pairs are trivial (obvious from name), others require deep \
recall. Reduces discriminatory power.

  MATCH_TRIVIAL_DISTRACTORS
    distractor_rights are so obviously wrong (off-topic, absurd) that \
they add no challenge.

When rejecting, write a `recommended_fix` (≤120 words) — a concrete \
edit naming the specific change (which pair to fix, which distractor \
to replace, how to disambiguate). No "make it better" comments.

In `reasoning`, write 3-5 short sentences. Walk through each \
canonical pair's correctness, check whether the mapping is unique, \
inspect each distractor for hidden validity, and note objective \
alignment.
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_ALLOWED_VIOLATIONS = Literal[
    "MATCH_WRONG_PAIR",
    "MATCH_AMBIGUOUS_PAIRING",
    "MATCH_DISTRACTOR_VALID",
    "MATCH_OFF_OBJECTIVE",
    "MATCH_UNEVEN_DIFFICULTY",
    "MATCH_TRIVIAL_DISTRACTORS",
]


class MatchingVerdict(BaseModel):
    """Structured output for the matching judge."""
    reasoning: str = Field(
        description=(
            "3-5 short sentences covering each pair's correctness, "
            "uniqueness of the mapping, distractor validity, and "
            "objective alignment."
        ),
        max_length=2000,
    )
    passed: bool = Field(
        description=(
            "True iff the question is approved for use. False when "
            "any HARD violation present (WRONG_PAIR / "
            "AMBIGUOUS_PAIRING / DISTRACTOR_VALID / OFF_OBJECTIVE)."
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
    pairs = answer_data.get('pairs') or []
    distractor_rights = answer_data.get('distractor_rights') or []
    safe_pairs = []
    for p in pairs[:10]:
        if not isinstance(p, dict):
            continue
        safe_pairs.append({
            "left": str(p.get('left') or '')[:200],
            "right": str(p.get('right') or '')[:200],
        })
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
            "pairs": safe_pairs,
            "distractor_rights": [
                str(d)[:200] for d in distractor_rights[:8]
            ],
            "explanation": (explanation or "").strip()[:500],
        },
    }
    return (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Based on the lesson context and the matching question above, "
        "decide whether this question is approved for use. Apply each "
        "rejection code definition strictly."
    )


def run_matching_judge(
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
    judge_purpose: str = "content_judge_matching",
    max_tokens: int = 3500,
    force_model_config=None,
) -> JudgeResult:
    """Verify a matching exit-ticket question."""
    result = JudgeResult()
    answer_data = answer_data or {}

    if not (question_text or '').strip():
        result.skipped = True
        result.skip_reason = "empty_question_text"
        return result
    pairs = answer_data.get('pairs') or []
    if not pairs or len(pairs) < 2:
        result.skipped = True
        result.skip_reason = "too_few_pairs"
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
            "[MatchingJudge] no providers available "
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

    # Matching pair correctness IS factual — Gemini search grounding
    # catches wrong pairs (e.g. "Eurasian Plate ↔ South America")
    # that the curriculum KB might miss.
    if _grounding_enabled():
        call = call_judge_grounded_then_structured(
            user_prompt,
            providers,
            MatchingVerdict,
            system_prompt=_SYSTEM_INSTRUCTION,
            max_tokens=max_tokens,
        )
    else:
        call = call_judge_structured_with_fallback(
            user_prompt,
            providers,
            MatchingVerdict,
            system_prompt=_SYSTEM_INSTRUCTION,
            max_tokens=max_tokens,
        )
    if not call.success:
        logger.warning(
            f"[MatchingJudge] all providers failed: "
            f"{call.error_class}: {call.error_detail}"
        )
        result.skipped = True
        result.skip_reason = f"all_providers_failed: {call.error_class}"
        return result

    result.provider = call.provider
    result.model_name = call.model_name

    verdict: MatchingVerdict = call.verdict
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
        f"[MatchingJudge] {'PASS' if result.passed else 'REJECT'} via "
        f"{call.provider}/{call.model_name} violations={violations}"
    )

    return result


__all__ = [
    "VIOLATION_CODES",
    "VIOLATION_WRONG_PAIR",
    "VIOLATION_AMBIGUOUS_PAIRING",
    "VIOLATION_DISTRACTOR_VALID",
    "VIOLATION_OFF_OBJECTIVE",
    "VIOLATION_UNEVEN_DIFFICULTY",
    "VIOLATION_TRIVIAL_DISTRACTORS",
    "MatchingVerdict",
    "run_matching_judge",
]
