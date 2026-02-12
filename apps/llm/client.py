"""
LLM Client - Abstracts calls to different LLM providers.

Design decisions:
- Factory pattern: get_client(model_config) returns the right client
- All clients share the same interface: generate(messages, system_prompt)
- Handles API key lookup from environment variables
- Returns structured response with content + token usage

Starting with Anthropic, easy to add OpenAI/Ollama later.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import anthropic

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
        key = os.getenv(self.config.api_key_env_var, '')
        if not key:
            raise ValueError(
                f"API key not found. Set {self.config.api_key_env_var} environment variable."
            )
        return key
    
    @abstractmethod
    def generate(
        self,
        messages: list[dict],
        system_prompt: str,
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


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic's Claude API."""
    
    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self.client = anthropic.Anthropic(api_key=self.api_key)
    
    def generate(
        self,
        messages: list[dict],
        system_prompt: str,
    ) -> LLMResponse:
        """Call Claude API and return standardized response."""
        
        response = self.client.messages.create(
            model=self.config.model_name,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=system_prompt,
            messages=messages,
        )
        
        return LLMResponse(
            content=response.content[0].text,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            model=response.model,
            stop_reason=response.stop_reason,
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
    
    # TODO: Add more providers
    # elif config.provider == ModelConfig.Provider.OPENAI:
    #     return OpenAIClient(config)
    # elif config.provider == ModelConfig.Provider.LOCAL_OLLAMA:
    #     return OllamaClient(config)
    
    raise ValueError(f"Unsupported provider: {config.provider}")
