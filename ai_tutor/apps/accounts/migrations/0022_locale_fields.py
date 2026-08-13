"""Adds Institution.default_locale + StudentProfile.preferred_locale.

Part of M4 of memory/portuguese_mozambique_pilot_plan.md. Together
with curriculum.0030_course_locale these three fields drive the
LocaleResolverMiddleware chain:

    course.locale (in-session)
      > student.preferred_locale
        > institution.default_locale
          > settings.LANGUAGE_CODE

Backfills existing Seychelles institutions to 'en-us'. Student
profiles default to NULL (no override) — students never need a
preference unless they actively want one.
"""
from django.conf import settings
from django.db import migrations, models


def _backfill_locales(apps, schema_editor):
    Institution = apps.get_model('accounts', 'Institution')
    # Re-runnable: only touch rows that are unset (empty string).
    Institution.objects.filter(default_locale='').update(default_locale='en-us')
    # StudentProfile.preferred_locale stays NULL by default; nothing
    # to backfill — None already means "use my school's default".


def _reverse_backfill(apps, schema_editor):
    """No-op reverse — the RemoveField in the schema reverse drops
    the columns; data stays until then."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0021_membership_password_reset_required'),
    ]

    operations = [
        migrations.AddField(
            model_name='institution',
            name='default_locale',
            field=models.CharField(
                choices=settings.LANGUAGES,
                default='en-us',
                help_text=(
                    "Default UI language for users of this institution. "
                    "Falls back to settings.LANGUAGE_CODE when unset."
                ),
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='studentprofile',
            name='preferred_locale',
            field=models.CharField(
                blank=True,
                choices=settings.LANGUAGES,
                help_text=(
                    "Student's preferred UI language. Blank = follow "
                    "the school's default. Course-scoped views "
                    "(chat tutor) always render in the course's locale, "
                    "regardless of this preference."
                ),
                max_length=10,
                null=True,
            ),
        ),
        migrations.RunPython(_backfill_locales, _reverse_backfill),
    ]
