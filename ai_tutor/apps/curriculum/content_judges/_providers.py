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
#
# `judge_fallback` and `judge_fallback_2` are dedicated purposes for
# tier-2 and tier-3 cross-vendor fallbacks. With judge=Gemini,
# judge_fallback=OpenAI, judge_fallback_2=Haiku, the chain resolves to
# [Gemini, OpenAI, Haiku] — full 3-vendor judge fallback regardless of
# what generation/tutoring happen to be set to. Placing judge_fallback_2
# BEFORE `generation` is important: it claims the Anthropic provider
# slot for Haiku so the chain doesn't drift to whatever Claude model
# generation is configured with (currently Sonnet 4).
_FALLBACK_PURPOSES = (
    'judge', 'judge_fallback', 'judge_fallback_2',
    'generation', 'tutoring', 'exit_tickets',
)


def _local_judge_provider(exclude_provider: Optional[str] = None):
    """Single-provider judge chain pinned to the local Ollama model, or None.

    WHY: on an offline box (the Jetson) the tutor runs on local Ollama while
    the judge chain still resolved to Gemini → OpenAI → Haiku, so every judged
    turn needed the network and spent API credit. A run that is local for the
    tutor should be local end-to-end.

    Trigger: ``JUDGE_MODEL_OVERRIDE`` when set, else ``TUTOR_MODEL_OVERRIDE``
    when it names a ``local_ollama/`` spec. Set ``JUDGE_MODEL_OVERRIDE=off`` to
    keep the cloud cascade while the tutor stays local.

    The judge deliberately defaults to the SAME tag as the tutor rather than a
    dedicated judge model. Ollama serves one model at a time under
    ``OLLAMA_MAX_LOADED_MODELS=1``, so two different local tags would unload
    and reload between the tutor call and the judge call on EVERY turn —
    measured 30-45 s per load on the Orin, each one re-running the fit
    preflight. Sharing the tag keeps it resident.

    Purpose is forced to JUDGE so ``ModelConfig.effective_temperature`` pins
    temperature to 0. That matters here specifically: the rows seeded by
    ``offline_eval/seed_ollama_configs.py`` carry ``purpose='tutoring'``, which
    would clamp to [0.1, 0.3] and quietly break the judge-consistency
    invariant. The config is built unsaved rather than by mutating the seeded
    row — nothing here should touch the database.

    Fail-soft: anything unresolvable logs a warning and returns None, so the
    caller falls through to the normal cloud chain.
    """
    import os

    spec = (os.environ.get('JUDGE_MODEL_OVERRIDE') or '').strip()
    if spec.lower() in ('off', 'none', 'cloud'):
        return None
    if not spec:
        tutor_spec = (os.environ.get('TUTOR_MODEL_OVERRIDE') or '').strip()
        if not tutor_spec.startswith('local_ollama/'):
            return None
        spec = tutor_spec

    provider, _, model_name = spec.partition('/')
    provider, model_name = provider.strip().lower(), model_name.strip()
    if not provider or not model_name:
        logger.warning("[LocalJudge] unparseable model spec %r — ignoring", spec)
        return None
    if exclude_provider and provider == exclude_provider:
        # Cross-provider review is a correctness requirement for the content
        # judges (a judge must not be the model that produced the artefact).
        # Honour it rather than silently defeating it to stay local.
        return None

    from ai_tutor.apps.llm.models import ModelConfig
    from ai_tutor.apps.llm.client import get_llm_client

    try:
        institution = None
        try:
            from ai_tutor.apps.accounts.models import Institution
            institution = Institution.get_global()
        except Exception:
            pass
        config = ModelConfig(
            institution=institution,
            name=f"Local judge — {provider}/{model_name}",
            provider=provider,
            model_name=model_name,
            api_key_env_var='',
            api_base=('http://localhost:11434' if provider == 'local_ollama' else ''),
            api_key_encrypted='',
            max_tokens=2048,
            temperature=0.0,
            purpose=ModelConfig.Purpose.JUDGE,
            is_active=False,
        )
        client = get_llm_client(config)
    except Exception as exc:
        logger.warning(
            "[LocalJudge] could not build local judge client for %s: %s — "
            "falling back to the cloud chain", spec, exc,
        )
        return None

    logger.info("[LocalJudge] judging on %s (temperature %s)",
                spec, config.effective_temperature)
    return JudgeProvider(
        name=provider, model_name=model_name, client=client, config=config,
    )


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
    # Token + cache telemetry (populated on success, when provider
    # exposes them). Used by unified.py to surface cache-hit rates
    # in the [UnifiedJudge] log line.
    tokens_in: int = 0
    tokens_out: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


