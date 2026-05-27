"""Add GRADER_VERIFIER + QUESTION_EXTRACTOR purposes and seed defaults.

Phase 4 — unverified-trap + open-question-pivot redesign
(memory/v2_unverified_trap_redesign.md).

GRADER_VERIFIER     — Haiku-backed answer-consistency check that fires
                      AFTER the grounded adjudicator when confidence is
                      low. Replaces the blanket
                      _GROUNDED_CONFIDENCE_THRESHOLD downgrade so a
                      complete correct natural-language proof no longer
                      gets trapped in UNVERIFIED.
QUESTION_EXTRACTOR  — Haiku-backed post-render extractor that counts
                      action-prompts in the tutor's rendered turn. Used
                      by tutor_engine to enforce the "one question per
                      turn" + "open_question single writer" invariants.
"""

from django.db import migrations, models


def _seed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    Institution = apps.get_model('accounts', 'Institution')

    inst = Institution.objects.filter(slug='global').first()
    if inst is None:
        inst = Institution.objects.order_by('id').first()
    if inst is None:
        print("  [llm.0035] no Institution rows present — skipping seed")
        return

    rows = [
        ('grader_verifier',    'anthropic', 'claude-haiku-4-5-20251001',
         'ANTHROPIC_API_KEY', 600, 0.0),
        ('question_extractor', 'anthropic', 'claude-haiku-4-5-20251001',
         'ANTHROPIC_API_KEY', 600, 0.0),
    ]

    n_created = 0
    for purpose, provider, model_name, env_var, max_tokens, temperature in rows:
        if ModelConfig.objects.filter(purpose=purpose, is_active=True).exists():
            continue
        ModelConfig.objects.create(
            institution=inst,
            name=f"{purpose} — v2 default (auto-seeded)",
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
    print(f"  [llm.0035] seeded {n_created} verifier/extractor ModelConfig row(s)")


def _unseed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    deleted, _ = ModelConfig.objects.filter(
        purpose__in=['grader_verifier', 'question_extractor'],
        name__endswith='— v2 default (auto-seeded)',
    ).delete()
    print(f"  [llm.0035 backwards] removed {deleted} auto-seeded row(s)")


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0034_retune_v2_engine_models'),
    ]

    operations = [
        migrations.AlterField(
            model_name='modelconfig',
            name='purpose',
            field=models.CharField(
                choices=[
                    ('generation', 'Content Generation (Curriculum, Lessons)'),
                    ('tutoring', 'Student Tutoring'),
                    ('exit_tickets', 'Exit Ticket Generation'),
                    ('skill_extraction', 'Skill Extraction'),
                    ('image_generation', 'Image Generation'),
                    ('help_assistant', 'In-app Help Assistant'),
                    ('judge', 'Post-response Judge'),
                    ('judge_fallback', 'Post-response Judge — Tier 2 Fallback'),
                    ('judge_fallback_2', 'Post-response Judge — Tier 3 Fallback'),
                    ('regen', 'Tutor Response Regeneration'),
                    ('student_sim', 'Synthetic Student (Simulator)'),
                    ('content_judge_image_prompt', 'Content Judge — Image Prompt (PRE-gen)'),
                    ('content_judge_factual_step', 'Content Judge — Factual (Lesson Step)'),
                    ('content_judge_figure_alignment', 'Content Judge — Figure Alignment (POST-gen vision)'),
                    ('content_judge_exit_question', 'Content Judge — Exit Ticket Question (MCQ)'),
                    ('content_judge_pedagogy_step', 'Content Judge — Pedagogical Soundness (Lesson Step)'),
                    ('content_judge_safety_content', 'Content Judge — Safety + Cultural Fit (Content)'),
                    ('grader_math', 'v2 Grader — Math Path'),
                    ('grader_grounded', 'v2 Grader — Grounded (KB + Google) — Gemini-pinned'),
                    ('tutor_move', 'v2 StudentTutor — Per-Move Response'),
                    ('conformance_classifier', 'v2 Conformance Classifier'),
                    ('tutor_claim_adjudicator', 'v2 Tutor-Claim Adjudicator — Gemini-pinned'),
                    ('profiler_summary', 'v2 StudentProfiler — End-of-Session Summary'),
                    ('grader_verifier', 'v2 Grader — Answer-Consistency Verifier'),
                    ('question_extractor', 'v2 Post-render Question Extractor'),
                ],
                default='generation',
                max_length=40,
            ),
        ),
        migrations.RunPython(_seed, _unseed),
    ]
