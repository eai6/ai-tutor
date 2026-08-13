"""POST-generation judge for factual claims in a generated lesson step.

Runs after `apps/curriculum/pipeline.py::generate_lesson_content()` saves
each LessonStep. Uses ONE LLM call to (a) identify every claim worth
checking in the step text and (b) verify each against curriculum-KB
evidence. No regex extraction — see
`auto-memory/feedback_llm_claim_extraction.md` for why.

**Hooks at:** `apps/curriculum/pipeline.py::generate_lesson_content()` —
post-save loop after each LessonStep is created.
**Generator-side providers:** Anthropic (default for `generation` purpose
on this project) — we exclude it from the judge chain so the judge runs
on a different vendor (Gemini → OpenAI fallback).

Two violation codes — keep the closed set tight (Rule of Three):
  - STEP_FACT_CONTRADICTED — a claim is directly contradicted by KB
    evidence. Must regen / edit; the student will see a wrong fact.
  - STEP_FACT_UNSUPPORTED — a hard claim (number, date, proper noun)
    has no supporting evidence in the KB. Soft signal — surface to
    teacher; doesn't always mean fabrication (KB might be incomplete).

Verdict semantics:
  - `passed=True, violations=[]` — no claims identified OR every claim
    is supported by KB. Step is OK.
  - `passed=False, violations=[STEP_FACT_CONTRADICTED, ...]` — at least
    one contradicted claim. The orchestrator (Q2 regen ensemble, when
    it lands) should regen.
  - `passed=True, violations=[STEP_FACT_UNSUPPORTED]` — soft warning;
    teacher review surface only. Doesn't trigger regen on its own.
    (Promoted to passed=False only when paired with CONTRADICTED.)

Skips when: step text empty / lesson missing / no KB evidence retrievable
/ all providers fail / verdict unparseable. All skips return passed=True
so the generation pipeline never blocks on fact-checker infra.
"""

from __future__ import annotations

import json
import logging
import re
from typing import List, Literal, Optional

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
VIOLATION_CONTRADICTED = "STEP_FACT_CONTRADICTED"
VIOLATION_UNSUPPORTED = "STEP_FACT_UNSUPPORTED"
VIOLATION_CODES = (VIOLATION_CONTRADICTED, VIOLATION_UNSUPPORTED)


