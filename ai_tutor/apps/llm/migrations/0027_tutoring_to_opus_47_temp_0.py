"""Switch the active tutoring ModelConfig to Claude Opus 4.7 @ temperature 0.0.

Background: Sonnet 4 was emitting `<tool_use>` XML as prose text instead of
calling pose_question / pose_inline_question — breaking the dry-run +
apply-on-winner tool-use architecture (see commits 39ef40f, 422e40f, the
self-retry stack in apps/tutoring/regen/self_retry.py). Opus 4.7 follows
tool instructions reliably; dropping temperature to 0.0 gives the most
deterministic instruction-following.

Validated 2026-05-17 via session 65 E2E on lesson 540 — every retry cycle
converged clean (score≥-0.5), no XML leaks, 10/10 exit ticket.

Mirrors the pattern from 0017_tutoring_to_sonnet.py. Reversible: backwards
step restores Sonnet 4 @ temp 0.2 (the previous active config per 0019).

Refs: auto-memory/project_tutor_model_choice.md, memory/tutor_self_retry_plan.md
"""

from django.db import migrations


_TARGET_PROVIDER = 'anthropic'
_TARGET_MODEL = 'claude-opus-4-7'
_TARGET_TEMP = 0.0
_TARGET_API_KEY_ENV = 'ANTHROPIC_API_KEY'

_PREV_PROVIDER = 'anthropic'
_PREV_MODEL = 'claude-sonnet-4-20250514'
_PREV_TEMP = 0.2
_PREV_API_KEY_ENV = 'ANTHROPIC_API_KEY'


def _to_opus(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(is_active=True, purpose='tutoring')
    updated = qs.update(
        provider=_TARGET_PROVIDER,
        model_name=_TARGET_MODEL,
        temperature=_TARGET_TEMP,
        api_key_env_var=_TARGET_API_KEY_ENV,
        api_key_encrypted='',
    )
    print(
        f"  [llm.0027] updated {updated} active tutoring row(s) to "
        f"{_TARGET_PROVIDER}/{_TARGET_MODEL} @ temp {_TARGET_TEMP}"
    )


def _to_sonnet(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(
        is_active=True, purpose='tutoring',
        provider=_TARGET_PROVIDER, model_name=_TARGET_MODEL,
    )
    qs.update(
        provider=_PREV_PROVIDER,
        model_name=_PREV_MODEL,
        temperature=_PREV_TEMP,
        api_key_env_var=_PREV_API_KEY_ENV,
        api_key_encrypted='',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0026_add_pedagogy_safety_purposes'),
    ]

    operations = [
        migrations.RunPython(_to_opus, _to_sonnet),
    ]
