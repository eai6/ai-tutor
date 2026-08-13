"""Create / activate a ModelConfig row for the help-assistant purpose.

Idempotent — safe to run on every deploy. Defaults to Anthropic
Haiku 4.5 (`claude-haiku-4-5-20251001`), pulling its API key from
the ANTHROPIC_API_KEY env var that other Anthropic configs already
use.

Usage:
    python manage.py seed_help_assistant_model
    python manage.py seed_help_assistant_model --model claude-sonnet-4-6
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Ensure a ModelConfig exists for the help_assistant purpose (Haiku 4.5 by default)."

    def add_arguments(self, parser):
        parser.add_argument('--model', default='claude-haiku-4-5-20251001',
                            help='Anthropic model identifier.')
        parser.add_argument('--api-key-env-var', default='ANTHROPIC_API_KEY',
                            help='Env var name to read the API key from.')

    def handle(self, *args, **options):
        from ai_tutor.apps.llm.models import ModelConfig
        from ai_tutor.apps.accounts.models import Institution

        global_inst = Institution.get_global()
        cfg, created = ModelConfig.objects.update_or_create(
            institution=global_inst,
            purpose=ModelConfig.Purpose.HELP_ASSISTANT,
            defaults={
                'name': 'Help Assistant — Haiku',
                'provider': ModelConfig.Provider.ANTHROPIC,
                'model_name': options['model'],
                'api_key_env_var': options['api_key_env_var'],
                'max_tokens': 1024,
                'temperature': 0.3,
                'is_active': True,
            },
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} ModelConfig for help_assistant: "
            f"{cfg.provider}/{cfg.model_name} (institution={global_inst.id})"
        ))
