"""
LLM app - PromptPack and ModelConfig models.

These models let institutions customize:
1. How the AI tutor behaves (prompts, persona, safety rules)
2. Which LLM provider/model to use
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models

from apps.accounts.models import Institution


class PromptPack(models.Model):
    """
    A collection of prompts that define the tutor's behavior.
    
    Institutions can create multiple packs (e.g., "Friendly Grade 2", 
    "Formal High School") and switch between them.
    
    The prompts are assembled in layers:
    - system_prompt: Core persona and boundaries
    - teaching_style_prompt: How to teach (Socratic, direct, etc.)
    - safety_prompt: Content restrictions
    - format_rules_prompt: Output formatting rules
    """
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='prompt_packs',
        null=True,
        blank=True,
        help_text="Null = platform-wide prompts"
    )
    name = models.CharField(max_length=100, help_text="e.g., 'Friendly K-5 Tutor'")
    
    # Prompt components - assembled into final system prompt
    system_prompt = models.TextField(
        help_text="Core persona and role definition"
    )
    teaching_style_prompt = models.TextField(
        blank=True,
        help_text="Teaching methodology (Socratic, direct instruction, etc.)"
    )
    safety_prompt = models.TextField(
        blank=True,
        help_text="Content restrictions and safety guidelines"
    )
    format_rules_prompt = models.TextField(
        blank=True,
        help_text="Output formatting rules (length, structure, etc.)"
    )

    # Extended prompts — empty means use built-in default
    tutor_system_prompt = models.TextField(
        blank=True, default='',
        help_text="Full tutor system prompt override. Supports {institution_name}, {locale_context}, {tutor_name}, {language}, {grade_level}, {safety_prompt} placeholders."
    )
    content_generation_prompt = models.TextField(
        blank=True, default='',
        help_text="System prompt for lesson content generation."
    )
    exit_ticket_prompt = models.TextField(
        blank=True, default='',
        help_text="System prompt for exit ticket generation."
    )
    grading_prompt = models.TextField(
        blank=True, default='',
        help_text="System prompt for answer grading."
    )
    image_generation_prompt = models.TextField(
        blank=True, default='',
        help_text="Prefix/context for image generation prompts (style, safety, educational context)."
    )

    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', '-updated_at']

    def __str__(self):
        scope = self.institution.slug if self.institution else 'platform'
        return f"{self.name} v{self.version} ({scope})"

    def get_full_system_prompt(self):
        """Assemble all prompt components into the final system prompt."""
        parts = [self.system_prompt]
        if self.teaching_style_prompt:
            parts.append(self.teaching_style_prompt)
        if self.safety_prompt:
            parts.append(self.safety_prompt)
        if self.format_rules_prompt:
            parts.append(self.format_rules_prompt)
        return "\n\n".join(parts)


class ModelConfig(models.Model):
    """
    Configuration for which LLM to use and how to call it.

    Design decision: API keys are stored as references (env var names),
    not actual secrets. The runtime looks up the actual key from env.
    """
    class Provider(models.TextChoices):
        ANTHROPIC = 'anthropic', 'Anthropic (Claude)'
        OPENAI = 'openai', 'OpenAI (GPT)'
        GOOGLE = 'google', 'Google (Gemini)'
        AZURE_OPENAI = 'azure_openai', 'Azure OpenAI'
        LOCAL_OLLAMA = 'local_ollama', 'Local (Ollama)'

    class Purpose(models.TextChoices):
        GENERATION = 'generation', 'Content Generation (Curriculum, Lessons)'
        TUTORING = 'tutoring', 'Student Tutoring'
        EXIT_TICKETS = 'exit_tickets', 'Exit Ticket Generation'
        SKILL_EXTRACTION = 'skill_extraction', 'Skill Extraction'

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='model_configs'
    )
    name = models.CharField(max_length=100, help_text="e.g., 'Default Claude'")
    
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.ANTHROPIC
    )
    model_name = models.CharField(
        max_length=100,
        default='claude-haiku-4-5-20251001',
        help_text="Model identifier (e.g., 'claude-haiku-4-5-20251001', 'claude-sonnet-4-20250514', 'gpt-4o')"
    )
    
    # Connection settings
    api_base = models.URLField(
        blank=True,
        null=True,
        help_text="Custom API endpoint (for Azure, Ollama, etc.)"
    )
    api_key_env_var = models.CharField(
        max_length=100,
        default='ANTHROPIC_API_KEY',
        help_text="Environment variable name containing the API key"
    )
    api_key_encrypted = models.TextField(
        blank=True,
        default='',
        help_text="Fernet-encrypted API key (overrides env var when set)"
    )

    # Generation parameters
    max_tokens = models.PositiveIntegerField(default=1024)
    temperature = models.FloatField(default=0.7)

    purpose = models.CharField(
        max_length=20,
        choices=Purpose.choices,
        default=Purpose.GENERATION,
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', '-updated_at']
        verbose_name = "Model Configuration"

    def __str__(self):
        scope = self.institution.slug if self.institution else 'platform'
        return f"{self.name} - {self.model_name} ({scope})"

    @staticmethod
    def _get_fernet() -> Fernet:
        """Derive a Fernet key from Django SECRET_KEY."""
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)

    def set_api_key(self, raw_key: str):
        """Encrypt and store an API key."""
        if not raw_key:
            self.api_key_encrypted = ''
            return
        f = self._get_fernet()
        self.api_key_encrypted = f.encrypt(raw_key.encode()).decode()

    def get_api_key(self) -> str:
        """Decrypt stored key, falling back to env var."""
        if self.api_key_encrypted:
            try:
                f = self._get_fernet()
                return f.decrypt(self.api_key_encrypted.encode()).decode()
            except Exception:
                pass
        # Fallback to environment variable
        if self.api_key_env_var:
            return os.getenv(self.api_key_env_var, '')
        return ''

    @classmethod
    def get_for(cls, purpose: str):
        """Get active config for a specific purpose, with fallback to any active config."""
        config = cls.objects.filter(is_active=True, purpose=purpose).first()
        if not config:
            config = cls.objects.filter(is_active=True).first()
        return config
