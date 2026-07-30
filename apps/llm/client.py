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


# ── Transient-error retry (shared by the student-sim + rubric judge) ──────────
# An Anthropic 529 "Overloaded" or a Vertex 429 during a run must not wipe whole
# sessions: retry with backoff, then re-raise so the caller's own error handling
# still runs. The engine's tutor call has its own no-block variant of this in
# apps/tutoring/simple_tutor/engine.py — the two detections should be consolidated
# here when next touched. Toggle SIMPLE_TUTOR_TRANSIENT_RETRY=0 disables it.
# Extended past 12s on 2026-07-22: the oss13_mt sweep lost 10 scenarios to an
# Anthropic overload window that outlasted the short ladder.
_TRANSIENT_BACKOFF = (2, 5, 12, 30, 60)


def is_transient_error(exc: Exception) -> bool:
    """Retryable cloud failure (rate limit / overload / 5xx / connection blip) vs.
    a permanent error (400 / auth / schema) which must fail fast."""
    name = type(exc).__name__.lower()
    if any(k in name for k in ('ratelimit', 'internalserver', 'serviceunavailable',
                               'apiconnection', 'apitimeout', 'timeout', 'overloaded')):
        return True
    for attr in ('status_code', 'code'):
        try:
            if int(getattr(exc, attr, None)) in (429, 500, 502, 503, 504, 529):
                return True
        except (TypeError, ValueError):
            pass
    msg = str(exc).lower()
    return any(s in msg for s in (
        '429', '503', '529', 'resource exhausted', 'overloaded', 'unavailable',
        'please try again', 'rate limit', 'timed out', 'connection error'))


def call_with_transient_retry(fn, *, label: str = 'llm', backoff=_TRANSIENT_BACKOFF):
    """Call ``fn()``; retry transient failures with backoff; re-raise the last
    exception on final failure (so existing try/except around the call fires)."""
    retries = len(backoff) if os.getenv(
        'SIMPLE_TUTOR_TRANSIENT_RETRY', '1').strip() != '0' else 0
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            if is_transient_error(exc) and attempt < retries:
                logger.warning("%s transient %s — retry %d/%d in %ds", label,
                               type(exc).__name__, attempt + 1, retries, backoff[attempt])
                time.sleep(backoff[attempt])
                continue
            raise


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


def _adapt_gemini_response(response, *, model_name: str = '', tools: list | None = None) -> AdaptedMessage:
    """Adapt google.genai GenerateContentResponse → AdaptedMessage.

    Walks `candidates[0].content.parts`. Each part is either:
      - text part: `.text` set
      - function_call part: `.function_call.name` + `.function_call.args`
    Mirrors Anthropic block order (text first, tool_use after — matches
    Gemini's own ordering when both are emitted). ``tools`` enables B1
    leaked-tool-call recovery (a call emitted as plain text).
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

    blocks = _recover_text_tool_call(blocks, tools)
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


def _adapt_openai_response(response, *, model_name: str = '', tools: list | None = None) -> AdaptedMessage:
    """Adapt openai ChatCompletion → AdaptedMessage.

    Walks `choices[0].message`: `.content` (text or None) and
    `.tool_calls` (list with .id, .function.name, .function.arguments
    as JSON string). ``tools`` enables B1 leaked-tool-call recovery.
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
        # B4: reasoning-channel tool call (thinking models populate this when
        # `content` is empty). Recovered below.
        reasoning = (
            getattr(msg, 'reasoning_content', None)
            or getattr(msg, 'reasoning', None) or ''
        ) if msg else ''
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
    else:
        reasoning = ''

    blocks = _recover_text_tool_call(blocks, tools)
    blocks = _recover_reasoning_tool_call(blocks, reasoning, tools)
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


