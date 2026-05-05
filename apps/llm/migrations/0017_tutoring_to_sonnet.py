"""Switch the active tutoring ModelConfig from gpt-4o back to Sonnet 4.

Background: in a single week the tutoring model was swapped twice
(Sonnet 4 → Opus 4.7 → gpt-4o). Each swap exposed model-specific
quirks. Notably gpt-4o is more *literal* than Opus — it follows the
explicit `CURRENT STEP DIRECTIVE` block ("deliver this teaching
content + ask comprehension check") and ignores the conversation
history that's embedded as text in the user prompt. Result:
TEACH-step looping where the model re-delivers the same content
turn after turn.

Sonnet 4 was the original tutoring model and behaved well with the
prompt as designed. The bench (scripts/bench_tutor_quality.py)
confirmed:
  - 100% pose_question tool compliance
  - 100% math correctness on the standard angles-around-a-point check
  - ~2× faster than Opus 4.7
  - Closer to the prompt-tuning the codebase was built around

This migration keeps every architectural win from the past week
(pose_question tool, combined judge on Sonnet, EO-first bank scope,
prereq-lesson recap, edit-with-context regen) and just rolls the
tutor model back to Sonnet 4.

Reversible: backwards step restores gpt-4o.
"""

from django.db import migrations


def _to_sonnet(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(is_active=True, purpose='tutoring')
    updated = qs.exclude(
        provider='anthropic', model_name='claude-sonnet-4-20250514',
    ).update(
        provider='anthropic',
        model_name='claude-sonnet-4-20250514',
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )
    print(
        f"  [llm.0017] updated {updated} tutoring row(s) to "
        "anthropic/claude-sonnet-4-20250514"
    )


def _to_gpt4o(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(
        is_active=True, purpose='tutoring',
        provider='anthropic', model_name='claude-sonnet-4-20250514',
    )
    qs.update(
        provider='openai',
        model_name='gpt-4o',
        api_key_env_var='OPENAI_API_KEY',
        api_key_encrypted='',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0016_tutoring_to_gpt4o'),
    ]

    operations = [
        migrations.RunPython(_to_sonnet, _to_gpt4o),
    ]
