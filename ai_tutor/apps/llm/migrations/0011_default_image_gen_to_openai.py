"""Switch the platform default image-generation model to OpenAI gpt-image-2.

Up to now, image_generation ModelConfig rows were using
provider='google' / model='gemini-3.1-flash-image-preview'. After
verifying gpt-image-2 quality locally, we make OpenAI the platform
default. Teachers can still override per-image via the Regenerate
UI dropdown, and per-institution via /dashboard/settings.

This migration:
  - Flips ANY active image_generation ModelConfig from google to
    openai/gpt-image-2 (clears the stored encrypted key so the
    service falls through to OPENAI_API_KEY env var).
  - Creates one row for the Global institution if none exists.

Reversible: backwards step restores google/gemini-3.1-flash-image-preview.
"""

from django.db import migrations


def _to_openai(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    Institution = apps.get_model('accounts', 'Institution')

    # Flip every active image-gen row to OpenAI.
    qs = ModelConfig.objects.filter(is_active=True, purpose='image_generation')
    updated = qs.exclude(provider='openai').update(
        provider='openai',
        model_name='gpt-image-2',
        api_key_env_var='OPENAI_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0011] updated {updated} image_generation row(s) to openai/gpt-image-2")

    # Ensure at least one row exists, anchored to Global if available.
    if not ModelConfig.objects.filter(is_active=True, purpose='image_generation').exists():
        global_inst = Institution.objects.filter(slug='global').first()
        if global_inst:
            ModelConfig.objects.create(
                institution=global_inst,
                name='OpenAI - image_generation',
                provider='openai',
                model_name='gpt-image-2',
                api_key_env_var='OPENAI_API_KEY',
                api_key_encrypted='',
                purpose='image_generation',
                is_active=True,
            )
            print("  [llm.0011] created default image_generation row for Global")


def _to_gemini(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    qs = ModelConfig.objects.filter(is_active=True, purpose='image_generation', provider='openai')
    qs.update(
        provider='google',
        model_name='gemini-3.1-flash-image-preview',
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0010_mobile_api'),
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(_to_openai, _to_gemini),
    ]
