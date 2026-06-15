"""
LLM Client - Abstracts calls to different LLM providers.

Design decisions:
- Factory pattern: get_client(model_config) returns the right client
- All clients share the same interface: generate(messages, system_prompt)
- Handles API key lookup from environment variables
- Returns structured response with content + token usage
- Supports streaming via generate_stream()

Supports: Anthropic, OpenAI, Ollama (local)
"""

import os
import re
import json
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Generator, List, Union
import anthropic

logger = logging.getLogger(__name__)

from apps.llm.models import ModelConfig


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""
    content: str
    tokens_in: int
    tokens_out: int
    model: str
    stop_reason: Optional[str] = None
    # Prompt-cache telemetry. Populated when the provider supports
    # prompt caching and the call hit / wrote a cache.
    # - cache_creation_tokens: input tokens that wrote to the cache
    #   (charged at premium rate on Anthropic; informational on Gemini)
    # - cache_read_tokens: input tokens served from cache
    #   (charged at ~10% of normal input rate on Anthropic; ~25% on Gemini)
    # tokens_in is the TOTAL input tokens (uncached + cache_read);
    # subtract cache_read to get the "fresh" billed portion.
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


# -- Cross-provider response-shape adapter (task #245) -----------------
# The tutor engine + SelfRetry walk Anthropic Message objects:
#   message.content → list of blocks
#   block.type      → 'text' | 'tool_use'
#   block.text      → str (text blocks)
#   block.name      → str (tool name)
#   block.input     → dict (tool args)
#
# Gemini returns google.genai.types.GenerateContentResponse and OpenAI
# returns openai.types.chat.ChatCompletion. Both fail the
# `isinstance(message.content, list)` shape check at
# `apps/tutoring/regen/self_retry.py:368-374`, raising TypeError on
# every regen attempt. The dataclasses below are duck-typed Anthropic
# Message stand-ins; the per-provider adapter functions translate the
# native response into one.

@dataclass
class AdaptedTextBlock:
    text: str = ''
    type: str = 'text'


@dataclass
class AdaptedToolUseBlock:
    name: str = ''
    input: dict = field(default_factory=dict)
    id: str = ''
    type: str = 'tool_use'


@dataclass
class AdaptedUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class AdaptedMessage:
    """Anthropic-Message-shaped adapter for cross-provider tool calls.

    Built by `_adapt_gemini_response` / `_adapt_openai_response`. Lets
    the tutor engine + SelfRetry consume Gemini / OpenAI tool responses
    through the same `.content[block].{type,text,name,input}` contract
    as the Anthropic SDK.
    """
    content: List[Union[AdaptedTextBlock, AdaptedToolUseBlock]] = field(
        default_factory=list,
    )
    stop_reason: str = 'end_turn'
    usage: AdaptedUsage = field(default_factory=AdaptedUsage)
    model: str = ''


def _adapt_gemini_response(response, *, model_name: str = '') -> AdaptedMessage:
    """Adapt google.genai GenerateContentResponse → AdaptedMessage.

    Walks `candidates[0].content.parts`. Each part is either:
      - text part: `.text` set
      - function_call part: `.function_call.name` + `.function_call.args`
    Mirrors Anthropic block order (text first, tool_use after — matches
    Gemini's own ordering when both are emitted).
    """
    blocks: List[Union[AdaptedTextBlock, AdaptedToolUseBlock]] = []
    stop_reason = 'end_turn'

    candidates = getattr(response, 'candidates', None) or []
    if candidates:
        cand = candidates[0]
        finish = getattr(cand, 'finish_reason', None)
        finish_name = (
            getattr(finish, 'name', None) if finish is not None else None
        ) or (finish if isinstance(finish, str) else None)
        if finish_name == 'MAX_TOKENS':
            stop_reason = 'max_tokens'
        elif finish_name in ('SAFETY', 'RECITATION'):
            stop_reason = 'stop_sequence'

        content = getattr(cand, 'content', None)
        parts = getattr(content, 'parts', None) or [] if content else []
        for idx, part in enumerate(parts):
            fc = getattr(part, 'function_call', None)
            fc_name = getattr(fc, 'name', '') if fc is not None else ''
            if fc is not None and fc_name:
                args = getattr(fc, 'args', None) or {}
                if not isinstance(args, dict):
                    try:
                        args = dict(args)
                    except Exception:
                        try:
                            args = json.loads(
                                json.dumps(args, default=str)
                            )
                        except Exception:
                            args = {}
                blocks.append(AdaptedToolUseBlock(
                    id=f'gemini_tool_{idx}',
                    name=fc_name,
                    input=args,
                ))
                if stop_reason == 'end_turn':
                    stop_reason = 'tool_use'
                continue
            text = getattr(part, 'text', None)
            if text:
                blocks.append(AdaptedTextBlock(text=text))

    usage = AdaptedUsage()
    usage_meta = getattr(response, 'usage_metadata', None)
    if usage_meta is not None:
        usage.input_tokens = (
            getattr(usage_meta, 'prompt_token_count', 0) or 0
        )
        usage.output_tokens = (
            getattr(usage_meta, 'candidates_token_count', 0) or 0
        )
        # Gemini implicit caching: cached_content_token_count.
        usage.cache_read_input_tokens = (
            getattr(usage_meta, 'cached_content_token_count', 0) or 0
        )

    return AdaptedMessage(
        content=blocks,
        stop_reason=stop_reason,
        usage=usage,
        model=model_name or getattr(response, 'model_version', '') or '',
    )


def _adapt_openai_response(response, *, model_name: str = '') -> AdaptedMessage:
    """Adapt openai ChatCompletion → AdaptedMessage.

    Walks `choices[0].message`: `.content` (text or None) and
    `.tool_calls` (list with .id, .function.name, .function.arguments
    as JSON string).
    """
    blocks: List[Union[AdaptedTextBlock, AdaptedToolUseBlock]] = []
    stop_reason = 'end_turn'

    choices = getattr(response, 'choices', None) or []
    if choices:
        choice = choices[0]
        finish = getattr(choice, 'finish_reason', None)
        if finish == 'length':
            stop_reason = 'max_tokens'
        elif finish == 'tool_calls':
            stop_reason = 'tool_use'
        elif finish == 'content_filter':
            stop_reason = 'stop_sequence'

        msg = getattr(choice, 'message', None)
        text_content = getattr(msg, 'content', None) if msg else None
        if text_content:
            blocks.append(AdaptedTextBlock(text=text_content))
        tool_calls = (
            getattr(msg, 'tool_calls', None) or [] if msg else []
        )
        for tc in tool_calls:
            fn = getattr(tc, 'function', None)
            if fn is None:
                continue
            name = getattr(fn, 'name', '') or ''
            raw_args = getattr(fn, 'arguments', '') or ''
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (ValueError, TypeError):
                args = {}
            blocks.append(AdaptedToolUseBlock(
                id=getattr(tc, 'id', '') or f'openai_tool_{len(blocks)}',
                name=name,
                input=args,
            ))

    usage = AdaptedUsage()
    usage_obj = getattr(response, 'usage', None)
    if usage_obj is not None:
        usage.input_tokens = (
            getattr(usage_obj, 'prompt_tokens', 0) or 0
        )
        usage.output_tokens = (
            getattr(usage_obj, 'completion_tokens', 0) or 0
        )
        details = getattr(usage_obj, 'prompt_tokens_details', None)
        if details is not None:
            usage.cache_read_input_tokens = (
                getattr(details, 'cached_tokens', 0) or 0
            )

    return AdaptedMessage(
        content=blocks,
        stop_reason=stop_reason,
        usage=usage,
        model=model_name or getattr(response, 'model', '') or '',
    )


