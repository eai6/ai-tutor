"""POST-generation vision judge for generated figures.

Runs after `apps/tutoring/image_service.py::get_or_generate_image()`
saves a new image. The PRE-gen `image_prompt` judge already validates
the prompt before generation; this judge checks the ACTUAL produced
figure against three things at once:

  1. The prompt that was used to generate it (faithfulness check)
  2. The specific step objective the figure is supposed to support
  3. The overall lesson objective the figure must stay within

Catches the failure modes that PRE-gen review can't:
  - Image gen produced something off-topic despite a good prompt
  - Image contains factual errors (wrong geography, fabricated stats)
  - Labels in the image are mis-spelled / point to wrong elements
  - Image is technically OK but won't anchor figure-driven tutoring
    (too cluttered, key concept invisible, ambiguous elements)
  - Visual quality issues (illegible labels, blurry, wrong aspect)

**Hooks at:** `apps/tutoring/image_service.py::get_or_generate_image()`
**Generator-side providers:** OpenAI gpt-image-2 (primary) / Gemini Imagen
**Judge-side providers:** Gemini → Anthropic → OpenAI (cross-provider;
  the provider that generated the image is EXCLUDED). See `_providers.py`
  for the chain construction.

Five stable violation codes — each names a distinct failure mode that
warrants a different remediation:

  - FIGURE_OFF_OBJECTIVE — image doesn't address what the step/lesson
    is teaching. Regen with sharper prompt anchored to the objective.
  - FIGURE_FACTUAL_ERROR — image contains factually wrong content
    (geographic errors, fabricated values, wrong place names, invented
    species, misrepresented historical content). Regen with prompt
    that excludes the wrong fact OR replaces image with a known-good one.
  - FIGURE_LABEL_INACCURATE — labels are mis-spelled, point to the
    wrong element, contradict the figure they label, or use letters/
    numbers inconsistent with the prompt. Regen with explicit label
    spec or strip labels entirely.
  - FIGURE_PEDAGOGICALLY_WEAK — technically correct but won't support
    figure-driven tutoring: too cluttered to reference, key concept
    not visually salient, multiple ambiguous elements with similar
    labels, distractor elements that confuse the lesson. Regen with
    simpler/clearer prompt.
  - FIGURE_VISUAL_QUALITY — illegible labels, blurry rendering,
    broken aspect ratio, low contrast, watermark / artefact.
    Regen at the same provider OR fall back to alternate provider.

Verdict semantics:
  - passed=True, violations=[] → image is good for the lesson, ship it
  - passed=False, violations=[...] → at least one critical violation
    (FACTUAL / LABEL / OFF_OBJECTIVE / VISUAL — anything except
    PEDAGOGICALLY_WEAK alone). Caller should regen.
  - passed=True, violations=[FIGURE_PEDAGOGICALLY_WEAK] → soft signal,
    teacher review surface only. Doesn't auto-trigger regen because
    "pedagogical strength" is judgement-call territory.

Skips when: no image bytes / no objective context / providers fail /
verdict unparseable. All skips return passed=True so the image
pipeline never blocks on judge infrastructure.
"""

from __future__ import annotations

import base64
import logging
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from apps.curriculum.content_judges import JudgeResult
from apps.curriculum.content_judges._providers import (
    _get_instructor_client_for,
    get_judge_provider_chain,
)

logger = logging.getLogger(__name__)


# ─── Stable violation codes ────────────────────────────────────────────
VIOLATION_OFF_OBJECTIVE = "FIGURE_OFF_OBJECTIVE"
VIOLATION_FACTUAL_ERROR = "FIGURE_FACTUAL_ERROR"
VIOLATION_LABEL_INACCURATE = "FIGURE_LABEL_INACCURATE"
VIOLATION_PEDAGOGICALLY_WEAK = "FIGURE_PEDAGOGICALLY_WEAK"
VIOLATION_VISUAL_QUALITY = "FIGURE_VISUAL_QUALITY"
VIOLATION_CODES = (
    VIOLATION_OFF_OBJECTIVE,
    VIOLATION_FACTUAL_ERROR,
    VIOLATION_LABEL_INACCURATE,
    VIOLATION_PEDAGOGICALLY_WEAK,
    VIOLATION_VISUAL_QUALITY,
)

# Codes that, when present alone, still let passed=True (soft signal).
# Anything else (factual / label / off-objective / visual quality) is a
# hard reject because students will see something wrong or unusable.
_SOFT_ONLY_CODES = frozenset({VIOLATION_PEDAGOGICALLY_WEAK})


