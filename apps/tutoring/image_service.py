"""Image Generation Service — OpenAI gpt-image-2 (primary) + Gemini (fallback).

Provider routing:

  - ModelConfig(purpose='image_generation') determines the configured
    primary. provider='openai' → gpt-image-2; provider='google' →
    gemini-3.1-flash-image-preview. The OTHER provider is used as a
    cross-provider fallback if the primary 503s or returns no image.
  - Callers can force a specific provider per-call via
    `get_or_generate_image(..., model_override='openai'|'gemini')`.
    The teacher's per-image Regenerate UI uses this so they can flip
    providers when one model produces a bad result.

Set DISABLE_IMAGE_GEN=1 in .env to disable both providers.
"""

import os
import logging
import hashlib
import mimetypes
import base64
from typing import Optional, Dict
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

# OpenAI
DEFAULT_OPENAI_MODEL = 'gpt-image-2'

# Gemini
DEFAULT_GEMINI_MODEL = 'gemini-3.1-flash-image-preview'
GEMINI_FALLBACK_MODEL = 'gemini-3-pro-image-preview'

FACTUAL_CATEGORIES = {'diagram', 'map', 'chart', 'infographic', 'flowchart'}


class ImageGenerationService:
    """
    Generate images via OpenAI gpt-image-2 or Gemini.

    Usage:
        service = ImageGenerationService(lesson)
        result = service.get_or_generate_image(
            prompt="Diagram showing relief rainfall on a mountain",
            category="diagram",
            model_override="openai",  # optional; force a provider
        )
        # Returns: {'url': '/media/...', 'title': '...', 'generated': True/False}
    """

    def __init__(self, lesson=None, institution=None):
        self.lesson = lesson
        self.institution = institution
        self._model_config = None
        self._load_model_config()
        self.available = self._check_available()
        # Last error message — surfaced to the teacher Regenerate UI
        # when get_or_generate_image returns None so they know WHY.
        self.last_error: Optional[str] = None

    def _load_model_config(self):
        """Load image generation config from ModelConfig if available."""
        try:
            from apps.llm.models import ModelConfig
            self._model_config = ModelConfig.objects.filter(
                is_active=True, purpose='image_generation'
            ).first()
        except Exception:
            pass

    def _configured_provider(self) -> str:
        """Return 'openai' or 'gemini' from ModelConfig, defaulting to openai
        when both keys are available, gemini otherwise."""
        if self._model_config:
            provider = (self._model_config.provider or '').lower()
            if provider == 'openai':
                return 'openai'
            if provider in ('google', 'gemini'):
                return 'gemini'
        # No ModelConfig → prefer OpenAI when both keys are present.
        if self._get_openai_key():
            return 'openai'
        return 'gemini'

    def _get_openai_key(self) -> Optional[str]:
        """OpenAI API key from ModelConfig (when provider matches) or env."""
        if self._model_config and (self._model_config.provider or '').lower() == 'openai':
            key = self._model_config.get_api_key()
            if key:
                return key
        return os.environ.get('OPENAI_API_KEY')

    def _get_gemini_key(self) -> Optional[str]:
        """Gemini API key from ModelConfig (when provider matches) or env."""
        if self._model_config and (self._model_config.provider or '').lower() in ('google', 'gemini'):
            key = self._model_config.get_api_key()
            if key:
                return key
        return os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY')

    def _openai_model(self) -> str:
        """OpenAI model name from ModelConfig (if provider matches) or default."""
        if self._model_config and (self._model_config.provider or '').lower() == 'openai':
            return self._model_config.model_name or DEFAULT_OPENAI_MODEL
        return DEFAULT_OPENAI_MODEL

    def _gemini_model(self) -> str:
        """Gemini primary model from ModelConfig (if provider matches) or default."""
        if self._model_config and (self._model_config.provider or '').lower() in ('google', 'gemini'):
            return self._model_config.model_name or DEFAULT_GEMINI_MODEL
        return DEFAULT_GEMINI_MODEL

    def _check_available(self) -> bool:
        """Available if disabled flag isn't set AND at least one provider has a key."""
        if os.environ.get('DISABLE_IMAGE_GEN'):
            return False
        if not (self._get_openai_key() or self._get_gemini_key()):
            logger.info("Image generation not available: No API key configured")
            return False
        return True

    def get_or_generate_image(
        self,
        prompt: str,
        category: str = "general",
        textbook_context: str = "",
        include_bytes: bool = False,
        model_override: Optional[str] = None,
        **kwargs
    ) -> Optional[Dict]:
        """
        Generate a new image.

        Args:
            prompt: Description of the image needed
            category: Type of image (diagram, photo, illustration, etc.)
            textbook_context: Optional description of the textbook figure style
            include_bytes: If True, include '_raw_bytes' key with raw image data
            model_override: 'openai' or 'gemini' — force a specific provider
                            and skip cross-provider fallback. Used by the
                            teacher Regenerate UI.

        Returns:
            Dict with 'url', 'title', 'caption', 'generated', 'model' keys,
            or None on failure.
        """
        self.last_error = None  # reset between calls
        if not self.available:
            logger.warning(f"Image generation unavailable for: {prompt[:50]}...")
            self.last_error = "Image generation is disabled or no API keys configured."
            return None

        override = (model_override or '').lower().strip() or None

        if override == 'openai':
            return self._generate_with_openai(prompt, category, textbook_context, include_bytes)
        if override in ('gemini', 'google'):
            return self._generate_with_gemini(prompt, category, textbook_context, include_bytes)

        # No override — primary by config, with cross-provider fallback.
        primary = self._configured_provider()
        order = (
            (self._generate_with_openai, self._generate_with_gemini)
            if primary == 'openai'
            else (self._generate_with_gemini, self._generate_with_openai)
        )
        for fn in order:
            result = fn(prompt, category, textbook_context, include_bytes)
            if result:
                return result

        logger.warning("Both providers failed to generate image")
        return None

    # ─── OpenAI gpt-image-2 ─────────────────────────────────────────

    def _generate_with_openai(
        self, prompt: str, category: str, textbook_context: str = "",
        include_bytes: bool = False,
    ) -> Optional[Dict]:
        """Generate image via OpenAI Images API (gpt-image-2)."""
        api_key = self._get_openai_key()
        if not api_key:
            return None

        try:
            from openai import OpenAI
        except Exception as e:
            logger.warning(f"openai SDK not installed: {e}")
            return None

        enhanced_prompt = self._enhance_prompt(prompt, category, textbook_context)
        model = self._openai_model()
        # Use landscape for factual/spatial categories, square for the rest.
        size = "1536x1024" if category in FACTUAL_CATEGORIES else "1024x1024"

        try:
            logger.info(f"Generating image with {model}: {prompt[:80]}...")
            client = OpenAI(api_key=api_key)
            response = client.images.generate(
                model=model,
                prompt=enhanced_prompt,
                size=size,
                n=1,
            )  # noqa: B009
            data = response.data[0] if response.data else None
            if not data:
                logger.warning(f"{model}: empty response.data")
                return None

            image_bytes = None
            if getattr(data, 'b64_json', None):
                image_bytes = base64.b64decode(data.b64_json)
            elif getattr(data, 'url', None):
                # Fall back to fetching the URL if SDK returned a URL instead.
                import urllib.request
                with urllib.request.urlopen(data.url, timeout=30) as r:
                    image_bytes = r.read()

            if not image_bytes:
                logger.warning(f"{model}: no image bytes in response")
                return None

            saved_url = self._save_generated_image_bytes(image_bytes, prompt, '.png')
            if not saved_url:
                return None

            logger.info(f"Image generated successfully with {model}")
            result = {
                'url': saved_url,
                'title': prompt[:100],
                'caption': f"AI-generated: {prompt[:200]}",
                'alt_text': prompt,
                'generated': True,
                'model': model,
                'provider': 'openai',
            }
            if include_bytes:
                result['_raw_bytes'] = image_bytes
            return result

        except Exception as e:
            err = str(e)
            logger.error(f"{model} failed: {err[:300]}")
            # Detect the verification gate so the UI can show a clear
            # action ("verify your OpenAI org") instead of a generic
            # "no result" message.
            if 'verified' in err.lower() or 'must be verified' in err.lower():
                self.last_error = (
                    "OpenAI organization is not verified for gpt-image-2. "
                    "Verify at platform.openai.com/settings/organization/general, "
                    "wait ~15 min for propagation, then retry."
                )
            elif '403' in err or 'forbidden' in err.lower():
                self.last_error = f"OpenAI {model}: 403 Forbidden — check API key permissions."
            else:
                self.last_error = f"OpenAI {model} error: {err[:200]}"
            return None

    # ─── Gemini ─────────────────────────────────────────────────────

    def _generate_with_gemini(
        self, prompt: str, category: str, textbook_context: str = "",
        include_bytes: bool = False,
    ) -> Optional[Dict]:
        """Generate image — tries configured Gemini model, falls back to GEMINI_FALLBACK_MODEL on 503."""
        api_key = self._get_gemini_key()
        if not api_key:
            return None

        try:
            from google import genai
            from google.genai import types
        except Exception as e:
            logger.warning(f"google-genai SDK not installed: {e}")
            return None

        client = genai.Client(api_key=api_key)
        enhanced_prompt = self._enhance_prompt(prompt, category, textbook_context)

        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=enhanced_prompt)],
            ),
        ]

        aspect = "16:9" if category in FACTUAL_CATEGORIES else "1:1"

        config = types.GenerateContentConfig(
            image_config=types.ImageConfig(
                aspect_ratio=aspect,
                image_size="1K",
            ),
            response_modalities=["IMAGE", "TEXT"],
        )

        # Always enable web + image search grounding for factual accuracy.
        tools = None
        try:
            tools = [types.Tool(google_search=types.GoogleSearch(
                search_types=types.SearchTypes(
                    web_search=types.WebSearch(),
                    image_search=types.ImageSearch(),
                )
            ))]
            logger.info(f"Search grounding (web+image) enabled for category: {category}")
        except Exception as e:
            logger.warning(f"Could not create search grounding tool: {e}")

        for model in [self._gemini_model(), GEMINI_FALLBACK_MODEL]:
            result = self._call_gemini(
                client, model, contents, config, prompt,
                tools=tools, include_bytes=include_bytes,
            )
            if result is not None:
                return result

        logger.warning("Both primary and fallback Gemini models failed")
        return None

    def _call_gemini(
        self, client, model: str, contents, config, prompt: str,
        tools=None, include_bytes: bool = False,
    ) -> Optional[Dict]:
        """Generate image from a single Gemini model. Returns result dict or None."""
        try:
            logger.info(f"Generating image with {model}: {prompt[:80]}...")

            kwargs = dict(model=model, contents=contents, config=config)
            if tools:
                kwargs['config'] = type(config)(
                    image_config=config.image_config,
                    response_modalities=config.response_modalities,
                    tools=tools,
                )

            try:
                response = client.models.generate_content(**kwargs)
            except Exception as tool_err:
                if tools:
                    logger.warning(f"{model}: tools param failed ({tool_err}), retrying without")
                    response = client.models.generate_content(
                        model=model, contents=contents, config=config
                    )
                else:
                    raise

            image_bytes = None
            mime_type = None

            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        image_bytes = part.inline_data.data
                        mime_type = part.inline_data.mime_type

            if not image_bytes:
                logger.warning(f"{model}: No image data in response")
                return None

            ext = mimetypes.guess_extension(mime_type) if mime_type else '.png'
            if ext is None:
                ext = '.png'

            saved_url = self._save_generated_image_bytes(image_bytes, prompt, ext)

            logger.info(f"Image generated successfully with {model}")
            result = {
                'url': saved_url,
                'title': prompt[:100],
                'caption': f"AI-generated: {prompt[:200]}",
                'alt_text': prompt,
                'generated': True,
                'model': model,
                'provider': 'gemini',
            }

            if include_bytes:
                result['_raw_bytes'] = image_bytes

            return result

        except Exception as e:
            err = str(e)
            if '503' in err or 'UNAVAILABLE' in err.upper() or 'overloaded' in err.lower():
                logger.warning(f"{model} unavailable (503), trying fallback...")
                self.last_error = f"{model} unavailable (503)"
                return None
            logger.error(f"{model} failed: {err[:300]}")
            self.last_error = f"Gemini {model} error: {err[:200]}"
            return None

    # ─── Prompt enhancement ─────────────────────────────────────────

    def _enhance_prompt(self, prompt: str, category: str, textbook_context: str = "") -> str:
        """Enhance prompt for better image results (provider-agnostic)."""
        from apps.llm.prompts import get_prompt_or_default
        institution_id = self.institution.id if self.institution else None
        context = get_prompt_or_default(
            institution_id, 'image_generation_prompt',
            "Educational visual for secondary school students. "
        )

        lesson_context = ""
        if self.lesson:
            try:
                course = self.lesson.unit.course
                lesson_context = (
                    f"Subject: {course.title}. "
                    f"Grade: {course.grade_level}. "
                    f"Lesson: {self.lesson.title}. "
                )
            except Exception:
                pass

        style_map = {
            'diagram': (
                "Create a detailed educational diagram with clear labels, "
                "arrows, and annotations on a clean white background. "
                "Make it precise and suitable for a textbook. "
            ),
            'photo': (
                "Create a high-quality photorealistic image, sharp focus, "
                "natural lighting. "
            ),
            'illustration': (
                "Create a colorful digital educational illustration with "
                "clean lines, vibrant colours, suitable for a "
                "secondary school textbook. "
            ),
            'map': (
                "Create a SCHEMATIC geographic map (NOT a satellite photo). "
                "Professional cartographic style with clean outlines, "
                "a legend, compass rose, and distinct colour-coded regions. "
            ),
            'chart': (
                "Create a professional chart with clear axis labels, "
                "a title, distinct colours, and a clean white background. "
            ),
            'flowchart': (
                "Create a flowchart with clear boxes, directional arrows, "
                "and concise labels on a clean white background. "
                "Make it easy to follow. "
            ),
            'infographic': (
                "Create a detailed educational infographic with icons, "
                "short text labels, vibrant colours, and a clear visual hierarchy. "
            ),
        }

        style = style_map.get(category, (
            "Create a detailed educational visual, clear and professional, "
            "suitable for a secondary school textbook. "
        ))

        textbook_style = ""
        if textbook_context:
            textbook_style = f"Match this style: {textbook_context}. "

        anti_hallucination = (
            "\n\nRULES:\n"
            "1. Do NOT include text labels with made-up or misspelled words. "
            "If unsure of spelling, omit the label.\n"
            "2. Do NOT fabricate geographic features, place names, or scientific data.\n"
            "3. Keep text labels minimal — prefer arrows and colour-coding.\n"
            "4. If the figure depicts a problem with an unknown "
            "(a 'find x' style question), mark the unknown with '?' or "
            "'x = ?'. NEVER write the computed answer in place of the "
            "unknown. The figure helps the student SEE the problem; "
            "the answer must come from the student's working, not "
            "from reading the image.\n"
        )

        return f"{style}{context}{lesson_context}{textbook_style}{prompt}{anti_hallucination}"

    # ─── Save ───────────────────────────────────────────────────────

    def _save_generated_image_bytes(
        self, image_bytes: bytes, prompt: str, ext: str = '.png'
    ) -> Optional[str]:
        """Save generated image bytes to media library."""
        try:
            from apps.media_library.models import MediaAsset
            from apps.accounts.models import Institution

            prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
            filename = f"generated_{prompt_hash}{ext}"

            # MediaAsset.institution is NOT NULL. For platform-wide
            # lessons (institution=None) fall back to the Global
            # institution so the save doesn't violate the FK.
            asset_institution = self.institution or Institution.get_global()

            asset = MediaAsset.objects.create(
                institution=asset_institution,
                title=prompt[:100],
                asset_type='image',
                caption=f"AI-generated image for: {prompt[:200]}",
                alt_text=prompt[:200],
            )

            asset.file.save(filename, ContentFile(image_bytes))
            asset.save()

            logger.info(f"Saved generated image: {asset.file.url}")

            # Best-effort: extract figure_facts so the runtime tutor can
            # anchor scaffolding in real labelled features instead of
            # asking the student to imagine. Non-fatal — if extraction
            # fails, the figure still serves; the runtime falls through
            # to text-only behaviour. See memory/figure_facts_plan.md.
            try:
                from apps.curriculum.figure_facts_extractor import (
                    extract_figure_facts,
                )
                facts, err = extract_figure_facts(image_bytes)
                if err is None and facts is not None:
                    asset.figure_facts = facts.model_dump(mode="json")
                    asset.save(update_fields=["figure_facts", "updated_at"])
                    logger.info(
                        f"[FigureFacts] extracted for asset #{asset.id} "
                        f"(type={facts.type}, "
                        f"features={len(facts.labelled_features)})"
                    )
                else:
                    logger.info(
                        f"[FigureFacts] skip asset #{asset.id}: {err}"
                    )
            except Exception as ff_err:
                logger.warning(
                    f"[FigureFacts] extraction crashed for asset "
                    f"#{asset.id}: {ff_err}"
                )

            return asset.file.url

        except Exception as e:
            logger.error(f"Failed to save generated image: {e}")
            self.last_error = f"Saved image, but DB write failed: {str(e)[:200]}"
            return None


def get_image_for_lesson(lesson, prompt: str, category: str = "diagram") -> Optional[Dict]:
    """
    Convenience function to get/generate image for a lesson.

    Usage in engine:
        image = get_image_for_lesson(self.lesson, "relief rainfall diagram", "diagram")
        if image:
            commands.append({'type': 'show_media', 'data': image})
    """
    service = ImageGenerationService(
        lesson=lesson,
        institution=lesson.institution if hasattr(lesson, 'institution') else None
    )
    return service.get_or_generate_image(prompt, category)
