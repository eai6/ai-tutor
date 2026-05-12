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
import json
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Generator
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

    def _stream_kwargs(self, max_tokens: int, system_prompt: str, messages: list[dict],
                       temperature: float | None = None) -> dict:
        kwargs = dict(
            model=self.config.model_name,
            max_tokens=max_tokens,
            system=system_prompt,
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

                return LLMResponse(
                    content=full_content,
                    tokens_in=final_message.usage.input_tokens,
                    tokens_out=final_message.usage.output_tokens,
                    model=final_message.model,
                    stop_reason=final_message.stop_reason,
                )

            except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
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
    ):
        """Non-streaming call that returns the full Anthropic Message
        object (not just text) so the caller can introspect tool_use
        content blocks.

        Used by the tutor's pose_question tool flow — the LLM emits a
        tool_use block to pose a verified bank question and the server
        renders it. Streaming is intentionally NOT used here because
        the tool-use flow needs the whole message structure, not a
        text stream.

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
            system=system_prompt,
            messages=messages,
            tools=tools,
        )
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
                logger.info(
                    "[QuestionTool] llm_response: stop_reason=%s in=%d out=%d "
                    "blocks=[%s]",
                    message.stop_reason,
                    message.usage.input_tokens,
                    message.usage.output_tokens,
                    ", ".join(block_summary),
                )
                return message
            except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
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


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI's GPT API."""

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
        openai_messages.extend(messages)

        resolved_temp = temperature if temperature is not None else self.config.temperature

        response = self.client.chat.completions.create(
            model=self.config.model_name,
            max_tokens=max_tokens or self.config.max_tokens,
            temperature=resolved_temp,
            messages=openai_messages,
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
    ):
        """OpenAI function-calling wrapper.

        Accepts the same Anthropic-style tool schema {name, description,
        input_schema} and converts to OpenAI's {type: "function",
        function: {name, description, parameters}} shape. Returns the
        raw ChatCompletion response so the caller can introspect
        choices[0].message.tool_calls (list of tool calls, each with
        .function.name and .function.arguments JSON-string).
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
        openai_messages.extend(messages)

        kwargs = dict(
            model=self.config.model_name,
            messages=openai_messages,
            tools=openai_tools,
        )
        # Reasoning models (o1/o3 family) reject `max_tokens` — they
        # use `max_completion_tokens` and ignore `temperature`. Fall back
        # gracefully on TypeError so we don't have to maintain a list.
        max_t = max_tokens or self.config.max_tokens
        try:
            response = self.client.chat.completions.create(
                **kwargs,
                max_tokens=max_t,
                temperature=self.config.temperature,
            )
        except Exception as e:
            msg = str(e).lower()
            if "max_tokens" in msg or "temperature" in msg:
                # Reasoning model — retry with the supported params only.
                logger.info(
                    "[OpenAITools] retrying without max_tokens/temperature for %s",
                    self.config.model_name,
                )
                response = self.client.chat.completions.create(**kwargs)
            else:
                raise
        logger.info(
            "[OpenAITools] response: model=%s in=%d out=%d finish=%s tool_calls=%d",
            response.model,
            getattr(response.usage, 'prompt_tokens', 0),
            getattr(response.usage, 'completion_tokens', 0),
            response.choices[0].finish_reason,
            len(response.choices[0].message.tool_calls or []),
        )
        return response


class GeminiClient(BaseLLMClient):
    """Client for Google's Gemini API."""

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

        response = self.client.models.generate_content(
            model=self.config.model_name,
            contents=gemini_contents,
            config=types.GenerateContentConfig(**config_kwargs),
        )

        tokens_in = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
        tokens_out = getattr(response.usage_metadata, 'candidates_token_count', 0) or 0

        return LLMResponse(
            content=response.text or '',
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=self.config.model_name,
            stop_reason='stop',
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
    ):
        """Gemini function-calling wrapper.

        Accepts the same Anthropic-style tool schema {name, description,
        input_schema} and converts to Gemini's FunctionDeclaration shape.
        Returns the raw GenerateContentResponse so the caller can
        introspect candidates[0].content.parts for FunctionCall blocks.
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

        response = self.client.models.generate_content(
            model=self.config.model_name,
            contents=gemini_contents,
            config=types.GenerateContentConfig(**config_kwargs),
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
        return response


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
