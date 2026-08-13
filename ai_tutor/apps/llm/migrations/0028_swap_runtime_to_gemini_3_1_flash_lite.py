"""Switch runtime model configs to Gemini 3.1 Flash Lite Preview.

Production swap 2026-05-20 after browser e2e + 36-cell matrix + per-turn
latency instrumentation showed:

  - Mean turn ~13.5s on Gemini 3.1 Flash Lite Preview vs ~37s on Opus
    (~2.8× faster end-to-end)
  - Zero answer leaks, zero tool-call markup leaks (after the strip +
    detector + validator fixes shipped in 8500fe4 / cd4ef31 / 38ad6fb)
  - Quality holds: exit-ticket completion rate matched Opus on L540,
    figure-handling on L638 clean

Changes:
  tutoring        anthropic/claude-opus-4-7      → google/gemini-3.1-flash-lite-preview @ 0.2
  judge           google/gemini-2.5-flash        → google/gemini-3.1-flash-lite-preview @ 0.0
  regen           anthropic/claude-sonnet-4-...  → google/gemini-3.1-flash-lite-preview @ 0.2
  judge_fallback  anthropic/claude-haiku-4-5-... → google/gemini-3.5-flash             @ 0.0

Cross-vendor fallback chain (Gemini 3.1 → Gemini 3.5 → Opus 4.7) for
the TUTORING path is NOT yet wired in code — currently the engine
binds to one llm_client per session and relies on the per-provider
retry-with-backoff (D+E fix in ad0b2b4) for transient failures.
Follow-up task: add tutoring_fallback + tutoring_fallback_2 purposes
and a wrapper that catches exhausted retries and re-instantiates
llm_client against the next tier.

Reversible: backwards step restores Opus tutoring + Sonnet regen +
Gemini 2.5 Flash judge + Haiku judge_fallback (the pre-2026-05-20
production config).

Refs:
  - memory/overnight_run_summary.md
  - memory/deepmind_model_experiment_results.md
  - 0027_tutoring_to_opus_47_temp_0.py (the prior swap)
"""

from django.db import migrations


def _swap_to_gemini(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')

    # tutoring → Gemini 3.1 Flash Lite Preview
    n = ModelConfig.objects.filter(is_active=True, purpose='tutoring').update(
        provider='google',
        model_name='gemini-3.1-flash-lite-preview',
        temperature=0.2,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0028] tutoring → google/gemini-3.1-flash-lite-preview ({n} row(s))")

    # judge → Gemini 3.1 Flash Lite Preview (same model, temp 0 enforced
    # at runtime by ModelConfig.effective_temperature for JUDGE purpose)
    n = ModelConfig.objects.filter(is_active=True, purpose='judge').update(
        provider='google',
        model_name='gemini-3.1-flash-lite-preview',
        temperature=0.0,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0028] judge → google/gemini-3.1-flash-lite-preview ({n} row(s))")

    # regen → Gemini 3.1 Flash Lite Preview
    n = ModelConfig.objects.filter(is_active=True, purpose='regen').update(
        provider='google',
        model_name='gemini-3.1-flash-lite-preview',
        temperature=0.2,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0028] regen → google/gemini-3.1-flash-lite-preview ({n} row(s))")

    # judge_fallback → Gemini 3.5 Flash (cross-vendor diversity inside
    # Google instead of cross-vendor; Opus stays as final-tier in DB
    # but not wired to the chain yet — see migration docstring)
    n = ModelConfig.objects.filter(is_active=True, purpose='judge_fallback').update(
        provider='google',
        model_name='gemini-3.5-flash',
        temperature=0.0,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0028] judge_fallback → google/gemini-3.5-flash ({n} row(s))")


def _restore_opus_stack(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')

    ModelConfig.objects.filter(is_active=True, purpose='tutoring').update(
        provider='anthropic',
        model_name='claude-opus-4-7',
        temperature=0.0,
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )
    ModelConfig.objects.filter(is_active=True, purpose='judge').update(
        provider='google',
        model_name='gemini-2.5-flash',
        temperature=0.7,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    ModelConfig.objects.filter(is_active=True, purpose='regen').update(
        provider='anthropic',
        model_name='claude-sonnet-4-20250514',
        temperature=0.2,
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )
    ModelConfig.objects.filter(is_active=True, purpose='judge_fallback').update(
        provider='anthropic',
        model_name='claude-haiku-4-5-20251001',
        temperature=0.0,
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0027_tutoring_to_opus_47_temp_0'),
    ]

    operations = [
        migrations.RunPython(_swap_to_gemini, _restore_opus_stack),
    ]
