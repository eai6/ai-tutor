"""Create / activate a ModelConfig row for the teacher-report purpose.

Idempotent — safe to run on every deploy. Defaults to Anthropic Haiku 4.5,
reading the API key from the ANTHROPIC_API_KEY env var other Anthropic configs
already use.

Haiku because the job is small and bounded: two sentences per competency band,
from numbers the platform has already worked out, cached on those numbers so
one distinct report state costs one call. Nothing here reasons — the reasoning
is the exit-ticket matching that produced the bands.

Without this row the report renders the constant per-band templates it always
has; see apps/dashboard/report_instructions.py.

Usage:
    python manage.py seed_teacher_report_model
    python manage.py seed_teacher_report_model --model claude-sonnet-4-6
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Ensure a ModelConfig exists for the teacher_report purpose (Haiku 4.5 by default)."

    def add_arguments(self, parser):
        parser.add_argument('--model', default='claude-haiku-4-5-20251001',
                            help='Anthropic model identifier.')
        parser.add_argument('--api-key-env-var', default='ANTHROPIC_API_KEY',
                            help='Env var name to read the API key from.')

    def handle(self, *args, **options):
        from ai_tutor.apps.accounts.models import Institution
        from ai_tutor.apps.llm.models import ModelConfig

        global_inst = Institution.get_global()
        cfg, created = ModelConfig.objects.update_or_create(
            institution=global_inst,
            purpose=ModelConfig.Purpose.TEACHER_REPORT,
            defaults={
                'name': 'Teacher Report Instructions — Haiku',
                'provider': ModelConfig.Provider.ANTHROPIC,
                'model_name': options['model'],
                'api_key_env_var': options['api_key_env_var'],
                'max_tokens': 1024,
                'temperature': 0.2,
                'is_active': True,
            },
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} ModelConfig for teacher_report: "
            f"{cfg.provider}/{cfg.model_name} (institution={global_inst.id})"
        ))
