"""Add READY_WITH_WARNINGS to Lesson.content_status choices.

Layer 3 (verify-then-retry) sets this status when a math lesson's
content was generated but Layer 1/2 couldn't fully resolve all
arithmetic mismatches. Lesson is still usable; teacher reviews
the audit panel on the lesson detail page.

See memory/llm_arithmetic_defense_plan.md (Layer 3 section).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("curriculum", "0020_course_allow_student_duration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lesson",
            name="content_status",
            field=models.CharField(
                choices=[
                    ("empty", "Empty"),
                    ("generating", "Generating"),
                    ("ready", "Ready"),
                    ("ready_with_warnings", "Ready with warnings"),
                    ("failed", "Failed"),
                ],
                default="empty",
                help_text="Status of generated content for this lesson",
                max_length=20,
            ),
        ),
    ]
