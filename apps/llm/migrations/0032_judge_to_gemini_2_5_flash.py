"""Update the judge ModelConfig from the retired
``gemini-3.1-flash-lite-preview`` to the live ``gemini-2.5-flash``.

Google retired the 3.1 Flash Lite Preview model. Every judge call has
been returning ``404 NOT_FOUND. {'error': {'code': 404, 'message':
'This model models/gemini-3.1-flash-lite-preview is no longer
available...'}}``. The fallback cascade (``judge_fallback`` →
``judge_fallback_2``) eventually picked up, but the failed primary
call added latency and was the suspected cause of the exit-ticket
batch grader hanging on a fresh staging session (M12.9 E2E).

Picked ``gemini-2.5-flash``:
  - Same vendor (no client / key changes)
  - Stable model name (not a preview)
  - Comparable latency to 3.1 Flash Lite
  - Already used pre-0028 (this restores that name)

The fallback chain is unchanged:
  judge            google/gemini-2.5-flash                  @ 0.0
  judge_fallback   google/gemini-3.5-flash                  @ 0.0
  judge_fallback_2 anthropic/claude-haiku-4-5-20251001     @ 0.0

Reversible: backwards step restores the (broken) 3.1 Flash Lite
Preview name so the migration is symmetric — though there is no good
reason to roll back since the source model is dead.

Refs: memory/simple_tutor_pose_grade_architecture_research.md
"""

from django.db import migrations


def _swap_judge_to_2_5_flash(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')

    n = ModelConfig.objects.filter(
        is_active=True,
        purpose='judge',
        model_name='gemini-3.1-flash-lite-preview',
    ).update(
        provider='google',
        model_name='gemini-2.5-flash',
        temperature=0.0,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    print(
        f"  [llm.0032] judge: gemini-3.1-flash-lite-preview "
        f"→ gemini-2.5-flash ({n} row(s))"
    )


def _restore_3_1_flash_lite(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    n = ModelConfig.objects.filter(
        is_active=True,
        purpose='judge',
        model_name='gemini-2.5-flash',
    ).update(model_name='gemini-3.1-flash-lite-preview')
    print(
        f"  [llm.0032] reverse: gemini-2.5-flash "
        f"→ gemini-3.1-flash-lite-preview ({n} row(s))"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0031_partial_rollback_tutoring_to_opus'),
    ]

    operations = [
        migrations.RunPython(_swap_judge_to_2_5_flash, _restore_3_1_flash_lite),
    ]