def _adapt_openai_dict(data: dict, *, model_name: str = '', tools: list | None = None) -> AdaptedMessage:
    """Adapt a RAW OpenAI-shaped response *dict* → AdaptedMessage.

    Sibling of `_adapt_openai_response` (which reads the SDK's parsed object).
    Used by `VertexModelGardenClient`, which parses `json.loads(raw.text)`
    because the SDK's parsed `ChatCompletion.choices` is intermittently None
    for DeepSeek MaaS even when the body is valid (verified 2026-06-17).
    Reads `data['choices'][0]['message']` ('content' / 'tool_calls'); ignores
    'reasoning_content' (thinking models populate it).
    """
    blocks: List[Union[AdaptedTextBlock, AdaptedToolUseBlock]] = []
    stop_reason = 'end_turn'
    choices = (data or {}).get('choices') or []
    if choices:
        choice = choices[0] or {}
        finish = choice.get('finish_reason')
        if finish == 'length':
            stop_reason = 'max_tokens'
        elif finish == 'tool_calls':
            stop_reason = 'tool_use'
        elif finish == 'content_filter':
            stop_reason = 'stop_sequence'
        msg = choice.get('message') or {}
        text = msg.get('content')
        # B4: kept for reasoning-channel tool-call recovery below (thinking
        # models put the call here when `content` is empty).
        reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
        if text:
            blocks.append(AdaptedTextBlock(text=text))
        for i, tc in enumerate(msg.get('tool_calls') or []):
            fn = (tc or {}).get('function') or {}
            raw_args = fn.get('arguments') or ''
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (ValueError, TypeError):
                args = {}
            blocks.append(AdaptedToolUseBlock(
                id=(tc or {}).get('id') or f'vertex_tool_{i}',
                name=fn.get('name') or '',
                input=args,
            ))
    else:
        reasoning = ''
    blocks = _recover_text_tool_call(blocks, tools)
    blocks = _recover_reasoning_tool_call(blocks, reasoning, tools)
    usage_obj = (data or {}).get('usage') or {}
    usage = AdaptedUsage(
        input_tokens=usage_obj.get('prompt_tokens', 0) or 0,
        output_tokens=usage_obj.get('completion_tokens', 0) or 0,
    )
    return AdaptedMessage(
        content=blocks, stop_reason=stop_reason, usage=usage,
        model=(data or {}).get('model') or model_name or '',
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


def _resolve_recovered_tool_name(raw: str, tools: list | None) -> str | None:
    """Map a tool name recovered from free text onto a real tool, or None.

    ``_maybe_parse_text_tool_call`` accepts any ``{"name": ..., "arguments": ...}``
    object, so a model echoing its own schema template produced the literal
    placeholder ``tool_name``; others emitted ``requestFigure`` (camelCase) and
    ``" record_answer"`` (whitespace-padded). Those reached the engine and were
    dispatched as unknown tools. Validate against the tools actually offered.
    """
    known = {t.get('name') for t in (tools or []) if t.get('name')}
    if not known:
        return None
    name = str(raw or '').strip()
    if name in known:
        return name
    snake = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
    if snake in known:
        return snake
    logger.warning(
        "[ToolRecovery] discarding text-parsed tool call with unknown name %r", raw,
    )
    return None


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


def _recover_text_tool_call(blocks, tools):
    """If ``blocks`` carries no structured ``tool_use`` block but a text block
    *is* (or contains) a leaked tool-call signature — e.g. the literal
    ``record_answer(extracted_answer="110")`` emitted as plain text — return a
    new block list with a recovered ``AdaptedToolUseBlock`` and the leaked text
    removed. Mirrors the leak recovery ``_adapt_ollama_response`` already does,
    for the OpenAI/Vertex/Gemini adapters: those models occasionally emit the
    call as text instead of via the structured tool_calls/function_call channel,
    and the student would otherwise see the raw signature as the reply (B1).

    No-op when ``tools`` is falsy, a structured ``tool_use`` already exists, or
    no signature is found — so ordinary text replies pass through untouched.
    """
    if not tools or not blocks:
        return blocks
    if any(getattr(b, 'type', '') == 'tool_use' for b in blocks):
        return blocks
    text = ''.join(
        getattr(b, 'text', '') or '' for b in blocks
        if getattr(b, 'type', '') == 'text'
    )
    if not text.strip():
        return blocks
    parsed = (_maybe_parse_text_call_syntax(text, tools)
              or _maybe_parse_text_tool_call(text))
    if not parsed or not parsed.get('name'):
        return blocks
    name = _resolve_recovered_tool_name(parsed.get('name', ''), tools)
    if not name:
        return blocks
    return [AdaptedToolUseBlock(
        name=name,
        input=parsed.get('arguments') or {},
        id='recovered_tool_0',
    )]


def _recover_reasoning_tool_call(blocks, reasoning, tools):
    """B4: salvage a tool call a thinking model emitted inside its reasoning
    channel.

    Some reasoning models (e.g. qwen3-next-80b-thinking on Vertex MaaS) return
    their tool call inside ``reasoning_content`` — a channel the standard OpenAI
    response shape carries OUTSIDE ``content``, which the adapters otherwise
    ignore. When ``content`` produced no tool call, the whole turn is lost: in
    sweep 2 this deadlocked qwen3-next-80b-thinking 14/15. This reuses the same
    text-call parser on the reasoning text and, if it finds a call, keeps that
    tool_use block plus any genuine visible text. The raw reasoning is never
    surfaced to the student — only a recovered tool_use block is added.

    No-op unless enabled, ``tools`` given, reasoning present, and no structured
    tool_use already exists. Toggle SIMPLE_TUTOR_THINKING_RECOVERY=0 disables it
    so a sweep can isolate this variable.
    """
    if os.getenv('SIMPLE_TUTOR_THINKING_RECOVERY', '1').strip() == '0':
        return blocks
    if not tools or not reasoning or not str(reasoning).strip():
        return blocks
    if any(getattr(b, 'type', '') == 'tool_use' for b in blocks):
        return blocks
    recovered = _recover_text_tool_call([AdaptedTextBlock(text=str(reasoning))], tools)
    tool_blocks = [b for b in recovered if getattr(b, 'type', '') == 'tool_use']
    if not tool_blocks:
        return blocks
    logger.info(
        "[llm] recovered %s from the reasoning channel — thinking model emitted "
        "the tool call outside `content`",
        [getattr(b, 'name', '?') for b in tool_blocks],
    )
    text_blocks = [b for b in blocks if getattr(b, 'type', '') == 'text']
    return text_blocks + tool_blocks


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
        recovered_name = (
            _resolve_recovered_tool_name(parsed.get('name', ''), tools)
            if parsed is not None else None
        )
        if recovered_name:
            blocks.append(AdaptedToolUseBlock(
                name=recovered_name,
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
        *,
        sampling: dict | None = None,
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
            # Eval may pass a per-family temperature via ``sampling``;
            # otherwise use the config value (production behaviour).
            _samp = sampling or {}
            kwargs["temperature"] = (
                _samp["temperature"] if _samp.get("temperature") is not None
                else self.config.temperature
            )
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


# Cache of Ollama /api/show payloads, keyed by model tag. The fit preflight
# runs on every generate call and the architecture metadata never changes for a
# given tag, so one fetch per tag per process is enough.
_OLLAMA_MODEL_INFO_CACHE: dict[str, dict] = {}

# Bytes per KV element by OLLAMA_KV_CACHE_TYPE. Ollama defaults to f16.
_OLLAMA_KV_ELEM_BYTES = {'f16': 2.0, 'f32': 4.0, 'q8_0': 1.0, 'q4_0': 0.5}

# Weights + KV is not the whole runner: llama.cpp also reserves compute buffers
# and per-context scratch that the GGUF metadata does not describe. Measured on
# qwen3-4b-jetson at num_ctx=16384, fully offloaded — `ollama ps` reported
# 3.9 GB against a 3.45 GB weights+KV projection, a 0.45 GB shortfall (CUDA0
# compute buffer 106 MiB + CUDA_Host 26 MiB + runtime overhead). Counted here
# so the check errs toward refusing: a false refusal costs one eval cell, a
# false pass costs the whole box.
_OLLAMA_RUNTIME_OVERHEAD_BYTES = 512 * 1024 * 1024


def _parse_modelfile_num_ctx(parameters: str) -> int | None:
    """num_ctx baked into the tag's Modelfile, from /api/show `parameters`."""
    for line in (parameters or '').splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == 'num_ctx':
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def _ollama_model_footprint(api_base: str, model_name: str) -> tuple[int, dict, int | None] | None:
    """Weight bytes, architecture metadata, and Modelfile num_ctx for a tag."""
    if model_name in _OLLAMA_MODEL_INFO_CACHE:
        entry = _OLLAMA_MODEL_INFO_CACHE[model_name]
        return entry['size'], entry['info'], entry['num_ctx']
    import requests                              # module-local, as elsewhere in this file
    try:
        show = requests.post(
            f"{api_base}/api/show", json={'model': model_name}, timeout=20,
        )
        show.raise_for_status()
        shown = show.json()
        info = shown.get('model_info') or {}
        file_ctx = _parse_modelfile_num_ctx(shown.get('parameters') or '')
        tags = requests.get(f"{api_base}/api/tags", timeout=20)
        tags.raise_for_status()
        # /api/tags always reports the fully-qualified tag, so a bare name like
        # "qwen3-4b-jetson" never matches the listed "qwen3-4b-jetson:latest".
        # ModelConfig rows carry the bare form, so without this the lookup
        # returns 0 bytes and the fit check silently passes on every model.
        candidates = {model_name}
        if ':' not in model_name:
            candidates.add(f'{model_name}:latest')
        size = 0
        for m in tags.json().get('models') or []:
            if m.get('name') in candidates or m.get('model') in candidates:
                size = int(m.get('size') or 0)
                break
    except Exception as e:
        logger.warning("[OllamaFit] could not read model metadata for %s: %s", model_name, e)
        return None
    _OLLAMA_MODEL_INFO_CACHE[model_name] = {'size': size, 'info': info, 'num_ctx': file_ctx}
    return size, info, file_ctx


def _ollama_resident(api_base: str) -> list[dict]:
    """Models Ollama currently has loaded, from `/api/ps`.

    Deliberately NOT cached: residency changes over the life of a process as
    OLLAMA_KEEP_ALIVE expires, and a stale answer would skip the fit check on a
    model that had since been unloaded — the exact case the guard exists for.

    Fail-soft returns [], which routes to the plain fit check rather than waving
    a load through on a failed lookup.
    """
    import requests

    try:
        resp = requests.get(f"{api_base}/api/ps", timeout=10)
        resp.raise_for_status()
        return resp.json().get('models') or []
    except Exception as e:
        logger.debug("[OllamaFit] /api/ps lookup failed: %s", e)
        return []


def _ollama_evictable_bytes(resident: list[dict], model_name: str) -> int:
    """Memory Ollama will reclaim by evicting to make room for ``model_name``.

    Loading a model when OLLAMA_MAX_LOADED_MODELS is already satisfied evicts an
    existing one, so that memory IS available to the incoming model even though
    MemAvailable does not show it yet. Without this the guard blocks every model
    SWAP: measured 2026-07-27 switching qwen3-4b-jetson (3.9 GB resident, 0.7 GB
    free) to qwen3.5:4b — refused for 3.7 GB it was about to get back.

    Only counted when OLLAMA_MAX_LOADED_MODELS is explicitly set and already
    reached, because that is when eviction is guaranteed. Left unset, Ollama
    sizes concurrency dynamically and may keep both resident, so assuming a
    reclaim would be the dangerous direction to be wrong in.
    """
    try:
        max_loaded = int(os.environ.get('OLLAMA_MAX_LOADED_MODELS') or 0)
    except ValueError:
        return 0
    if max_loaded <= 0 or len(resident) < max_loaded:
        return 0
    return sum(int(m.get('size') or 0) for m in resident)


def _ollama_kv_bytes(
    info: dict, num_ctx: int, kv_type: str | None = None,
) -> float | None:
    """Projected KV-cache bytes at num_ctx, from GGUF architecture metadata.

    ``kv_type`` overrides OLLAMA_KV_CACHE_TYPE. Callers that know what the
    SERVER was launched with should pass it: the env var is a server setting,
    and a client process that cannot see it silently assumes f16 and doubles the
    estimate.
    """
    arch = info.get('general.architecture')
    if not arch:
        return None
    layers = info.get(f'{arch}.block_count')
    kv_heads = info.get(f'{arch}.attention.head_count_kv')
    if not layers or not kv_heads:
        return None
    if isinstance(kv_heads, list):          # per-layer GQA schedules
        kv_heads = max(kv_heads)
    k_len = info.get(f'{arch}.attention.key_length')
    v_len = info.get(f'{arch}.attention.value_length')
    if not k_len or not v_len:
        # Fall back to the MHA identity: head_dim = embedding_length / head_count.
        emb = info.get(f'{arch}.embedding_length')
        heads = info.get(f'{arch}.attention.head_count')
        if not emb or not heads:
            return None
        k_len = v_len = emb / heads
    elem = _OLLAMA_KV_ELEM_BYTES.get(
        (kv_type or os.environ.get('OLLAMA_KV_CACHE_TYPE') or 'f16').lower(), 2.0,
    )
    # Parallel slots each get their own KV cache.
    slots = max(1, int(os.environ.get('OLLAMA_NUM_PARALLEL') or 1))
    return num_ctx * layers * kv_heads * (k_len + v_len) * elem * slots


def _ollama_fit_preflight(api_base: str, model_name: str, num_ctx: int | None) -> None:
    """
    Refuse to load an Ollama model that cannot fit in available memory.

    WHY THIS IS A HARD FAILURE, not a warning. On a discrete GPU an oversized
    model fails cleanly ("cannot allocate CUDA0 buffer"). On Jetson the GPU
    allocates from the SAME pool as the CPU, so there is no separate limit to
    hit — Ollama just consumes system RAM until the kernel is out. With
    zram-backed swap the reclaim path then livelocks (zram needs RAM to
    compress into), the CPU stops making progress, and the CCPLEX watchdog
    resets the board: BCCPLEXWDT, no logs, no OOM kill, ~2 min of downtime.
    Measured 2026-07-27 on an Orin Nano Super 8 GB.

    Refusing costs one failed eval cell. Not refusing costs the whole box, and
    takes any concurrent sweep results with it.

    Fail-soft by design: any metadata we cannot read (non-Linux, older Ollama,
    unknown architecture) logs a warning and allows the load. Kill-switch:
    OLLAMA_FIT_GUARD=off.
    """
    if (os.environ.get('OLLAMA_FIT_GUARD') or '').lower() == 'off':
        return
    # Already-resident models need no new allocation, so the fit question does
    # not apply to them. Checking this FIRST is not an optimisation — without
    # it the guard breaks every multi-turn conversation: turn 1 loads the model,
    # the model's own ~4 GB then makes MemAvailable look too small, and turn 2
    # onwards is refused for memory the model itself is holding. Measured on the
    # Jetson 2026-07-27 (1.4 GB "available" against a 4.0 GB projection, for a
    # model that was already loaded and answering).
    resident = _ollama_resident(api_base)
    _names = {model_name}
    if ':' not in model_name:
        _names.add(f'{model_name}:latest')
    if any(m.get('name') in _names or m.get('model') in _names for m in resident):
        return
    try:
        with open('/proc/meminfo') as fh:
            avail_kb = next(
                int(ln.split()[1]) for ln in fh if ln.startswith('MemAvailable:')
            )
    except Exception:
        return                                  # not Linux / no procfs — nothing to check
    footprint = _ollama_model_footprint(api_base, model_name)
    if footprint is None:
        return
    weights, info, file_ctx = footprint
    # Precedence matches Ollama's: an explicit options.num_ctx wins, else the
    # tag's Modelfile value, else Ollama's 4096 default. Assuming the default
    # when a Modelfile pins a larger window under-counts KV by that ratio —
    # measured 4x on qwen3-4b-jetson, which pins num_ctx 16384.
    if num_ctx is None:
        num_ctx = file_ctx or 4096
    elif file_ctx is None:
        # We are pinning a window this tag does NOT bake in. Every caller that
        # bypasses this client — notably the grader's Tier-2 verifier, which
        # uses instructor's OpenAI-compatible endpoint and has no num_ctx field
        # — will instead get Ollama's 4096 default. Ollama keys a runner on
        # (model, params), so the two alternate and evict each other, reloading
        # the model 30-45 s each way on every graded turn.
        #
        # Measured on the bare qwen3.5:2b tag 2026-07-28: 4 reloads across 2
        # turns, gone after building a Modelfile-pinned tag. Warn loudly rather
        # than let it look like ordinary slowness.
        logger.warning(
            "[OllamaFit] %s has no num_ctx in its Modelfile but the profile "
            "pins %d — paths that bypass this client will load a separate "
            "%d-context runner and evict this one on every call. Build a "
            "pinned tag (see infra/ollama/Modelfile.qwen3.5-2b-jetson).",
            model_name, num_ctx, 4096,
        )
    if not weights:
        # Never silently pass: an unmeasurable model is exactly the case where
        # a bad fit goes undetected and takes the box down.
        logger.warning(
            "[OllamaFit] no weight size reported for %s — fit NOT checked. "
            "Confirm the tag is present in `ollama list`.", model_name,
        )
        return
    kv = _ollama_kv_bytes(info, num_ctx)
    evictable = _ollama_evictable_bytes(resident, model_name)
    available = avail_kb * 1024 + evictable
    projected = weights + (kv or 0) + _OLLAMA_RUNTIME_OVERHEAD_BYTES
    gb = 1024 ** 3
    detail = (
        f"{model_name}: weights {weights / gb:.1f} GB"
        + (f" + KV {kv / gb:.1f} GB @ num_ctx={num_ctx}" if kv else " (KV unknown)")
        + f" + {_OLLAMA_RUNTIME_OVERHEAD_BYTES / gb:.1f} GB overhead"
        + f" = {projected / gb:.1f} GB projected against {available / gb:.1f} GB available"
        + (f" ({evictable / gb:.1f} GB of it reclaimed by evicting the "
           f"resident model)" if evictable else "")
    )
    if projected > available:
        raise MemoryError(
            f"[OllamaFit] refusing to load — {detail}. Loading it would exhaust "
            f"this box's unified memory pool and hang the kernel (BCCPLEXWDT "
            f"watchdog reset), not raise a clean OOM. Options: pick a smaller "
            f"tag, lower num_ctx in the model's MODEL_PROFILES entry, set "
            f"OLLAMA_KV_CACHE_TYPE=q8_0, or free memory. Override with "
            f"OLLAMA_FIT_GUARD=off if you know this box is bigger."
        )
    if kv is None:
        # Some architectures report attention.head_count_kv as null (measured:
        # the `qwen35` arch, Ollama 0.30.7), and without the GQA ratio the cache
        # cannot be projected. Guessing would mean either false refusals (assume
        # MHA) or false confidence (assume heavy GQA), so this check degrades to
        # weights-only and says so. num_ctx must stay pinned in MODEL_PROFILES
        # for these — that is the only thing bounding the cache.
        logger.warning(
            "[OllamaFit] KV cache NOT projected for %s (architecture reports no "
            "head_count_kv) — checked weights only. Keep num_ctx pinned in "
            "MODEL_PROFILES; an unpinned window is what OOMs this box.",
            model_name,
        )
    # Within 15% of the ceiling the load itself may succeed while any other
    # allocation on the box tips it over. Worth saying before it happens.
    elif projected > available * 0.85:
        logger.warning("[OllamaFit] tight fit — %s. Close other workloads.", detail)
    else:
        logger.info("[OllamaFit] ok — %s", detail)


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

        # Apply the SAME profile knobs the tools path uses. Ollama keys a loaded
        # runner on (model, params): a request with a different num_ctx is a
        # different runner, so under OLLAMA_MAX_LOADED_MODELS=1 it EVICTS the
        # resident one and reloads from scratch.
        #
        # This path is the judge, the grader's verifier and the exit ticket;
        # the tools path is the tutor. Left unpinned here, every graded turn
        # cost two full reloads — tutor at 16384, judge at Ollama's 4096
        # default, tutor again at 16384. Measured on the Jetson 2026-07-28:
        # 14 loads across a handful of turns, alternating 16384 / 4096 cells.
        # The tutor's own tag hid this, because its Modelfile pins 16384 and
        # both paths therefore agreed by accident.
        options = {
            "temperature": resolved_temp,
            "num_predict": max_tokens or self.config.max_tokens,
        }
        _profile_ctx = None
        try:
            from apps.llm.model_profiles import get_model_profile
            _prov = str(getattr(self.config, 'provider', '') or '').lower()
            _prof = get_model_profile(
                f'{_prov}/{self.config.model_name}' if _prov else self.config.model_name
            )
            if _prof is not None:
                if _prof.num_ctx is not None:
                    options['num_ctx'] = _profile_ctx = _prof.num_ctx
                if _prof.num_gpu is not None:
                    options['num_gpu'] = _prof.num_gpu
        except Exception as exc:
            logger.warning("[Ollama] profile lookup failed for %s: %s",
                           self.config.model_name, exc)

        _ollama_fit_preflight(api_base, self.config.model_name, _profile_ctx)

        try:
            response = requests.post(
                url,
                json={
                    "model": self.config.model_name,
                    "messages": ollama_messages,
                    "stream": False,
                    "options": options,
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

    def _stream_chat(self, url: str, payload: dict, on_delta) -> dict:
        """Drive Ollama's streaming /api/chat and rebuild the buffered shape.

        Ollama's stream is NDJSON: one JSON object per line, each with a
        partial ``message``, ending with a line carrying ``done: true`` plus
        the per-phase counters and durations. Reassembling those into the
        same dict the non-streaming call returns is what keeps
        `_adapt_ollama_response` — and therefore `_dispatch_tools`, the
        tool-leak recovery, and the timing log — one code path.

        Two details that are easy to get wrong:

        * ``message.thinking`` is accumulated but NEVER passed to on_delta.
          The hybrid Qwen3.5 templates emit a reasoning channel; forwarding
          it would stream a monologue to a student.
        * Ollama emits WHOLE tool-call objects per line, not the
          partial-JSON argument fragments the OpenAI streaming protocol
          uses, so no incremental JSON parser is needed here. If that ever
          changes, `_adapt_ollama_response` will start seeing malformed
          arguments — that is the symptom to look for.
        """
        import requests

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list = []
        final: dict = {}
        role = 'assistant'

        with requests.post(url, json=payload, stream=True, timeout=600) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    # A partial or malformed line is not fatal: the final
                    # line carries the authoritative counters, and dropping
                    # a delta only costs preview fidelity.
                    logger.warning("[OllamaStream] unparsable line: %r", line[:200])
                    continue
                if chunk.get('error'):
                    raise RuntimeError(f"Ollama stream error: {chunk['error']}")

                msg = chunk.get('message') or {}
                role = msg.get('role') or role
                piece = msg.get('content') or ''
                if piece:
                    content_parts.append(piece)
                    try:
                        on_delta(piece)
                    except Exception:
                        # A transport-side failure must never abort
                        # generation — the buffered result is still good.
                        logger.exception("[OllamaStream] on_delta raised")
                if msg.get('thinking'):
                    thinking_parts.append(msg['thinking'])
                if msg.get('tool_calls'):
                    tool_calls.extend(msg['tool_calls'])

                if chunk.get('done'):
                    final = chunk

        data = dict(final)
        message = {'role': role, 'content': ''.join(content_parts)}
        if tool_calls:
            message['tool_calls'] = tool_calls
        if thinking_parts:
            message['thinking'] = ''.join(thinking_parts)
        data['message'] = message
        if not final:
            logger.warning(
                "[OllamaStream] stream ended with no done-line — token "
                "counts and durations will read 0",
            )
        return data

    def generate_with_tools(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int | None = None,
        *,
        tool_choice: dict | str | None = None,
        sampling: dict | None = None,
        on_delta=None,
    ):
        """Ollama tool-calling. Accepts the same Anthropic-style tool
        schema ``{name, description, input_schema}`` the tutor engine
        builds, converts to Ollama's ``{type:function, function:{...}}``
        shape, and returns an AdaptedMessage so the engine can introspect
        tool_use content blocks uniformly (task #245).

        ``tool_choice`` is accepted for signature parity but NOT
        forwarded — Ollama's forced-tool support is unreliable across
        models, and the tutor prompt already directs tool use.

        ``on_delta`` — when set, the request is made with ``stream: true``
        and each text delta is handed to the callback as it arrives. The
        RETURN VALUE IS UNCHANGED: the final NDJSON line is folded back
        into the same ``data`` dict the buffered path builds, so
        `_adapt_ollama_response` and every caller downstream are identical
        either way. Callers must treat deltas as a preview — the engine
        still post-processes the complete text before it is safe to show
        (see simple_tutor/stream_filter.py).
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

        # tool_choice: Ollama's /api/chat has no portable forced-tool field, so
        # we cannot honour it. Say so out loud rather than dropping it silently
        # — the engine relies on forcing to guarantee a gradable question slot,
        # and a silent no-op is what let every local Qwen model narrate its
        # questions as prose through the whole Eval-3 sweep. The engine's
        # pose-repair path is the compensating mechanism. Set
        # OLLAMA_FORWARD_TOOL_CHOICE=1 to forward it anyway on builds that
        # accept it.
        if tool_choice:
            if os.environ.get('OLLAMA_FORWARD_TOOL_CHOICE') == '1':
                logger.info("[OllamaTools] forwarding tool_choice=%s (opt-in)", tool_choice)
            else:
                logger.warning(
                    "[OllamaTools] tool_choice=%s requested but NOT forwarded — "
                    "Ollama has no portable forced-tool field; the engine's "
                    "pose-repair path compensates", tool_choice,
                )
                tool_choice = None

        api_base = self.config.api_base or "http://localhost:11434"
        url = f"{api_base}/api/chat"
        # Eval-only per-family sampling (apps/llm/model_profiles); production
        # passes none, so options stay the config temperature + num_predict.
        sampling = sampling or {}
        num_predict = max_tokens or self.config.max_tokens
        options = {
            'temperature': (
                sampling['temperature'] if sampling.get('temperature') is not None
                else self.config.temperature
            ),
            'num_predict': num_predict,
            # num_ctx = the FULL context window (prompt + generation). Ollama
            # defaults to 4096, which is fatal for reasoning models: the ~2K-token
            # prompt leaves ~2K for output, the <think> trace consumes it, and the
            # answer is truncated away → empty completion → the engine serves its
            # "Let's keep going." placeholder (qwen3.5 lost 34-48/60 turns this way,
            # inverting the size curve). Size it to hold the prompt + num_predict +
            # the two-call tool-result history. Non-thinking models (qwen2.5) are
            # unaffected — they answered in <400 tokens and already fit. A profile
            # may pin it via sampling['num_ctx'].
            'num_ctx': sampling.get('num_ctx') or max(8192, (num_predict or 2048) + 8192),
        }
        # An unpinned num_ctx means no exact-spec profile matched and we fell
        # through to a FAMILY_PATTERNS entry sized for cloud endpoints. On a
        # memory-constrained box that is silently fatal — the derived window is
        # what OOMs an 8 GB Jetson ("cannot allocate CUDA0 buffer"), and the
        # whole Eval-3 sweep measured qwen3.5:4b that way without anyone
        # noticing. Say it out loud rather than no-op'ing into an OOM.
        if sampling.get('num_ctx') is None:
            logger.warning(
                "[OllamaTools] no num_ctx pinned for %s — derived %d from "
                "num_predict=%s. Add an exact-spec entry to MODEL_PROFILES if "
                "this model runs on a memory-constrained box.",
                self.config.model_name, options['num_ctx'], num_predict,
            )
        # num_gpu = layers to offload. Left unset, Ollama autofits against FREE
        # memory at load time, so the same model lands anywhere from 17/34 to
        # 34/34 layers on GPU depending on what else happened to be running.
        # Decode throughput tracks it almost linearly (measured on Jetson Orin
        # Nano Super, qwen3.5:4b: 25/34 → 6.7 tok/s, 34/34 → 13.5 tok/s), so an
        # unpinned value makes latency non-reproducible for reasons invisible
        # from the app. Pin it in the profile on boxes where the fit is known.
        if sampling.get('num_gpu') is not None:
            options['num_gpu'] = sampling['num_gpu']
        if sampling.get('top_p') is not None:
            options['top_p'] = sampling['top_p']
        if sampling.get('top_k') is not None:
            options['top_k'] = sampling['top_k']
        if sampling.get('presence_penalty') is not None:
            options['presence_penalty'] = sampling['presence_penalty']
        payload = {
            'model': self.config.model_name,
            'messages': ollama_messages,
            'tools': ollama_tools,
            'stream': bool(on_delta),
            'options': options,
        }
        # Ollama's top-level `think` flag (distinct from `options`), set by a
        # profile's ollama_think. The vLLM chat_template_kwargs toggle in
        # extra_body is ignored by Ollama, so this is the only `think` lever
        # here. Omitted entirely when None (model default).
        #
        # Caveat — it depends on the model's chat template, so verify per model
        # (measured 2026-07-26, Ollama 0.30.7):
        #   - Hybrid templates that gate on `Think` (qwen3:8b, qwen3.5:4b/9b)
        #     pre-close the block as `<think>\n\n</think>` and honour it
        #     correctly: thinking is empty and content is the direct answer.
        #   - Templates that open `<think>` UNCONDITIONALLY (qwen3:4b, i.e.
        #     Qwen3-4B-Thinking-2507) do not. There `think: false` only turns
        #     off Ollama's thinking *parser*, so the model still reasons and the
        #     monologue lands in message.content, pushing the real answer past
        #     num_predict. For those, pick a non-thinking checkpoint instead —
        #     see the local_ollama/qwen3-4b-jetson profile.
        if sampling.get('think') is not None:
            payload['think'] = bool(sampling['think'])
        if tool_choice:
            # OpenAI-shaped; only reached under OLLAMA_FORWARD_TOOL_CHOICE=1.
            if isinstance(tool_choice, dict) and tool_choice.get('name'):
                payload['tool_choice'] = {
                    'type': 'function',
                    'function': {'name': tool_choice['name']},
                }
            elif isinstance(tool_choice, str):
                payload['tool_choice'] = tool_choice
        logger.info(
            "[OllamaTools] llm_call: messages=%d system_chars=%d tools=%d model=%s",
            len(ollama_messages), len(system_prompt or ''),
            len(ollama_tools), self.config.model_name,
        )
        _ollama_fit_preflight(api_base, self.config.model_name, options['num_ctx'])
        try:
            if on_delta:
                data = self._stream_chat(url, payload, on_delta)
            else:
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
        # Ollama returns per-phase nanosecond timings on every response and we
        # were discarding them, so every latency claim about this box had to be
        # reconstructed from wall-clock curl probes — which is how a cold-start
        # prefill got mistaken for a per-turn cache penalty. prefill and decode
        # have completely different fixes (prompt size vs reply length), so a
        # number that conflates them is worse than none.
        _ns = 1_000_000_000
        pd = (data.get('prompt_eval_duration') or 0) / _ns
        ed = (data.get('eval_duration') or 0) / _ns
        ld = (data.get('load_duration') or 0) / _ns
        logger.info(
            "[OllamaTools] response: in=%d out=%d blocks=%s "
            "prefill=%.2fs(%.0f tok/s) decode=%.2fs(%.1f tok/s) load=%.2fs",
            adapted.usage.input_tokens, adapted.usage.output_tokens,
            [getattr(b, 'type', '?') for b in adapted.content],
            pd, (adapted.usage.input_tokens / pd) if pd else 0.0,
            ed, (adapted.usage.output_tokens / ed) if ed else 0.0,
            ld,
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

        Two jobs:
        1. Multimodal user messages: `{"type":"image",...}` →
           `{"type":"image_url","image_url":{"url":"data:<mime>;base64,..."}}`
           (OpenAI rejects the Anthropic image shape). Task #247.
        2. The tutor's two-call tool loop re-sends Call-1's assistant
           `tool_use` blocks + a user `tool_result` block. These arrive as
           either dicts (engine-built) OR the module's ``Adapted*`` dataclass
           objects. The old code passed non-dict blocks through verbatim,
           which the OpenAI SDK then failed to JSON-serialise
           (``AdaptedToolUseBlock is not JSON serializable``) — breaking
           Call 2 for every Vertex/OpenAI model. We now fold the continuation
           into plain text turns: the ``tool_result`` verdict becomes a user
           turn (that is what Call 2 needs to compose its reply) and the bare
           ``tool_use`` call is dropped (surfacing the call syntax would also
           teach the model to leak it). The structured function-call protocol
           is not required just to author a text reply.

        Handles dict and ``Adapted*`` blocks; string contents pass through.
        """
        translated: list[dict] = []
        for msg in messages or []:
            role = msg.get('role', 'user')
            content = msg.get('content')
            if not isinstance(content, list):
                # Plain-string content — OpenAI accepts as-is (judge/regen path).
                translated.append(msg)
                continue
            new_blocks: list[dict] = []       # text / image_url parts
            tool_result_texts: list[str] = []  # rendered grading verdict(s)
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
                        new_blocks.append({"type": "text", "text": t})
                elif btype == 'image':
                    src = getv('source') or {}
                    media_type = (src.get('media_type') if isinstance(src, dict) else None) or 'image/png'
                    stype = src.get('type') if isinstance(src, dict) else None
                    if stype == 'base64':
                        url = f"data:{media_type};base64,{src.get('data') or ''}"
                    elif stype == 'url':
                        url = src.get('url') or ''
                    else:
                        logger.warning(
                            "[OpenAITranslate] unknown image source type=%s — dropping", stype)
                        continue
                    new_blocks.append({"type": "image_url", "image_url": {"url": url}})
                elif btype == 'image_url':
                    iu = getv('image_url') or {}
                    new_blocks.append({"type": "image_url", "image_url": iu})
                elif btype == 'tool_use':
                    continue  # fold away — do not surface tool-call syntax
                elif btype == 'tool_result':
                    c = getv('content')
                    tool_result_texts.append(
                        c if isinstance(c, str) else json.dumps(c, default=str))
            if tool_result_texts:
                # Call-2 user turn carrying the grading verdict.
                extra = ' '.join(b['text'] for b in new_blocks if b.get('type') == 'text')
                text = '\n'.join(tool_result_texts)
                translated.append({'role': 'user', 'content': (text + ('\n' + extra if extra else '')).strip()})
            elif role == 'assistant':
                # Call-1 assistant turn: keep its text; placeholder if empty so
                # the turn isn't blank (some providers reject empty content).
                text = ' '.join(b['text'] for b in new_blocks if b.get('type') == 'text').strip()
                translated.append({'role': 'assistant', 'content': text or '...'})
            else:
                translated.append({**msg, 'content': new_blocks})
        return translated

    def _build_completion_kwargs(
        self,
        *,
        max_tokens: int | None,
        temperature: float | None,
        sampling: dict | None = None,
    ) -> dict:
        """Return the kwargs to pass to chat.completions.create that
        match the target model's expectations:
          - new-gen (GPT-5+, o-series): max_completion_tokens, no
            temperature override (model reasons internally)
          - legacy: max_tokens + temperature

        ``sampling`` (eval-only, from apps/llm/model_profiles) carries
        per-family overrides: temperature / top_p / presence_penalty go
        as top-level Chat Completions params; ``top_k`` and any provider
        thinking-mode toggle ride ``extra_body`` (the OpenAI-compatible
        Vertex MaaS endpoint forwards extra_body into the request JSON).
        Absent keys fall back to the existing behaviour — production
        callers pass no sampling and are unaffected.
        """
        resolved_max = max_tokens or self.config.max_tokens
        if self._is_new_generation():
            # New-gen — only max_completion_tokens, no temperature/sampling.
            return {"max_completion_tokens": resolved_max}
        sampling = sampling or {}
        # Legacy: max_tokens + temperature (sampling overrides when present)
        if sampling.get("temperature") is not None:
            resolved_temp = sampling["temperature"]
        elif temperature is not None:
            resolved_temp = temperature
        else:
            resolved_temp = self.config.temperature
        out = {"max_tokens": resolved_max, "temperature": resolved_temp}
        if sampling.get("top_p") is not None:
            out["top_p"] = sampling["top_p"]
        if sampling.get("presence_penalty") is not None:
            out["presence_penalty"] = sampling["presence_penalty"]
        # top_k is not a Chat Completions param; fold it + any thinking
        # toggle into extra_body for the OpenAI-compatible endpoint.
        extra_body = dict(sampling.get("extra_body") or {})
        if sampling.get("top_k") is not None:
            extra_body["top_k"] = sampling["top_k"]
        if extra_body:
            out["extra_body"] = extra_body
        return out

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
        sampling: dict | None = None,
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
            max_tokens=max_tokens, temperature=None, sampling=sampling,
        )
        logger.info(
            "[OpenAITools] llm_call: model=%s max_tokens=%s sampling=%s",
            self.config.model_name,
            completion_kwargs.get("max_tokens") or completion_kwargs.get("max_completion_tokens"),
            sampling or {},
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
            response, model_name=self.config.model_name, tools=tools,
        )


class VertexModelGardenClient(OpenAIClient):
    """DeepSeek / Kimi / etc. served by Vertex AI Model Garden (MaaS) over the
    OpenAI-compatible endpoint.

    Auth is Application Default Credentials (`google.auth`) with an hourly
    OAuth token; the base URL is per-region. Overrides the two generate methods
    to parse from the RAW JSON body and RETRY on empty `choices` — the OpenAI
    SDK's parsed `ChatCompletion.choices` is intermittently None for DeepSeek
    MaaS even when the body is valid (verified 2026-06-17). Reuses the parent's
    message translation + tool-schema/tool_choice handling.
    """

    _SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
    EMPTY_MAX_RETRIES = 3
    EMPTY_RETRY_BACKOFF = [1, 2, 4]  # seconds

    def __init__(self, config: ModelConfig):
        # Skip OpenAIClient.__init__ (it builds a static-key client + raises on
        # empty key). Grandparent sets config + api_key('').
        BaseLLMClient.__init__(self, config)
        project = (os.environ.get('GOOGLE_CLOUD_PROJECT', '') or '').strip()
        if not project:
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT must be set for the vertex_model_garden "
                "provider (Vertex AI Model Garden MaaS)."
            )
        self._project = project
        self._location = (
            (os.environ.get('GOOGLE_CLOUD_LOCATION', '') or '').strip()
            or 'us-central1'
        )
        import google.auth
        self._creds, _ = google.auth.default(scopes=self._SCOPES)
        host = ('aiplatform.googleapis.com' if self._location == 'global'
                else f'{self._location}-aiplatform.googleapis.com')
        self._base_url = (
            f"https://{host}/v1/projects/{self._project}"
            f"/locations/{self._location}/endpoints/openapi"
        )

    def _get_api_key(self) -> str:
        # Credential is the OAuth token (see `client`), not a static env key.
        return ''

    @property
    def client(self):
        """Fresh OpenAI client pointed at the Vertex MaaS endpoint, with a
        refreshed OAuth token (tokens expire ~hourly — refresh when invalid)."""
        import google.auth.transport.requests
        import openai
        if not self._creds.valid:
            self._creds.refresh(google.auth.transport.requests.Request())
        return openai.OpenAI(base_url=self._base_url, api_key=self._creds.token)

    def _create_raw(self, **kwargs) -> dict:
        """POST chat/completions via `with_raw_response`, return the parsed JSON
        dict. Retry when `choices` is empty/None (intermittent DeepSeek MaaS
        failure). Raise after the budget."""
        for attempt in range(self.EMPTY_MAX_RETRIES + 1):
            # Re-read the `client` property each attempt so a token that expires
            # mid-retry is refreshed (the property refreshes when invalid).
            raw = self.client.chat.completions.with_raw_response.create(**kwargs)
            data = json.loads(raw.text)
            if data.get('choices'):
                return data
            logger.warning(
                "[VertexMaaS] empty choices attempt=%d/%d model=%s body=%s",
                attempt + 1, self.EMPTY_MAX_RETRIES + 1,
                self.config.model_name, (raw.text or '')[:200],
            )
            if attempt < self.EMPTY_MAX_RETRIES:
                time.sleep(self.EMPTY_RETRY_BACKOFF[attempt])
        raise RuntimeError(
            "Vertex MaaS returned empty choices after "
            f"{self.EMPTY_MAX_RETRIES + 1} attempts: {self.config.model_name}"
        )

    def _generate_impl(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        openai_messages = [{"role": "system", "content": system_prompt}]
        openai_messages.extend(self._translate_messages_for_openai(messages))
        completion_kwargs = self._build_completion_kwargs(
            max_tokens=max_tokens, temperature=temperature,
        )
        data = self._create_raw(
            model=self.config.model_name,
            messages=openai_messages,
            **completion_kwargs,
        )
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return LLMResponse(
            content=msg.get("content") or "",
            tokens_in=usage.get("prompt_tokens", 0) or 0,
            tokens_out=usage.get("completion_tokens", 0) or 0,
            model=data.get("model") or self.config.model_name,
            stop_reason=choice.get("finish_reason"),
        )

    def generate_with_tools(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int | None = None,
        *,
        tool_choice: dict | str | None = None,
        sampling: dict | None = None,
    ):
        # Anthropic-style tool schema -> OpenAI function schema. Duplicated from
        # OpenAIClient per the project's Rule of Three (2nd use; don't extract).
        openai_tools = [{
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        } for t in (tools or [])]

        openai_messages = [{"role": "system", "content": system_prompt}]
        openai_messages.extend(self._translate_messages_for_openai(messages))
        kwargs = dict(
            model=self.config.model_name,
            messages=openai_messages,
            tools=openai_tools,
        )
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "tool" and tool_choice.get("name"):
                kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tool_choice["name"]},
                }
            elif tool_choice.get("type") in ("any", "required"):
                kwargs["tool_choice"] = "required"
            elif tool_choice.get("type") == "none":
                kwargs["tool_choice"] = "none"
        elif isinstance(tool_choice, str):
            low = tool_choice.strip().lower()
            if low == "any":
                kwargs["tool_choice"] = "required"
            elif low in ("required", "none"):
                kwargs["tool_choice"] = low

        completion_kwargs = self._build_completion_kwargs(
            max_tokens=max_tokens, temperature=None, sampling=sampling,
        )
        logger.info(
            "[VertexMaaSTools] llm_call: model=%s max_tokens=%s sampling=%s",
            self.config.model_name,
            completion_kwargs.get("max_tokens") or completion_kwargs.get("max_completion_tokens"),
            sampling or {},
        )
        data = self._create_raw(**kwargs, **completion_kwargs)
        return _adapt_openai_dict(data, model_name=self.config.model_name, tools=tools)


class GeminiClient(BaseLLMClient):
    """Client for Google's Gemini API."""

    # Retry policy for transient Google capacity issues (Phase 2b of
    # task #229). 503 UNAVAILABLE is common during peak hours; 429
    # is per-project quota; 5xx are server-side. The judge fallback
    # chain handles judge calls when the retries exhaust, but the
    # tutor path has no fallback so retries are the only defense.
    # GEMINI_MAX_RETRIES env override lets ops fail-fast to the judge fallback
    # chain when Gemini is degraded/quota-exhausted (avoids ~19s of wasted
    # backoff per call before cascading). Defaults to 3.
    MAX_RETRIES = int(os.environ.get('GEMINI_MAX_RETRIES', 3))
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
        """Map chat messages to Gemini Content objects, supporting multimodal.

        Handles dict AND the module's ``Adapted*`` dataclass blocks. The tutor
        two-call loop re-sends Call-1's ``tool_use`` blocks + a user
        ``tool_result`` block; the old code called ``block.get('type')`` on the
        dataclass objects (``AttributeError: 'AdaptedToolUseBlock' object has no
        attribute 'get'``), crashing Call 2 for every Gemini model. We fold the
        continuation into plain text turns — the ``tool_result`` verdict becomes
        a user turn (what Call 2 needs) and the bare ``tool_use`` call is dropped
        — which also preserves Gemini's required user/model role alternation.
        """
        import base64
        from google.genai import types
        contents = []
        for msg in messages:
            role_in = msg.get('role', 'user')
            content = msg.get('content')

            if not isinstance(content, list):
                contents.append(types.Content(
                    role='model' if role_in == 'assistant' else 'user',
                    parts=[types.Part(text=str(content))],
                ))
                continue

            parts = []
            tool_result_texts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get('type')
                    getv = block.get
                else:
                    btype = getattr(block, 'type', None)
                    getv = lambda k, d=None, _b=block: getattr(_b, k, d)
                if btype == 'text':
                    t = getv('text')
                    if t:
                        parts.append(types.Part(text=t))
                elif btype == 'image':
                    source = getv('source') or {}
                    parts.append(types.Part(inline_data=types.Blob(
                        mime_type=(source.get('media_type', 'image/jpeg') if isinstance(source, dict) else 'image/jpeg'),
                        data=base64.b64decode(source.get('data', '') if isinstance(source, dict) else ''),
                    )))
                elif btype == 'image_url':
                    iu = getv('image_url') or {}
                    url = iu.get('url', '') if isinstance(iu, dict) else ''
                    if url.startswith('data:'):
                        header, b64_data = url.split(',', 1)
                        mime = header.split(':')[1].split(';')[0]
                        parts.append(types.Part(inline_data=types.Blob(
                            mime_type=mime, data=base64.b64decode(b64_data))))
                elif btype == 'tool_use':
                    continue  # fold away
                elif btype == 'tool_result':
                    c = getv('content')
                    tool_result_texts.append(
                        c if isinstance(c, str) else json.dumps(c, default=str))

            if tool_result_texts:
                preamble = ' '.join(getattr(p, 'text', '') or '' for p in parts).strip()
                text = '\n'.join(tool_result_texts) + (('\n' + preamble) if preamble else '')
                contents.append(types.Content(role='user', parts=[types.Part(text=text.strip())]))
            else:
                if not parts:
                    parts = [types.Part(text='...')]  # avoid empty parts
                contents.append(types.Content(
                    role='model' if role_in == 'assistant' else 'user', parts=parts))
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
        sampling: dict | None = None,
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
        )
        # Temperature: production (no sampling) keeps the config temperature
        # for back-compat. Eval (sampling provided) uses the per-family value,
        # or OMITS temperature when None so Gemini uses its own default — the
        # framework says leave Gemini-3 at the default (do not send 0.0).
        if sampling is None:
            config_kwargs["temperature"] = self.config.temperature
        else:
            if sampling.get("temperature") is not None:
                config_kwargs["temperature"] = sampling["temperature"]
            if sampling.get("top_p") is not None:
                config_kwargs["top_p"] = sampling["top_p"]
            if sampling.get("top_k") is not None:
                config_kwargs["top_k"] = sampling["top_k"]
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
            response, model_name=self.config.model_name, tools=tools,
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
    elif config.provider == ModelConfig.Provider.VERTEX_MODEL_GARDEN:
        return VertexModelGardenClient(config)

    raise ValueError(f"Unsupported provider: {config.provider}")
