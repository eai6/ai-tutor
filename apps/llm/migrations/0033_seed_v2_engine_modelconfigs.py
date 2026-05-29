"""Seed default ModelConfig rows for the six v2 engine purposes.

Phase 1 §7 of design/refactor/refactor-implementation-plan.md.

Purposes seeded:
  - GRADER_MATH                 (provider tuned during Phase 2)
  - GRADER_GROUNDED             (Gemini — Google-grounding required)
  - TUTOR_MOVE                  (provider tuned during Phase 2)
  - CONFORMANCE_CLASSIFIER      (fast/cheap classifier; provider tuned)
  - TUTOR_CLAIM_ADJUDICATOR     (Gemini — Google-grounding required)
  - PROFILER_SUMMARY            (fast/cheap; provider tuned in Phase 3)

Seeded for the global / first-available institution so each purpose
is dispatchable from day one. Operators retune via the admin UI; the
deploy workflow can override at runtime via the per-purpose env vars
introduced in Phase 3 DEPLOY.md.

Idempotent — running twice doesn't duplicate rows. Reversible —
backwards step removes only the auto-seeded rows (matched by name).
"""

from django.db import migrations


# (purpose, provider, model_name, api_key_env_var, max_tokens, temperature)
# Temperature stored value is the starting point; effective_temperature
# clamps per-purpose at runtime (Phase 1 §7 invariants).
_SEED_ROWS = [
    ('grader_math',              'anthropic', 'claude-haiku-4-5-20251001', 'ANTHROPIC_API_KEY', 2048, 0.0),
    ('grader_grounded',          'google',    'gemini-2.5-flash',          'GOOGLE_API_KEY',    2048, 0.0),
    ('tutor_move',               'anthropic', 'claude-haiku-4-5-20251001', 'ANTHROPIC_API_KEY', 1500, 0.2),
    ('conformance_classifier',   'anthropic', 'claude-haiku-4-5-20251001', 'ANTHROPIC_API_KEY',  600, 0.0),
    ('tutor_claim_adjudicator',  'google',    'gemini-2.5-flash',          'GOOGLE_API_KEY',    1500, 0.0),
    ('profiler_summary',         'anthropic', 'claude-haiku-4-5-20251001', 'ANTHROPIC_API_KEY', 1500, 0.0),
]

_SEED_NAME_FMT = "{purpose} — v2 default (auto-seeded)"


def _pick_institution(Institution):
    inst = Institution.objects.filter(slug='global').first()
    if inst is None:
        inst = Institution.objects.order_by('id').first()
    return inst


def _seed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    Institution = apps.get_model('accounts', 'Institution')
    inst = _pick_institution(Institution)
    if inst is None:
        print("  [llm.0033] no Institution rows present — skipping v2 seed")
        return

    n_created = 0
    for purpose, provider, model_name, env_var, max_tokens, temperature in _SEED_ROWS:
        existing = ModelConfig.objects.filter(
            purpose=purpose, is_active=True,
        ).first()
        if existing:
            continue
        ModelConfig.objects.create(
            institution=inst,
            name=_SEED_NAME_FMT.format(purpose=purpose),
            provider=provider,
            model_name=model_name,
            api_key_env_var=env_var,
            api_key_encrypted='',
            max_tokens=max_tokens,
            temperature=temperature,
            purpose=purpose,
            is_active=True,
        )
        n_created += 1
    print(f"  [llm.0033] seeded {n_created} v2 engine ModelConfig row(s)")


def _unseed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    purposes = [p for p, *_ in _SEED_ROWS]
    deleted, _ = ModelConfig.objects.filter(
        purpose__in=purposes,
        name__endswith='— v2 default (auto-seeded)',
    ).delete()
    print(f"  [llm.0033 backwards] removed {deleted} auto-seeded v2 row(s)")


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0032_add_v2_engine_purposes'),
        ('accounts', '0022_add_v2_profiler_columns'),
    ]

    operations = [
        migrations.RunPython(_seed, _unseed),
    ]
