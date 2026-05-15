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
import json
import logging
import re
from typing import Any, Dict, List, Optional

from apps.curriculum.content_judges import JudgeResult
from apps.curriculum.content_judges._providers import (
    call_judge_with_fallback,
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

Output exactly two XML blocks in this order, with no extra prose:

<reasoning>
3-5 short sentences. Walk through what the figure depicts, whether \
each rejection code applies, and cite specific elements (labels, \
positions, missing concepts). Reference the step + lesson \
objective explicitly when judging on-topic-ness.
</reasoning>
<verdict>
{"passed": true|false, "violations": ["CODE", ...], \
"recommended_fix": "rewritten instruction or empty string", \
"figure_summary": "<<= 120 char description of what the figure shows"}
</verdict>
"""


# ─── Output parsing ────────────────────────────────────────────────────
_REASONING_RE = re.compile(
    r"<reasoning>(.*?)</reasoning>", re.DOTALL | re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"<verdict>(.*?)</verdict>", re.DOTALL | re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_verdict(raw: str) -> Optional[Dict[str, Any]]:
    """Extract the verdict JSON. Returns None on parse failure."""
    if not raw:
        return None
    m = _VERDICT_RE.search(raw)
    if not m:
        m2 = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m2:
            return None
        candidate = m2.group(0)
    else:
        candidate = m.group(1).strip()
    candidate = _FENCE_RE.sub("", candidate).strip()
    try:
        data = json.loads(candidate)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _normalise_violations(raw_codes: Any) -> List[str]:
    if not isinstance(raw_codes, list):
        return []
    out: List[str] = []
    for code in raw_codes:
        s = str(code or "").strip().upper()
        if s in VIOLATION_CODES and s not in out:
            out.append(s)
    return out


# ─── User prompt assembly ──────────────────────────────────────────────
# Multimodal ordering: image FIRST (Gemini docs single-image rule),
# context blocks AFTER, instruction at the very end (long-context
# query-last). Anthropic-style content blocks; the BaseLLMClient
# adapters translate to Gemini's inline_data and OpenAI's image_url.
def _build_user_blocks(
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
) -> List[Dict[str, Any]]:
    """Build the user message content blocks.

    Order: image → context (lesson + step + prompt) → instruction.
    """
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
        "elements you see in the figure when justifying a verdict. "
        "Reply with ONE <reasoning> block then ONE <verdict> JSON "
        "block — no other prose."
    )

    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type or "image/png",
                "data": image_b64,
            },
        },
        {"type": "text", "text": context_text},
        {"type": "text", "text": instruction},
    ]


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
    max_tokens: int = 1200,
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

    user_blocks = _build_user_blocks(
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

    # call_judge_with_fallback expects a string user prompt; for
    # multimodal we need to pass the content blocks directly. Inline
    # the per-provider loop here so we can pass the structured
    # content array.
    last_provider_name = ""
    last_model = ""
    last_error = ""
    raw_text = ""
    success = False

    for provider in providers:
        try:
            response = provider.client.generate(
                messages=[{"role": "user", "content": user_blocks}],
                system_prompt=_SYSTEM_INSTRUCTION,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning(
                f"[FigureAlignmentJudge] {provider.name}/{provider.model_name} "
                f"vision call failed: {last_error} — trying next"
            )
            continue
        raw_text = (getattr(response, "content", None) or "").strip()
        if not raw_text:
            last_error = "empty_response"
            continue
        last_provider_name = provider.name
        last_model = provider.model_name
        success = True
        break

    if not success:
        result.skipped = True
        result.skip_reason = (
            f"all_providers_failed: {last_error or 'unknown'}"
        )
        return result

    result.provider = last_provider_name
    result.model_name = last_model

    verdict = _parse_verdict(raw_text)
    if verdict is None:
        logger.warning(
            f"[FigureAlignmentJudge] unparseable verdict from "
            f"{last_provider_name}/{last_model}: {raw_text[:200]!r}"
        )
        result.skipped = True
        result.skip_reason = "verdict_unparseable"
        return result

    reasoning_match = _REASONING_RE.search(raw_text)
    if reasoning_match:
        result.reasoning = reasoning_match.group(1).strip()[:300]

    raw_passed = bool(verdict.get("passed", True))
    violations = _normalise_violations(verdict.get("violations"))
    fix = str(verdict.get("recommended_fix") or "").strip()[:800]

    # Pass policy: violations that are NOT in _SOFT_ONLY_CODES force
    # passed=False. PEDAGOGICALLY_WEAK alone is a soft warning.
    hard_violations = [v for v in violations if v not in _SOFT_ONLY_CODES]
    if hard_violations:
        passed = False
    else:
        # Either no violations OR only PEDAGOGICALLY_WEAK — respect
        # the model's passed value when it's already False (model may
        # see another reason); otherwise pass.
        passed = raw_passed or not violations

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
