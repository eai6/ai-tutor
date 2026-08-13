"""Multi-provider chain + per-call timeout for content-generation LLM calls.

Mirror of `apps/curriculum/content_judges/_providers.py` but for
GENERATION calls (regen + content gen) — the judge chain already
handled fallback for verification; this closes the same gap on the
generation side.

The problem this solves: when the generation ModelConfig points at
a single provider (e.g. Gemini 3 Pro) and that provider stalls /
errors out, the entire content-gen pipeline hangs indefinitely.
Lesson 549 run 10 demonstrated this — Gemini hung on a regen call
for 10+ minutes with CPU 0% before the process was killed.

Two layers:
  - `get_gen_provider_chain()` — returns ordered list of providers
    to try, with instructor-wrapped clients pre-built. Distinct
    providers (one per provider), supports exclude_provider for
    cross-provider review.
  - `call_gen_structured_with_fallback()` — tries each provider in
    order with per-provider timeout (via concurrent.futures). First
    success wins; logs failures + falls through.

Per-provider timeout uses `concurrent.futures.ThreadPoolExecutor` +
`.result(timeout=N)` rather than provider-SDK timeouts because each
SDK has a different timeout mechanism (anthropic.Anthropic(timeout=),
OpenAI(timeout=), google-genai uses request-level options). A
timeout-cancelled future may still consume the upstream API quota
for the in-flight call, but that's acceptable vs. hanging forever.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Type

logger = logging.getLogger(__name__)


# Generation-side fallback purposes consulted (in order) when
# building the chain. The requested purpose comes first, then we
# fall through to other gen-capable purposes to find distinct
# providers.
_FALLBACK_PURPOSES = (
    'generation', 'judge', 'judge_fallback', 'tutoring',
    'exit_tickets', 'image_prompt',
)


_PROVIDER_INSTRUCTOR_MAP = {
    'anthropic': 'anthropic',
    'openai': 'openai',
    'google': 'google',
    'local_ollama': 'ollama',
}


@dataclass
class GenProvider:
    """One generation provider in the fallback chain."""
    name: str            # 'google' / 'anthropic' / 'openai'
    model_name: str      # 'gemini-3.1-pro-preview' / etc.
    instructor_client: Any  # instructor-wrapped client
    config: Any          # ModelConfig instance


@dataclass
class GenCallResult:
    """Outcome of one provider's attempt at a structured-output call."""
    verdict: Any = None       # Validated Pydantic instance, or None
    success: bool = False
    provider: str = ""
    model_name: str = ""
    error_class: str = ""
    error_detail: str = ""


def _build_instructor_client(config) -> Optional[Any]:
    """Wrap a ModelConfig in instructor. Returns None on failure."""
    try:
        import instructor
    except ImportError:
        logger.warning(
            "instructor not installed — content-gen fallback chain disabled"
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
            f"instructor.from_provider({provider_key}/{config.model_name}) "
            f"failed: {type(exc).__name__}: {exc}"
        )
        return None


def get_gen_provider_chain(
    purpose: str = 'generation',
    *,
    exclude_provider: Optional[str] = None,
) -> List[GenProvider]:
    """Return ordered list of DISTINCT providers to try for a gen call.

    Picks active ModelConfigs in `purpose` first, then falls through
    to other generation-capable purposes to find DISTINCT providers
    so a same-vendor outage doesn't kill the whole chain.

    Args:
        purpose: ModelConfig.purpose to consult first.
        exclude_provider: When set, drops any config whose provider
            matches. Used for cross-provider review.

    Returns:
        Ordered list of GenProvider. May be empty if no providers
        survive — callers should handle that.
    """
    from ai_tutor.apps.llm.models import ModelConfig

    seen_providers = set()
    chain: List[GenProvider] = []

    purposes_to_try = [purpose] + [
        p for p in _FALLBACK_PURPOSES if p != purpose
    ]

    for p in purposes_to_try:
        try:
            config = ModelConfig.get_for(p)
        except Exception as exc:
            logger.debug(f"ModelConfig.get_for({p!r}) raised: {exc}")
            continue
        if not config:
            continue
        provider_name = str(config.provider)
        if provider_name in seen_providers:
            continue
        if exclude_provider and provider_name == exclude_provider:
            continue
        ic = _build_instructor_client(config)
        if ic is None:
            continue
        seen_providers.add(provider_name)
        chain.append(GenProvider(
            name=provider_name,
            model_name=config.model_name or '',
            instructor_client=ic,
            config=config,
        ))

    return chain


