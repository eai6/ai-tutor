"""Load a curriculum pack into this deployment.

    python manage.py import_curriculum_pack dist/curriculum-pack-20260812.tar.gz

Refuses a desktop content pack: those carry a student roster, which belongs to
the institution that built them and must not land on another organisation's
server. See apps/curriculum/curriculum_pack.py.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.curriculum.curriculum_pack import PackError, import_curriculum_pack


class Command(BaseCommand):
    help = 'Seed this deployment from a curriculum pack.'

    def add_arguments(self, parser):
        parser.add_argument('archive', help='Path to the .tar.gz pack.')
        parser.add_argument(
            '--force', action='store_true',
            help='Import even though this deployment already has courses. '
                 'Overwrites content by id — do not use on a live server '
                 'without a backup.')
        parser.add_argument(
            '--if-empty', action='store_true',
            help='Do nothing if this deployment already has courses, instead '
                 'of failing. For running on every container start.')
        parser.add_argument(
            '--allow-schema-drift', action='store_true',
            help='Import a pack built against a different migration state. '
                 'Rows referencing columns this database lacks will fail.')

    def handle(self, *args, **options):
        # Startup mode: this runs on every container start, so "already seeded"
        # is the normal case after the first boot and must not be an error.
        if options['if_empty']:
            from apps.curriculum.models import Course
            if Course.objects.exists():
                self.stdout.write(
                    f'[import_curriculum_pack] {Course.objects.count()} course(s) '
                    f'already present — nothing to seed.'
                )
                return
            if not Path(options['archive']).exists():
                # A deployment may legitimately ship without a seed pack.
                self.stdout.write(
                    f'[import_curriculum_pack] no pack at {options["archive"]} '
                    f'— starting with an empty curriculum.'
                )
                return

        try:
            manifest = import_curriculum_pack(
                Path(options['archive']),
                force=options['force'],
                strict_schema=not options['allow_schema_drift'],
            )
        except PackError as exc:
            # A refusal is the expected outcome for the wrong file, not a
            # crash. CommandError prints the message without a traceback,
            # which is what the person holding the wrong pack needs to read.
            raise CommandError(str(exc))

        counts = manifest['counts']
        self.stdout.write(self.style.SUCCESS('Curriculum imported.'))
        self.stdout.write(
            f"  courses/units/lessons : {counts['courses']}/{counts['units']}"
            f"/{counts['lessons']}\n"
            f"  exit tickets          : {counts['tickets']}\n"
            f"  knowledge-base chunks : {counts['chunks']}\n"
            f"  media files           : {manifest['media_files']}"
        )
        self.stdout.write(
            '\nContent is platform-wide, so every school on this deployment '
            'can see it. Courses stay unpublished until a teacher publishes '
            'them.'
        )
