"""Multi-provider chain for content judges.

Mirrors `apps/curriculum/vision_ocr.py::get_vision_provider_chain` but
specialised for content-judge usage:
  - Per-judge ModelConfig purpose (e.g. `content_judge_image_prompt`)
  - Cross-provider enforcement: optional `exclude_provider` filter so a
    judge never runs on the SAME provider that generated the artefact
    (image gen by OpenAI → judge can't be OpenAI). Reduces same-model
    self-confirmation bias.
  - Falls back through the active configured purposes
    (generation → judge → tutoring) to find usable providers when the
    judge-specific purpose isn't configured.

Public:
  - `JudgeProvider` — wraps a (config, client) pair with `name` +
    `model_name` for audit
  - `get_judge_provider_chain(judge_purpose, exclude_provider=None)` —
    returns ordered list of distinct providers to try
  - `call_judge_with_fallback(prompt, providers, system_prompt, ...)` —
    tries each provider in order, returns first success or last failure
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional


logger = logging.getLogger(__name__)


# Fallback purposes consulted (in order) when building a judge's
# provider chain. The judge-specific purpose comes first, then we
# fall through to general-purpose configs to find any provider with
# vision-LLM access.
_FALLBACK_PURPOSES = ('generation', 'judge', 'tutoring', 'exit_tickets')


@dataclass
class JudgeProvider:
    """One provider entry in a judge's fallback chain."""
    name: str            # 'google' / 'anthropic' / 'openai'
    model_name: str      # 'gemini-3.1-pro-preview' / 'claude-sonnet-4-20250514' / etc.
    client: object       # BaseLLMClient instance
    config: object       # ModelConfig instance (for telemetry / purpose attribution)


@dataclass
class JudgeCallResult:
    """Outcome of one provider's attempt to run a judge."""
    text: str = ""
    success: bool = False
    provider: str = ""
    model_name: str = ""
    error_class: str = ""
    error_detail: str = ""


def get_judge_provider_chain(
    judge_purpose: str,
    *,
    exclude_provider: Optional[str] = None,
) -> List[JudgeProvider]:
    """Return ordered list of providers to try for a judge.

    Picks DISTINCT providers from active ModelConfigs so a same-vendor
    outage doesn't kill the whole judge stack.

    Args:
        judge_purpose: ModelConfig.purpose to consult first (e.g.
            `content_judge_image_prompt`). When that purpose has no
            active config, falls through to (generation, judge,
            tutoring, exit_tickets) and picks distinct providers.
        exclude_provider: When set, drops any config whose provider
            matches. Used to enforce cross-provider review — pass the
            generator's provider (e.g. 'openai' for images generated
            by OpenAI) so the judge can't be the same model that
            produced the artefact.

    Returns:
        Ordered list of JudgeProvider. May be empty if no providers
        survive the exclusion filter — callers should handle that.
    """
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client

    seen_providers = set()
    chain: List[JudgeProvider] = []

    # Per-judge-purpose first
    purposes_to_try = [judge_purpose]
    # Then the fallback purposes (skip the judge_purpose if it's
    # already in there to avoid double-trying)
    for p in _FALLBACK_PURPOSES:
        if p != judge_purpose:
            purposes_to_try.append(p)

    for purpose in purposes_to_try:
        try:
            config = ModelConfig.get_for(purpose)
        except Exception as exc:
            logger.debug(f"ModelConfig.get_for({purpose!r}) raised: {exc}")
            continue
        if not config:
            continue
        provider_name = str(config.provider)
        if provider_name in seen_providers:
            continue
        if exclude_provider and provider_name == exclude_provider:
            continue
        try:
            client = get_llm_client(config)
        except Exception as exc:
            logger.warning(
                f"Could not instantiate judge client for purpose={purpose} "
                f"provider={provider_name}: {exc}"
            )
            continue
        seen_providers.add(provider_name)
        chain.append(JudgeProvider(
            name=provider_name,
            model_name=config.model_name or '',
            client=client,
            config=config,
        ))

    return chain


