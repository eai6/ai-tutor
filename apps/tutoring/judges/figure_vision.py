"""Figure-vision judge — verifies that an attached figure actually
matches the question being posed about it.

Uses an LLM with vision capability (Claude vision / GPT-4o vision /
Gemini multimodal). Catches the failure mode where the tutor attaches
the wrong figure (or a figure that no longer matches because the
question was authored on the fly) — the student sees a 2-angle
diagram while being asked about 3 angles.

Cost: vision calls are ~5–10× a text call. The orchestrator should
gate this judge so it only runs when:
  - a figure was actually attached this turn (attached_media_count > 0)
  - the response is in a question-bearing context (or the response
    explicitly references the figure: "looking at the diagram…")

When neither precondition is true we skip — no LLM call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from pydantic import BaseModel, Field

from apps.tutoring.judges._instructor_helper import get_instructor_from_client
from apps.tutoring.tracing import traced_judge

logger = logging.getLogger(__name__)


@dataclass
class FigureVisionResult:
    aligned: Optional[bool] = None  # True / False / None (unknown)
    mismatch_reason: str = ""
    figure_summary: str = ""  # what the vision model sees in the figure
    skipped: bool = False
    skip_reason: str = ""


_SYSTEM = (
    "You are a focused figure-question alignment reviewer for a "
    "tutoring response. You receive ONE attached figure and the "
    "tutor's response (which contains a question that references the "
    "figure). Decide whether the figure ACTUALLY matches what the "
    "question describes.\n"
    "\n"
    "Examples of MISALIGNMENT (return aligned=false):\n"
    "  - question describes 3 angles on a straight line, figure shows 2\n"
    "  - question gives angles 30°/62°/w, figure shows different values\n"
    "  - question says 'two intersecting lines', figure shows parallel lines\n"
    "  - question references a specific labelled element (point P, "
    "ray AB, etc.) that is not visible in the figure\n"
    "\n"
    "Examples of ALIGNMENT (return aligned=true):\n"
    "  - question's setup matches what the figure depicts (number of "
    "angles, labels, configuration)\n"
    "  - figure values may differ slightly but the structural setup is "
    "the same and the figure clearly illustrates the concept the "
    "question is about\n"
    "\n"
    "Return aligned=null ONLY when you genuinely cannot tell — e.g. "
    "the figure is too unclear or the question is too generic to "
    "verify. Do not use null as a hedge.\n"
    "\n"
    "Be CONSERVATIVE about flagging misalignment — only flag when a "
    "student would clearly be confused by the mismatch.\n"
    "\n"
    "Return null for `aligned` ONLY when you genuinely cannot tell. "
    "`mismatch_reason` is empty when aligned=true."
)


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
class _FigureVisionVerdict(BaseModel):
    aligned: Optional[bool] = Field(
        default=None,
        description=(
            "True = figure matches the question; False = clear "
            "misalignment a student would notice; null = genuinely "
            "cannot tell (don't use as hedge)."
        ),
    )
    figure_summary: str = Field(
        default="",
        description="≤120-char description of what the figure shows.",
        max_length=200,
    )
    mismatch_reason: str = Field(
        default="",
        description="≤200-char explanation; empty when aligned=true.",
        max_length=300,
    )


def _has_figure_question(text: str) -> bool:
    """Does the response pose a question that depends on the figure?
    Cheap heuristic — looks for figure-reference phrases combined with
    question markers."""
    if not text:
        return False
    has_question = "?" in text or "___" in text
    if not has_question:
        return False
    fig_refs = (
        "diagram", "figure", "image", "shown above", "shown below",
    )
    low = text.lower()
    return any(r in low for r in fig_refs)


from apps.tutoring.judges._prompt_meta import prompt_fingerprint
PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_SYSTEM)


@traced_judge('figure_vision')
def run_figure_vision_judge(
    response_text: str,
    *,
    attached_media: Optional[List[dict]] = None,
    image_reader=None,  # callable(url) → (b64_str, media_type)
    llm_client=None,
) -> FigureVisionResult:
    """Vision-check that the attached figure matches the posed question.

    Args:
      response_text: cleaned tutor response (post media-signal parsing)
      attached_media: list of media dicts the engine attached this turn,
        each with at least {'url': str, 'description': str}
      image_reader: callable that returns (b64_str, media_type) for a
        given URL — pass `tutor._read_image_for_vision`
      llm_client: a vision-capable client (Anthropic Claude vision,
        OpenAI gpt-4o, Google Gemini)

    Skip cases (no LLM call):
      - no attached media
      - response doesn't pose a figure-dependent question
      - llm_client / image_reader missing
      - image fetch returns empty bytes
    """
    result = FigureVisionResult()
    if not attached_media:
        result.skipped = True
        result.skip_reason = "no_attached_media"
        return result
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if not _has_figure_question(response_text):
        result.skipped = True
        result.skip_reason = "no_figure_question"
        return result
    if llm_client is None or image_reader is None:
        result.skipped = True
        result.skip_reason = "no_llm_client_or_reader"
        return result

    # Use the FIRST attached figure — we expect at most 1 per turn.
    media = attached_media[0]
    url = media.get("url") or ""
    if not url:
        result.skipped = True
        result.skip_reason = "media_no_url"
        return result

    try:
        b64, media_type = image_reader(url)
    except Exception as e:
        logger.warning("[FigureVisionJudge] image_reader failed: %s", e)
        result.skipped = True
        result.skip_reason = f"image_read_error: {type(e).__name__}"
        return result

    if not b64:
        result.skipped = True
        result.skip_reason = "image_fetch_empty"
        return result

    # instructor.Image auto-translates the base64 payload to each
    # provider's native image format (Anthropic image blocks / OpenAI
    # image_url / Gemini inline_data). Image first, text after — per
    # the gemini-prompting-expert "image FIRST, text AFTER for
    # single-image tasks" rule.
    from instructor.processing.multimodal import Image

    user_prompt_text = (
        "Verify whether the attached figure aligns with the question "
        "in the tutor response.\n\n"
        f"TUTOR_RESPONSE (contains the question):\n{response_text[:2000]}\n\n"
        f"FIGURE_DESCRIPTION_FROM_CATALOG: "
        f"{(media.get('description') or media.get('alt') or '')[:200]}"
    )

    instructor_client = get_instructor_from_client(llm_client)
    if instructor_client is None:
        result.skipped = True
        result.skip_reason = "instructor_unavailable"
        return result

    # Data-URI form so the MIME is encoded with the payload.
    # instructor.Image.from_base64 accepts data:<mime>;base64,<data>
    # directly, then translates per-provider at call time.
    img = Image.from_base64(f"data:{media_type};base64,{b64}")
    create_kwargs = dict(
        response_model=_FigureVisionVerdict,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": [img, user_prompt_text]},
        ],
        max_retries=1,
    )
    provider = str(getattr(llm_client.config, 'provider', '')).lower()
    if provider == 'google':
        create_kwargs['generation_config'] = {'max_tokens': 3500}
    else:
        create_kwargs['max_tokens'] = 3500

    try:
        verdict = instructor_client.chat.completions.create(**create_kwargs)
    except Exception as e:
        logger.warning("[FigureVisionJudge] vision call failed: %s", e)
        result.skipped = True
        result.skip_reason = f"vision_error: {type(e).__name__}"
        return result

    result.aligned = verdict.aligned
    result.mismatch_reason = (verdict.mismatch_reason or "")[:300]
    result.figure_summary = (verdict.figure_summary or "")[:200]
    return result
