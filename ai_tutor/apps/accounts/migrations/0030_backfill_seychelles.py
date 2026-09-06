"""Give every existing school and course a country.

Content that was platform-wide becomes Seychelles-wide. That is the whole
point of the change: `institution=None` used to mean "every school on the
platform", and after this it means "every school in this course's country".
Nothing is visible to a future Tanzania account until it is deliberately
created there.

Separate from 0029, which added the fields, so that this step can be dry-run
against a copy of the production dump on its own — which CLAUDE.md requires
for backfills.

No third "make non-null" migration is needed: the fields are non-null from
0029 because they carry a default. That default is the hidden Platform
country, which is also what reversing this migration returns rows to.
"""

from django.db import migrations

# Not schools. `global` carries platform-wide content and `eval-harness` is
# the evaluation fixture; a ministry must never see either in its list.
SYNTHETIC = ('global', 'eval-harness')


def forwards(apps, schema_editor):
    Country = apps.get_model('accounts', 'Country')
    Institution = apps.get_model('accounts', 'Institution')
    Course = apps.get_model('curriculum', 'Course')

    platform, _ = Country.objects.get_or_create(
        slug='platform',
        defaults={'name': 'Platform', 'is_hidden': True, 'default_locale': 'en-us'},
    )
    seychelles, _ = Country.objects.get_or_create(
        slug='seychelles',
        defaults={'name': 'Seychelles', 'is_hidden': False, 'default_locale': 'en-us'},
    )

    Institution.objects.filter(slug__in=SYNTHETIC).update(country=platform)
    Institution.objects.exclude(slug__in=SYNTHETIC).update(country=seychelles)

    # A course with a school takes that school's country; a shared course
    # becomes shared within Seychelles.
    Course.objects.filter(institution__isnull=True).update(country=seychelles)
    for inst_id, country_id in Institution.objects.values_list('id', 'country_id'):
        Course.objects.filter(institution_id=inst_id).update(country_id=country_id)

    stranded = (
        Institution.objects.filter(country__isnull=True).count()
        + Course.objects.filter(country__isnull=True).count()
    )
    if stranded:
        raise RuntimeError(
            f"{stranded} row(s) would be left without a country. Refusing to "
            "continue: a row with no country is invisible to every scoped "
            "query, which presents as data loss rather than a permissions bug."
        )


def backwards(apps, schema_editor):
    """Return every row to the hidden Platform country.

    Not None: the FKs are non-null from 0029, because they carry a default.
    Platform IS that default, so this restores exactly the state a row would
    have had before this migration ran. The Country rows themselves are left
    alone — deleting Seychelles would take any school added since with it.
    """
    Country = apps.get_model('accounts', 'Country')
    platform, _ = Country.objects.get_or_create(
        slug='platform',
        defaults={'name': 'Platform', 'is_hidden': True, 'default_locale': 'en-us'},
    )
    apps.get_model('accounts', 'Institution').objects.update(country=platform)
    apps.get_model('curriculum', 'Course').objects.update(country=platform)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0029_add_country'),
        ('curriculum', '0035_add_course_country'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
