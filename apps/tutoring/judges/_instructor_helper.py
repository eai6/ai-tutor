"""Shared helper for wrapping a tutor-side BaseLLMClient with instructor.

Tutor judges receive an already-constructed `llm_client` instance from
the orchestrator (`apps/tutoring/judges/__init__.py::run_all_judges`)
rather than looking up their own ModelConfig. This module wraps that
client in `instructor.from_provider(...)` so judges can request typed
Pydantic output and avoid the fragile `json.loads + regex` path.

Mirrors the content-judge helper in
`apps/curriculum/content_judges/_providers.py::_get_instructor_client_for`
but takes a BaseLLMClient (not a ModelConfig) since that's what tutor
judges hold.

See `auto-memory/feedback_use_instructor_for_structured_output.md` for
the broader rule + Gemini-3 token-budget pitfall.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

logger = logging.getLogger(__name__)


# Same mapping as content-side helper; instructor uses this prefix in
# `from_provider("<prefix>/<model>")`.
_PROVIDER_INSTRUCTOR_MAP = {
    'anthropic': 'anthropic',
    'openai': 'openai',
    'google': 'google',
    'local_ollama': 'ollama',
}


def get_instructor_from_client(llm_client) -> Optional[Any]:
    """Wrap a BaseLLMClient's underlying provider in instructor.

    Returns None when:
      - llm_client is None or has no .config attribute
      - the instructor package isn't installed
      - instructor.from_provider raises (unsupported provider, etc.)

    Caller falls back to skip-with-skip_reason — judges never block
    the tutor turn on infrastructure failure.
    """
    if llm_client is None:
        return None
    config = getattr(llm_client, 'config', None)
    if config is None:
        return None
    try:
        import instructor
    except ImportError:
        logger.warning(
            "instructor package not installed — tutor judges fall back "
            "to raw JSON parsing (which is less reliable)"
        )
        return None
    provider_key = _PROVIDER_INSTRUCTOR_MAP.get(
        str(config.provider).lower(), str(config.provider).lower()
    )
    try:
        return instructor.from_provider(
            f"{provider_key}/{config.model_name}",
            api_key=config.get_api_key(),
        )
    except Exception as exc:
        logger.warning(
            f"instructor.from_provider({provider_key}/"
            f"{config.model_name}) failed: {type(exc).__name__}: {exc}"
        )
        return None


def structured_completion(
    instructor_client,
    response_model: Type,
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 3500,
    max_retries: int = 2,
    provider: str = "",
):
    """Issue a structured-output chat completion.

    Provider-shaped kwargs: Google wants `generation_config={'max_tokens': N}`,
    others want `max_tokens=N`. `provider` should match
    `ModelConfig.provider` (lowercase string); when empty, defaults to
    the non-Google shape.

    `max_tokens=3500` default is Gemini-3 safe — see
    auto-memory/feedback_use_instructor_for_structured_output.md for
    why (Gemini 3 burns 1500-2500 tokens on internal thinking before
    emitting the function call; lower caps truncate with MAX_TOKENS).
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    create_kwargs: Dict[str, Any] = dict(
        response_model=response_model,
        messages=messages,
        max_retries=max_retries,
    )
    if str(provider).lower() == 'google':
        create_kwargs['generation_config'] = {'max_tokens': max_tokens}
    else:
        create_kwargs['max_tokens'] = max_tokens
    return instructor_client.chat.completions.create(**create_kwargs)


__all__ = ["get_instructor_from_client", "structured_completion"]
