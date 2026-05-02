"""Vision-LLM extractor for `MediaAsset.figure_facts` (F2 of
memory/figure_facts_plan.md).

Sends a figure to a vision-capable Anthropic model and asks for
structured `figure_facts` output. Used by:

  - the backfill management command (one-time, on existing figures)
  - the content-generation pipeline (per-figure, after generation)

Anthropic-only for v1. Other providers (OpenAI, Gemini) get added
when F7 generalises BaseLLMClient with a `supports_vision` flag.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Optional, Tuple, Union

from apps.curriculum.figure_facts_schema import (
    FigureFacts,
    validate_figure_facts,
)

logger = logging.getLogger(__name__)


_EXTRACTOR_SYSTEM = (
    "You extract structured visual facts from figures shown to "
    "students in a tutoring system. The facts let the AI tutor "
    "anchor its scaffolding in REAL labelled features instead of "
    "asking students to imagine geometry. Be precise — every claim "
    "you emit will be cited as ground truth by the tutor."
)


_EXTRACTOR_USER_PROMPT = """\
Extract the structured facts of this figure into JSON matching the schema below.

REQUIRED FIELDS:
  - "type": short category tag (e.g. "parallel_lines_with_transversal",
    "bar_chart", "map_of_seychelles", "unstructured" if no clear structure).
  - "scene_description": 1-3 sentences describing what the student sees.
    Concrete enough that a tutor could read it verbatim into prose
    (e.g. "Two horizontal parallel lines, l (top) and m (bottom), are
    cut by a diagonal transversal t."). DO NOT just list what's there
    — describe it.
  - "labelled_features": list of every labelled point/line/region with
    `label` (text), `location` (where it sits — be specific so the tutor
    can direct the student's eye), and optional `color`.

OPTIONAL FIELDS (populate when applicable):
  - "angle_relationships": for geometry figures with labelled angles,
    the verified equalities and sums. Each entry has `pair: [int, int]`,
    `relationship: corresponding|alternate_interior|alternate_exterior|
    co_interior|vertically_opposite|supplementary|complementary`, and
    either `equal: true` OR `sum: int`. If the figure has a "key" or
    legend panel that lists relationships, COPY THOSE VERBATIM — they
    are authoritative.
  - "extra_facts": list of strings — any facts the figure asserts that
    don't fit `angle_relationships` (axis labels, panel callouts,
    captions, geographic features, etc.).
  - "anchor_prompts": list of 2-4 short questions a tutor could use
    VERBATIM to direct the student's attention to specific features
    (e.g. "Look at angle 1 — what colour is it?" / "Find the angle
    on the bottom-right of the lower intersection").

STRICT RULES:
  - Return ONLY a JSON object matching the schema. No prose, no
    markdown fences. Just the JSON.
  - When a "key" / legend / rule panel is visible, its facts override
    your visual interpretation.
  - For unstructured figures (photos without overlays, decorative
    images), set type="unstructured" and put what you can describe in
    `scene_description` + `extra_facts`. Empty `labelled_features` is
    OK in this case.
  - Never fabricate relationships you can't see in the figure.
"""


def _read_image_bytes(image: Union[str, Path, bytes]) -> Tuple[bytes, str]:
    """Normalise image input to (bytes, media_type).

    Accepts a path-like (file path) or raw bytes. Sniffs the media
    type from the file extension or magic bytes — defaults to
    image/png when uncertain (Anthropic accepts png/jpeg/gif/webp).
    """
    if isinstance(image, (str, Path)):
        p = Path(image)
        data = p.read_bytes()
        ext = p.suffix.lower()
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(ext, "image/png")
        return data, media_type
    data = bytes(image)
    # Magic-byte sniffing for raw bytes input
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return data, "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return data, "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, "image/webp"
    return data, "image/png"


def _strip_code_fences(text: str) -> str:
    """Drop ```json ... ``` fences a model may add around JSON output."""
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)


def extract_figure_facts(
    image: Union[str, Path, bytes],
    *,
    llm_client=None,
    extra_context: str = "",
) -> Tuple[Optional[FigureFacts], Optional[str]]:
    """Extract `figure_facts` from a single image via vision LLM.

    Args:
      image: file path, Path, or raw bytes.
      llm_client: an AnthropicClient instance (the only vision-capable
        provider in v1). When None, defaults to ModelConfig.get_for(
        'generation') if Anthropic, else fails with a useful error.
      extra_context: optional extra text appended to the user prompt —
        e.g., the lesson title and step concept_tag for disambiguation.

    Returns (FigureFacts, None) on success, (None, error_message) on
    failure. Never raises; designed for batch backfill where one bad
    figure shouldn't crash the whole run.
    """
    # Resolve a vision-capable client
    if llm_client is None:
        try:
            from apps.llm.client import AnthropicClient
            from apps.llm.models import ModelConfig
        except Exception as e:
            return None, f"could not import LLM client: {e}"
        config = ModelConfig.get_for("generation")
        if not config or config.provider != "anthropic":
            return (
                None,
                "no Anthropic ModelConfig found for purpose=generation; "
                "configure one or pass llm_client= explicitly",
            )
        try:
            llm_client = AnthropicClient(config)
        except Exception as e:
            return None, f"failed to construct AnthropicClient: {e}"

    # Read + base64-encode the image
    try:
        data, media_type = _read_image_bytes(image)
    except Exception as e:
        return None, f"failed to read image: {e}"
    b64 = base64.standard_b64encode(data).decode("ascii")

    # Build the Anthropic content-blocks message. The text block carries
    # the prompt + (optional) lesson context.
    user_prompt = _EXTRACTOR_USER_PROMPT
    if extra_context:
        user_prompt = (
            user_prompt + "\n\nLESSON CONTEXT (for disambiguation):\n" + extra_context.strip()
        )
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {"type": "text", "text": user_prompt},
            ],
        }
    ]

    # Generate
    try:
        response = llm_client.generate(
            messages=messages,
            system_prompt=_EXTRACTOR_SYSTEM,
            max_tokens=2000,
        )
    except Exception as e:
        return None, f"LLM call failed: {e}"

    raw = (response.content or "").strip()
    cleaned = _strip_code_fences(raw)
    try:
        data_dict = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(f"[FigureFacts] JSON decode failed: {e}; raw[:200]={cleaned[:200]!r}")
        return None, f"model returned non-JSON: {e}"

    facts, err = validate_figure_facts(data_dict)
    if err:
        logger.warning(f"[FigureFacts] schema validation failed: {err}")
        return None, f"schema validation failed: {err}"

    return facts, None


def extract_and_save_for_asset(
    asset,
    *,
    force: bool = False,
    generation_prompt: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Extract `figure_facts` for a saved MediaAsset and persist it.

    Best-effort. Used by every code path that creates a new image
    (auto-generated images, teacher uploads, content regen) so that
    every figure entering the system arrives with facts attached.

    Args:
      generation_prompt: optional original LLM prompt used to create
        the image. When provided, stored alongside the extracted
        facts so the runtime tutor sees both what's IN the figure
        and what it was MEANT to depict. See P1/item-3 of
        memory/curriculum_tutor_v2_plan.md.

    Skips when:
      - asset is not an image
      - asset has no file
      - asset.figure_facts is already non-null and force=False

    Returns (saved_facts, error_message). Never raises.
    """
    if asset is None or asset.asset_type != "image":
        return False, "asset_not_image"
    if not asset.file or not asset.file.name:
        return False, "asset_has_no_file"
    if asset.figure_facts and not force:
        return False, "already_has_facts"

    # Read bytes from the asset's file. Local storage exposes .path;
    # remote storages need .read() instead.
    try:
        try:
            image_arg = asset.file.path  # local FS
        except (NotImplementedError, ValueError):
            asset.file.open("rb")
            try:
                image_arg = asset.file.read()
            finally:
                try:
                    asset.file.close()
                except Exception:
                    pass
    except Exception as e:
        return False, f"could not read asset file: {e}"

    facts, err = extract_figure_facts(image_arg)
    if err is not None or facts is None:
        return False, err or "unknown_extractor_error"

    if generation_prompt:
        # Stash the original prompt so the tutor sees the intent
        # alongside what the extractor found IN the image.
        facts.generation_prompt = generation_prompt[:2000]

    asset.figure_facts = facts.model_dump(mode="json")
    asset.save(update_fields=["figure_facts", "updated_at"])
    logger.info(
        f"[FigureFacts] saved facts for asset #{asset.id} "
        f"(type={facts.type}, features={len(facts.labelled_features)}"
        f"{', prompt-attached' if generation_prompt else ''})"
    )
    return True, None
