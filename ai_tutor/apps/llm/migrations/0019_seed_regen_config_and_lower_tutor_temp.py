"""Seed a Sonnet ModelConfig for `Purpose.REGEN` + drop the active
tutoring config's temperature to 0.2.

Why:
  - The regen ensemble (apps/tutoring/regen/) reads `Purpose.REGEN`
    configs to decide which models to fan out to. With zero REGEN
    configs the orchestrator degrades to single-model regen using
    the tutoring client. We want at least ONE REGEN config in the
    DB so the focused-prompt path actually engages on production
    turns. Edward's call (2026-05-07): start with one Sonnet, escalate
    to multi-model only if Sonnet alone keeps producing dirty output.
  - Tutor temperature: we want more deterministic, predictable
    teaching responses. The regen orchestrator already overrides
    temperature per-call on regen turns; this only affects the
    primary tutor turn.

Idempotent — running twice doesn't duplicate the REGEN row, and
won't clobber an institution's already-customised regen / tutoring
configs.

Reversible — backwards step removes the REGEN row created by the
forward step (only ones with the seed marker name) and restores
the tutoring temperature to the previous default.
"""

from django.db import migrations


_REGEN_NAME = "Sonnet 4 — Regen (auto-seeded)"
_TUTOR_TARGET_TEMP = 0.2


def _seed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')

    # 1) Drop tutoring temperature to 0.2 across active configs.
    tutoring_qs = ModelConfig.objects.filter(
        is_active=True, purpose='tutoring',
    )
    n_temp = 0
    for cfg in tutoring_qs:
        if abs(float(cfg.temperature) - _TUTOR_TARGET_TEMP) > 1e-6:
            cfg.temperature = _TUTOR_TARGET_TEMP
            cfg.save(update_fields=['temperature'])
            n_temp += 1
    print(
        f"  [llm.0019] lowered tutoring temperature to "
        f"{_TUTOR_TARGET_TEMP} on {n_temp} config(s)"
    )

    # 2) Seed a REGEN config per institution that has an active
    #    tutoring config — but only when no active REGEN config
    #    already exists for that institution. Mirrors the institution
    #    + provider + model + api_key of the active tutoring config
    #    so credentials are inherited from prod's existing setup.
    n_regen = 0
    for tutor_cfg in tutoring_qs:
        existing = ModelConfig.objects.filter(
            institution=tutor_cfg.institution,
            purpose='regen',
            is_active=True,
        ).first()
        if existing:
            continue
        ModelConfig.objects.create(
            institution=tutor_cfg.institution,
            name=_REGEN_NAME,
            provider=tutor_cfg.provider,
            model_name=tutor_cfg.model_name,
            api_base=tutor_cfg.api_base or None,
            api_key_env_var=tutor_cfg.api_key_env_var,
            api_key_encrypted=tutor_cfg.api_key_encrypted or "",
            max_tokens=900,
            # Regen orchestrator overrides per-call (cycle decay), but
            # this is the fallback if a client doesn't accept the
            # explicit kwarg.
            temperature=_TUTOR_TARGET_TEMP,
            purpose='regen',
            is_active=True,
        )
        n_regen += 1
    print(f"  [llm.0019] seeded {n_regen} REGEN config(s)")


def _unseed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    # Remove only the auto-seeded REGEN rows (matched by the name we
    # used) — leaves any human-edited REGEN configs intact.
    deleted, _ = ModelConfig.objects.filter(
        purpose='regen', name=_REGEN_NAME,
    ).delete()
    print(f"  [llm.0019 backwards] removed {deleted} auto-seeded REGEN row(s)")
    # We don't restore the tutoring temperature — the prior default
    # was 0.7 in the model field but operators may have customised it.


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0018_alter_modelconfig_purpose'),
    ]

    operations = [
        migrations.RunPython(_seed, _unseed),
    ]
