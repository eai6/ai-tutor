"""Revert runtime tutoring/regen to Claude Opus 4.7 and widen the judge
fallback chain to a 3-tier cross-vendor cascade.

Background: migration 0028 swapped tutoring/judge/regen/judge_fallback all
onto Gemini 3.1 Flash Lite Preview for the 2.8× latency win. In practice
the all-Gemini stack hurt tutor quality (per user 2026-05-22), and the
chain collapsed to a single vendor — `judge_fallback` was Gemini 3.5
Flash, so a Google outage takes the whole judge layer down.

This migration:

  tutoring         google/gemini-3.1-flash-lite-preview  → anthropic/claude-opus-4-7        @ 0.0
  regen            google/gemini-3.1-flash-lite-preview  → anthropic/claude-opus-4-7        @ 0.2
  judge_fallback   google/gemini-3.5-flash               → openai/gpt-4o-mini               @ 0.0
  judge_fallback_2 (NEW row)                             → anthropic/claude-haiku-4-5-...   @ 0.0

Unchanged:
  judge            google/gemini-3.1-flash-lite-preview  (stays, search grounding intact)
  generation       anthropic/claude-sonnet-4-20250514    (already on Claude)
  exit_tickets     anthropic/claude-sonnet-4-20250514    (already on Claude)
  image_generation openai/gpt-image-2

Resulting judge provider chain (distinct-provider rule in
apps/curriculum/content_judges/_providers.py): Gemini → OpenAI → Haiku.
The `generation` purpose's Anthropic slot is skipped at tier 3 because
`judge_fallback_2` already filled it with Haiku.

Why regen = tutor model: regen ensemble produces the same kind of
tutoring text the validator just rejected. Matching the tutor model gives
the regen the same voice + capability ceiling so the validator's bar is
hit by the same competence that produced the original. Stored temp 0.2
is the starting point of the regen decay curve (0.20 → 0.15 over the
2-cycle cap; see DEFAULT_MAX_CYCLES).

Reversible: backwards step restores the post-0028 Gemini 3.1 Flash Lite
stack and deletes the new judge_fallback_2 row.

Refs:
  - 0028_swap_runtime_to_gemini_3_1_flash_lite.py (the swap this reverses)
  - auto-memory/project_model_routing.md
  - auto-memory/project_tutor_model_choice.md
"""

from django.db import migrations


def _resolve_global_institution_id(apps):
    """Return the id of the institution other ModelConfigs are scoped to,
    or None if the table is empty.

    Earlier draft hard-coded ``12`` (the prod 'Global (All Schools)' id),
    which broke test-DB fixtures that don't seed that institution. This
    lookup uses whatever institution the existing judge config points to,
    which on prod resolves to 12 anyway and on test DBs degrades gracefully
    (returns None → judge_fallback_2 row is skipped, which tests don't need).
    """
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    sibling = (
        ModelConfig.objects
        .filter(purpose__in=('judge', 'judge_fallback', 'tutoring'))
        .first()
    )
    return sibling.institution_id if sibling else None


def _apply(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')

    # tutoring → Opus 4.7
    n = ModelConfig.objects.filter(is_active=True, purpose='tutoring').update(
        provider='anthropic',
        model_name='claude-opus-4-7',
        temperature=0.0,
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0030] tutoring → anthropic/claude-opus-4-7 ({n} row(s))")

    # regen → Opus 4.7 (matches tutor; temp 0.2 is decay-curve start)
    n = ModelConfig.objects.filter(is_active=True, purpose='regen').update(
        provider='anthropic',
        model_name='claude-opus-4-7',
        temperature=0.2,
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0030] regen → anthropic/claude-opus-4-7 ({n} row(s))")

    # judge_fallback → OpenAI gpt-4o-mini (tier-2 cross-vendor)
    n = ModelConfig.objects.filter(is_active=True, purpose='judge_fallback').update(
        provider='openai',
        model_name='gpt-4o-mini',
        temperature=0.0,
        api_key_env_var='OPENAI_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0030] judge_fallback → openai/gpt-4o-mini ({n} row(s))")

    # judge_fallback_2 → Haiku 4.5 (tier-3 last-resort). Created on the
    # same institution other ModelConfigs are scoped to (on prod this is
    # the 'Global (All Schools)' row, id=12). If the table has no
    # ModelConfig rows yet (fresh test DB), skip the create — tests
    # don't exercise the tier-3 cascade.
    inst_id = _resolve_global_institution_id(apps)
    if inst_id is None:
        print("  [llm.0030] judge_fallback_2 → SKIPPED (no sibling ModelConfig to inherit institution from)")
        return
    obj, created = ModelConfig.objects.update_or_create(
        institution_id=inst_id,
        purpose='judge_fallback_2',
        defaults=dict(
            is_active=True,
            name='Anthropic - judge fallback (tier 3)',
            provider='anthropic',
            model_name='claude-haiku-4-5-20251001',
            temperature=0.0,
            max_tokens=2048,
            api_key_env_var='ANTHROPIC_API_KEY',
            api_key_encrypted='',
        ),
    )
    print(
        f"  [llm.0030] judge_fallback_2 → anthropic/claude-haiku-4-5-20251001 "
        f"({'created' if created else 'updated'})"
    )


def _reverse(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')

    ModelConfig.objects.filter(is_active=True, purpose='tutoring').update(
        provider='google',
        model_name='gemini-3.1-flash-lite-preview',
        temperature=0.2,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    ModelConfig.objects.filter(is_active=True, purpose='regen').update(
        provider='google',
        model_name='gemini-3.1-flash-lite-preview',
        temperature=0.2,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    ModelConfig.objects.filter(is_active=True, purpose='judge_fallback').update(
        provider='google',
        model_name='gemini-3.5-flash',
        temperature=0.0,
        api_key_env_var='GOOGLE_API_KEY',
        api_key_encrypted='',
    )
    # Drop the tier-3 row entirely — it didn't exist before this migration.
    ModelConfig.objects.filter(purpose='judge_fallback_2').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0029_widen_purpose_judge_fallback_tiers'),
    ]

    operations = [
        migrations.RunPython(_apply, _reverse),
    ]
