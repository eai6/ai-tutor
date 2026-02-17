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
        
        # Load existing media for fallback
        self.existing_media = self._load_existing_media()
    
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
    
    def _load_existing_media(self) -> List[Dict]:
        """Load existing media from the lesson."""
        if not self.lesson:
            return []
        
        from apps.media_library.models import StepMedia, MediaAsset
        
        # Get media attached to this lesson
        attachments = StepMedia.objects.filter(
            lesson_step__lesson=self.lesson
        ).select_related('media_asset')
        
        media_list = []
        for att in attachments:
            asset = att.media_asset
            media_list.append({
                'id': asset.id,
                'title': asset.title.lower(),
                'type': asset.asset_type,
                'url': asset.file.url if asset.file else None,
                'caption': asset.caption or '',
                'alt_text': asset.alt_text or '',
                'keywords': (asset.caption or '').lower() + ' ' + (asset.alt_text or '').lower(),
            })
        
        # Also get general media assets for the institution
        if self.institution:
            general_assets = MediaAsset.objects.filter(
                institution=self.institution
            )[:50]  # Limit for performance
            
            for asset in general_assets:
                if not any(m['id'] == asset.id for m in media_list):
                    media_list.append({
                        'id': asset.id,
                        'title': asset.title.lower(),
                        'type': asset.asset_type,
                        'url': asset.file.url if asset.file else None,
                        'caption': asset.caption or '',
                        'alt_text': asset.alt_text or '',
                        'keywords': (asset.caption or '').lower() + ' ' + (asset.alt_text or '').lower(),
                    })
        
        return media_list
    
    def get_or_generate_image(
        self,
        prompt: str,
        category: str = "general",
        prefer_existing: bool = True
    ) -> Optional[Dict]:
        """
        Get an existing image or generate a new one.
        
        Args:
            prompt: Description of the image needed
            category: Type of image (diagram, photo, illustration, etc.)
            prefer_existing: If True, try existing media first
        
        Returns:
            Dict with 'url', 'title', 'caption', 'generated' keys, or None
        """
        # Try existing media first
        if prefer_existing:
            existing = self._find_matching_media(prompt, category)
            if existing:
                logger.info(f"Using existing media: {existing['title']}")
                return {
                    'url': existing['url'],
                    'title': existing['title'],
                    'caption': existing.get('caption', ''),
                    'alt_text': existing.get('alt_text', prompt),
                    'generated': False,
                }
        
        # Try DALL-E generation
        if self.dalle_available:
            generated = self._generate_with_dalle(prompt, category)
            if generated:
                return generated
        
        # Final fallback - any related existing media
        if not prefer_existing:
            existing = self._find_matching_media(prompt, category)
            if existing:
                return {
                    'url': existing['url'],
                    'title': existing['title'],
                    'caption': existing.get('caption', ''),
                    'alt_text': existing.get('alt_text', prompt),
                    'generated': False,
                }
        
        logger.warning(f"No image available for prompt: {prompt[:50]}...")
        return None
    
    def _find_matching_media(self, prompt: str, category: str) -> Optional[Dict]:
        """Find existing media that matches the prompt."""
        if not self.existing_media:
            return None
        
        prompt_lower = prompt.lower()
        prompt_words = set(prompt_lower.split())
        
        best_match = None
        best_score = 0
        
        for media in self.existing_media:
            if not media.get('url'):
                continue
            
            # Score based on keyword matching
            score = 0
            
            # Title match
            title_words = set(media['title'].split())
            title_overlap = len(prompt_words & title_words)
            score += title_overlap * 3
            
            # Keyword match
            keyword_words = set(media['keywords'].split())
            keyword_overlap = len(prompt_words & keyword_words)
            score += keyword_overlap * 2
            
            # Category/type match
            if category.lower() in media['type'].lower():
                score += 5
            
            # Specific keyword boosts
            if 'diagram' in prompt_lower and 'diagram' in media['keywords']:
                score += 10
            if 'rainfall' in prompt_lower and 'rainfall' in media['keywords']:
                score += 10
            if 'map' in prompt_lower and 'map' in media['keywords']:
                score += 10
            
            if score > best_score:
                best_score = score
                best_match = media
        
        # Require minimum score to match
        if best_score >= 3:
            return best_match
        
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