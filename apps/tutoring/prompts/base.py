"""Base classes for provider-specific tutor prompt builders.

The tutor system prompt has historically been one Anthropic-shaped
XML string interpolated by `_build_system_prompt` in
`apps.tutoring.conversational_tutor`. Phase 1 of task #229 (see
`memory/provider_specific_prompt_system_plan.md`) extracts the
stable prefix into a per-provider builder so we can ship parallel
Gemini and OpenAI prompt shapes without two prompts colliding in
one file.

This module defines:

- `StablePrefixContext` — the runtime data the stable prefix needs
  (institution, tutor identity, grade, locale, safety overrides).
  Per-turn dynamic blocks (figure_facts, bank_grade, scaffolding
  directive, etc.) stay in `conversational_tutor.py` and are
  appended AFTER the builder returns.

- `TutorPromptBuilder` — ABC. Subclasses implement
  `build_stable_prefix(ctx, prompt_pack_override=None) -> str`.

Per-provider builders live in sibling modules (`anthropic.py`,
later `gemini.py`, `openai.py`). The `get_prompt_builder(provider)`
dispatcher lives in `__init__.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StablePrefixContext:
    """Inputs needed by every tutor prompt builder to produce the
    stable per-session prefix.

    Frozen so a context can be passed around freely without callers
    mutating shared state.
    """

    institution_name: str
    locale_context: str
    tutor_name: str
    language: str
    grade_level: str
    safety_prompt: str


class TutorPromptBuilder(ABC):
    """Provider-specific tutor prompt builder.

    Implementations shape the stable prefix natively for their
    provider — XML+ for Anthropic, markdown+system_instruction for
    Gemini, developer-role+markdown for OpenAI.

    The per-turn dynamic suffix (figure_facts, bank_grade, scaffolding
    directive, etc.) is NOT this builder's responsibility — it's
    appended by the caller after this method returns, so providers
    don't have to re-implement the per-turn signals.
    """

    @abstractmethod
    def build_stable_prefix(
        self,
        ctx: StablePrefixContext,
        prompt_pack_override: Optional[str] = None,
    ) -> str:
        """Return the stable per-session prefix.

        Args:
            ctx: Interpolation data — names, locale, grade, safety.
            prompt_pack_override: When non-empty, use this string in
                place of the provider's default template. PromptPack
                lets institutions override the tutor system prompt;
                that override is provider-agnostic raw text and is
                passed through after light interpolation.

        Returns:
            The stable prefix as a string. Caller appends per-turn
            dynamic blocks (figure_facts, bank_grade, scaffolding
            directive) AFTER this. For Anthropic, the
            `CACHE_BREAK_MARKER` is inserted by the caller between
            the prefix and the dynamic suffix.
        """
        raise NotImplementedError
