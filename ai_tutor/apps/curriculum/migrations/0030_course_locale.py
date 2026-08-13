"""Adds Course.locale + backfills existing rows to 'en-us'.

Part of M4 of memory/portuguese_mozambique_pilot_plan.md. The field
drives per-course tutor response language and UI rendering inside
chat sessions. Existing Seychelles courses backfill to 'en-us'; the
Mozambique import in M5 sets 'pt-mz' on the new Course rows.
"""
from django.conf import settings
from django.db import migrations, models


def _backfill_locale(apps, schema_editor):
    Course = apps.get_model('curriculum', 'Course')
    # Be defensive — only touch rows that are unset. Post-AddField
    # they should already carry the default 'en-us', but this makes
    # the migration safely re-runnable in dev.
    Course.objects.filter(locale='').update(locale='en-us')


def _reverse_backfill(apps, schema_editor):
    """No-op reverse — the AddField reverse drops the column."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('curriculum', '0029_curriculumchunk'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='locale',
            field=models.CharField(
                choices=settings.LANGUAGES,
                default='en-us',
                help_text=(
                    "Course curriculum language. Hyphenated lowercase "
                    "per Django LANGUAGE_CODE (e.g. 'en-us', 'pt-mz'). "
                    "Drives tutor response language + UI activation "
                    "during chat sessions."
                ),
                max_length=10,
            ),
        ),
        migrations.RunPython(_backfill_locale, _reverse_backfill),
    ]
