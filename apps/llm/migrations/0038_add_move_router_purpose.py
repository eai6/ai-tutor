"""Add MOVE_ROUTER purpose and seed Claude Sonnet 4.6 default.

Companion to design/tasks/move-router-implementation-plan.md — the LLM
Move Router replaces the deterministic ``select_move`` ladder and the
standalone Haiku intent-classifier. It is transcript-aware and picks
the move + 1-3 principle-emphasis tags + a per-turn focus note. Pinned
to a Sonnet-class model because the decision is pedagogical (not a
cheap classifier).
"""

from django.db import migrations, models


def _seed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    Institution = apps.get_model('accounts', 'Institution')

    inst = Institution.objects.filter(slug='global').first()
    if inst is None:
        inst = Institution.objects.order_by('id').first()
    if inst is None:
        print("  [llm.0038] no Institution rows present — skipping seed")
        return

    if ModelConfig.objects.filter(
        purpose='move_router', is_active=True,
    ).exists():
        return

    ModelConfig.objects.create(
        institution=inst,
        name="move_router — v2 default (auto-seeded)",
        provider='anthropic',
        model_name='claude-sonnet-4-6',
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
        max_tokens=900,
        temperature=0.0,
        purpose='move_router',
        is_active=True,
    )
    print("  [llm.0038] seeded move_router ModelConfig")


def _unseed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    deleted, _ = ModelConfig.objects.filter(
        purpose='move_router',
        name__endswith='— v2 default (auto-seeded)',
    ).delete()
    print(f"  [llm.0038 backwards] removed {deleted} auto-seeded row(s)")


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0037_add_grader_student_response_purpose'),
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
                    ('grader_verifier', 'v2 Grader — Answer-Consistency Verifier (DEPRECATED)'),
                    ('question_extractor', 'v2 Post-render Question Extractor'),
                    ('grader_student_claims', 'v2 Grader — Student-Claims Extractor (Math)'),
                    ('grader_student_response', 'v2 Grader — Student-Response Extractor (Non-Math)'),
                    ('move_router', 'v2 Move Router — Move + Principle Emphasis + Focus Note'),
                ],
                default='generation',
                max_length=40,
            ),
        ),
        migrations.RunPython(_seed, _unseed),
    ]