def call_gen_structured_with_fallback(
    providers: List[GenProvider],
    response_model: Type,
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4000,
    max_retries: int = 2,
    timeout_seconds: float = 120.0,
) -> GenCallResult:
    """Try each provider in order with per-provider timeout.

    Returns first success. Never raises — failures land in the
    returned GenCallResult so the caller can decide whether to fall
    through to a legacy path or skip.

    Per-provider timeout via concurrent.futures: a hung LLM call
    (Gemini stalling on a complex regen, network black-hole)
    triggers TimeoutError after `timeout_seconds`, the future is
    abandoned, and the chain moves on. The orphaned upstream call
    may still consume quota but that's acceptable vs. hanging the
    pipeline forever.

    Args:
        providers: Ordered list of GenProvider from
            get_gen_provider_chain.
        response_model: Pydantic schema for the expected output.
        system_prompt: System-message content.
        user_prompt: User-message content.
        max_tokens: Output token cap. Gemini-safe default 4000.
        max_retries: Instructor schema-violation retry budget per
            provider call. (Separate from the chain fallback.)
        timeout_seconds: Per-provider wall-time cap. Default 120s
            covers typical Gemini thinking + structured output on
            complex prompts; bump for very large outputs.

    Returns:
        GenCallResult. On success, `verdict` is a validated instance
        of `response_model`.
    """
    if not providers:
        return GenCallResult(
            success=False,
            provider="(no-providers)",
            error_class="NoProviders",
            error_detail="get_gen_provider_chain returned empty",
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_result: Optional[GenCallResult] = None

    for provider in providers:
        create_kwargs = dict(
            response_model=response_model,
            messages=messages,
            max_retries=max_retries,
        )
        if str(provider.config.provider).lower() == 'google':
            create_kwargs['generation_config'] = {'max_tokens': max_tokens}
        else:
            create_kwargs['max_tokens'] = max_tokens

        # ThreadPoolExecutor lets us enforce a hard wall-time cap.
        # The underlying instructor / SDK call doesn't expose a
        # consistent timeout knob across providers; futures + .result(
        # timeout=) is the portable escape hatch.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                provider.instructor_client.chat.completions.create,
                **create_kwargs,
            )
            try:
                verdict = future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError:
                last_result = GenCallResult(
                    success=False,
                    provider=provider.name,
                    model_name=provider.model_name,
                    error_class="Timeout",
                    error_detail=f"hit {timeout_seconds:.0f}s wall-time cap",
                )
                logger.warning(
                    f"[GenChain] {provider.name}/{provider.model_name} "
                    f"timed out after {timeout_seconds:.0f}s — "
                    f"trying next provider"
                )
                # Cancel the future so the worker thread doesn't hang
                # the executor's shutdown. The underlying API call
                # may still be in flight (no client-side cancel) but
                # the orchestration moves on.
                future.cancel()
                continue
            except Exception as exc:
                last_result = GenCallResult(
                    success=False,
                    provider=provider.name,
                    model_name=provider.model_name,
                    error_class=type(exc).__name__,
                    error_detail=str(exc)[:300],
                )
                logger.warning(
                    f"[GenChain] {provider.name}/{provider.model_name} "
                    f"call failed: {type(exc).__name__}: "
                    f"{str(exc)[:150]} — trying next"
                )
                continue

        return GenCallResult(
            verdict=verdict,
            success=True,
            provider=provider.name,
            model_name=provider.model_name,
        )

    return last_result if last_result else GenCallResult(
        success=False, provider="(unknown)",
    )


@dataclass
class GenTextResult:
    """Outcome of a plain-text gen call (no structured schema)."""
    text: str = ""
    success: bool = False
    provider: str = ""
    model_name: str = ""
    error_class: str = ""
    error_detail: str = ""


def call_gen_text_with_fallback(
    providers: List[GenProvider],
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
    temperature: Optional[float] = None,
    timeout_seconds: float = 90.0,
) -> GenTextResult:
    """Plain-text counterpart to `call_gen_structured_with_fallback`.

    Some regen paths (text-only step rewrite, teacher-prompt-driven
    rewrites) need raw text back rather than a Pydantic instance.
    This helper uses the same provider chain + timeout shape but
    calls each provider's underlying BaseLLMClient.generate()
    directly (instructor isn't needed when there's no schema to
    validate against).

    Returns first success, or last error if every provider failed.
    """
    if not providers:
        return GenTextResult(
            success=False,
            provider="(no-providers)",
            error_class="NoProviders",
            error_detail="get_gen_provider_chain returned empty",
        )

    from ai_tutor.apps.llm.client import get_llm_client

    last_result: Optional[GenTextResult] = None

    for provider in providers:
        try:
            client = get_llm_client(provider.config)
        except Exception as exc:
            last_result = GenTextResult(
                success=False,
                provider=provider.name,
                model_name=provider.model_name,
                error_class=type(exc).__name__,
                error_detail=str(exc)[:300],
            )
            continue

        gen_kwargs = dict(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        if temperature is not None:
            gen_kwargs['temperature'] = temperature

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(client.generate, **gen_kwargs)
            try:
                response = future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError:
                last_result = GenTextResult(
                    success=False,
                    provider=provider.name,
                    model_name=provider.model_name,
                    error_class="Timeout",
                    error_detail=f"hit {timeout_seconds:.0f}s wall-time cap",
                )
                logger.warning(
                    f"[GenChain/text] {provider.name}/{provider.model_name} "
                    f"timed out after {timeout_seconds:.0f}s — trying next"
                )
                future.cancel()
                continue
            except Exception as exc:
                last_result = GenTextResult(
                    success=False,
                    provider=provider.name,
                    model_name=provider.model_name,
                    error_class=type(exc).__name__,
                    error_detail=str(exc)[:300],
                )
                logger.warning(
                    f"[GenChain/text] {provider.name}/{provider.model_name} "
                    f"call failed: {type(exc).__name__}: {str(exc)[:150]} "
                    f"— trying next"
                )
                continue

        text = (getattr(response, 'content', None) or '').strip()
        if not text:
            last_result = GenTextResult(
                success=False,
                provider=provider.name,
                model_name=provider.model_name,
                error_class="EmptyResponse",
                error_detail="provider returned no text",
            )
            continue

        return GenTextResult(
            text=text,
            success=True,
            provider=provider.name,
            model_name=provider.model_name,
        )

    return last_result if last_result else GenTextResult(
        success=False, provider="(unknown)",
    )


__all__ = [
    "GenProvider",
    "GenCallResult",
    "GenTextResult",
    "get_gen_provider_chain",
    "call_gen_structured_with_fallback",
    "call_gen_text_with_fallback",
]
