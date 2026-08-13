"""Vision-OCR provider abstraction with multi-provider fallback.

Mirrors the pluggable shape of ``apps/llm/client.py::BaseLLMClient`` per
the Rule-of-Three / "mirror existing patterns" guidance in CLAUDE.md.
Adding a new vision-capable provider = subclass ``VisionOCRProvider``,
register it in ``_provider_factory()``, no caller changes needed.

Why this exists: the previous inline implementation in
``curriculum_parser.py::_extract_pdf_with_vision`` mixed provider-specific
content-block format with the batch-orchestration logic. When one provider
hit a content-filter rejection mid-batch, the whole extraction failed.
This module separates **what to send** (provider-specific) from **how to
sequence + recover** (orchestration), so a fallback chain lives at one
clean integration point.

Public API:
    - ``RenderedPage`` : value type for one rendered PDF page
    - ``OCRResult``    : value type for one provider attempt's outcome
    - ``VisionOCRProvider`` : abstract base
    - ``AnthropicVisionProvider``, ``GenericVisionProvider`` : concrete
    - ``get_vision_provider_chain()`` : ordered list to try
    - ``extract_text_with_fallback()`` : try chain, return first success
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


logger = logging.getLogger(__name__)


# ============================================================================
# VALUE TYPES
# ============================================================================

@dataclass(frozen=True)
class RenderedPage:
    """One PDF page already rendered to base64 image bytes."""
    b64: str
    media_type: str   # e.g. 'image/png', 'image/jpeg'


@dataclass
class OCRResult:
    """Outcome of one provider attempt on one batch.

    Always-returned (no raise) so the orchestrator can see what happened
    on every attempt and decide whether to fall back / skip / accept.
    """
    text: str = ""
    success: bool = False
    provider: str = ""           # 'anthropic' / 'google' / etc.
    model_name: str = ""         # 'claude-opus-4-7' / 'gemini-3-pro' / etc.
    error_class: str = ""        # exception class name
    error_detail: str = ""       # exception message (truncated)
    error_reason: str = ""       # OCRFailure.REASONS slug


# ============================================================================
# ABSTRACT BASE
# ============================================================================

class VisionOCRProvider(ABC):
    """Pluggable vision-OCR provider. One subclass per content-block style.

    Stateful only insofar as it caches the LLM client; thread-safe for
    concurrent batch dispatch from the parser.
    """

    name: str = ""           # short label for logs ('anthropic', 'google')

    def __init__(self, model_config):
        from ai_tutor.apps.llm.client import get_llm_client
        self.config = model_config
        self.client = get_llm_client(model_config)
        self.model_name = getattr(model_config, 'model_name', '') or ''

    @abstractmethod
    def _build_content_blocks(
        self, pages: List[RenderedPage], extraction_prompt: str,
    ) -> list:
        """Return provider-specific content-blocks for the user message.

        Anthropic uses ``{type: image, source: {type: base64, ...}}``;
        OpenAI / Gemini use ``{type: image_url, image_url: {url: data:...}}``.
        Implementations should append a final ``{type: text, text: prompt}``
        block.
        """

    def extract_text(
        self,
        pages: List[RenderedPage],
        system_prompt: str,
        extraction_prompt: str,
        max_tokens: int = 4096,
    ) -> OCRResult:
        """Call the provider, return OCRResult (never raises)."""
        if not pages:
            return OCRResult(
                text="", success=True, provider=self.name,
                model_name=self.model_name,
            )

        content = self._build_content_blocks(pages, extraction_prompt)
        messages = [{"role": "user", "content": content}]
        try:
            response = self.client.generate(
                messages=messages,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            from ai_tutor.apps.curriculum.curriculum_parser import _classify_llm_error
            return OCRResult(
                text="",
                success=False,
                provider=self.name,
                model_name=self.model_name,
                error_class=type(exc).__name__,
                error_detail=str(exc)[:400],
                error_reason=_classify_llm_error(exc),
            )

        text = (getattr(response, 'content', None) or "").strip()
        if not text:
            return OCRResult(
                text="",
                success=False,
                provider=self.name,
                model_name=self.model_name,
                error_class='EmptyResponse',
                error_detail='Provider returned no text',
                error_reason='empty_response',
            )
        return OCRResult(
            text=text, success=True,
            provider=self.name, model_name=self.model_name,
        )


# ============================================================================
# CONCRETE IMPLEMENTATIONS
# ============================================================================

class AnthropicVisionProvider(VisionOCRProvider):
    """Anthropic Claude — uses ``image`` + ``source: {type: base64}`` blocks."""
    name = "anthropic"

    def _build_content_blocks(self, pages, extraction_prompt):
        blocks = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": p.media_type,
                    "data": p.b64,
                },
            }
            for p in pages
        ]
        blocks.append({"type": "text", "text": extraction_prompt})
        return blocks


class GenericVisionProvider(VisionOCRProvider):
    """OpenAI / Gemini / Azure-OpenAI — uses ``image_url`` blocks with data URI."""

    def __init__(self, model_config, name):
        super().__init__(model_config)
        self.name = name

    def _build_content_blocks(self, pages, extraction_prompt):
        blocks = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{p.media_type};base64,{p.b64}"},
            }
            for p in pages
        ]
        blocks.append({"type": "text", "text": extraction_prompt})
        return blocks


# ============================================================================
# FACTORY + CHAIN
# ============================================================================

def _provider_for_config(model_config) -> VisionOCRProvider:
    """Map a ModelConfig.provider to the matching VisionOCRProvider subclass.

    Adding a new provider type: add the branch here and (optionally) a
    new VisionOCRProvider subclass if the content-block shape differs.
    """
    from ai_tutor.apps.llm.models import ModelConfig

    if model_config.provider == ModelConfig.Provider.ANTHROPIC:
        return AnthropicVisionProvider(model_config)
    # OpenAI, Google (Gemini), Azure OpenAI all use the OpenAI-shaped
    # image_url content-blocks (Gemini's adapter translates internally).
    return GenericVisionProvider(model_config, name=str(model_config.provider))


# Purposes consulted (in order) when building the fallback chain. Each
# active ModelConfig with a NEW provider type contributes one tier to
# the chain. Anything beyond the first 'generation' purpose is a
# fallback for when the primary provider rejects/fails.
_FALLBACK_PURPOSES = ('generation', 'judge', 'tutoring', 'exit_tickets')


def get_vision_provider_chain() -> List[VisionOCRProvider]:
    """Return ordered list of vision OCR providers to try.

    Source of truth: active ModelConfigs across purposes. Picks DISTINCT
    providers (one per provider type) so a content-filter rejection from
    Anthropic falls through to Gemini (or vice versa) automatically.

    Order: 'generation' first (the canonical OCR choice), then 'judge',
    'tutoring', 'exit_tickets' as fallbacks. Each contributes only if it
    introduces a NEW provider type. Anthropic + Gemini configured =
    2-tier chain; one provider only = 1-tier (no fallback, but graceful).
    """
    from ai_tutor.apps.llm.models import ModelConfig

    seen_providers = set()
    chain: List[VisionOCRProvider] = []
    for purpose in _FALLBACK_PURPOSES:
        config = ModelConfig.get_for(purpose)
        if not config or config.provider in seen_providers:
            continue
        seen_providers.add(config.provider)
        try:
            chain.append(_provider_for_config(config))
        except Exception as exc:
            logger.warning(
                "Vision OCR: failed to instantiate provider for purpose=%s "
                "model=%s/%s: %s",
                purpose, config.provider, config.model_name, exc,
            )
    return chain


# ============================================================================
# ORCHESTRATION
# ============================================================================

def extract_text_with_fallback(
    pages: List[RenderedPage],
    providers: List[VisionOCRProvider],
    system_prompt: str,
    extraction_prompt: str,
    max_tokens: int = 4096,
) -> OCRResult:
    """Try each provider in order. Return the first success.

    If every provider fails, returns the LAST attempt's OCRResult (with
    success=False) so the orchestrator can log the final reason. The
    caller decides whether to skip-and-continue or propagate.
    """
    if not providers:
        return OCRResult(
            text="", success=False, provider="(no providers)",
            error_class='NoProviders',
            error_detail='get_vision_provider_chain() returned empty',
            error_reason='no_config',
        )

    last_result: Optional[OCRResult] = None
    for provider in providers:
        result = provider.extract_text(
            pages, system_prompt, extraction_prompt, max_tokens=max_tokens,
        )
        last_result = result
        if result.success:
            if last_result is not result and len(providers) > 1:
                logger.info(
                    "Vision OCR fallback succeeded via %s/%s after primary failed",
                    result.provider, result.model_name,
                )
            return result
        # Failure — log and try next provider
        logger.warning(
            "Vision OCR provider %s/%s failed (%s): %s — trying next",
            result.provider, result.model_name,
            result.error_reason, result.error_detail[:200],
        )

    return last_result
