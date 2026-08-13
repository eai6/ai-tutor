#!/usr/bin/env python
"""Seed minimal ModelConfig rows for an eval CI run on a fresh DB.

The A/B harness monkey-patches the ``tutoring`` purpose at runtime, but
the engine still calls ``ModelConfig.get_for(...)`` for ``judge`` /
``regen`` / ``generation`` purposes during a synthetic session.
On a fresh SQLite DB those rows don't exist, so seed them here to
mirror the post-merge production stack:

    tutoring        -> Anthropic claude-opus-4-7        (overridden by harness; placeholder)
    regen           -> Anthropic claude-sonnet-4         (PR #7 partial rollback)
    judge           -> Google gemini-3.1-flash-lite-preview
    judge_fallback  -> OpenAI gpt-4o-mini
    judge_fallback_2-> Anthropic claude-haiku-4-5
    generation      -> Anthropic claude-sonnet-4
    student_sim     -> Google gemini-2.5-flash

Idempotent: re-runs update_or_create per purpose.

Run from a checkout where migrations have been applied:

    python manage.py migrate --no-input
    python scripts/seed_eval_modelconfig.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()

from ai_tutor.apps.accounts.models import Institution  # noqa: E402
from ai_tutor.apps.llm.models import ModelConfig  # noqa: E402


# (purpose, provider, model_name, temperature)
SEED_SPEC = [
    ('tutoring',         'anthropic', 'claude-opus-4-7',                  0.0),
    ('regen',            'anthropic', 'claude-sonnet-4-20250514',         0.2),
    ('judge',            'google',    'gemini-3.1-flash-lite-preview',    0.0),
    ('judge_fallback',   'openai',    'gpt-4o-mini',                      0.0),
    ('judge_fallback_2', 'anthropic', 'claude-haiku-4-5-20251001',        0.0),
    ('generation',       'anthropic', 'claude-sonnet-4-20250514',         0.7),
    ('student_sim',      'google',    'gemini-2.5-flash',                 0.7),
]

API_KEY_ENV = {
    'anthropic': 'ANTHROPIC_API_KEY',
    'google':    'GOOGLE_API_KEY',
    'openai':    'OPENAI_API_KEY',
}


def main():
    inst = Institution.objects.filter(slug='global').first()
    if not inst:
        inst = Institution.objects.filter(slug__icontains='global').first()
    if not inst:
        # Fixture-loaded institutions might be Belonie etc.; fall back to whichever exists.
        inst = Institution.objects.order_by('id').first()
    if not inst:
        inst = Institution.objects.create(name='Global', slug='global')

    for purpose, provider, model_name, temp in SEED_SPEC:
        cfg, _ = ModelConfig.objects.update_or_create(
            institution=inst,
            purpose=purpose,
            defaults=dict(
                name=f"Eval seed — {purpose}",
                provider=provider,
                model_name=model_name,
                temperature=temp,
                max_tokens=2048,
                api_key_env_var=API_KEY_ENV[provider],
                api_key_encrypted='',
                is_active=True,
            ),
        )
        print(f"  seeded {purpose:18} -> {provider}/{model_name}")

    print(f"All ModelConfig seeds attached to institution_id={inst.id} ({inst.name!r}).")


if __name__ == '__main__':
    main()
