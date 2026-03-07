"""
3-Layer Image Safety Pipeline for on-the-fly image generation.

Layer 0: Regex pre-filter (ImageSafetyFilter)
Layer 1: LLM prompt validator (instructor → PromptSafetyResult)
Layer 2: Gemini generates image (ImageGenerationService)
Layer 3: Multimodal LLM describes image → second LLM verifies description
"""

import base64
import logging
from typing import Optional, Dict

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models for structured LLM output
# ---------------------------------------------------------------------------

class PromptSafetyResult(BaseModel):
    """Layer 1: Is the image generation prompt safe for students?"""
    is_safe: bool
    reason: str
    sanitized_prompt: str


class ImageDescription(BaseModel):
    """Layer 3a: Multimodal LLM describes what's in the generated image."""
    description: str
    contains_text: bool
    detected_objects: list[str]


class ImageVerificationResult(BaseModel):
    """Layer 3b: Is the described image appropriate for students?"""
    is_appropriate: bool
    concern: str
    confidence: float


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class ImageSafetyPipeline:
    """
    Full safety pipeline for on-the-fly image generation.

    Uses the tutor's existing instructor_client for structured output
    and google.genai directly for multimodal vision (Layer 3a).
    """

    def __init__(self, instructor_client, provider, lesson, session, student):
        self.instructor_client = instructor_client
        self.provider = provider
        self.lesson = lesson
        self.session = session
        self.student = student

    # -- Layer 1 ---------------------------------------------------------------

    def validate_prompt(self, prompt: str, category: str) -> Optional[PromptSafetyResult]:
        """LLM checks if the prompt is appropriate for educational image gen."""
        if not self.instructor_client:
            logger.warning("No instructor client — skipping Layer 1 (prompt validation)")
            return PromptSafetyResult(
                is_safe=True, reason="no-llm-fallback", sanitized_prompt=prompt,
            )

        system = (
            "You are a school safety officer reviewing image-generation prompts "
            "for a K-12 educational tutoring app. Decide whether the prompt is safe "
            "to send to an image generator that students will see. "
            "Reject anything violent, sexual, discriminatory, frightening, or "
            "unrelated to education. If safe, return a sanitized (cleaned) version "
            "of the prompt that preserves educational intent."
        )
        user_msg = (
            f"Category: {category}\n"
            f"Prompt: {prompt}\n"
            f"Lesson: {self.lesson.title if self.lesson else 'unknown'}\n\n"
            "Is this prompt safe for generating an educational image?"
        )

        try:
            kwargs = dict(
                response_model=PromptSafetyResult,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
            )
            if self.provider != 'google':
                kwargs['max_tokens'] = 300
            else:
                kwargs['max_retries'] = 2

            return self.instructor_client.chat.completions.create(**kwargs)
        except Exception as e:
            logger.error(f"Layer 1 prompt validation failed: {e}")
            # Fail open — rely on Layer 0 regex + Layer 3 post-check
            return PromptSafetyResult(
                is_safe=True, reason=f"llm-error: {e}", sanitized_prompt=prompt,
            )

    # -- Layer 3 ---------------------------------------------------------------

    def verify_image(self, image_bytes: bytes, original_prompt: str) -> ImageVerificationResult:
        """
        Layer 3: Multimodal describe (3a) → text verify (3b).

        3a uses google.genai vision directly (Gemini supports inline images).
        3b uses the tutor's instructor_client for structured output.
        """
        # --- 3a: describe the image with multimodal Gemini ---
        description = self._describe_image(image_bytes)
        if description is None:
            # Can't verify — fail safe (reject)
            return ImageVerificationResult(
                is_appropriate=False,
                concern="Could not describe image for verification",
                confidence=0.0,
            )

        # --- 3b: verify the description with structured LLM ---
        return self._verify_description(description, original_prompt)

    def _describe_image(self, image_bytes: bytes) -> Optional[ImageDescription]:
        """3a: Send image to Gemini vision → ImageDescription."""
        try:
            import os
            from google import genai
            from google.genai import types

            api_key = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY')
            if not api_key:
                logger.warning("No Google API key for image verification")
                return None

            client = genai.Client(api_key=api_key)
            b64 = base64.standard_b64encode(image_bytes).decode()

            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    types.Content(role="user", parts=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                        types.Part.from_text(
                            "Describe this image in detail. List all objects, text, "
                            "and notable features. Is it an educational diagram? "
                            "Respond in JSON: {\"description\": \"...\", "
                            "\"contains_text\": true/false, "
                            "\"detected_objects\": [\"obj1\", \"obj2\"]}"
                        ),
                    ]),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ImageDescription,
                ),
            )

            import json
            data = json.loads(response.text)
            return ImageDescription(**data)

        except Exception as e:
            logger.error(f"Layer 3a image description failed: {e}")
            return None

    def _verify_description(self, desc: ImageDescription, original_prompt: str) -> ImageVerificationResult:
        """3b: LLM checks whether the described image is appropriate."""
        if not self.instructor_client:
            # No LLM — fail open with low confidence
            return ImageVerificationResult(
                is_appropriate=True, concern="", confidence=0.5,
            )

        system = (
            "You are a school content reviewer. Given a description of an "
            "AI-generated image intended for K-12 students, decide if it is "
            "appropriate. Flag anything violent, sexual, discriminatory, "
            "frightening, or misleading. Educational diagrams/charts/maps are "
            "expected and appropriate."
        )
        user_msg = (
            f"Original prompt: {original_prompt}\n\n"
            f"Image description: {desc.description}\n"
            f"Contains text: {desc.contains_text}\n"
            f"Detected objects: {', '.join(desc.detected_objects)}\n\n"
            "Is this image appropriate for K-12 students?"
        )

        try:
            kwargs = dict(
                response_model=ImageVerificationResult,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
            )
            if self.provider != 'google':
                kwargs['max_tokens'] = 300
            else:
                kwargs['max_retries'] = 2

            return self.instructor_client.chat.completions.create(**kwargs)
        except Exception as e:
            logger.error(f"Layer 3b verification failed: {e}")
            # Fail safe — reject on error
            return ImageVerificationResult(
                is_appropriate=False,
                concern=f"Verification error: {e}",
                confidence=0.0,
            )

    # -- Full pipeline ---------------------------------------------------------

    def run(self, prompt: str, category: str) -> Optional[Dict]:
        """
        Full pipeline: Layer 0 → 1 → 2 → 3.

        Returns a media dict {'type', 'url', 'alt', 'caption'} or None.
        """
        from apps.safety import ImageSafetyFilter, SafetyAuditLog

        audit_base = {
            'prompt': prompt[:200],
            'category': category,
            'lesson': self.lesson.title if self.lesson else None,
            'session_id': self.session.id if self.session else None,
        }

        # --- Layer 0: regex pre-filter ---
        subject = ''
        if self.lesson and hasattr(self.lesson, 'unit') and self.lesson.unit:
            course = getattr(self.lesson.unit, 'course', None)
            if course:
                subject = course.title
        safety_result = ImageSafetyFilter.check_image_request(
            prompt,
            lesson_title=self.lesson.title if self.lesson else "",
            subject=subject,
        )
        if safety_result.blocked:
            SafetyAuditLog.log(
                'image_gen_blocked_layer0',
                user=self.student,
                session_id=self.session.id if self.session else None,
                details={**audit_base, 'reason': safety_result.block_reason},
                severity='warning',
            )
            logger.warning(f"Layer 0 blocked image gen: {prompt[:60]}")
            return None

        # --- Layer 1: LLM prompt validation ---
        prompt_check = self.validate_prompt(prompt, category)
        if prompt_check and not prompt_check.is_safe:
            SafetyAuditLog.log(
                'image_gen_blocked_layer1',
                user=self.student,
                session_id=self.session.id if self.session else None,
                details={**audit_base, 'reason': prompt_check.reason},
                severity='warning',
            )
            logger.warning(f"Layer 1 blocked image gen: {prompt_check.reason}")
            return None

        # Use sanitized prompt from Layer 1
        safe_prompt = prompt_check.sanitized_prompt if prompt_check else prompt

        # --- Layer 2: generate the image ---
        from apps.tutoring.image_service import ImageGenerationService

        service = ImageGenerationService(
            lesson=self.lesson,
            institution=self.session.institution if self.session else None,
        )
        result = service.get_or_generate_image(safe_prompt, category, include_bytes=True)
        if not result:
            logger.info(f"Image generation returned nothing for: {safe_prompt[:60]}")
            return None

        raw_bytes = result.pop('_raw_bytes', None)

        # --- Layer 3: verify generated image ---
        if raw_bytes:
            verification = self.verify_image(raw_bytes, safe_prompt)
            if not verification.is_appropriate:
                SafetyAuditLog.log(
                    'image_gen_blocked_layer3',
                    user=self.student,
                    session_id=self.session.id if self.session else None,
                    details={
                        **audit_base,
                        'concern': verification.concern,
                        'confidence': verification.confidence,
                    },
                    severity='warning',
                )
                logger.warning(f"Layer 3 rejected image: {verification.concern}")
                # Delete the saved asset
                self._delete_asset(result.get('url'))
                return None
        else:
            logger.info("No raw bytes for Layer 3 verification — skipping post-check")

        # --- Success: audit and return media dict ---
        SafetyAuditLog.log(
            'image_gen_success',
            user=self.student,
            session_id=self.session.id if self.session else None,
            details=audit_base,
            severity='info',
        )

        return {
            'type': 'image',
            'url': result['url'],
            'alt': result.get('alt_text', safe_prompt),
            'caption': result.get('caption', f"Generated: {safe_prompt[:100]}"),
            'generated': True,
        }

    @staticmethod
    def _delete_asset(url: str):
        """Best-effort delete of a MediaAsset by URL (Layer 3 rejection cleanup)."""
        if not url:
            return
        try:
            from apps.media_library.models import MediaAsset
            asset = MediaAsset.objects.filter(file=url.replace('/media/', '')).first()
            if asset:
                asset.file.delete(save=False)
                asset.delete()
                logger.info(f"Deleted rejected image asset: {url}")
        except Exception as e:
            logger.error(f"Failed to delete rejected asset: {e}")
