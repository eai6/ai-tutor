"""
Image Generation Service - DALL-E with fallback to existing media.

Supports:
1. DALL-E generation when API key available and online
2. Fallback to existing media library when offline/no credits
3. Caching generated images to avoid regeneration
"""

import os
import logging
import hashlib
from typing import Optional, Dict, List
from django.conf import settings
from django.core.files.base import ContentFile
import requests

logger = logging.getLogger(__name__)


class ImageGenerationService:
    """
    Generate images with DALL-E, with fallback to existing media.
    
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
        self.dalle_available = self._check_dalle_available()
    
    def _check_dalle_available(self) -> bool:
        """Check if DALL-E API is available."""
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            logger.info("DALL-E not available: No OPENAI_API_KEY")
            return False
        
        # Quick connectivity check
        try:
            response = requests.head('https://api.openai.com', timeout=2)
            return True
        except:
            logger.info("DALL-E not available: No internet connection")
            return False
    
    def get_or_generate_image(
        self,
        prompt: str,
        category: str = "general",
        **kwargs  # Ignore any extra args for compatibility
    ) -> Optional[Dict]:
        """
        Generate a new image with DALL-E.
        
        Args:
            prompt: Description of the image needed
            category: Type of image (diagram, photo, illustration, etc.)
        
        Returns:
            Dict with 'url', 'title', 'caption', 'generated' keys, or None
        """
        # Always generate fresh with DALL-E
        if self.dalle_available:
            generated = self._generate_with_dalle(prompt, category)
            if generated:
                return generated
        
        logger.warning(f"DALL-E unavailable for: {prompt[:50]}...")
        return None
    
    def _generate_with_dalle(self, prompt: str, category: str) -> Optional[Dict]:
        """Generate image with DALL-E API."""
        try:
            import openai
            
            client = openai.OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
            
            # Enhance prompt for educational context
            enhanced_prompt = self._enhance_prompt(prompt, category)
            
            logger.info(f"Generating DALL-E image: {enhanced_prompt[:100]}...")
            
            response = client.images.generate(
                model="dall-e-3",
                prompt=enhanced_prompt,
                size="1024x1024",
                quality="standard",
                n=1,
            )
            
            image_url = response.data[0].url
            
            # Download and save the image
            saved_url = self._save_generated_image(image_url, prompt)
            
            return {
                'url': saved_url or image_url,
                'title': prompt[:100],
                'caption': f"AI-generated: {prompt[:200]}",
                'alt_text': prompt,
                'generated': True,
            }
            
        except Exception as e:
            logger.error(f"DALL-E generation failed: {e}")
            return None
    
    def _enhance_prompt(self, prompt: str, category: str) -> str:
        """Enhance prompt for better DALL-E results."""
        # Add educational context
        context = "Educational diagram for secondary school students in Seychelles. "
        
        # Add style based on category
        style_map = {
            'diagram': "Clear, labeled scientific diagram with arrows and annotations. ",
            'photo': "High-quality photograph. ",
            'illustration': "Clean, colorful educational illustration. ",
            'map': "Clear geographic map with labels. ",
            'chart': "Professional chart or graph with clear labels. ",
        }
        
        style = style_map.get(category, "Clear educational visual. ")
        
        return context + style + prompt
    
    def _save_generated_image(self, image_url: str, prompt: str) -> Optional[str]:
        """Download and save generated image to media library."""
        try:
            from apps.media_library.models import MediaAsset
            
            # Download image
            response = requests.get(image_url, timeout=30)
            if response.status_code != 200:
                return None
            
            # Create unique filename
            prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
            filename = f"generated_{prompt_hash}.png"
            
            # Save to media library
            asset = MediaAsset.objects.create(
                institution=self.institution,
                title=prompt[:100],
                asset_type='image',
                caption=f"AI-generated image for: {prompt[:200]}",
                alt_text=prompt[:200],
            )
            
            asset.file.save(filename, ContentFile(response.content))
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