def get_judge_provider_chain(
    judge_purpose: str,
    *,
    exclude_provider: Optional[str] = None,
    force_model_config=None,
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
        force_model_config: Optional ModelConfig (saved or in-memory)
            to use as the SINGLE provider for this run. When set,
            short-circuits the purpose lookup + the exclude_provider
            filter and returns a one-item chain. Used by the dashboard
            "Run AI review" picker (task #216) so a teacher can pick
            "use Opus for this run" without DB edits.

    Returns:
        Ordered list of JudgeProvider. May be empty if no providers
        survive the exclusion filter — callers should handle that.
    """
    from ai_tutor.apps.llm.models import ModelConfig
    from ai_tutor.apps.llm.client import get_llm_client

    # Forced override — bypass the purpose chain entirely. Used by
    # the dashboard provider picker (task #216).
    if force_model_config is not None:
        try:
            client = get_llm_client(force_model_config)
        except Exception as exc:
            logger.warning(
                "Could not instantiate forced judge client "
                "provider=%s model=%s: %s",
                force_model_config.provider,
                force_model_config.model_name,
                exc,
            )
            return []
        return [JudgeProvider(
            name=str(force_model_config.provider or ''),
            model_name=force_model_config.model_name or '',
            client=client,
            config=force_model_config,
        )]

    # Local-only run: judge on the same local model the tutor is using rather
    # than reaching for the cloud cascade. Placed AFTER force_model_config so an
    # explicit per-run pick still wins, and BEFORE the purpose walk so it
    # short-circuits every judge. Returns a one-item chain deliberately — there
    # is no second local vendor to fall back to, and silently falling through to
    # Gemini would defeat the point of running offline.
    local = _local_judge_provider(exclude_provider)
    if local is not None:
        return [local]

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
            tokens_in=getattr(response, 'tokens_in', 0) or 0,
            tokens_out=getattr(response, 'tokens_out', 0) or 0,
            cache_creation_tokens=getattr(response, 'cache_creation_tokens', 0) or 0,
            cache_read_tokens=getattr(response, 'cache_read_tokens', 0) or 0,
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


# Settings flag that gates Gemini search grounding on factual judges.
# Default True; can be disabled if cost / quota becomes a concern.
def _grounding_enabled() -> bool:
    try:
        from django.conf import settings
        return bool(getattr(
            settings, 'CONTENT_JUDGE_GEMINI_GROUNDING_ENABLED', True,
        ))
    except Exception:
        return True


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


# ─── Two-call Gemini grounding pattern ──────────────────────────────────
# Gemini API treats `responseSchema` (structured output) and
# `tools=[google_search]` (search grounding) as MUTUALLY EXCLUSIVE in
# the same request. To get both rigour mechanisms on factual judges:
#   Call 1: Gemini with google_search tool, free-form output → reasoning
#           grounded in live web search.
#   Call 2: instructor structured output that translates the grounded
#           reasoning into the typed Pydantic verdict. No new reasoning
#           in this call — just format conversion.
#
# Cost: ~2× the LLM calls per Gemini-judged claim. Worth it for
# factual judges (factual_step, exit_question, fill_in_blank,
# short_answer, matching, figure_alignment) where catching
# fabricated facts protects students. Skip on pedagogy / safety /
# image_prompt judges — those check structural properties, not
# truthfulness, so grounding adds no value.
#
# Per project_model_routing memory: Gemini-with-grounding is the
# intended primary for content judges. The grounding-fallback wires
# into the standard chain so when Gemini is picked, this path
# fires; otherwise the regular `call_judge_structured_with_fallback`
# applies.


def call_judge_grounded_then_structured(
    user_prompt: str,
    providers: List[JudgeProvider],
    response_model,
    *,
    system_prompt: str = "",
    max_tokens: int = 3500,
    max_retries: int = 1,
    grounding_instruction: str = (
        "Use Google Search to verify any factual claim that could be "
        "checked against authoritative sources (numbers, dates, names "
        "of people / places / institutions). Cite the sources you used."
    ),
) -> JudgeStructuredResult:
    """Two-call Gemini-grounded structured judge.

    Iterates the provider chain. When the picked provider is Google,
    uses the 2-call pattern (grounded free-form → structured format).
    For non-Google providers (Anthropic, OpenAI), falls through to
    the standard structured-output path.

    Returns the same `JudgeStructuredResult` shape as the
    non-grounded sibling so callers can swap freely.
    """
    if not providers:
        return JudgeStructuredResult(
            success=False,
            provider="(no-providers)",
            error_class="NoProviders",
            error_detail="get_judge_provider_chain returned empty",
        )

    last_result: Optional[JudgeStructuredResult] = None

    for provider in providers:
        provider_name = str(provider.config.provider).lower()

        if provider_name == 'google':
            # Two-call pattern. Call 1 uses google-genai SDK directly
            # (instructor doesn't expose tools=[google_search] cleanly,
            # and grounding requires the native config shape).
            grounded_text, gr_error = _call_gemini_grounded(
                provider.config, system_prompt, user_prompt,
                grounding_instruction, max_tokens,
            )
            if gr_error:
                last_result = JudgeStructuredResult(
                    success=False,
                    provider=provider.name,
                    model_name=provider.model_name,
                    error_class=gr_error[0],
                    error_detail=gr_error[1],
                )
                logger.warning(
                    f"[GroundedJudge] {provider.name}/{provider.model_name} "
                    f"call-1 (grounded) failed: {gr_error[0]}: "
                    f"{gr_error[1][:150]} — trying next"
                )
                continue
            if not grounded_text:
                last_result = JudgeStructuredResult(
                    success=False,
                    provider=provider.name,
                    model_name=provider.model_name,
                    error_class="EmptyGroundedResponse",
                    error_detail="Gemini returned empty grounded text",
                )
                continue

            # Call 2: format the grounded reasoning into the Pydantic
            # schema. Use the SAME provider+model (no new reasoning, just
            # extraction) so we stay on Gemini's quality but use
            # structured output (which can't coexist with grounding in
            # one call).
            format_user_prompt = (
                f"FACT-CHECKER REASONING (with web-search grounding):\n"
                f"{grounded_text}\n\n"
                f"ORIGINAL INPUT THAT WAS REVIEWED:\n{user_prompt}\n\n"
                f"Translate the fact-checker's reasoning into the "
                f"verdict schema. Preserve every violation code the "
                f"reasoning identified; if reasoning is silent on a "
                f"required field, use the schema default."
            )
            format_system = (
                "You are translating a previous fact-checker's reasoning "
                "into a structured verdict. Do not introduce new "
                "judgments — only format what the reasoning already "
                "states."
            )
            ic = _get_instructor_client_for(provider.config)
            if ic is None:
                last_result = JudgeStructuredResult(
                    success=False,
                    provider=provider.name,
                    model_name=provider.model_name,
                    error_class="InstructorUnavailable",
                    error_detail="instructor.from_provider returned None for format call",
                )
                continue
            create_kwargs = dict(
                response_model=response_model,
                messages=[
                    {"role": "system", "content": format_system},
                    {"role": "user", "content": format_user_prompt},
                ],
                max_retries=max_retries,
                generation_config={'max_tokens': max_tokens},
            )
            try:
                verdict = ic.chat.completions.create(**create_kwargs)
            except Exception as exc:
                last_result = JudgeStructuredResult(
                    success=False,
                    provider=provider.name,
                    model_name=provider.model_name,
                    error_class=type(exc).__name__,
                    error_detail=str(exc)[:300],
                )
                logger.warning(
                    f"[GroundedJudge] {provider.name}/{provider.model_name} "
                    f"call-2 (format) failed: {type(exc).__name__}: "
                    f"{str(exc)[:150]} — trying next"
                )
                continue

            logger.info(
                f"[GroundedJudge] {provider.name}/{provider.model_name} "
                f"two-call grounded judge succeeded"
            )
            return JudgeStructuredResult(
                verdict=verdict,
                success=True,
                provider=provider.name,
                model_name=provider.model_name,
            )

        # Non-Google: use the standard 1-call structured path.
        ic = _get_instructor_client_for(provider.config)
        if ic is None:
            last_result = JudgeStructuredResult(
                success=False,
                provider=provider.name,
                model_name=provider.model_name,
                error_class="InstructorUnavailable",
                error_detail="instructor.from_provider returned None",
            )
            continue
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ] if system_prompt else [{"role": "user", "content": user_prompt}]
        create_kwargs = dict(
            response_model=response_model,
            messages=messages,
            max_retries=max_retries,
            max_tokens=max_tokens,
        )
        try:
            verdict = ic.chat.completions.create(**create_kwargs)
        except Exception as exc:
            last_result = JudgeStructuredResult(
                success=False,
                provider=provider.name,
                model_name=provider.model_name,
                error_class=type(exc).__name__,
                error_detail=str(exc)[:300],
            )
            logger.warning(
                f"[GroundedJudge/non-google] {provider.name}/"
                f"{provider.model_name} failed: {type(exc).__name__}: "
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


def _call_gemini_grounded(
    config,
    system_prompt: str,
    user_prompt: str,
    grounding_instruction: str,
    max_tokens: int,
) -> tuple:
    """Call Gemini with google_search tool enabled. Returns
    (grounded_text, None) on success or ('', (error_class,
    error_detail)) on failure.

    Uses the google-genai SDK directly — instructor doesn't expose
    `tools=[google_search]` since that conflicts with response_schema
    in Gemini's API.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        return '', ('ImportError', str(exc))

    try:
        client = genai.Client(api_key=config.get_api_key())
        # Combine system + grounding instruction so the model knows
        # WHEN to use search (only on factual claims, not on every
        # token). Gemini's `system_instruction` is per-call.
        sys_combined = (
            (system_prompt or '').strip()
            + "\n\n"
            + grounding_instruction
        ).strip()
        gen_config = types.GenerateContentConfig(
            system_instruction=sys_combined,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            max_output_tokens=max_tokens,
        )
        response = client.models.generate_content(
            model=config.model_name,
            contents=user_prompt,
            config=gen_config,
        )
        text = (getattr(response, 'text', None) or '').strip()
        return text, None
    except Exception as exc:
        return '', (type(exc).__name__, str(exc)[:300])