def _extract_balanced(s: str, start: int):
    """Return the balanced ``{...}`` substring beginning at ``s[start] == '{'``
    (quote- and escape-aware), or None if unbalanced."""
    depth, in_str, esc = 0, None, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def _maybe_parse_text_tool_call(text: str):
    """If ``text`` contains a JSON object shaped like a tool call —
    ``{"name": ..., "arguments": {...}}`` — return that dict, else None.

    Some Ollama model templates (e.g. qwen2.5) emit the tool call as a JSON
    object in the *content* string instead of the structured ``tool_calls``
    channel — sometimes wrapped in a ``json`` code fence or ``<tool_call>``
    tags, or after a short preamble. We strip those wrappers and scan for the
    first balanced ``{...}`` carrying both ``name`` and ``arguments`` keys.
    """
    if not text:
        return None
    s = text.strip()
    s = re.sub(r'</?tool_call>', '', s, flags=re.IGNORECASE)  # GLM/Hermes tags
    s = re.sub(r'```[a-zA-Z]*', '', s)                        # code fences
    idx = 0
    while True:
        b = s.find('{', idx)
        if b == -1:
            return None
        cand = _extract_balanced(s, b)
        if cand:
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict) and 'name' in obj and 'arguments' in obj:
                    return obj
            except Exception:
                pass
            idx = b + len(cand)
        else:
            idx = b + 1


