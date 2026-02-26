"""
Image Generation Service - Gemini Imagen with fallback.

Supports:
1. Gemini Imagen generation when GOOGLE_API_KEY available and online
2. Returns None when unavailable (callers handle gracefully)
"""

import os
import logging
import hashlib
from typing import Optional, Dict
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)


class ImageGenerationService:
    """
    Generate images with Gemini Imagen.

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
        self.imagen_available = self._check_imagen_available()

    def _check_imagen_available(self) -> bool:
        """Check if Gemini Imagen API is available."""
        # Disabled until image quality is acceptable
        # Set ENABLE_IMAGEN=1 in .env to re-enable
        if not os.environ.get('ENABLE_IMAGEN'):
            return False

        api_key = os.environ.get('GOOGLE_API_KEY')
        if not api_key:
            logger.info("Imagen not available: No GOOGLE_API_KEY")
            return False

        try:
            import requests
            response = requests.head(
                'https://generativelanguage.googleapis.com', timeout=2
            )
            return True
        except Exception:
            logger.info("Imagen not available: No internet connection")
            return False

    def get_or_generate_image(
        self,
        prompt: str,
        category: str = "general",
        textbook_context: str = "",
        **kwargs  # Ignore extra args for compatibility
    ) -> Optional[Dict]:
        """
        Generate a new image with Gemini Imagen.

        Args:
            prompt: Description of the image needed
            category: Type of image (diagram, photo, illustration, etc.)
            textbook_context: Optional description of the textbook figure style to match

        Returns:
            Dict with 'url', 'title', 'caption', 'generated' keys, or None
        """
        if self.imagen_available:
            generated = self._generate_with_imagen(prompt, category, textbook_context)
            if generated:
                return generated

        logger.warning(f"Imagen unavailable for: {prompt[:50]}...")
        return None

    def _generate_with_imagen(self, prompt: str, category: str, textbook_context: str = "") -> Optional[Dict]:
        """Generate image with Gemini Imagen API."""
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=os.environ['GOOGLE_API_KEY'])

            enhanced_prompt = self._enhance_prompt(prompt, category, textbook_context)

            logger.info(f"Generating Imagen image: {enhanced_prompt[:100]}...")

            response = client.models.generate_images(
                model='imagen-4.0-generate-001',
                prompt=enhanced_prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    safety_filter_level="BLOCK_LOW_AND_ABOVE",
                    person_generation="DONT_ALLOW",
                ),
            )

            if not response.generated_images:
                logger.warning("Imagen returned no images (possibly blocked by safety filter)")
                return None

            image_bytes = response.generated_images[0].image.image_bytes

            # Save directly from bytes (no URL download needed)
            saved_url = self._save_generated_image_bytes(image_bytes, prompt)

            return {
                'url': saved_url,
                'title': prompt[:100],
                'caption': f"AI-generated: {prompt[:200]}",
                'alt_text': prompt,
                'generated': True,
            }

        except Exception as e:
            logger.error(f"Imagen generation failed: {e}")
            return None

    def _enhance_prompt(self, prompt: str, category: str, textbook_context: str = "") -> str:
        """Enhance prompt for better Imagen 4 results.

        Applies best practices from the Imagen prompt guide:
        - Subject + context + style structure
        - Category-specific descriptive language and quality modifiers
        - Keeps total under 480 tokens
        """
        from apps.llm.prompts import get_prompt_or_default
        institution_id = self.institution.id if self.institution else None
        context = get_prompt_or_default(
            institution_id, 'image_generation_prompt',
            "Educational visual for secondary school students. "
        )

        style_map = {
            'diagram': (
                "A high-quality detailed educational diagram with clear labels, "
                "arrows, and annotations on a clean white background. "
                "Professional, precise, suitable for a textbook. "
            ),
            'photo': (
                "A high-quality 4K photograph, sharp focus, natural lighting, "
                "taken by a professional photographer. "
            ),
            'illustration': (
                "A high-quality colorful digital educational illustration, "
                "clean lines, detailed, vibrant colours, suitable for a "
                "secondary school textbook. "
            ),
            'map': (
                "A high-quality detailed geographic map with clear labels, "
                "a legend, compass rose, and distinct colour-coded regions. "
                "Professional cartographic style. "
            ),
            'chart': (
                "A high-quality professional chart with clear axis labels, "
                "a title, distinct colours, and a clean white background. "
                "Suitable for a textbook or presentation. "
            ),
            'flowchart': (
                "A high-quality flowchart with clear boxes, directional arrows, "
                "and concise labels on a clean white background. "
                "Professional, easy to follow. "
            ),
            'infographic': (
                "A high-quality detailed educational infographic with icons, "
                "short text labels, vibrant colours, and a clear visual hierarchy. "
            ),
        }

        style = style_map.get(category, (
            "A high-quality detailed educational visual, clear and professional, "
            "suitable for a secondary school textbook. "
        ))

        textbook_style = ""
        if textbook_context:
            textbook_style = f"In the style of: {textbook_context}. "

        # Build final prompt: style + context + textbook ref + user prompt
        # Keep concise to stay under 480 token limit
        enhanced = f"{style}{context}{textbook_style}{prompt}"
        return enhanced[:1500]

    def _save_generated_image_bytes(
        self, image_bytes: bytes, prompt: str
    ) -> Optional[str]:
        """Save generated image bytes to media library."""
        try:
            from apps.media_library.models import MediaAsset

            prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
            filename = f"generated_{prompt_hash}.png"

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
