"""Provider-specific tutor prompt builders.

Phase 1 of task #229 (see
`memory/provider_specific_prompt_system_plan.md`). The dispatcher
returns the right `TutorPromptBuilder` for a given provider so the
tutor engine can stay provider-agnostic at the call site.

Current registry:
  - "anthropic" → AnthropicTutorPromptBuilder (Claude Opus/Sonnet/Haiku)

Phase 2 will add "google" → GeminiTutorPromptBuilder.
Phase 3 will add "openai" → OpenAITutorPromptBuilder.
"""

from __future__ import annotations

from .anthropic import (
    AnthropicTutorPromptBuilder,
    TUTOR_SYSTEM_PROMPT_TEMPLATE,
)
from .base import StablePrefixContext, TutorPromptBuilder
from .gemini import (
    GEMINI_TUTOR_SYSTEM_PROMPT_TEMPLATE,
    GeminiTutorPromptBuilder,
)
from .openai import (
    OPENAI_TUTOR_SYSTEM_PROMPT_TEMPLATE,
    OpenAITutorPromptBuilder,
)


_REGISTRY: dict[str, type[TutorPromptBuilder]] = {
    "anthropic": AnthropicTutorPromptBuilder,
    "google": GeminiTutorPromptBuilder,
    "gemini": GeminiTutorPromptBuilder,  # alias — ModelConfig.provider uses 'google'
    "openai": OpenAITutorPromptBuilder,
}


def get_prompt_builder(provider: str) -> TutorPromptBuilder:
    """Return the tutor prompt builder for the given provider.

    Unknown providers fall back to Anthropic so existing call sites
    that pass an unfamiliar provider name (e.g. local Ollama models
    in dev) keep working. This is a Phase-1 compatibility fallback;
    once Gemini + OpenAI builders ship, callers should always be
    able to resolve a real provider.
    """
    key = (provider or "").strip().lower()
    cls = _REGISTRY.get(key, AnthropicTutorPromptBuilder)
    return cls()


__all__ = [
    "StablePrefixContext",
    "TutorPromptBuilder",
    "AnthropicTutorPromptBuilder",
    "GeminiTutorPromptBuilder",
    "OpenAITutorPromptBuilder",
    "TUTOR_SYSTEM_PROMPT_TEMPLATE",
    "GEMINI_TUTOR_SYSTEM_PROMPT_TEMPLATE",
    "OPENAI_TUTOR_SYSTEM_PROMPT_TEMPLATE",
    "get_prompt_builder",
]
