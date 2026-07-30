"""Build an offline content pack for one institution (server side).

    python manage.py build_content_pack --institution 3 --out dist/

Produces content-pack-<institution>-v<N>.tar.gz containing the curriculum
tree, exit tickets, KB chunks with precomputed embeddings, and media. See
apps/desktop/packs.py for the format and memory/desktop_offline_app_plan.md.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.desktop.packs import build_pack


class Command(BaseCommand):
    help = 'Build an offline content pack for an institution.'

    def add_arguments(self, parser):
        parser.add_argument('--institution', type=int, required=True,
                            help='Institution PK to pack.')
        parser.add_argument('--out', default='dist',
                            help='Output directory (default: dist/).')
        parser.add_argument('--no-media', action='store_true',
                            help='Skip media files (much smaller; lessons '
                                 'referencing figures will show gaps).')

    def handle(self, *args, **options):
        from apps.accounts.models import Institution
        institution_id = options['institution']
        if not Institution.objects.filter(pk=institution_id).exists():
            available = ', '.join(
                f'{i.pk}={i.name}' for i in Institution.objects.all()[:10]
            ) or 'none'
            raise CommandError(
                f'No institution with pk={institution_id}. Available: {available}'
            )

        manifest = build_pack(
            institution_id, options['out'],
            include_media=not options['no_media'],
        )
        counts = manifest['counts']
        self.stdout.write(self.style.SUCCESS(
            f"built {manifest['archive']}"))
        self.stdout.write(
            f"  institution : {manifest['institution_id']} "
            f"({manifest['institution_name']})\n"
            f"  version     : v{manifest['version']}\n"
            f"  size        : {manifest['archive_bytes'] / 1e6:.1f} MB\n"
            f"  lessons     : {counts['lessons']} "
            f"(steps {counts['steps']}, tickets {counts['tickets']})\n"
            f"  kb chunks   : {counts['chunks']}\n"
            f"  media       : {manifest['media_files']} file(s), "
            f"{manifest['media_bytes'] / 1e6:.1f} MB\n"
            f"  schema_rev  : {manifest['schema_rev']}"
        )
