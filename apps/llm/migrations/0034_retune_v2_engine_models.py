"""Retune the four LLM-call v2 ModelConfig purposes before Phase 3 cutover.

Migration 0033 seeded all six v2 purposes onto Haiku 4.5 / Gemini 2.5
Flash as Phase-1 placeholders pending Phase-2 benchmark tuning. This
migration commits the post-benchmark selections:

  grader_math             anthropic/claude-haiku-4-5 → anthropic/claude-sonnet-4-6
  tutor_move              anthropic/claude-haiku-4-5 → anthropic/claude-sonnet-4-6
  grader_grounded         google/gemini-2.5-flash    → google/gemini-3-flash-preview
  tutor_claim_adjudicator google/gemini-2.5-flash    → google/gemini-3-flash-preview

  conformance_classifier  unchanged (Haiku 4.5 — fast classifier path)
  profiler_summary        unchanged (Haiku 4.5 — async end-of-session)

Rationale:
  - Math grading + per-move generation are the two paths where model
    capability most directly drives student-visible quality. Sonnet
    4.6 is the validated choice; Haiku stays on the bounded
    classifier and async summarizer paths where latency / cost matter
    more than headroom.
  - Gemini 3 Flash Preview is the current generation of Google's
    grounded-adjudication tier; the Gemini pin is architecture-
    required (Google-grounding is provider-only).
  - Temperature invariants from Phase 1 §7 are unchanged: all four
    JUDGE-class purposes stay at 0.0, tutor_move stays at 0.2.

Updates rows by ``(purpose, name)`` so only the auto-seeded rows from
0033 are touched. Operator-edited rows (different name) are left
alone. Idempotent — re-running converges on the target values.
Reversible — backwards step restores the 0033 starting values.
"""

from django.db import migrations


# (purpose, target_provider, target_model_name, target_api_key_env_var,
#  prior_provider, prior_model_name, prior_api_key_env_var)
_RETUNE_ROWS = [
    (
        'grader_math',
        'anthropic', 'claude-sonnet-4-6',         'ANTHROPIC_API_KEY',
        'anthropic', 'claude-haiku-4-5-20251001', 'ANTHROPIC_API_KEY',
    ),
    (
        'tutor_move',
        'anthropic', 'claude-sonnet-4-6',         'ANTHROPIC_API_KEY',
        'anthropic', 'claude-haiku-4-5-20251001', 'ANTHROPIC_API_KEY',
    ),
    (
        'grader_grounded',
        'google',    'gemini-3-flash-preview',    'GOOGLE_API_KEY',
        'google',    'gemini-2.5-flash',          'GOOGLE_API_KEY',
    ),
    (
        'tutor_claim_adjudicator',
        'google',    'gemini-3-flash-preview',    'GOOGLE_API_KEY',
        'google',    'gemini-2.5-flash',          'GOOGLE_API_KEY',
    ),
]

_SEED_NAME_FMT = "{purpose} — v2 default (auto-seeded)"


def _retune(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    n_updated = 0
    for purpose, provider, model_name, env_var, *_prior in _RETUNE_ROWS:
        n = ModelConfig.objects.filter(
            purpose=purpose,
            name=_SEED_NAME_FMT.format(purpose=purpose),
            is_active=True,
        ).update(
            provider=provider,
            model_name=model_name,
            api_key_env_var=env_var,
        )
        n_updated += n
        if n == 0:
            print(
                f"  [llm.0034] no auto-seeded row for purpose={purpose!r}; "
                "operator-edited row left alone"
            )
    print(f"  [llm.0034] retuned {n_updated} v2 ModelConfig row(s)")


def _revert(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    n_reverted = 0
    for purpose, _p, _m, _e, prior_provider, prior_model_name, prior_env_var in _RETUNE_ROWS:
        n = ModelConfig.objects.filter(
            purpose=purpose,
            name=_SEED_NAME_FMT.format(purpose=purpose),
            is_active=True,
        ).update(
            provider=prior_provider,
            model_name=prior_model_name,
            api_key_env_var=prior_env_var,
        )
        n_reverted += n
    print(f"  [llm.0034 backwards] reverted {n_reverted} v2 ModelConfig row(s)")


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0033_seed_v2_engine_modelconfigs'),
    ]

    operations = [
        migrations.RunPython(_retune, _revert),
    ]