# ─── System instruction ────────────────────────────────────────────────
# Direct task statement (Gemini 3 anti-flowery rule). Multi-axis review
# in one pass; closed violation enum; reason-in-prose then emit JSON
# wrapper to avoid the constrained-decoding accuracy hit on the
# verdict body (Tam et al. 2025).
#
# Pedagogical-strength axis is explicit because the user direction was
# "ensure figure supports figure-driven tutoring" — the tutor needs to
# be able to reference labelled elements ("look at point X"), so an
# image that's technically correct but doesn't surface its concept
# clearly is still a problem.
_SYSTEM_INSTRUCTION = """\
Review one generated figure for use in a secondary-school lesson. The \
figure will be shown to students while a tutor scaffolds them through \
the lesson — the tutor needs to be able to direct the student's eye \
to specific labelled elements ("look at the river labelled X").

Approve a figure when ALL of these hold:
  - It depicts what the step objective and lesson objective need.
  - Every label, value, place name, date, or stated fact in the \
figure is correct.
  - Labels are placed on the right elements and read cleanly.
  - One or two key concepts stand out visually so the tutor has \
something concrete to anchor on.
  - Image is legible: text readable, contrast adequate, no broken \
rendering or watermarks.

Reject otherwise. Use ONLY these codes:

  FIGURE_OFF_OBJECTIVE
    The figure does not address the step or lesson objective. The \
subject is wrong, or the figure is generic where a specific concept \
was needed.
    Example: lesson is "rivers and erosion in Seychelles", figure is \
a beach photo with no river visible.

  FIGURE_FACTUAL_ERROR
    The figure contains factually wrong content. Wrong place names, \
fabricated statistics, invented geography, mislabeled scientific \
content, dates that don't match the period.
    Example: a map of Seychelles with islands labelled with \
Tanzanian place names; a chart showing "rainfall in Victoria" with \
fabricated numbers.

  FIGURE_LABEL_INACCURATE
    Labels point to the wrong element, are mis-spelled, contradict \
what they label, or use letters/numbers inconsistent with the \
prompt.
    Example: arrow labelled "evaporation" points at a river, not \
rising water vapour.

  FIGURE_PEDAGOGICALLY_WEAK
    Technically correct but won't anchor figure-driven tutoring. \
Too cluttered to reference, key concept not visually salient, \
multiple similar-looking labels that confuse, distractor elements \
that pull attention from the learning concept.
    Example: a water-cycle diagram with so many tiny arrows that \
the student can't see which one is "precipitation".

  FIGURE_VISUAL_QUALITY
    Illegible labels, blurry rendering, broken aspect ratio, very \
low contrast, watermark or generation artefact.
    Example: text is so blurred you can't tell "Victoria" from \
"Vibtona".

When rejecting, write a `recommended_fix` (≤120 words) that gives \
the regen layer a concrete, self-contained instruction — name the \
element to add/remove/relabel. No "make it better" comments.

In `reasoning`, write 3-5 short sentences walking through what the \
figure depicts, whether each rejection code applies, and citing \
specific elements (labels, positions, missing concepts). Reference \
the step + lesson objective explicitly when judging on-topic-ness.

`figure_summary` is a ≤120-char description of what the figure shows.
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_ALLOWED_VIOLATIONS = Literal[
    "FIGURE_OFF_OBJECTIVE",
    "FIGURE_FACTUAL_ERROR",
    "FIGURE_LABEL_INACCURATE",
    "FIGURE_PEDAGOGICALLY_WEAK",
    "FIGURE_VISUAL_QUALITY",
]


class FigureAlignmentVerdict(BaseModel):
    """Structured output for the figure_alignment vision judge."""
    reasoning: str = Field(
        description=(
            "3-5 short sentences walking through what the figure "
            "depicts, whether each rejection code applies, citing "
            "specific elements (labels, positions). Reference the "
            "step + lesson objective when judging on-topic-ness."
        ),
        max_length=2000,
    )
    passed: bool = Field(
        description=(
            "True iff the figure is approved for use. False when any "
            "hard violation present (anything except PEDAGOGICALLY_WEAK)."
        ),
    )
    violations: List[_ALLOWED_VIOLATIONS] = Field(
        default_factory=list,
        description="Zero or more violation codes from the enum.",
    )
    recommended_fix: str = Field(
        default="",
        description=(
            "When rejecting: ≤120-word concrete instruction. Empty "
            "when passing clean."
        ),
        max_length=800,
    )
    figure_summary: str = Field(
        default="",
        description="≤120-char description of what the figure shows.",
        max_length=200,
    )


def _dedupe_violations(codes: List[str]) -> List[str]:
    out: List[str] = []
    for code in codes:
        s = str(code or "").strip().upper()
        if s in VIOLATION_CODES and s not in out:
            out.append(s)
    return out


# ─── User prompt assembly ──────────────────────────────────────────────
# Multimodal ordering: image FIRST (Gemini docs single-image rule),
# context blocks AFTER, instruction at the very end (long-context
# query-last). Uses instructor.processing.multimodal.Image which
# auto-translates to each provider's native image format.
def _build_user_content(
    image_b64: str,
    image_media_type: str,
    *,
    image_prompt: str,
    lesson_subject: str,
    lesson_grade: str,
    lesson_title: str,
    lesson_objective: str,
    step_objective: str,
    step_concept_tag: str,
) -> list:
    """Build the user message content list — Image first, then text.

    Returns a list with an `instructor.Image` wrapper followed by the
    lesson context + instruction strings. Instructor translates the
    Image into each provider's native shape at call time.
    """
    from instructor.processing.multimodal import Image

    context_text = (
        "<lesson>\n"
        f"  <subject>{(lesson_subject or '(unspecified)').strip()[:120]}</subject>\n"
        f"  <grade>{(lesson_grade or '(unspecified)').strip()[:80]}</grade>\n"
        f"  <title>{(lesson_title or '(unspecified)').strip()[:200]}</title>\n"
        f"  <overall_objective>{(lesson_objective or '(none)').strip()[:400]}</overall_objective>\n"
        "</lesson>\n"
        "<step>\n"
        f"  <concept>{(step_concept_tag or '(unspecified)').strip()[:200]}</concept>\n"
        f"  <objective>{(step_objective or '(none)').strip()[:400]}</objective>\n"
        "</step>\n"
        "<image_prompt>\n"
        f"{(image_prompt or '(prompt unavailable)').strip()[:1200]}\n"
        "</image_prompt>"
    )

    instruction = (
        "Based on the figure above and the lesson + step + prompt "
        "context, decide whether this figure is approved for use. "
        "Apply each rejection code definition strictly. Cite specific "
        "elements you see in the figure when justifying."
    )

    # Build a data-URI so the MIME is encoded with the payload —
    # instructor's `from_base64` accepts that shape directly, and
    # `from_raw_base64` takes only the bare data string (no
    # media_type arg). Either path translates per-provider at call
    # time via instructor.Image.to_anthropic / to_openai / to_genai.
    mime = (image_media_type or "image/png").strip()
    img = Image.from_base64(f"data:{mime};base64,{image_b64}")
    return [img, context_text, instruction]


def _bytes_to_b64(image_bytes: bytes) -> str:
    return base64.standard_b64encode(image_bytes).decode("ascii")


# ─── Provider chain helper for vision ──────────────────────────────────
# Standard chain helper picks any provider for the judge purpose.
# Vision narrows that — not every config is on a vision-capable model.
# We let `call_judge_with_fallback` handle errors (a non-vision client
# will fail at the API layer; the chain falls through to the next
# provider). In practice the judge_purpose default routes to Gemini
# (vision-native) → Anthropic (vision via image blocks) → OpenAI
# (vision via image_url) — all three are vision-capable.


# ─── Public entry point ────────────────────────────────────────────────
def run_figure_alignment_judge(
    image_bytes: Optional[bytes] = None,
    image_media_type: str = "image/png",
    *,
    image_prompt: str = "",
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_objective: str = "",
    step_concept_tag: str = "",
    exclude_provider: Optional[str] = None,
    judge_purpose: str = "content_judge_figure_alignment",
    max_tokens: int = 3500,
    image_b64: Optional[str] = None,
) -> JudgeResult:
    """Vision-check a generated figure against the lesson context.

    Args:
        image_bytes: Raw image bytes. Either this OR image_b64 must be
            supplied. Use image_bytes when calling fresh from the
            generator; use image_b64 when re-judging a stored asset.
        image_media_type: 'image/png' / 'image/jpeg' / 'image/webp'.
        image_prompt: The prompt that was sent to the image gen model.
            Used so the judge can verify the figure is FAITHFUL to the
            ask (not just on-topic for the lesson).
        lesson_subject / lesson_grade / lesson_title / lesson_objective:
            Lesson-level context. lesson_objective is the BROAD goal.
        step_objective: The SPECIFIC step objective this figure supports.
            More important than lesson_objective for the on-topic check —
            the figure must serve THIS step, not just the broader lesson.
        step_concept_tag: Optional concept anchor (e.g. "rivers_erosion").
        exclude_provider: Provider that GENERATED the image (e.g.
            'openai' for gpt-image-2). Judge chain skips this provider
            so the judge can't be the same vendor that produced the
            artefact (cross-provider review).
        judge_purpose: ModelConfig purpose to consult first.
        max_tokens: Cap on judge output. 1200 covers reasoning + verdict
            + recommended_fix comfortably.
        image_b64: Pre-encoded base64 string. Use INSTEAD of image_bytes
            when calling from a place that already has b64 (e.g. when
            re-judging a stored asset that was never raw bytes).

    Returns:
        JudgeResult. On infrastructure failure (no providers, all
        providers errored, malformed output, missing image): passed=True
        + skipped=True with skip_reason — image pipeline never blocks.
    """
    result = JudgeResult()

    # Pre-gates
    if image_bytes is None and not image_b64:
        result.skipped = True
        result.skip_reason = "no_image"
        return result
    if not (step_objective or lesson_objective):
        # Without ANY objective context the judge can't do the
        # on-topic check, which is half its job. Skip rather than
        # render a half-blind verdict.
        result.skipped = True
        result.skip_reason = "no_objective_context"
        return result

    if image_b64 is None:
        try:
            image_b64 = _bytes_to_b64(image_bytes)
        except Exception as exc:
            result.skipped = True
            result.skip_reason = f"b64_encode_failed: {type(exc).__name__}"
            return result

    providers = get_judge_provider_chain(
        judge_purpose, exclude_provider=exclude_provider,
    )
    if not providers:
        logger.warning(
            "[FigureAlignmentJudge] no providers available "
            f"(purpose={judge_purpose}, exclude={exclude_provider})"
        )
        result.skipped = True
        result.skip_reason = "no_providers_available"
        return result

    user_content = _build_user_content(
        image_b64,
        image_media_type,
        image_prompt=image_prompt,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        step_objective=step_objective,
        step_concept_tag=step_concept_tag,
    )

    # Per-provider loop: wrap each provider's underlying client in
    # instructor and try the structured-output vision call. First
    # success wins. Instructor's Image wrapper auto-translates to each
    # provider's native image format (Anthropic / OpenAI / Gemini).
    last_provider_name = ""
    last_model = ""
    last_error = ""
    last_error_class = ""
    verdict: Optional[FigureAlignmentVerdict] = None

    for provider in providers:
        iclient = _get_instructor_client_for(provider.config)
        if iclient is None:
            last_error = "instructor_init_failed"
            last_error_class = "InstructorUnavailable"
            continue

        create_kwargs = dict(
            response_model=FigureAlignmentVerdict,
            messages=[
                {"role": "system", "content": _SYSTEM_INSTRUCTION},
                {"role": "user", "content": user_content},
            ],
            max_retries=1,
        )
        if str(provider.config.provider).lower() == 'google':
            create_kwargs['generation_config'] = {'max_tokens': max_tokens}
        else:
            create_kwargs['max_tokens'] = max_tokens

        try:
            verdict = iclient.chat.completions.create(**create_kwargs)
        except Exception as exc:
            last_error = str(exc)[:200]
            last_error_class = type(exc).__name__
            logger.warning(
                f"[FigureAlignmentJudge] {provider.name}/{provider.model_name} "
                f"vision call failed: {last_error_class}: {last_error} — "
                f"trying next"
            )
            continue
        last_provider_name = provider.name
        last_model = provider.model_name
        break

    if verdict is None:
        result.skipped = True
        result.skip_reason = (
            f"all_providers_failed: {last_error_class or 'unknown'}"
        )
        return result

    result.provider = last_provider_name
    result.model_name = last_model
    result.reasoning = (verdict.reasoning or "").strip()[:300]

    violations = _dedupe_violations(list(verdict.violations or []))
    fix = (verdict.recommended_fix or "").strip()[:800]

    # Pass policy: violations that are NOT in _SOFT_ONLY_CODES force
    # passed=False. PEDAGOGICALLY_WEAK alone is a soft warning.
    hard_violations = [v for v in violations if v not in _SOFT_ONLY_CODES]
    if hard_violations:
        passed = False
    else:
        passed = bool(verdict.passed) or not violations

    result.passed = passed
    result.violations = violations
    result.recommended_fix = fix if not passed or violations else ""

    logger.info(
        f"[FigureAlignmentJudge] {'PASS' if result.passed else 'REJECT'} via "
        f"{last_provider_name}/{last_model} violations={violations}"
    )

    return result


__all__ = [
    "VIOLATION_CODES",
    "VIOLATION_OFF_OBJECTIVE",
    "VIOLATION_FACTUAL_ERROR",
    "VIOLATION_LABEL_INACCURATE",
    "VIOLATION_PEDAGOGICALLY_WEAK",
    "VIOLATION_VISUAL_QUALITY",
    "run_figure_alignment_judge",
]
