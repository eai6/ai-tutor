"""Import an offline content pack into this device's database.

    python manage.py import_content_pack dist/content-pack-3-v1.tar.gz

Idempotent and transactional: re-importing the same version is a no-op, and a
failure leaves the previous content intact. See apps/desktop/packs.py.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ai_tutor.apps.desktop.packs import PackError, import_pack, read_manifest


class Command(BaseCommand):
    help = "Import an offline content pack into the local database."

    def add_arguments(self, parser):
        parser.add_argument('archive', help='Path to a content-pack .tar.gz')
        parser.add_argument('--force', action='store_true',
                            help='Re-import even if the version is not newer, '
                                 'or the pack is for another institution.')
        parser.add_argument('--allow-schema-mismatch', action='store_true',
                            help='Import a pack built against a different '
                                 'schema. Can fail mid-import; last resort.')
        parser.add_argument('--inspect', action='store_true',
                            help='Print the manifest and exit without writing.')

    def handle(self, *args, **options):
        archive = Path(options['archive'])
        try:
            if options['inspect']:
                manifest = read_manifest(archive)
                for key, value in manifest.items():
                    self.stdout.write(f'  {key}: {value}')
                return

            manifest = import_pack(
                archive, force=options['force'],
                strict_schema=not options['allow_schema_mismatch'],
            )
        except PackError as exc:
            raise CommandError(str(exc))

        if manifest.get('skipped'):
            self.stdout.write(self.style.WARNING(
                f"skipped: {manifest['skipped']} (device already at "
                f"v{manifest['version']})"))
            return

        counts = manifest['counts']
        self.stdout.write(self.style.SUCCESS(
            f"imported pack v{manifest['version']} for institution "
            f"{manifest['institution_id']} ({manifest.get('institution_name')})"))
        self.stdout.write(
            f"  lessons   : {counts['lessons']}\n"
            f"  kb chunks : {counts['chunks']}\n"
            f"  media     : {manifest['media_files']} file(s)"
        )