def _maybe_parse_text_call_syntax(text: str, tools: list | None):
    """If ``text`` contains a Python-style call to one of the engine's tools —
    e.g. ``pose_question("...")`` or ``advance_step()`` — return a
    ``{"name", "arguments"}`` dict, else None.

    Some model templates (e.g. glm4) write the tool call as a function-call
    *string* in the content instead of using the structured ``tool_calls``
    channel or a JSON object (handled by ``_maybe_parse_text_tool_call``).
    Parsed with ``ast`` (no eval); only calls whose name matches a supplied
    tool are accepted, and the tool schema maps positional args to parameter
    names. Conservative: unknown names / unparseable args are ignored.
    """
    if not text or not tools:
        return None
    import ast

    schemas = {t.get('name'): (t.get('input_schema') or {})
               for t in tools if t.get('name')}
    if not schemas:
        return None

    def _from_call(node):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            return None
        name = node.func.id
        if name not in schemas:
            return None
        props = list((schemas[name].get('properties') or {}).keys())
        required = [p for p in (schemas[name].get('required') or []) if p in props]
        order = required + [p for p in props if p not in required]
        args = {}
        for i, a in enumerate(node.args):
            try:
                val = ast.literal_eval(a)
            except Exception:
                continue
            if i < len(order):
                args[order[i]] = val
        for kw in node.keywords:
            if kw.arg is None:
                continue
            try:
                args[kw.arg] = ast.literal_eval(kw.value)
            except Exception:
                continue
        return {'name': name, 'arguments': args}

    s = text.strip()
    if s.startswith('```'):
        s = s.strip('`').strip()
        for prefix in ('python', 'json'):
            if s.lower().startswith(prefix):
                s = s[len(prefix):].strip()

    # Candidate sources: the whole string, plus a quote-aware balanced
    # extraction of "<tool_name>(...)" for each known tool (so commas /
    # parens / '?' inside a string arg don't truncate the call).
    candidates = [s]
    for name in schemas:
        start = s.find(name + '(')
        if start == -1:
            continue
        depth, in_str, esc, end = 0, None, False, None
        for i in range(start + len(name), len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == in_str:
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            candidates.append(s[start:end])

    for src in candidates:
        src = src.strip()
        try:
            nodes = [ast.parse(src, mode='eval').body]
        except Exception:
            try:
                nodes = list(ast.walk(ast.parse(src)))
            except Exception:
                continue
        for node in nodes:
            result = _from_call(node)
            if result is not None:
                return result
    return None


def _adapt_ollama_response(
    data: dict, *, model_name: str = '', tools: list | None = None,
) -> AdaptedMessage:
    """Adapt an Ollama ``/api/chat`` JSON response → AdaptedMessage.

    Ollama returns an OpenAI-ish shape: ``data['message']`` has
    ``content`` (str) and optional ``tool_calls`` (list of
    ``{'function': {'name', 'arguments'}}``). Lets the tutor engine +
    SelfRetry walk ``.content/.type/.text/.name/.input`` uniformly
    across every provider (task #245).
    """
    msg = (data or {}).get('message', {}) or {}
    blocks: List[Union[AdaptedTextBlock, AdaptedToolUseBlock]] = []
    text = msg.get('content') or ''
    structured = msg.get('tool_calls') or []

    if structured:
        if text.strip():
            blocks.append(AdaptedTextBlock(text=text))
        for i, tc in enumerate(structured):
            fn = (tc or {}).get('function', {}) or {}
            args = fn.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            blocks.append(AdaptedToolUseBlock(
                name=fn.get('name', ''), input=args or {},
                id=f"ollama_tool_{i}",
            ))
    else:
        # No structured tool call — some templates leak it as JSON text
        # (qwen-style) or as a Python-style call string (glm4-style).
        parsed = _maybe_parse_text_tool_call(text)
        if parsed is None:
            parsed = _maybe_parse_text_call_syntax(text, tools)
        if parsed is None and text.strip() and os.environ.get('OLLAMA_DEBUG_RAW'):
            # Diagnostic: a model leaked a tool call in a shape we don't parse
            # yet. Flip OLLAMA_DEBUG_RAW=1 to log the raw content and extend the
            # parser to match. (See _maybe_parse_text_call_syntax / _tool_call.)
            logger.warning("[OllamaToolLeak] unparsed content for %s: %r",
                           model_name, text[:800])
        if parsed is not None:
            blocks.append(AdaptedToolUseBlock(
                name=parsed.get('name', ''),
                input=parsed.get('arguments') or {},
                id="ollama_tool_0",
            ))
        elif text:
            blocks.append(AdaptedTextBlock(text=text))

    stop = 'tool_use' if any(
        getattr(b, 'type', '') == 'tool_use' for b in blocks
    ) else 'end_turn'
    usage = AdaptedUsage(
        input_tokens=int(data.get('prompt_eval_count', 0) or 0),
        output_tokens=int(data.get('eval_count', 0) or 0),
    )
    return AdaptedMessage(
        content=blocks, stop_reason=stop, usage=usage, model=model_name,
    )


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients.

    Subclasses implement ``_generate_impl()``. The public ``generate()`` is
    concrete here — it wraps the impl in a tracing span (see
    ``apps.tutoring.tracing``). When no span buffer is active (e.g.,
    curriculum generation, background tasks), span emission is a no-op and
    the call is unchanged.
    """

    def __init__(self, config: ModelConfig):
        self.config = config
        self.api_key = self._get_api_key()

    def _get_api_key(self) -> str:
        """Get API key via ModelConfig (encrypted DB key → env var fallback)."""
        return self.config.get_api_key()

    def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Generate a response from the LLM.

        Wraps the subclass implementation in a tracing span. Applies
        purpose-based temperature constraints (judges → 0, tutoring →
        [0.1, 0.3]) via ``ModelConfig.effective_temperature``. Callers
        may override with an explicit ``temperature`` kwarg — used by
        the regen ensemble for per-cycle decay.

        Span records model, purpose, duration, tokens. No-op when no
        span buffer is active. See ``apps.tutoring.tracing``.
        """
        # Import inside to avoid a circular import at module load:
        # apps.llm imports → apps.tutoring → apps.llm.
        from apps.tutoring.tracing import emit_span
        purpose = getattr(self.config, 'purpose', '') or ''
        # Resolve temperature: explicit kwarg wins; otherwise pull the
        # purpose-clamped value (judges → 0, tutoring → [0.1, 0.3]).
        if temperature is None:
            temperature = getattr(self.config, 'effective_temperature', None)
            if temperature is None:
                temperature = self.config.temperature
        with emit_span('llm_call', 'generate',
                       model=self.config.model_name,
                       purpose=purpose) as span:
            response = self._generate_impl(
                messages, system_prompt, max_tokens, temperature=temperature,
            )
            if span is not None:
                span['tokens_in'] = response.tokens_in
                span['tokens_out'] = response.tokens_out
            return response

    @abstractmethod
    def _generate_impl(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Subclass implementation. Same shape as ``generate()`` but without
        the tracing wrapper. Subclasses MUST override and use the resolved
        ``temperature`` (never read ``self.config.temperature`` directly).
        """
        pass

    def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str,
    ) -> Generator[str, None, LLMResponse]:
        """
        Generate a streaming response from the LLM.
        
        Yields chunks of text as they arrive.
        Returns final LLMResponse when complete.
        
        Default implementation falls back to non-streaming.
        """
        response = self.generate(messages, system_prompt)
        yield response.content
        return response


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic's Claude API."""

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        if not self.api_key:
            raise ValueError(
                f"API key not found. Set {self.config.api_key_env_var} environment variable."
            )
        self.client = anthropic.Anthropic(api_key=self.api_key)

    MAX_RETRIES = 4
    RETRY_BACKOFF = [15, 30, 60, 120]  # seconds

    # Known output token limits per model family
    MODEL_MAX_OUTPUT = {
        'haiku': 64000,
        'sonnet': 64000,
        'opus': 32000,
    }

    def _clamp_max_tokens(self, max_tokens: int) -> int:
        """Clamp max_tokens to the model's known output limit."""
        model = self.config.model_name.lower()
        for family, limit in self.MODEL_MAX_OUTPUT.items():
            if family in model:
                if max_tokens > limit:
                    logger.warning(
                        f"Clamping max_tokens from {max_tokens} to {limit} for {self.config.model_name}"
                    )
                    return limit
        return max_tokens

    def _supports_temperature(self) -> bool:
        """Newer Anthropic models (extended-thinking series) deprecate
        the `temperature` parameter and 400 if it is sent. Detect those
        and omit the parameter. Conservative: only known-deprecated
        model families are excluded; everything else still gets the
        configured temperature.
        """
        model = (self.config.model_name or "").lower()
        # Opus 4.7 onwards rejects temperature with
        #   400 invalid_request_error: `temperature` is deprecated for this model
        if "opus-4-7" in model:
            return False
        return True

    # Anthropic prompt caching threshold: ephemeral cache is only
    # worth writing when the prompt is large enough to amortize the
    # cache-write premium. Anthropic recommends ≥1024 tokens for Opus /
    # Sonnet and ≥2048 for Haiku. Use char-length proxy: ~4 chars/token.
    _CACHE_MIN_CHARS = 4096

    # Sentinel that callers (e.g. conversational_tutor._build_system_prompt)
    # insert to split a system prompt into a STABLE cacheable prefix and
    # a DYNAMIC per-turn suffix. Without it the whole prompt is treated
    # as one cache block — fine for short stable prompts (judges), bad
    # for tutor prompts that have per-turn-mutating tails that bust the
    # cache prefix on every call.
    CACHE_BREAK_MARKER = "<!--CACHE_BREAK-->"

    def _build_system_for_cache(
        self, system_prompt: str, cacheable: bool,
    ) -> str | list[dict]:
        """Convert system_prompt to either a plain string (no cache)
        or a structured cache-control block (ephemeral, 5-min TTL).

        Three modes:
          1. Cacheable + has CACHE_BREAK_MARKER → split into 2 blocks,
             cache only the prefix (stable session-level content).
             Per-turn dynamic suffix stays uncached.
          2. Cacheable + no marker, prompt ≥ threshold → cache whole prompt
             as one block. Right for stable prompts like judges.
          3. Otherwise → plain string, no caching.

        Anthropic prompt caching docs: cached input is billed at 10%
        of normal input rate on cache HIT; cache WRITE is 1.25× normal.
        Net win is ~7× on stable per-session prompts. See
        memory/deepmind_cost_analysis.md Reduction 1.
        """
        if not cacheable or not system_prompt:
            return system_prompt
        # Mode 1: explicit split via marker
        if self.CACHE_BREAK_MARKER in system_prompt:
            prefix, suffix = system_prompt.split(self.CACHE_BREAK_MARKER, 1)
            if len(prefix) < self._CACHE_MIN_CHARS:
                # Prefix too small to bother caching — strip marker and
                # send everything as plain string.
                return prefix + suffix
            blocks: list[dict] = [{
                "type": "text",
                "text": prefix,
                "cache_control": {"type": "ephemeral"},
            }]
            if suffix:
                blocks.append({"type": "text", "text": suffix})
            return blocks
        # Mode 2: whole prompt cached
        if len(system_prompt) < self._CACHE_MIN_CHARS:
            return system_prompt
        return [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }]

    def _stream_kwargs(self, max_tokens: int, system_prompt: str, messages: list[dict],
                       temperature: float | None = None,
                       cacheable_system: bool = True) -> dict:
        kwargs = dict(
            model=self.config.model_name,
            max_tokens=max_tokens,
            system=self._build_system_for_cache(system_prompt, cacheable_system),
            messages=messages,
        )
        if self._supports_temperature():
            # Use the resolved temperature passed in by _generate_impl;
            # fall back to config.temperature for legacy in-class callers.
            kwargs["temperature"] = temperature if temperature is not None else self.config.temperature
        return kwargs

    def _generate_impl(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Call Claude API using streaming to avoid 10-minute timeout.

        Retries with exponential backoff on rate limit (429) and
        overloaded (529) errors. ``temperature`` is the resolved value
        from ``BaseLLMClient.generate()`` (purpose-clamped or explicitly
        overridden by the regen ensemble).
        """
        resolved_max_tokens = self._clamp_max_tokens(max_tokens or self.config.max_tokens)

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                full_content = ""
                with self.client.messages.stream(
                    **self._stream_kwargs(
                        resolved_max_tokens, system_prompt, messages,
                        temperature=temperature,
                    )
                ) as stream:
                    for text in stream.text_stream:
                        full_content += text
                    final_message = stream.get_final_message()

                # Cache metrics — Anthropic reports them on usage when
                # prompt caching is engaged. Absent → zero (no cache).
                usage = final_message.usage
                cache_create = getattr(usage, 'cache_creation_input_tokens', 0) or 0
                cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
                return LLMResponse(
                    content=full_content,
                    tokens_in=usage.input_tokens,
                    tokens_out=usage.output_tokens,
                    model=final_message.model,
                    stop_reason=final_message.stop_reason,
                    cache_creation_tokens=cache_create,
                    cache_read_tokens=cache_read,
                )

            except (
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIStatusError,  # catches 529 OverloadedError
            ) as e:
                # Only retry on retryable statuses (429, 5xx). Re-raise
                # auth / validation errors immediately.
                status = getattr(e, 'status_code', None)
                if status is not None and status < 429:
                    raise
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                if attempt >= self.MAX_RETRIES:
                    raise
                wait = self.RETRY_BACKOFF[attempt]
                logger.warning(
                    f"Retryable error (attempt {attempt + 1}/{self.MAX_RETRIES + 1}), "
                    f"retrying in {wait}s: {e}"
                )
                time.sleep(wait)
    
    def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str,
    ) -> Generator[str, None, LLMResponse]:
        """Stream response from Claude API."""

        full_content = ""
        tokens_in = 0
        tokens_out = 0

        with self.client.messages.stream(
            **self._stream_kwargs(
                self.config.max_tokens, system_prompt, messages,
            )
        ) as stream:
            for text in stream.text_stream:
                full_content += text
                yield text
            
            # Get final message for token counts
            final_message = stream.get_final_message()
            tokens_in = final_message.usage.input_tokens
            tokens_out = final_message.usage.output_tokens
        
        return LLMResponse(
            content=full_content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=self.config.model_name,
            stop_reason="end_turn",
        )

    def generate_with_tools(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int | None = None,
        tool_choice: dict | None = None,
    ):
        """Non-streaming call that returns the full Anthropic Message
        object (not just text) so the caller can introspect tool_use
        content blocks.

        Used by the tutor's pose_question tool flow — the LLM emits a
        tool_use block to pose a verified bank question and the server
        renders it. Streaming is intentionally NOT used here because
        the tool-use flow needs the whole message structure, not a
        text stream.

        `tool_choice` — when provided, forwarded to Anthropic's
        messages.create. Common values:
          {"type": "auto"} — model decides whether to call a tool (default).
          {"type": "any"}  — model MUST call one of the provided tools.
          {"type": "tool", "name": "..."} — model MUST call this specific tool.
        Pilot 2026-05-16: tutor uses {"type": "any"} on math turns to
        force the LLM through pose_question or pose_inline_question
        rather than authoring numerical questions in free text.

        Returns:
          anthropic.types.Message with .content (list of blocks),
          .stop_reason, .usage. Caller is responsible for assembling
          the final response text from the blocks.
        """
        resolved_max_tokens = self._clamp_max_tokens(
            max_tokens or self.config.max_tokens
        )
        kwargs = dict(
            model=self.config.model_name,
            max_tokens=resolved_max_tokens,
            # Prompt caching: tutor system prompt is ~30KB and stable
            # across all turns of a session. Cache it ephemerally so
            # every call after the first reads at 10% the input rate.
            # See memory/deepmind_cost_analysis.md Reduction 1.
            system=self._build_system_for_cache(system_prompt, True),
            messages=messages,
            tools=tools,
        )
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if self._supports_temperature():
            kwargs["temperature"] = self.config.temperature
        logger.info(
            "[QuestionTool] llm_call: messages=%d system_chars=%d "
            "tools=%d max_tokens=%d model=%s",
            len(messages), len(system_prompt or ""),
            len(tools or []), resolved_max_tokens,
            self.config.model_name,
        )
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                message = self.client.messages.create(**kwargs)
                # Summarise the response so failures are diagnosable
                # without reading the whole tool-use block.
                block_summary = []
                for b in (message.content or []):
                    btype = getattr(b, "type", "unknown")
                    if btype == "tool_use":
                        name = getattr(b, "name", "?")
                        block_summary.append(f"tool_use({name})")
                    elif btype == "text":
                        text_len = len(getattr(b, "text", "") or "")
                        block_summary.append(f"text({text_len}c)")
                    else:
                        block_summary.append(btype)
                # Surface cache hits so we can see prompt-caching
                # working in the live log. See Reduction 1 in
                # memory/deepmind_cost_analysis.md.
                _cache_create = getattr(message.usage, 'cache_creation_input_tokens', 0) or 0
                _cache_read = getattr(message.usage, 'cache_read_input_tokens', 0) or 0
                logger.info(
                    "[QuestionTool] llm_response: stop_reason=%s in=%d out=%d "
                    "cache_read=%d cache_write=%d blocks=[%s]",
                    message.stop_reason,
                    message.usage.input_tokens,
                    message.usage.output_tokens,
                    _cache_read, _cache_create,
                    ", ".join(block_summary),
                )
                return message
            except (
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIStatusError,  # catches 529 OverloadedError
            ) as e:
                # Only retry on retryable statuses (429, 5xx). Re-raise
                # auth / validation errors immediately.
                status = getattr(e, 'status_code', None)
                if status is not None and status < 429:
                    raise
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                if attempt >= self.MAX_RETRIES:
                    raise
                wait = self.RETRY_BACKOFF[attempt]
                logger.warning(
                    "[QuestionTool] llm_call retry %d/%d in %ds: %s",
                    attempt + 1, self.MAX_RETRIES + 1, wait, e,
                )
                time.sleep(wait)


class OllamaClient(BaseLLMClient):
    """
    Client for local Ollama server.
    
    Ollama runs locally and doesn't need an API key.
    Install: https://ollama.ai
    Pull a model: ollama pull llama3
    """
    
    def _get_api_key(self) -> str:
        """Ollama doesn't need an API key."""
        return ""
    
    def _generate_impl(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Call local Ollama API and return standardized response."""
        import requests

        # Build Ollama-compatible messages (with system as first message)
        ollama_messages = [{"role": "system", "content": system_prompt}]
        ollama_messages.extend(messages)

        # Ollama API endpoint
        api_base = self.config.api_base or "http://localhost:11434"
        url = f"{api_base}/api/chat"

        resolved_temp = temperature if temperature is not None else self.config.temperature

        try:
            response = requests.post(
                url,
                json={
                    "model": self.config.model_name,
                    "messages": ollama_messages,
                    "stream": False,
                    "options": {
                        "temperature": resolved_temp,
                        "num_predict": max_tokens or self.config.max_tokens,
                    }
                },
                timeout=120,  # Longer timeout for local models
            )
            response.raise_for_status()
            data = response.json()
            
            return LLMResponse(
                content=data["message"]["content"],
                tokens_in=data.get("prompt_eval_count", 0),
                tokens_out=data.get("eval_count", 0),
                model=self.config.model_name,
                stop_reason="stop",
            )
        except requests.exceptions.ConnectionError:
            raise ConnectionError(
                f"Could not connect to Ollama at {api_base}. "
                "Make sure Ollama is running (ollama serve)."
            )
        except requests.exceptions.Timeout:
            raise TimeoutError(
                "Ollama request timed out. The model may be loading or the request is too complex."
            )

    def _translate_messages_for_ollama(self, messages: list[dict]) -> list[dict]:
        """Translate Anthropic-shaped messages — including the tutor
        two-call tool loop's assistant content-blocks and user
        tool_result blocks — into Ollama ``/api/chat`` shape.

        - assistant message whose ``content`` is a list of blocks →
          ``{role:assistant, content:<text>, tool_calls:[...]}``
        - user message containing ``tool_result`` blocks → one
          ``{role:tool, content:<result>}`` per result
        - plain-string content passes through unchanged

        Handles both dict blocks (Anthropic native / engine-built) and
        the Adapted* dataclass blocks this module emits.
        """
        out: list[dict] = []
        for msg in messages or []:
            role = msg.get('role', 'user')
            content = msg.get('content')
            if isinstance(content, str):
                out.append({'role': role, 'content': content})
                continue
            if not isinstance(content, list):
                out.append({'role': role, 'content': str(content)})
                continue
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            tool_results: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get('type')
                    getv = block.get
                else:
                    btype = getattr(block, 'type', None)
                    getv = lambda k, d=None, _b=block: getattr(_b, k, d)
                if btype == 'text':
                    t = getv('text') or ''
                    if t:
                        text_parts.append(t)
                elif btype == 'tool_use':
                    tool_calls.append({'function': {
                        'name': getv('name') or '',
                        'arguments': getv('input') or {},
                    }})
                elif btype == 'tool_result':
                    c = getv('content')
                    tool_results.append(
                        c if isinstance(c, str) else json.dumps(c, default=str)
                    )
                elif btype == 'image':
                    # Ollama wants base64 images in a separate 'images'
                    # field; the tool loop's threaded turns don't carry
                    # images, so skip rather than mistranslate.
                    continue
            if role == 'assistant':
                m: dict = {'role': 'assistant', 'content': '\n'.join(text_parts)}
                if tool_calls:
                    m['tool_calls'] = tool_calls
                out.append(m)
            elif tool_results:
                for tr in tool_results:
                    out.append({'role': 'tool', 'content': tr})
                if text_parts:
                    out.append({'role': role, 'content': '\n'.join(text_parts)})
            else:
                out.append({'role': role, 'content': '\n'.join(text_parts)})
        return out

    def generate_with_tools(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int | None = None,
        *,
        tool_choice: dict | str | None = None,
    ):
        """Ollama tool-calling. Accepts the same Anthropic-style tool
        schema ``{name, description, input_schema}`` the tutor engine
        builds, converts to Ollama's ``{type:function, function:{...}}``
        shape, and returns an AdaptedMessage so the engine can introspect
        tool_use content blocks uniformly (task #245).

        ``tool_choice`` is accepted for signature parity but NOT
        forwarded — Ollama's forced-tool support is unreliable across
        models, and the tutor prompt already directs tool use.
        """
        import requests

        ollama_tools = []
        for t in (tools or []):
            ollama_tools.append({
                'type': 'function',
                'function': {
                    'name': t.get('name'),
                    'description': t.get('description', ''),
                    'parameters': (
                        t.get('input_schema')
                        or {'type': 'object', 'properties': {}}
                    ),
                },
            })

        ollama_messages = [{'role': 'system', 'content': system_prompt}]
        ollama_messages.extend(self._translate_messages_for_ollama(messages))

        api_base = self.config.api_base or "http://localhost:11434"
        url = f"{api_base}/api/chat"
        payload = {
            'model': self.config.model_name,
            'messages': ollama_messages,
            'tools': ollama_tools,
            'stream': False,
            'options': {
                'temperature': self.config.temperature,
                'num_predict': max_tokens or self.config.max_tokens,
            },
        }
        logger.info(
            "[OllamaTools] llm_call: messages=%d system_chars=%d tools=%d model=%s",
            len(ollama_messages), len(system_prompt or ''),
            len(ollama_tools), self.config.model_name,
        )
        try:
            response = requests.post(url, json=payload, timeout=600)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(
                f"Could not connect to Ollama at {api_base}. "
                "Make sure Ollama is running (ollama serve)."
            )
        adapted = _adapt_ollama_response(
            data, model_name=self.config.model_name, tools=tools,
        )
        logger.info(
            "[OllamaTools] response: in=%d out=%d blocks=%s",
            adapted.usage.input_tokens, adapted.usage.output_tokens,
            [getattr(b, 'type', '?') for b in adapted.content],
        )
        return adapted


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI's GPT API."""

    # Model-name prefixes whose Chat Completions endpoint requires
    # `max_completion_tokens` instead of `max_tokens` and rejects
    # `temperature` (Phase 2b of task #229 — validation slice
    # surfaced this on GPT-5).
    #   - gpt-5, gpt-5.1, gpt-5.2, gpt-5.3-*  (GPT-5 family)
    #   - o1, o3, o4, o5  (o-series reasoning models)
    # GPT-4 family + GPT-4o + GPT-4.1 still use the legacy params.
    _NEW_GEN_PREFIXES = ("gpt-5", "o1", "o3", "o4", "o5")

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        if not self.api_key:
            raise ValueError(
                f"API key not found. Set {self.config.api_key_env_var} environment variable."
            )
        try:
            import openai
            self.client = openai.OpenAI(api_key=self.api_key)
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

    def _is_new_generation(self, model_name: str | None = None) -> bool:
        """True when the target model uses Chat Completions' newer
        max_completion_tokens (instead of max_tokens) and rejects
        custom temperature (reasoning-internal models)."""
        name = (model_name or self.config.model_name or "").strip().lower()
        return any(name.startswith(p) for p in self._NEW_GEN_PREFIXES)

    def _translate_messages_for_openai(self, messages: list[dict]) -> list[dict]:
        """Convert Anthropic-shaped content blocks to OpenAI's
        Chat Completions shape.

        The tutor engine builds multimodal user messages in Anthropic
        shape: `content` is either a string or a list of blocks
        `{"type":"text","text":...}` / `{"type":"image","source":{...}}`.
        OpenAI Chat Completions accepts `{"type":"text",...}` verbatim
        but rejects `{"type":"image",...}` — it requires
        `{"type":"image_url","image_url":{"url":"data:<mime>;base64,<b64>"}}`.

        Block-by-block translation, idempotent (already-translated
        blocks pass through). String contents and non-image blocks
        are untouched. Math L638 (figures) was failing every
        OpenAI tutor call until this landed (task #247).
        """
        translated: list[dict] = []
        for msg in messages or []:
            content = msg.get('content')
            if not isinstance(content, list):
                # Plain-string content — OpenAI accepts as-is.
                translated.append(msg)
                continue
            new_blocks: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue
                btype = block.get('type')
                if btype == 'image':
                    src = block.get('source') or {}
                    media_type = src.get('media_type') or 'image/png'
                    if src.get('type') == 'base64':
                        data = src.get('data') or ''
                        url = f"data:{media_type};base64,{data}"
                    elif src.get('type') == 'url':
                        url = src.get('url') or ''
                    else:
                        # Unknown shape — drop the image quietly rather
                        # than crashing the whole request.
                        logger.warning(
                            "[OpenAITranslate] unknown image source "
                            "type=%s — dropping block", src.get('type'),
                        )
                        continue
                    new_blocks.append({
                        "type": "image_url",
                        "image_url": {"url": url},
                    })
                else:
                    # text / image_url / anything else OpenAI knows
                    # passes through verbatim.
                    new_blocks.append(block)
            translated.append({**msg, 'content': new_blocks})
        return translated

    def _build_completion_kwargs(
        self,
        *,
        max_tokens: int | None,
        temperature: float | None,
    ) -> dict:
        """Return the kwargs to pass to chat.completions.create that
        match the target model's expectations:
          - new-gen (GPT-5+, o-series): max_completion_tokens, no
            temperature override (model reasons internally)
          - legacy: max_tokens + temperature
        """
        resolved_max = max_tokens or self.config.max_tokens
        if self._is_new_generation():
            # New-gen — only max_completion_tokens, no temperature.
            return {"max_completion_tokens": resolved_max}
        # Legacy: max_tokens + temperature
        resolved_temp = (
            temperature if temperature is not None else self.config.temperature
        )
        return {
            "max_tokens": resolved_max,
            "temperature": resolved_temp,
        }

    def _generate_impl(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Call OpenAI API and return standardized response."""

        # OpenAI uses system message in the messages array
        openai_messages = [{"role": "system", "content": system_prompt}]
        openai_messages.extend(
            self._translate_messages_for_openai(messages)
        )

        completion_kwargs = self._build_completion_kwargs(
            max_tokens=max_tokens, temperature=temperature,
        )

        response = self.client.chat.completions.create(
            model=self.config.model_name,
            messages=openai_messages,
            **completion_kwargs,
        )

        return LLMResponse(
            content=response.choices[0].message.content,
            tokens_in=response.usage.prompt_tokens,
            tokens_out=response.usage.completion_tokens,
            model=response.model,
            stop_reason=response.choices[0].finish_reason,
        )

    def generate_with_tools(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int | None = None,
        *,
        tool_choice: dict | str | None = None,
    ):
        """OpenAI function-calling wrapper.

        Accepts the same Anthropic-style tool schema {name, description,
        input_schema} and converts to OpenAI's {type: "function",
        function: {name, description, parameters}} shape. Returns the
        raw ChatCompletion response so the caller can introspect
        choices[0].message.tool_calls (list of tool calls, each with
        .function.name and .function.arguments JSON-string).

        `tool_choice` — accepts the same Anthropic-style values the
        tutor engine passes, mapped to OpenAI's native parameter
        (Phase 2b of task #229 — was raising TypeError on the engine's
        regen path):
          - {"type": "tool", "name": "X"} → {"type": "function",
            "function": {"name": "X"}} — force this specific tool
          - "required" / "any" → "required" (force ANY tool)
          - "none" → "none" (disable tools entirely)
          - "auto" / None → omitted (OpenAI default = auto)
        """
        openai_tools = []
        for t in tools or []:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })

        openai_messages = [{"role": "system", "content": system_prompt}]
        openai_messages.extend(
            self._translate_messages_for_openai(messages)
        )

        kwargs = dict(
            model=self.config.model_name,
            messages=openai_messages,
            tools=openai_tools,
        )

        # Translate tool_choice from the Anthropic-shaped value the
        # engine passes to OpenAI's native shape.
        if tool_choice is not None:
            if isinstance(tool_choice, dict):
                # Anthropic: {"type": "tool", "name": "X"}
                # OpenAI:    {"type": "function", "function": {"name": "X"}}
                if tool_choice.get("type") == "tool" and tool_choice.get("name"):
                    kwargs["tool_choice"] = {
                        "type": "function",
                        "function": {"name": tool_choice["name"]},
                    }
                elif tool_choice.get("type") in ("any", "required"):
                    kwargs["tool_choice"] = "required"
                elif tool_choice.get("type") == "none":
                    kwargs["tool_choice"] = "none"
                elif tool_choice.get("type") == "auto":
                    pass  # omit → OpenAI default = auto
            elif isinstance(tool_choice, str):
                # Plain strings ("auto", "none", "required") pass through;
                # OpenAI accepts the same vocab.
                low = tool_choice.strip().lower()
                if low == "any":
                    kwargs["tool_choice"] = "required"
                elif low in ("required", "none"):
                    kwargs["tool_choice"] = low
                elif low == "auto":
                    pass  # omit
        # Pick the right token / temperature kwargs per model
        # generation — GPT-5+ and o-series use max_completion_tokens
        # and reject temperature; legacy GPT-4* uses max_tokens +
        # temperature (Phase 2b of task #229).
        completion_kwargs = self._build_completion_kwargs(
            max_tokens=max_tokens, temperature=None,
        )
        response = self.client.chat.completions.create(
            **kwargs,
            **completion_kwargs,
        )
        logger.info(
            "[OpenAITools] response: model=%s in=%d out=%d finish=%s tool_calls=%d",
            response.model,
            getattr(response.usage, 'prompt_tokens', 0),
            getattr(response.usage, 'completion_tokens', 0),
            response.choices[0].finish_reason,
            len(response.choices[0].message.tool_calls or []),
        )
        # Wrap in cross-provider adapter so the tutor engine + SelfRetry
        # can walk .content/.type/.text/.name/.input uniformly across
        # all three providers (task #245). The raw ChatCompletion fails
        # the engine's isinstance(.content, list) shape check.
        return _adapt_openai_response(
            response, model_name=self.config.model_name,
        )


class GeminiClient(BaseLLMClient):
    """Client for Google's Gemini API."""

    # Retry policy for transient Google capacity issues (Phase 2b of
    # task #229). 503 UNAVAILABLE is common during peak hours; 429
    # is per-project quota; 5xx are server-side. The judge fallback
    # chain handles judge calls when the retries exhaust, but the
    # tutor path has no fallback so retries are the only defense.
    MAX_RETRIES = 3
    RETRY_BACKOFF = [2, 5, 12]  # seconds — shorter than Anthropic's
                                # since Google capacity recovers faster
    _RETRYABLE_GEMINI_STATUSES = (429, 500, 502, 503, 504)

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        if not self.api_key:
            raise ValueError(
                f"API key not found. Set {self.config.api_key_env_var} environment variable "
                "or configure a key in AI Model settings."
            )
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
        except ImportError:
            raise ImportError("google-genai package not installed. Run: pip install google-genai")

    def _call_with_retry(self, fn, *, label: str = "call"):
        """Execute `fn()` with retry-with-backoff on transient Google
        API errors (429, 500/502/503/504). Re-raises non-retryable
        errors immediately.

        Use this wrapper for any `client.models.generate_content` /
        `generate_content_stream` call so the tutor doesn't error out
        on peak-hour 503s.
        """
        from google.genai import errors as genai_errors

        last_exc = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                return fn()
            except (genai_errors.ServerError, genai_errors.ClientError) as e:
                last_exc = e
                status = getattr(e, 'code', None) or getattr(e, 'status_code', None)
                if status not in self._RETRYABLE_GEMINI_STATUSES:
                    raise
                if attempt >= self.MAX_RETRIES:
                    raise
                wait = self.RETRY_BACKOFF[attempt]
                logger.warning(
                    "[GeminiRetry] %s status=%s attempt=%d/%d wait=%ds model=%s",
                    label, status, attempt + 1, self.MAX_RETRIES + 1,
                    wait, self.config.model_name,
                )
                time.sleep(wait)
        # Unreachable but keeps mypy happy
        if last_exc:
            raise last_exc

    def _build_contents(self, messages):
        """Map chat messages to Gemini Content objects, supporting multimodal."""
        import base64
        from google.genai import types
        contents = []
        for msg in messages:
            role = 'model' if msg['role'] == 'assistant' else 'user'
            content = msg['content']

            # Handle multimodal content blocks (list of dicts with type/text/image)
            if isinstance(content, list):
                parts = []
                for block in content:
                    if block.get('type') == 'text':
                        parts.append(types.Part(text=block['text']))
                    elif block.get('type') == 'image':
                        # Anthropic format: source.type=base64
                        source = block.get('source', {})
                        parts.append(types.Part(
                            inline_data=types.Blob(
                                mime_type=source.get('media_type', 'image/jpeg'),
                                data=base64.b64decode(source.get('data', '')),
                            )
                        ))
                    elif block.get('type') == 'image_url':
                        # OpenAI format: image_url.url=data:mime;base64,...
                        url = block.get('image_url', {}).get('url', '')
                        if url.startswith('data:'):
                            # Parse data URI: data:image/jpeg;base64,/9j/...
                            header, b64_data = url.split(',', 1)
                            mime = header.split(':')[1].split(';')[0]
                            parts.append(types.Part(
                                inline_data=types.Blob(
                                    mime_type=mime,
                                    data=base64.b64decode(b64_data),
                                )
                            ))
                contents.append(types.Content(role=role, parts=parts))
            else:
                # Simple string content
                contents.append(types.Content(
                    role=role,
                    parts=[types.Part(text=content)],
                ))
        return contents

    def _search_tools(self):
        """Build Google Search grounding tools list."""
        from google.genai import types
        try:
            return [types.Tool(google_search=types.GoogleSearch())]
        except Exception as e:
            logger.warning(f"Could not create search grounding tool: {e}")
            return None

    def _generate_impl(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Call Gemini API with Google Search grounding."""
        from google.genai import types

        gemini_contents = self._build_contents(messages)
        tools = self._search_tools()

        resolved_temp = temperature if temperature is not None else self.config.temperature

        config_kwargs = dict(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens or self.config.max_tokens,
            temperature=resolved_temp,
        )
        if tools:
            config_kwargs['tools'] = tools

        response = self._call_with_retry(
            lambda: self.client.models.generate_content(
                model=self.config.model_name,
                contents=gemini_contents,
                config=types.GenerateContentConfig(**config_kwargs),
            ),
            label="_generate_impl",
        )

        usage = response.usage_metadata
        tokens_in = getattr(usage, 'prompt_token_count', 0) or 0
        tokens_out = getattr(usage, 'candidates_token_count', 0) or 0
        # Gemini 2.5+ has implicit caching — when consecutive calls share
        # a stable prefix (system_instruction is the obvious one), the
        # cache_token_count reports the fraction served from cache.
        # Cached input is billed at ~25% of normal rate.
        cache_read = getattr(usage, 'cached_content_token_count', 0) or 0

        return LLMResponse(
            content=response.text or '',
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=self.config.model_name,
            stop_reason='stop',
            cache_read_tokens=cache_read,
            # cache_creation is N/A for Gemini implicit caching (no
            # explicit write step; cache is populated by the request itself).
        )

    def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str,
    ) -> Generator[str, None, LLMResponse]:
        """Stream response from Gemini API with Google Search grounding."""
        from google.genai import types

        gemini_contents = self._build_contents(messages)
        tools = self._search_tools()

        config_kwargs = dict(
            system_instruction=system_prompt,
            max_output_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )
        if tools:
            config_kwargs['tools'] = tools

        full_content = ""
        tokens_in = 0
        tokens_out = 0

        for chunk in self.client.models.generate_content_stream(
            model=self.config.model_name,
            contents=gemini_contents,
            config=types.GenerateContentConfig(**config_kwargs),
        ):
            text = chunk.text or ''
            full_content += text
            if chunk.usage_metadata:
                tokens_in = getattr(chunk.usage_metadata, 'prompt_token_count', 0) or 0
                tokens_out = getattr(chunk.usage_metadata, 'candidates_token_count', 0) or 0
            yield text

        return LLMResponse(
            content=full_content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=self.config.model_name,
            stop_reason='stop',
        )

    def generate_with_tools(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int | None = None,
        *,
        tool_choice: dict | str | None = None,
    ):
        """Gemini function-calling wrapper.

        Accepts the same Anthropic-style tool schema {name, description,
        input_schema} and converts to Gemini's FunctionDeclaration shape.
        Returns the raw GenerateContentResponse so the caller can
        introspect candidates[0].content.parts for FunctionCall blocks.

        `tool_choice` — accepts the same Anthropic-style values the
        tutor engine passes, mapped to Gemini's
        `tool_config.function_calling_config` (Phase 2b of task #229 —
        was raising TypeError on the engine's regen path):
          - {"type": "tool", "name": "X"} → mode="ANY" +
            allowed_function_names=["X"]
          - "required" / "any" → mode="ANY" (force ANY tool)
          - "none" → mode="NONE" (disable tools entirely)
          - "auto" / None → mode="AUTO" (default; omitted)
        """
        from google.genai import types

        # Convert Anthropic-style tools to Gemini function declarations.
        function_decls = []
        for t in tools or []:
            function_decls.append(types.FunctionDeclaration(
                name=t.get("name") or "",
                description=t.get("description") or "",
                parameters=t.get("input_schema") or {"type": "OBJECT"},
            ))
        gemini_tools = [types.Tool(function_declarations=function_decls)] if function_decls else None

        gemini_contents = self._build_contents(messages)

        config_kwargs = dict(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens or self.config.max_tokens,
            temperature=self.config.temperature,
        )
        if gemini_tools:
            config_kwargs["tools"] = gemini_tools

        # Translate tool_choice from Anthropic-shaped value to Gemini's
        # tool_config. None / "auto" / {"type": "auto"} → omit (AUTO).
        fcc_mode = None
        allowed_names: list[str] | None = None
        if isinstance(tool_choice, dict):
            t_type = tool_choice.get("type")
            if t_type == "tool" and tool_choice.get("name"):
                fcc_mode = "ANY"
                allowed_names = [tool_choice["name"]]
            elif t_type in ("any", "required"):
                fcc_mode = "ANY"
            elif t_type == "none":
                fcc_mode = "NONE"
            elif t_type == "auto":
                pass  # omit
        elif isinstance(tool_choice, str):
            low = tool_choice.strip().lower()
            if low in ("required", "any"):
                fcc_mode = "ANY"
            elif low == "none":
                fcc_mode = "NONE"
            elif low == "auto":
                pass  # omit

        if fcc_mode is not None:
            fcc_kwargs: dict = {"mode": fcc_mode}
            if allowed_names:
                fcc_kwargs["allowed_function_names"] = allowed_names
            config_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    **fcc_kwargs,
                ),
            )

        response = self._call_with_retry(
            lambda: self.client.models.generate_content(
                model=self.config.model_name,
                contents=gemini_contents,
                config=types.GenerateContentConfig(**config_kwargs),
            ),
            label="generate_with_tools",
        )
        # Summary log
        function_call_count = 0
        text_chars = 0
        try:
            for cand in (response.candidates or []):
                for part in (cand.content.parts or []):
                    if getattr(part, "function_call", None):
                        function_call_count += 1
                    elif getattr(part, "text", None):
                        text_chars += len(part.text or "")
        except Exception:
            pass
        logger.info(
            "[GeminiTools] response: model=%s in=%d out=%d "
            "function_calls=%d text_chars=%d",
            self.config.model_name,
            getattr(response.usage_metadata, "prompt_token_count", 0) or 0,
            getattr(response.usage_metadata, "candidates_token_count", 0) or 0,
            function_call_count,
            text_chars,
        )
        # Wrap in cross-provider adapter so the tutor engine + SelfRetry
        # can walk .content/.type/.text/.name/.input uniformly across
        # all three providers (task #245). The raw GenerateContentResponse
        # fails the engine's isinstance(.content, list) shape check.
        return _adapt_gemini_response(
            response, model_name=self.config.model_name,
        )


class MockLLMClient(BaseLLMClient):
    """
    Mock client for testing without API calls.
    Returns predictable responses based on input.
    """
    
    def _get_api_key(self) -> str:
        return "mock-key"
    
    def _generate_impl(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        # Simple mock: echo back a response based on last message
        last_msg = messages[-1]["content"] if messages else ""
        
        return LLMResponse(
            content=f"[Mock tutor response to: {last_msg[:50]}...]",
            tokens_in=len(system_prompt.split()) + sum(len(m["content"].split()) for m in messages),
            tokens_out=20,
            model="mock-model",
            stop_reason="end_turn",
        )


def get_llm_client(config: ModelConfig, use_mock: bool = False) -> BaseLLMClient:
    """
    Factory function to get the appropriate LLM client.
    
    Args:
        config: ModelConfig instance with provider and settings
        use_mock: If True, return mock client (for testing)
        
    Returns:
        Appropriate LLM client instance
    """
    if use_mock:
        return MockLLMClient(config)
    
    if config.provider == ModelConfig.Provider.ANTHROPIC:
        return AnthropicClient(config)
    elif config.provider == ModelConfig.Provider.OPENAI:
        return OpenAIClient(config)
    elif config.provider == ModelConfig.Provider.GOOGLE:
        return GeminiClient(config)
    elif config.provider == ModelConfig.Provider.LOCAL_OLLAMA:
        return OllamaClient(config)

    raise ValueError(f"Unsupported provider: {config.provider}")
