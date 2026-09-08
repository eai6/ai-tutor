"""Turn the brand colour from orange to Sesel AI's blue.

`primary_color` feeds the <style> block in each base template, which
overrides the brand ramp in static_src/app.css. Changing only the stylesheet
would repaint an un-themed install and leave every deployment that has a row
— which is all of them — still orange.

Guarded the same way as 0033: only a value still sitting on the old default is
moved. A deployment that picked its own colour on the settings page has said
what it wants, and a migration that overwrote it would be a white-label wiping
itself on deploy.
"""

from django.db import migrations, models

OLD = '#E8590C'
NEW = '#003BA4'


def _recolour(apps, old, new):
    PlatformConfig = apps.get_model('accounts', 'PlatformConfig')
    PlatformConfig.objects.filter(primary_color=old).update(primary_color=new)


def forwards(apps, schema_editor):
    _recolour(apps, OLD, NEW)


def backwards(apps, schema_editor):
    _recolour(apps, NEW, OLD)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0033_rename_platform_to_sesel_ai'),
    ]

    operations = [
        migrations.AlterField(
            model_name='platformconfig',
            name='primary_color',
            field=models.CharField(default='#003BA4', max_length=7),
        ),
        migrations.RunPython(forwards, backwards),
    ]
