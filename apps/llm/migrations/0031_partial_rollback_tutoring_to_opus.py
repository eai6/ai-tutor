"""Partial rollback: tutoring + regen → Opus/Sonnet; keep judge on Gemini.

Migration 0028 (2026-05-20) swapped the tutoring, judge, regen, and
judge_fallback purposes to Gemini 3.1 Flash Lite Preview. The
pre-deploy A/B was thin (2 lessons), and per `SCIENCE_LEARNING_AUDIT_v3.md`
section 3 H1 the math-quality regression most plausibly traces to that
swap — same small model now serves tutor, arithmetic verifier, and
regen, so the safety net and the responder share blind spots.

This is a TARGETED rollback. Following the v3 audit recommendation:

  tutoring        → anthropic/claude-opus-4-7         @ 0.0  (rolled back)
  regen           → anthropic/claude-sonnet-4-20250514 @ 0.2  (rolled back)
  judge           STAYS on google/gemini-3.1-flash-lite-preview @ 0.0
                  (judges don't need the depth; latency win is real)
  judge_fallback  STAYS on google/gemini-3.5-flash    @ 0.0

Reversible: backwards step re-applies the full 0028 Gemini stack.

Verification protocol (`SCIENCE_LEARNING_AUDIT_v3.md` Appendix A):
  1. python manage.py test apps.tutoring.tests.test_math_eval_integration
  2. python manage.py audit_math_false_positives  # last 7 days
  3. python manage.py verify_math_regression
  4. Teacher-scored sample of 20 random math sessions/week vs.
     pre-2026-05-20 baseline.

Refs:
  - design/SCIENCE_LEARNING_AUDIT_v3.md (§3 H1, §5 R1)
  - 0028_swap_runtime_to_gemini_3_1_flash_lite.py
"""

from django.db import migrations


def _partial_rollback(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')

    # tutoring → Opus 4.7 @ 0.0 (matches commit 0027 pre-swap config)
    n = ModelConfig.objects.filter(is_active=True, purpose='tutoring').update(
        provider='anthropic',
        model_name='claude-opus-4-7',
        temperature=0.0,
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0031] tutoring → anthropic/claude-opus-4-7 ({n} row(s))")

    # regen → Sonnet 4 @ 0.2 (the pre-0028 production config; cheaper
    # than Opus and historically the regen tier when the tutor is Opus)
    n = ModelConfig.objects.filter(is_active=True, purpose='regen').update(
        provider='anthropic',
        model_name='claude-sonnet-4-20250514',
        temperature=0.2,
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
    )
    print(f"  [llm.0031] regen → anthropic/claude-sonnet-4-20250514 ({n} row(s))")

    # judge stays on Gemini 3.1 Flash Lite — small task, latency win
    # is real, and the math-error class we're trying to fix is a tutor
    # production issue, not a judge classification issue.
    # judge_fallback stays on Gemini 3.5 Flash for the same reason.


def _reapply_gemini_stack(apps, schema_editor):
    """Reverse step: put tutoring + regen back on Gemini Flash Lite."""
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


class Migration(migrations.Migration):

    dependencies = [
        # Renamed from 0029 -> 0031 at merge time (2026-05-23) to resolve
        # collision with main's 0029_widen_purpose_judge_fallback_tiers.
        # Dependency advanced from 0028 -> 0030 so this runs AFTER the
        # main-side judge-chain widening + Opus revert. Net effect: this
        # migration only changes `regen` (Opus -> Sonnet 4); `tutoring`
        # stays on Opus 4.7 (no-op since 0030 already set it).
        ('llm', '0030_revert_runtime_to_claude_widen_judge_chain'),
    ]

    operations = [
        migrations.RunPython(_partial_rollback, _reapply_gemini_stack),
    ]
