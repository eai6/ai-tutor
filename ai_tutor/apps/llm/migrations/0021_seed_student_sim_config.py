"""Seed a Gemini 2.5 Flash ModelConfig for `Purpose.STUDENT_SIM`.

The synthetic-student simulator (`apps/tutoring/student_sim/`) drives
end-to-end tutoring sessions with an LLM playing the student persona.
Defaults to Gemini 2.5 Flash because it's cheap (~$0.075 in / $0.30 out
per 1M tokens), fast, and well-suited to instruction-following persona
prompts.

Idempotent — running twice doesn't duplicate the row, and won't clobber
an institution's customised STUDENT_SIM config.

See memory/llm_student_simulator_plan.md.
"""
from django.db import migrations


_SEED_NAME = "Gemini 2.5 Flash — Student Sim (auto-seeded)"
_PROVIDER = 'google'
_MODEL = 'gemini-2.5-flash'
_TEMPERATURE = 0.7
_MAX_TOKENS = 600


def _seed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    Institution = apps.get_model('accounts', 'Institution')

    institutions = list(Institution.objects.all())
    if not institutions:
        print("  [llm.0021] no institutions exist — skipping seed")
        return

    n_seeded = 0
    for inst in institutions:
        existing = ModelConfig.objects.filter(
            institution=inst,
            purpose='student_sim',
            is_active=True,
        ).first()
        if existing:
            continue
        ModelConfig.objects.create(
            institution=inst,
            name=_SEED_NAME,
            provider=_PROVIDER,
            model_name=_MODEL,
            api_key_env_var='GOOGLE_API_KEY',
            api_key_encrypted='',
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            purpose='student_sim',
            is_active=True,
        )
        n_seeded += 1
    print(f"  [llm.0021] seeded {n_seeded} STUDENT_SIM config(s)")


def _unseed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    deleted, _ = ModelConfig.objects.filter(
        purpose='student_sim', name=_SEED_NAME,
    ).delete()
    print(f"  [llm.0021 backwards] removed {deleted} auto-seeded row(s)")


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0020_add_student_sim_purpose'),
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(_seed, _unseed),
    ]
