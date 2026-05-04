"""Switch the active tutoring ModelConfig from Sonnet 4 to Opus 4.7.

Background: even with the structural no-authoring enforcement (force-
inject + per-turn final_reminder), Sonnet was still drifting on long
math conversations — paraphrasing later steps' questions, inventing
angle measures that don't sum to 360°, etc. The structural gate
catches it server-side, but a stronger model means the gate fires
less often (lower latency, fewer regen retries).

Reversible: backwards step restores Sonnet 4.
"""

from django.db import migrations


def _to_opus(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(
        is_active=True, purpose='tutoring', provider='anthropic',
    )
    updated = qs.exclude(model_name='claude-opus-4-7').update(
        model_name='claude-opus-4-7',
    )
    print(f"  [llm.0013] updated {updated} tutoring row(s) to claude-opus-4-7")


def _to_sonnet(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(
        is_active=True, purpose='tutoring',
        provider='anthropic', model_name='claude-opus-4-7',
    )
    qs.update(model_name='claude-sonnet-4-20250514')


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0012_alter_modelconfig_purpose'),
    ]

    operations = [
        migrations.RunPython(_to_opus, _to_sonnet),
    ]
