"""Adds CurriculumUpload.locale (M5-wire 2026-06-01).

Stores the course language the teacher picked at curriculum-upload
time. Propagates to Course.locale when the upload's course gets
created (apps/dashboard/views.py::curriculum_upload). Drives the
LLM content-generation language via apps/curriculum/locale_prompts.py.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0020_curriculum_upload_subject_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='curriculumupload',
            name='locale',
            field=models.CharField(
                default='en-us',
                help_text=(
                    "Course language picked at upload time (e.g. "
                    "'en-us', 'pt-mz'). Drives tutor response "
                    "language + generated content language for the "
                    "resulting Course."
                ),
                max_length=10,
            ),
        ),
    ]
