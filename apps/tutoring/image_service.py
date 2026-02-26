"""
Image Generation Service — Gemini native image generation.

Primary: gemini-3-pro-image-preview (Nano Banana Pro) — best quality for
educational diagrams, labeled visuals, and text-in-image.
Fallback: gemini-2.5-flash-image (Nano Banana) — used when primary is 503.

Set DISABLE_IMAGE_GEN=1 in .env to disable.
"""

import os
import logging
import hashlib
import mimetypes
from typing import Optional, Dict
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

PRIMARY_MODEL = 'gemini-2.5-flash-image'
FALLBACK_MODEL = 'gemini-2.5-flash-image'


class ImageGenerationService:
    """
    Generate images with Gemini native image generation.

    Usage:
        service = ImageGenerationService(lesson)
        result = service.get_or_generate_image(
            prompt="Diagram showing relief rainfall on a mountain",
            category="diagram"
        )
        # Returns: {'url': '/media/...', 'title': '...', 'generated': True/False}
    """

    def __init__(self, lesson=None, institution=None):
        self.lesson = lesson
        self.institution = institution
        self.available = self._check_available()

    def _check_available(self) -> bool:
        """Check if Gemini image generation is available."""
        if os.environ.get('DISABLE_IMAGE_GEN'):
            return False

        api_key = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY')
        if not api_key:
            logger.info("Image generation not available: No GOOGLE_API_KEY or GEMINI_API_KEY")
            return False

        return True

    def get_or_generate_image(
        self,
        prompt: str,
        category: str = "general",
        textbook_context: str = "",
        **kwargs
    ) -> Optional[Dict]:
        """
        Generate a new image with Gemini.

        Args:
            prompt: Description of the image needed
            category: Type of image (diagram, photo, illustration, etc.)
            textbook_context: Optional description of the textbook figure style

        Returns:
            Dict with 'url', 'title', 'caption', 'generated' keys, or None
        """
        if self.available:
            generated = self._generate_with_gemini(prompt, category, textbook_context)
            if generated:
                return generated

        logger.warning(f"Image generation unavailable for: {prompt[:50]}...")
        return None

    def _generate_with_gemini(self, prompt: str, category: str, textbook_context: str = "") -> Optional[Dict]:
        """Generate image — tries PRIMARY_MODEL, falls back to FALLBACK_MODEL on 503."""
        from google import genai
        from google.genai import types

        api_key = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY')
        client = genai.Client(api_key=api_key)

        enhanced_prompt = self._enhance_prompt(prompt, category, textbook_context)

        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=enhanced_prompt)],
            ),
        ]

        config = types.GenerateContentConfig(
            image_config=types.ImageConfig(
                aspect_ratio="1:1",
                image_size="1K",
            ),
            response_modalities=["IMAGE", "TEXT"],
        )

        for model in [PRIMARY_MODEL, FALLBACK_MODEL]:
            result = self._call_model(client, model, contents, config, prompt)
            if result is not None:
                return result

        logger.warning("Both primary and fallback image models failed")
        return None

    def _call_model(self, client, model: str, contents, config, prompt: str) -> Optional[Dict]:
        """Stream image from a single model. Returns result dict or None."""
        try:
            logger.info(f"Generating image with {model}: {prompt[:80]}...")

            image_bytes = None
            mime_type = None

            for chunk in client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            ):
                if chunk.parts is None:
                    continue
                for part in chunk.parts:
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
            return {
                'url': saved_url,
                'title': prompt[:100],
                'caption': f"AI-generated: {prompt[:200]}",
                'alt_text': prompt,
                'generated': True,
                'model': model,
            }

        except Exception as e:
            err = str(e)
            if '503' in err or 'UNAVAILABLE' in err.upper() or 'overloaded' in err.lower():
                logger.warning(f"{model} unavailable (503), trying fallback...")
                return None
            logger.error(f"{model} failed: {err[:300]}")
            return None

    def _enhance_prompt(self, prompt: str, category: str, textbook_context: str = "") -> str:
        """Enhance prompt for better Gemini image results."""
        from apps.llm.prompts import get_prompt_or_default
        institution_id = self.institution.id if self.institution else None
        context = get_prompt_or_default(
            institution_id, 'image_generation_prompt',
            "Educational visual for secondary school students. "
        )

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
                "Create a detailed geographic map with clear labels, "
                "a legend, compass rose, and distinct colour-coded regions. "
                "Use a professional cartographic style. "
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

        return f"{style}{context}{textbook_style}{prompt}"

    def _save_generated_image_bytes(
        self, image_bytes: bytes, prompt: str, ext: str = '.png'
    ) -> Optional[str]:
        """Save generated image bytes to media library."""
        try:
            from apps.media_library.models import MediaAsset

            prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
            filename = f"generated_{prompt_hash}{ext}"

            asset = MediaAsset.objects.create(
                institution=self.institution,
                title=prompt[:100],
                asset_type='image',
                caption=f"AI-generated image for: {prompt[:200]}",
                alt_text=prompt[:200],
            )

            asset.file.save(filename, ContentFile(image_bytes))
            asset.save()

            logger.info(f"Saved generated image: {asset.file.url}")
            return asset.file.url

        except Exception as e:
            logger.error(f"Failed to save generated image: {e}")
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