# ─── System instruction ────────────────────────────────────────────────
# Direct task statement (Gemini 3 anti-flowery rule). Two-phase task in
# one call: extract claims worth checking, then verify each.
#
# `is_high_stakes` is set BY THE MODEL during extraction — far better
# than the regex's "two capitalised tokens or contains digits"
# heuristic. The model knows that "115 islands" is a checkable factual
# claim and that "the lesson is fun" is not.
_SYSTEM_INSTRUCTION = """\
Fact-check a generated lesson step against the retrieved curriculum \
evidence. The step will be shown to secondary-school students.

Do this in two phases inside ONE response:

Phase 1 — extract every checkable factual claim from input.step_text. \
Treat as a claim: any specific number, date, proper noun (place / \
person / institution name), unit measurement, statistic, or named \
relationship. Skip generic statements ("rivers shape landscapes") \
and pedagogical scaffolding ("let's explore together"). Aim for \
completeness — false negatives at this phase let fabrications \
through.

For each claim, set is_high_stakes:
  true  — numbers, dates, proper nouns, unit measurements, statistics
  false — narrative observations the curriculum likely doesn't address

Phase 2 — for each extracted claim, assign exactly one status using \
input.evidence:
  supported    — evidence clearly states the claim or a matching value
  contradicted — evidence states a different value or the opposite
  unverified   — evidence does not address the claim either way

Be conservative on "supported". Approve only when the evidence \
explicitly contains the claim or a matching number / name. When in \
doubt return "unverified".

In `reasoning`, write 2-4 short sentences. Note how many claims you \
identified, cite the evidence span you relied on for the most \
important verdict, and flag if evidence was sparse for any claim.

If the step contains no checkable claims, return claims=[].
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_CLAIM_STATUS = Literal["supported", "contradicted", "unverified"]


class FactClaim(BaseModel):
    claim: str = Field(
        description="The claim text exactly as it appears in the step.",
        max_length=300,
    )
    status: _CLAIM_STATUS = Field(
        description=(
            "supported = evidence states the claim or a matching value; "
            "contradicted = evidence states a different value or the "
            "opposite; unverified = evidence does not address the claim."
        ),
    )
    is_high_stakes: bool = Field(
        default=True,
        description=(
            "true for numbers / dates / proper nouns / unit measurements "
            "/ statistics; false for narrative observations the "
            "curriculum likely doesn't address."
        ),
    )
    evidence: str = Field(
        default="",
        description="≤80-char quote from the evidence, or empty.",
        max_length=120,
    )


class FactualStepVerdict(BaseModel):
    """Structured output for the factual_step judge."""
    reasoning: str = Field(
        description=(
            "2-4 short sentences noting how many claims were identified, "
            "the evidence span relied on for the most important verdict, "
            "and any sparse-evidence flags."
        ),
        max_length=2000,
    )
    claims: List[FactClaim] = Field(
        default_factory=list,
        description="Every checkable factual claim in the step.",
    )


# ─── KB evidence retrieval ─────────────────────────────────────────────
def _retrieve_evidence_for_step(
    lesson, step_text: str, n_results: int = 6,
) -> str:
    """Pull KB evidence using the whole step text as the query.

    Differs from `apps.tutoring.fact_verifier._retrieve_evidence` —
    that one queries by joined regex-extracted claims (which the
    LLM-extraction approach doesn't have yet). The whole-text query
    is more semantic and lets us retrieve before the LLM call.
    """
    chunks: List[str] = []
    query = (step_text or "").strip()[:600]
    if not query:
        return ""

    # Curriculum KB — GLOBAL is canonical, institution adds on top.
    # Decision (2026-05-15): the global (institution_id=0) KB is the
    # default per-subject KB for all schools. The lesson's institution-
    # specific KB augments it with school-specific content (uploaded
    # syllabi, term plans, etc). This avoids the "no KB evidence"
    # surprise when a lesson sits at a school whose own KB is sparse
    # but the platform-wide subject KB has plenty.
    #
    # Slot allocation: global gets the full n_results; institution
    # gets a smaller boost (~1/3) on top so school-specific content
    # still surfaces when relevant. Dedup by content prefix so chunks
    # that exist in both buckets only count once.
    try:
        from ai_tutor.apps.curriculum.knowledge_base import get_knowledge_base
        institution_id = (
            getattr(lesson.unit.course.institution, "id", None)
            if lesson and lesson.unit and lesson.unit.course
            else None
        )
        # (bucket_id, slots, tag)
        kb_buckets = [(0, n_results, "curriculum")]
        if institution_id and institution_id != 0:
            kb_buckets.append((
                institution_id,
                max(2, n_results // 3),
                f"curriculum:inst{institution_id}",
            ))

        seen_content = set()
        for iid, slots, tag in kb_buckets:
            try:
                kb = get_knowledge_base(iid)
                results = kb.search(query, n_results=slots) or []
            except Exception as inner_exc:
                logger.debug(
                    f"[FactualStepJudge] KB inst={iid} unavailable: {inner_exc}"
                )
                continue
            for r in results:
                content = (r.get("content") or "").strip()
                if not content or content[:120] in seen_content:
                    continue
                seen_content.add(content[:120])
                chunks.append(f"[{tag}] {content[:600]}")
    except Exception as exc:
        logger.warning(f"[FactualStepJudge] KB retrieval failed: {exc}")

    # SeychellesContext entries — small table, keyword overlap.
    try:
        from ai_tutor.apps.curriculum.models import SeychellesContext
        keywords = {
            w.lower() for w in re.findall(r"\w{4,}", step_text or "")
        }
        if keywords:
            # Scope to the lesson's course locale — don't fact-check a
            # pt-mz step against Seychelles facts.
            _loc = 'en-us'
            try:
                _loc = (lesson.unit.course.locale or 'en-us').lower()
            except Exception:
                pass
            candidates = SeychellesContext.for_locale(_loc)
            scored = []
            for ctx in candidates:
                haystack = f"{ctx.title} {ctx.content}".lower()
                score = sum(1 for kw in keywords if kw in haystack)
                if score > 0:
                    scored.append((score, ctx))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            for _, ctx in scored[:3]:
                chunks.append(
                    f"[seychelles_context:{ctx.category}] "
                    f"{ctx.title} — {ctx.content[:400]}"
                )
    except Exception as exc:
        logger.warning(
            f"[FactualStepJudge] SeychellesContext retrieval failed: {exc}"
        )

    return "\n".join(chunks)[:3500]


def _build_user_prompt(
    step_text: str,
    *,
    evidence: str,
    lesson_subject: str,
    lesson_grade: str,
    lesson_title: str,
    lesson_objective: str,
) -> str:
    """Compose the user message — input goes LAST per long-context
    query-last rule."""
    payload = {
        "step_text": (step_text or "")[:2500],
        "evidence": (evidence or "(no evidence retrieved)")[:3500],
        "lesson": {
            "subject": (lesson_subject or "")[:120],
            "grade": (lesson_grade or "")[:80],
            "title": (lesson_title or "")[:200],
            "objective": (lesson_objective or "")[:400],
        },
    }
    return (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Based on input.step_text and input.evidence above, extract "
        "every checkable factual claim from the step text and assign "
        "each a status."
    )


# ─── Public entry point ────────────────────────────────────────────────
def run_factual_step_judge(
    step_text: str,
    *,
    lesson=None,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    exclude_provider: Optional[str] = None,
    judge_purpose: str = "content_judge_factual_step",
    max_tokens: int = 3500,
    _evidence_override: Optional[str] = None,
    force_model_config=None,
) -> JudgeResult:
    """Verify factual claims in a generated lesson step.

    Args:
        step_text: The generated text to check. Typically
            `step.teacher_script` (or `step.teacher_script + "\\n" +
            step.content` joined).
        lesson: Lesson instance for KB scoping. Optional; when None
            the judge skips with "no_lesson_for_kb".
        lesson_*: Lesson context for the judge prompt. Auto-derived
            from `lesson` when not supplied.
        exclude_provider: Provider that produced the step. The judge
            chain skips this provider so the judge can't be the same
            vendor that produced the artefact.
        judge_purpose: ModelConfig purpose to consult first.
        max_tokens: Cap on judge output. 1400 covers reasoning +
            verdict with up to ~10-15 claims comfortably.
        _evidence_override: Test-only — bypass KB retrieval and use
            the supplied evidence string. Used by unit smokes when
            the local KB is empty.

    Returns:
        JudgeResult. On infrastructure failure: passed=True+skipped=True
        with a skip_reason — generation never blocks on judge outage.
    """
    result = JudgeResult()

    # Cheap pre-gates.
    text = (step_text or "").strip()
    if not text:
        result.skipped = True
        result.skip_reason = "empty_step_text"
        return result
    if len(text) < 30:
        result.skipped = True
        result.skip_reason = "step_text_below_min_length"
        return result
    if lesson is None and _evidence_override is None:
        result.skipped = True
        result.skip_reason = "no_lesson_for_kb"
        return result

    # Pre-retrieve KB evidence using the whole step text as the query.
    if _evidence_override is not None:
        evidence = _evidence_override
    else:
        evidence = _retrieve_evidence_for_step(lesson, text)

    if not (evidence or "").strip():
        # No evidence at all — can't verify against the curriculum.
        # Skip rather than ask the LLM to verify against nothing.
        result.skipped = True
        result.skip_reason = "no_kb_evidence"
        return result

    # Auto-derive lesson context from the Lesson instance when caller
    # didn't supply it. Walks lesson → unit → course.
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
            "[FactualStepJudge] no providers available "
            f"(purpose={judge_purpose}, exclude={exclude_provider})"
        )
        result.skipped = True
        result.skip_reason = "no_providers_available"
        return result

    user_prompt = _build_user_prompt(
        text,
        evidence=evidence,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
    )

    # Factual judge → use Gemini search-grounded two-call pattern when
    # available (catches claims contradicted by live web sources, not
    # just by the curriculum KB). Falls through to non-Google providers
    # via single-call structured output. Settings flag toggles the
    # whole grounding path globally.
    if _grounding_enabled():
        call = call_judge_grounded_then_structured(
            user_prompt,
            providers,
            FactualStepVerdict,
            system_prompt=_SYSTEM_INSTRUCTION,
            max_tokens=max_tokens,
        )
    else:
        call = call_judge_structured_with_fallback(
            user_prompt,
            providers,
            FactualStepVerdict,
            system_prompt=_SYSTEM_INSTRUCTION,
            max_tokens=max_tokens,
        )
    if not call.success:
        logger.warning(
            f"[FactualStepJudge] all providers failed: "
            f"{call.error_class}: {call.error_detail}"
        )
        result.skipped = True
        result.skip_reason = f"all_providers_failed: {call.error_class}"
        return result

    result.provider = call.provider
    result.model_name = call.model_name

    verdict: FactualStepVerdict = call.verdict
    result.reasoning = (verdict.reasoning or "").strip()[:300]

    contradicted: List[str] = []
    unsupported_high_stakes: List[str] = []
    total_claims = 0

    for item in (verdict.claims or []):
        claim = (item.claim or "").strip()[:200]
        if not claim:
            continue
        total_claims += 1
        if item.status == "contradicted":
            contradicted.append(claim)
        elif item.status == "unverified" and item.is_high_stakes:
            unsupported_high_stakes.append(claim)

    violations: List[str] = []
    fix_lines: List[str] = []

    if contradicted:
        violations.append(VIOLATION_CONTRADICTED)
        fix_lines.append(
            "Contradicted by curriculum: " + "; ".join(contradicted[:3])
        )
    if unsupported_high_stakes:
        violations.append(VIOLATION_UNSUPPORTED)
        fix_lines.append(
            "Unsupported by curriculum (review or remove): "
            + "; ".join(unsupported_high_stakes[:3])
        )

    # Pass policy:
    #   - CONTRADICTED present → fail (must regen).
    #   - Only UNSUPPORTED → pass with soft warning (teacher review).
    #   - Neither → pass clean.
    result.passed = VIOLATION_CONTRADICTED not in violations
    result.violations = violations
    result.recommended_fix = "\n".join(fix_lines)[:600] if fix_lines else ""

    logger.info(
        f"[FactualStepJudge] {'PASS' if result.passed else 'REJECT'} via "
        f"{call.provider}/{call.model_name} claims={total_claims} "
        f"contradicted={len(contradicted)} unsupported_hs="
        f"{len(unsupported_high_stakes)}"
    )

    return result


__all__ = [
    "VIOLATION_CODES",
    "VIOLATION_CONTRADICTED",
    "VIOLATION_UNSUPPORTED",
    "run_factual_step_judge",
]
