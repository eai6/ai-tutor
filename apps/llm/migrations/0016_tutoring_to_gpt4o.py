"""Switch the active tutoring ModelConfig from Opus 4.7 to OpenAI gpt-4o.

Background: a head-to-head bench (scripts/bench_tutoring_models.py +
scripts/bench_tutor_quality.py) on a representative math tutor turn
ranked candidate models on three axes:
  - Latency (median, 3 trials per model)
  - Tool compliance (does the model call pose_question when prompted)
  - Math correctness when self-generating (no tool / no bank)

Result for our workload:
                              tool-call   math    median latency
  gpt-4o                         100%     100%     906 ms  ⭐
  claude-haiku-4-5-20251001      100%     100%    1260 ms
  gpt-4.1                        100%     100%    1346 ms
  claude-sonnet-4-20250514       100%     100%    1992 ms
  claude-opus-4-7  (current)     100%     100%    2969 ms  ← baseline
  gemini-3-pro-preview           100%      0%     7371 ms (failed math)
  o3-mini                        100%      —     10208 ms (api friction)

gpt-4o is 3.3× faster than Opus 4.7 with identical tool compliance
and math correctness. Switching cuts per-turn latency dramatically
without sacrificing the structural enforcement (the pose_question
tool is the actual guarantee — model choice doesn't change that).

This migration:
  - Flips the active tutoring ModelConfig from anthropic/claude-opus-4-7
    to openai/gpt-4o.
  - Clears the encrypted API key (the OpenAI client falls through to
    OPENAI_API_KEY env var, same pattern as the image-gen swap in
    migration 0011).

Reversible: backwards step restores claude-opus-4-7.
"""

from django.db import migrations


def _to_gpt4o(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(is_active=True, purpose='tutoring')
    updated = qs.exclude(
        provider='openai', model_name='gpt-4o',
    ).update(
        provider='openai',
        model_name='gpt-4o',
        api_key_env_var='OPENAI_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0016] updated {updated} tutoring row(s) to openai/gpt-4o")


def _to_opus(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(
        is_active=True, purpose='tutoring',
        provider='openai', model_name='gpt-4o',
    )
    qs.update(
        provider='anthropic',
        model_name='claude-opus-4-7',
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0015_seed_judge_to_sonnet'),
    ]

    operations = [
        migrations.RunPython(_to_gpt4o, _to_opus),
    ]