def call_judge_with_fallback(
    user_prompt: str,
    providers: List[JudgeProvider],
    *,
    system_prompt: str = "",
    max_tokens: int = 2048,
) -> JudgeCallResult:
    """Try each provider in order. Return first success.

    Used by individual judges so each judge gets the same fallback
    behaviour without duplicating the loop. Never raises — failures
    are returned in `JudgeCallResult` so the caller can decide whether
    to skip-with-skip_reason or fail-loud.
    """
    if not providers:
        return JudgeCallResult(
            success=False,
            provider="(no-providers)",
            error_class="NoProviders",
            error_detail="get_judge_provider_chain returned empty",
        )

    last_result: Optional[JudgeCallResult] = None
    messages = [{"role": "user", "content": user_prompt}]

    for provider in providers:
        try:
            response = provider.client.generate(
                messages=messages,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            last_result = JudgeCallResult(
                text="",
                success=False,
                provider=provider.name,
                model_name=provider.model_name,
                error_class=type(exc).__name__,
                error_detail=str(exc)[:300],
            )
            logger.warning(
                f"Judge call failed via {provider.name}/{provider.model_name}: "
                f"{type(exc).__name__}: {str(exc)[:150]} — trying next"
            )
            continue

        text = (getattr(response, 'content', None) or '').strip()
        if not text:
            last_result = JudgeCallResult(
                text="",
                success=False,
                provider=provider.name,
                model_name=provider.model_name,
                error_class="EmptyResponse",
                error_detail="provider returned no text",
            )
            continue

        return JudgeCallResult(
            text=text,
            success=True,
            provider=provider.name,
            model_name=provider.model_name,
        )

    return last_result if last_result else JudgeCallResult(
        success=False, provider="(unknown)",
    )


# ─── Structured-output (instructor-backed) sibling ──────────────────────
@dataclass
class JudgeStructuredResult:
    """Outcome of a structured-output judge call. `verdict` is the
    validated Pydantic instance (typed via `response_model`)."""
    verdict: object = None       # response_model instance, or None on failure
    success: bool = False
    provider: str = ""
    model_name: str = ""
    error_class: str = ""
    error_detail: str = ""


# Maps `ModelConfig.provider` (DB enum) to the prefix instructor expects
# in `from_provider("<prefix>/<model>")`. Same mapping used in
# apps/curriculum/pipeline.py::_get_instructor_client + grader.py.
_PROVIDER_INSTRUCTOR_MAP = {
    'anthropic': 'anthropic',
    'openai': 'openai',
    'google': 'google',
    'local_ollama': 'ollama',
}


def _get_instructor_client_for(config):
    """Wrap a ModelConfig in an instructor-augmented client.

    Returns None when instructor / the provider SDK isn't installed.
    Caller falls back to next provider in chain.
    """
    try:
        import instructor
    except ImportError:
        logger.warning("instructor package not installed — structured output unavailable")
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
            f"instructor.from_provider({provider_key}/{config.model_name}) "
            f"failed: {type(exc).__name__}: {exc}"
        )
        return None


def call_judge_structured_with_fallback(
    user_prompt: str,
    providers: List[JudgeProvider],
    response_model,
    *,
    system_prompt: str = "",
    max_tokens: int = 2048,
    max_retries: int = 1,
) -> JudgeStructuredResult:
    """Structured-output sibling of `call_judge_with_fallback`.

    Wraps each provider's underlying client in `instructor` and calls
    `chat.completions.create(response_model=...)` so the model output
    is validated against the Pydantic schema before return. Eliminates
    the fragile `json.loads` + regex-repair codepath.

    Provider-side failures (no instructor, schema-violation retries
    exhausted, network error) fall through to the next provider in the
    chain. Never raises — failures land in `JudgeStructuredResult` with
    `success=False`.

    Args:
        user_prompt: The user-message content (rendered prompt body).
        providers: Ordered list of JudgeProvider candidates.
        response_model: Pydantic model class for the expected output.
        system_prompt: System-message content.
        max_tokens: Output token cap.
        max_retries: Instructor's schema-violation retry budget per
            provider call. Cycle cap is separate (per provider).

    Returns:
        JudgeStructuredResult. On success, `verdict` is a validated
        instance of `response_model`.
    """
    if not providers:
        return JudgeStructuredResult(
            success=False,
            provider="(no-providers)",
            error_class="NoProviders",
            error_detail="get_judge_provider_chain returned empty",
        )

    last_result: Optional[JudgeStructuredResult] = None
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ] if system_prompt else [{"role": "user", "content": user_prompt}]

    for provider in providers:
        client = _get_instructor_client_for(provider.config)
        if client is None:
            last_result = JudgeStructuredResult(
                success=False,
                provider=provider.name,
                model_name=provider.model_name,
                error_class="InstructorUnavailable",
                error_detail="instructor.from_provider returned None",
            )
            continue

        # Provider-specific kwarg shape — Google wants
        # `generation_config={'max_tokens': N}`, others want
        # `max_tokens=N`. Mirrors pipeline.py::_get_instructor_client.
        create_kwargs = dict(
            response_model=response_model,
            messages=messages,
            max_retries=max_retries,
        )
        if str(provider.config.provider).lower() == 'google':
            create_kwargs['generation_config'] = {'max_tokens': max_tokens}
        else:
            create_kwargs['max_tokens'] = max_tokens

        try:
            verdict = client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            last_result = JudgeStructuredResult(
                success=False,
                provider=provider.name,
                model_name=provider.model_name,
                error_class=type(exc).__name__,
                error_detail=str(exc)[:300],
            )
            logger.warning(
                f"Structured judge call failed via {provider.name}/"
                f"{provider.model_name}: {type(exc).__name__}: "
                f"{str(exc)[:150]} — trying next"
            )
            continue

        return JudgeStructuredResult(
            verdict=verdict,
            success=True,
            provider=provider.name,
            model_name=provider.model_name,
        )

    return last_result if last_result else JudgeStructuredResult(
        success=False, provider="(unknown)",
    )
