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
    """Abstract base class for LLM clients."""
    
    def __init__(self, config: ModelConfig):
        self.config = config
        self.api_key = self._get_api_key()
    
    def _get_api_key(self) -> str:
        """Look up API key from environment variable."""
        if not self.config.api_key_env_var:
            return ""
        key = os.getenv(self.config.api_key_env_var, '')
        return key
    
    @abstractmethod
    def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """
        Generate a response from the LLM.
        
        Args:
            messages: List of {"role": "user"|"assistant", "content": "..."}
            system_prompt: The system prompt to use
            
        Returns:
            LLMResponse with content and token usage
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

    def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Call Claude API using streaming to avoid 10-minute timeout.

        Retries with exponential backoff on rate limit (429) errors.
        """

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                full_content = ""
                with self.client.messages.stream(
                    model=self.config.model_name,
                    max_tokens=max_tokens or self.config.max_tokens,
                    temperature=self.config.temperature,
                    system=system_prompt,
                    messages=messages,
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

            except anthropic.RateLimitError as e:
                if attempt >= self.MAX_RETRIES:
                    raise
                wait = self.RETRY_BACKOFF[attempt]
                logger.warning(
                    f"Rate limited (attempt {attempt + 1}/{self.MAX_RETRIES + 1}), "
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
            model=self.config.model_name,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=system_prompt,
            messages=messages,
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
    
    def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Call local Ollama API and return standardized response."""
        import requests

        # Build Ollama-compatible messages (with system as first message)
        ollama_messages = [{"role": "system", "content": system_prompt}]
        ollama_messages.extend(messages)

        # Ollama API endpoint
        api_base = self.config.api_base or "http://localhost:11434"
        url = f"{api_base}/api/chat"

        try:
            response = requests.post(
                url,
                json={
                    "model": self.config.model_name,
                    "messages": ollama_messages,
                    "stream": False,
                    "options": {
                        "temperature": self.config.temperature,
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

    def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Call OpenAI API and return standardized response."""

        # OpenAI uses system message in the messages array
        openai_messages = [{"role": "system", "content": system_prompt}]
        openai_messages.extend(messages)

        response = self.client.chat.completions.create(
            model=self.config.model_name,
            max_tokens=max_tokens or self.config.max_tokens,
            temperature=self.config.temperature,
            messages=openai_messages,
        )
        
        return LLMResponse(
            content=response.choices[0].message.content,
            tokens_in=response.usage.prompt_tokens,
            tokens_out=response.usage.completion_tokens,
            model=response.model,
            stop_reason=response.choices[0].finish_reason,
        )


class MockLLMClient(BaseLLMClient):
    """
    Mock client for testing without API calls.
    Returns predictable responses based on input.
    """
    
    def _get_api_key(self) -> str:
        return "mock-key"
    
    def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
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
    elif config.provider == ModelConfig.Provider.LOCAL_OLLAMA:
        return OllamaClient(config)
    
    raise ValueError(f"Unsupported provider: {config.provider}")
