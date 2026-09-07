"""Give the existing countries their ISO code.

The rows predate the field and were created by hand, so their slugs are
whatever the person typed — 'seychelles' here, 'tz' there. The account-request
form matches a chosen country against `iso_code`, and a row it fails to match
is a row it creates a second time: two "Tanzania" countries, with a country's
schools split between them and each invisible to the other. Matching on the
name once, here, is what stops that.

Separate from 0031 so the column and the data land in two reviewable steps,
the same way 0029 and 0030 were split.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Country = apps.get_model('accounts', 'Country')
    from ai_tutor.apps.accounts.countries import COUNTRIES

    by_name = {name.casefold(): code for code, name in COUNTRIES}
    # The slug is worth a look too: several rows were created with the ISO
    # code already in it.
    by_code = {code.casefold(): code for code, _ in COUNTRIES}

    taken = set()
    for country in Country.objects.all():
        code = by_name.get((country.name or '').strip().casefold())
        if code is None:
            code = by_code.get((country.slug or '').strip().casefold())
        # `iso_code` is unique, so a second row claiming the same code would
        # fail the whole migration. Leave it blank instead and let a person
        # look: two rows for one country is a data question, not a schema one.
        if code is None or code in taken:
            continue
        taken.add(code)
        country.iso_code = code
        country.save(update_fields=['iso_code'])


def backwards(apps, schema_editor):
    apps.get_model('accounts', 'Country').objects.update(iso_code=None)


class Migration(migrations.Migration):

    dependencies = [('accounts', '0031_add_country_iso_code')]

    operations = [migrations.RunPython(forwards, backwards)]
