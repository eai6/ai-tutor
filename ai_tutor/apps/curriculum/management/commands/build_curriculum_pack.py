"""Build a curriculum pack — teaching content only, no user data.

    python manage.py build_curriculum_pack --institution 3 --out dist/

For seeding a NEW deployment that belongs to someone else. If you want a pack
for a classroom laptop that syncs back to this server, you want
`build_content_pack` instead — that one carries a roster on purpose.

See apps/curriculum/curriculum_pack.py for why the two are separate.
"""
from django.core.management.base import BaseCommand, CommandError

from ai_tutor.apps.curriculum.curriculum_pack import build_curriculum_pack


class Command(BaseCommand):
    help = 'Build a curriculum-only pack for seeding another deployment.'

    def add_arguments(self, parser):
        parser.add_argument('--institution', type=int, required=True,
                            help='Institution PK whose content to pack. Its '
                                 'courses plus platform-wide ones are included.')
        parser.add_argument('--out', default='dist',
                            help='Output directory (default: dist/).')
        parser.add_argument('--no-media', action='store_true',
                            help='Skip figures and images. Much smaller, but '
                                 'lessons that reference a figure show a gap.')

    def handle(self, *args, **options):
        from ai_tutor.apps.accounts.models import Institution

        institution_id = options['institution']
        if not Institution.objects.filter(pk=institution_id).exists():
            available = ', '.join(
                f'{i.pk}={i.name}' for i in Institution.objects.all()[:10]
            ) or 'none'
            raise CommandError(
                f'No institution with pk={institution_id}. Available: {available}'
            )

        manifest = build_curriculum_pack(
            institution_id, options['out'],
            include_media=not options['no_media'],
        )

        counts = manifest['counts']
        mb = manifest['archive_bytes'] / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f"built {manifest['archive']}"))
        self.stdout.write(
            f"  courses/units/lessons : {counts['courses']}/{counts['units']}"
            f"/{counts['lessons']}\n"
            f"  steps                 : {counts['steps']}\n"
            f"  exit tickets/questions: {counts['tickets']}/{counts['questions']}\n"
            f"  knowledge-base chunks : {counts['chunks']}\n"
            f"  media files           : {manifest['media_files']}\n"
            f"  size                  : {mb:.1f} MB\n"
            f"  sha256                : {manifest['archive_sha256']}"
        )
        self.stdout.write(self.style.WARNING(
            '\nThis pack contains NO user data — no roster, no accounts, no '
            'session history. It is safe to hand to another organisation.\n'
            'The curriculum itself may still be licensed: content derived from '
            'a national syllabus is not automatically yours to redistribute.'
        ))
