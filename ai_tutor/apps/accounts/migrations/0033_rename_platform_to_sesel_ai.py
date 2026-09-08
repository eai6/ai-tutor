"""Name the platform Sesel AI.

The name was 'AI Tutor', which is what the thing does rather than what it is
called — the domain it has served from all along is www.seselai.sc. The public
pages now lead with the name and keep 'AI Tutor' as the line underneath it.

`platform_name` is an editable field on the settings page, so this renames it
only where it is still the untouched default. A deployment that has already
set its own name has said what it wants to be called, and a migration that
overwrote that would be a white-label wiping itself on deploy.
"""

from django.db import migrations, models

OLD = 'AI Tutor'
NEW = 'Sesel AI'


def _rename(apps, old, new):
    PlatformConfig = apps.get_model('accounts', 'PlatformConfig')
    PlatformConfig.objects.filter(platform_name=old).update(platform_name=new)


def forwards(apps, schema_editor):
    _rename(apps, OLD, NEW)


def backwards(apps, schema_editor):
    _rename(apps, NEW, OLD)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0032_backfill_country_iso_code'),
    ]

    operations = [
        migrations.AlterField(
            model_name='platformconfig',
            name='platform_name',
            field=models.CharField(default='Sesel AI', max_length=255),
        ),
        migrations.RunPython(forwards, backwards),
    ]
