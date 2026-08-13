"""Point the tutor at a local Ollama model when no cloud API key is configured.

Runs on every container start (see deploy/compose/docker-compose.yml). It is a
no-op whenever a cloud provider key is present, so a deployment that later adds
one is not overridden by this.

Why a DB row and not TUTOR_MODEL_OVERRIDE
-----------------------------------------
Setting the env var would work, but it short-circuits
``ModelConfig.get_for('tutoring')`` — which makes the admin's model picker
silently ineffective: an administrator saves a change in /admin/ and nothing
happens. ``infra/systemd/ai-tutor.service`` documents that exact trap and
refuses to pin it for the same reason. Seeding an active row instead leaves the
database authoritative, so the picker keeps working.

What this does NOT do
---------------------
It does not seed a ``judge`` config. With no cloud key there is nothing better
to point one at, and ``get_judge_provider_chain`` already falls through to the
tutoring config — which is precisely how the mt100 sweep ended up grading with
a local 4B model (see offline_eval/multi_turn_results/mt100/README.md). That
fallback is acceptable for a self-hosted deployment with no cloud access, but
it is worth knowing that local grading measured ~4 points more lenient than
cloud grading on the same 100 scenarios.
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand

from ai_tutor.apps.llm.models import ModelConfig

CLOUD_KEY_ENV = ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GOOGLE_API_KEY')

DEFAULT_MODEL = 'qwen3-4b-jetson'
DEFAULT_BASE_URL = 'http://ollama:11434'


class Command(BaseCommand):
    help = ('Seed a local Ollama tutoring model when no cloud API key is set, '
            'so the tutor answers out of the box.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--model', default=os.getenv('LOCAL_TUTOR_MODEL', DEFAULT_MODEL),
            help='Ollama tag to point at.')
        parser.add_argument(
            '--base-url', default=os.getenv('OLLAMA_BASE_URL', DEFAULT_BASE_URL),
            help='Where Ollama is listening.')
        parser.add_argument(
            '--force', action='store_true',
            help='Seed even when a cloud key is present.')

    def handle(self, *args, **opts):
        configured = [k for k in CLOUD_KEY_ENV if (os.getenv(k) or '').strip()]
        if configured and not opts['force']:
            self.stdout.write(
                f"[seed_local_tutor] {', '.join(configured)} present — "
                f"leaving model configuration alone."
            )
            return

        model = opts['model']
        base_url = opts['base_url']

        # ModelConfig.institution is NOT NULL, and a fresh install has no
        # institution at all — which is why the seed migrations log
        # "no institutions exist — skipping seed". Institution.get_global()
        # exists for exactly this: a platform-wide row that downstream
        # non-null FKs can point at.
        #
        # Attaching to Global rather than to a school is right here, because
        # `ModelConfig.get_for()` filters on is_active and purpose ONLY — it
        # never filters by institution — so one global row serves every school
        # and none of them has to be created first.
        from ai_tutor.apps.accounts.models import Institution
        institution = Institution.get_global()

        # Deactivate any other active tutoring row first. Two active rows would
        # make get_for('tutoring') depend on insertion order, which is exactly
        # the kind of thing that behaves differently on a rebuilt database.
        ModelConfig.objects.filter(
            is_active=True, purpose=ModelConfig.Purpose.TUTORING,
        ).exclude(provider='local_ollama', model_name=model).update(is_active=False)

        config, created = ModelConfig.objects.update_or_create(
            provider='local_ollama',
            model_name=model,
            purpose=ModelConfig.Purpose.TUTORING,
            defaults={
                'is_active': True,
                'api_base': base_url,
                'institution': institution,
                # Tutoring temperature is clamped to [0.1, 0.3] at the call
                # site regardless (see CLAUDE.md); 0.2 is the midpoint.
                'temperature': 0.2,
            },
        )

        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f"[seed_local_tutor] {verb} tutoring model "
            f"local_ollama/{model} at {base_url}."
        ))
        self.stdout.write(
            "[seed_local_tutor] No cloud API key is set, so the tutor runs on a "
            "local model. This is a fallback so the platform works out of the "
            "box — a small local model is weaker at tutoring than a frontier "
            "cloud model. Add a provider key and restart to switch over."
        )
