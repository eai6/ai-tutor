"""Seed an active judge ModelConfig pointing at Sonnet 4.

Background: combined_judge runs once per tutor turn (after the tutor
response generation) to check arithmetic / factual / rule_compliance.
Until now the judge inherited the tutoring model — Opus 4.7 — which
made every turn pay two Opus calls.

The judge is a sanity check, not a primary reasoner. Sonnet 4 has the
necessary structured-output reliability and runs ~5x cheaper / 2x
faster than Opus. Tutor stays on Opus where the reasoning depth
matters; judge swaps to Sonnet so the per-turn latency drops.

This migration:
  - Ensures the global institution has an active judge ModelConfig
    pointing at claude-sonnet-4-20250514 (provider=anthropic).
  - Reuses the API-key setup (env var fallback) — does not store an
    encrypted key in the migration.

Reversible: backwards step deactivates the seeded row.
"""

from django.db import migrations


def _seed_judge(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    Institution = apps.get_model('accounts', 'Institution')

    # Already has an active judge config? Nothing to do.
    if ModelConfig.objects.filter(is_active=True, purpose='judge').exists():
        print("  [llm.0015] active judge ModelConfig already exists — skipping seed")
        return

    global_inst = Institution.objects.filter(slug='global').first()
    if global_inst is None:
        # Fall back to the first institution we can find — better to
        # have a config than to fail silently.
        global_inst = Institution.objects.order_by('id').first()
    if global_inst is None:
        print("  [llm.0015] no institutions exist — skipping seed")
        return

    ModelConfig.objects.create(
        institution=global_inst,
        name='Anthropic - judge',
        provider='anthropic',
        model_name='claude-sonnet-4-20250514',
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
        purpose='judge',
        is_active=True,
    )
    print("  [llm.0015] seeded active judge ModelConfig (sonnet-4)")


def _unseed_judge(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(
        is_active=True, purpose='judge',
        provider='anthropic', model_name='claude-sonnet-4-20250514',
    )
    qs.update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0014_alter_modelconfig_purpose'),
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(_seed_judge, _unseed_judge),
    ]
