"""Add ExitTicketQuestion.template_data for Layer 4 parametric questions.

When non-null, the question was rendered from a ParametricQuestionTemplate
at content-generation time. The dict captures the source template
(template_text, parameter specs, answer_formula, etc.) and is used
for retake re-rendering + teacher-review visibility.

See `memory/llm_arithmetic_defense_plan.md` (Layer 4 section) and
`apps/curriculum/parametric_renderer.py`.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tutoring", "0025_student_competency_record"),
    ]

    operations = [
        migrations.AddField(
            model_name="exitticketquestion",
            name="template_data",
            field=models.JSONField(
                blank=True,
                null=True,
                help_text=(
                    "If set, this question was generated from a "
                    "parametric template. The dict has the shape of "
                    "ParametricQuestionTemplate (template_text, "
                    "parameters, answer_formula, etc.) and is used "
                    "to re-render alternative versions on retake."
                ),
            ),
        ),
    ]
