"""Add GRADER_STUDENT_CLAIMS purpose and seed Haiku 4.5 default.

Two-LLM grader (design/tasks/two-llm-grader-implementation-plan.md).

GRADER_STUDENT_CLAIMS — LLM-B in the math path. Parses the student's
                       response into a structured claim graph
                       (claims[] + conclusion{}) that the Python
                       comparator evaluates deterministically against
                       the canonical produced by GRADER_MATH (LLM-A).
                       Defaults to Haiku 4.5: cheap, fast, and reliable
                       on structured-JSON prompts.
"""

from django.db import migrations, models


def _seed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    Institution = apps.get_model('accounts', 'Institution')

    inst = Institution.objects.filter(slug='global').first()
    if inst is None:
        inst = Institution.objects.order_by('id').first()
    if inst is None:
        print("  [llm.0036] no Institution rows present — skipping seed")
        return

    if ModelConfig.objects.filter(
        purpose='grader_student_claims', is_active=True,
    ).exists():
        return

    ModelConfig.objects.create(
        institution=inst,
        name="grader_student_claims — v2 default (auto-seeded)",
        provider='anthropic',
        model_name='claude-haiku-4-5-20251001',
        api_key_env_var='ANTHROPIC_API_KEY',
        api_key_encrypted='',
        max_tokens=1200,
        temperature=0.0,
        purpose='grader_student_claims',
        is_active=True,
    )
    print("  [llm.0036] seeded grader_student_claims ModelConfig")


def _unseed(apps, schema_editor):
    ModelConfig = apps.get_model('llm', 'ModelConfig')
    deleted, _ = ModelConfig.objects.filter(
        purpose='grader_student_claims',
        name__endswith='— v2 default (auto-seeded)',
    ).delete()
    print(f"  [llm.0036 backwards] removed {deleted} auto-seeded row(s)")


class Migration(migrations.Migration):

    dependencies = [
        ('llm', '0035_add_verifier_and_extractor_purposes'),
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
                    ('grader_student_claims', 'v2 Grader — Student-Claims Extractor'),
                ],
                default='generation',
                max_length=40,
            ),
        ),
        migrations.RunPython(_seed, _unseed),
    ]
