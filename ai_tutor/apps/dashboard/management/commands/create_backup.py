"""Take a platform backup from the command line.

The same code path the settings page uses, so a scheduled backup and a
hand-taken one produce identical archives and appear in the same list. This is
what a cron entry or a scheduled ECS task should call:

    python manage.py create_backup
    python manage.py create_backup --wait

Without --wait it returns as soon as the job row exists, which is what a
fire-and-forget scheduler wants. With it, the command blocks and exits non-zero
if the backup failed — which is what a scheduler that alerts on failure wants.
"""
from django.core.management.base import BaseCommand, CommandError

from ai_tutor.apps.dashboard import backup as backup_service
from ai_tutor.apps.dashboard.models import BackupJob


class Command(BaseCommand):
    help = 'Take a backup of the database and media'

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-media', action='store_true',
            help='Database only — faster and far smaller, but a restore from it '
                 'leaves every lesson figure broken unless the media store is intact',
        )
        parser.add_argument(
            '--wait', action='store_true',
            help='Run in the foreground and exit non-zero if it fails',
        )

    def handle(self, *args, **options):
        backup_service.reap_stale()
        running = BackupJob.objects.filter(
            status__in=(BackupJob.Status.PENDING, BackupJob.Status.RUNNING)
        ).first()
        if running:
            raise CommandError(f'backup {running.pk} is already running')

        job = BackupJob.objects.create(include_media=not options['no_media'])
        if not options['wait']:
            backup_service.start(job)
            self.stdout.write(f'backup {job.pk} started')
            return

        backup_service.build(job)
        job.refresh_from_db()
        if job.status != BackupJob.Status.DONE:
            raise CommandError(f'backup {job.pk} failed: {job.error}')
        self.stdout.write(self.style.SUCCESS(
            f'backup {job.pk} complete: {job.storage_key} ({job.size_bytes} bytes)'
        ))
