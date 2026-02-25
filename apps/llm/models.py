"""
LLM app - PromptPack and ModelConfig models.

These models let institutions customize:
1. How the AI tutor behaves (prompts, persona, safety rules)
2. Which LLM provider/model to use
"""

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
        AZURE_OPENAI = 'azure_openai', 'Azure OpenAI'
        LOCAL_OLLAMA = 'local_ollama', 'Local (Ollama)'

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
        default='claude-sonnet-4-20250514',
        help_text="Model identifier (e.g., 'claude-sonnet-4-20250514', 'gpt-4o')"
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
    
    # Generation parameters
    max_tokens = models.PositiveIntegerField(default=1024)
    temperature = models.FloatField(default=0.7)
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', '-updated_at']
        verbose_name = "Model Configuration"

    def __str__(self):
        return f"{self.name} - {self.model_name} ({self.institution.slug})"
