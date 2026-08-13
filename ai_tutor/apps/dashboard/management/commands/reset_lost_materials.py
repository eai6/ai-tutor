"""Reset teaching-material uploads whose vectors were lost.

Pair to ``audit_kb_coverage``. For every TeachingMaterialUpload with
``status='completed'`` whose actual chunk count is significantly
below ``chunks_created``, flip the row back to ``status='pending'``
so the existing "Process materials" platform button picks it up.

This does NOT re-run the indexer. It only resets row state so a
teacher / super-admin can re-trigger processing manually through the
dashboard. That keeps the well-tested job-dispatch + mode-routing +
progress-tracking + audit-log path in charge.

Source files (PDF/DOCX) are persistent on the Azure Files mount —
only the vectors were lost. Re-processing will re-extract + re-chunk
+ re-embed using sentence-transformers (zero API cost).

Usage:
    python manage.py reset_lost_materials --dry-run
    python manage.py reset_lost_materials --threshold 0.5
    python manage.py reset_lost_materials --upload-ids 47,48,52
    python manage.py reset_lost_materials --institution-id 0
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone


class Command(BaseCommand):
    help = "Reset uploads with missing/partial vectors to status='pending'."

    def add_arguments(self, parser):
        parser.add_argument(
            '--threshold',
            type=float,
            default=0.5,
            help='Reset uploads where actual/expected < threshold. Default 0.5.',
        )
        parser.add_argument(
            '--institution-id',
            type=int,
            default=None,
            help='Restrict to one institution KB bucket (post-normalisation).',
        )
        parser.add_argument(
            '--upload-ids',
            type=str,
            default='',
            help='Comma-separated upload IDs to force-reset (bypasses threshold).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report which uploads would be reset, do not write.',
        )

    def handle(self, *args, **opts):
        from ai_tutor.apps.dashboard.models import TeachingMaterialUpload
        from ai_tutor.apps.curriculum.models import CurriculumChunk
        from ai_tutor.apps.curriculum.knowledge_base import CurriculumKnowledgeBase

        forced_ids = set()
        if opts['upload_ids']:
            try:
                forced_ids = {int(x.strip()) for x in opts['upload_ids'].split(',') if x.strip()}
            except ValueError:
                raise CommandError("--upload-ids must be a comma-separated list of integers")

        chunk_counts = dict(
            CurriculumChunk.objects
            .values('upload_id')
            .annotate(n=Count('id'))
            .values_list('upload_id', 'n')
        )

        uploads_qs = TeachingMaterialUpload.objects.filter(status='completed')
        if forced_ids:
            uploads_qs = uploads_qs.filter(id__in=forced_ids)

        threshold = opts['threshold']
        filter_inst = opts.get('institution_id')
        dry_run = opts['dry_run']

        targets = []
        for u in uploads_qs.order_by('institution_id', 'id'):
            inst_bucket = CurriculumKnowledgeBase._normalise_institution_id(u.institution_id)
            if filter_inst is not None and inst_bucket != filter_inst:
                continue

            expected = u.chunks_created or 0
            actual = chunk_counts.get(u.id, 0)
            ratio = (actual / expected) if expected else 1.0

            if u.id in forced_ids:
                reason = 'forced'
            elif expected > 0 and ratio < threshold:
                reason = f'gap (ratio {ratio:.2f} < {threshold})'
            else:
                continue

            # File-existence check — uploads whose source file is gone
            # can't be re-processed via the platform either, so flag them
            # but don't block the reset (the platform will error visibly).
            source_missing = not os.path.exists(u.file_path)

            targets.append((u, inst_bucket, expected, actual, reason, source_missing))

        if not targets:
            self.stdout.write(self.style.SUCCESS(
                "No reset candidates. KB coverage looks healthy."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Reset plan: {len(targets)} uploads"
            + (" (DRY-RUN)" if dry_run else "")
        ))
        for u, bucket, expected, actual, reason, source_missing in targets:
            warn = ' [SOURCE FILE MISSING]' if source_missing else ''
            self.stdout.write(
                f"  upload={u.id} bucket={bucket} expected={expected} "
                f"actual={actual} reason={reason}{warn} title={u.title!r}"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "\nDry-run — no rows modified. Re-run without --dry-run "
                "to flip status -> pending."
            ))
            return

        # Execute the reset
        now = timezone.now()
        reset_count = 0
        with_missing_source = 0
        for u, bucket, expected, actual, reason, source_missing in targets:
            u.status = 'pending'
            u.chunks_created = 0
            u.figures_extracted = 0
            u.error_message = ''
            u.processing_log = ''
            u.completed_at = None
            u.pages_processed = 0
            u.phase = ''
            u.job_execution_name = ''
            u.updated_at = now
            u.save(update_fields=[
                'status', 'chunks_created', 'figures_extracted',
                'error_message', 'processing_log', 'completed_at',
                'pages_processed', 'phase', 'job_execution_name',
                'updated_at',
            ])
            reset_count += 1
            if source_missing:
                with_missing_source += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n{reset_count} uploads flipped to status='pending'."
        ))
        if with_missing_source:
            self.stdout.write(self.style.WARNING(
                f"  Heads-up: {with_missing_source} of those have a missing "
                f"source file. Platform processing will fail visibly on those — "
                f"a teacher will need to re-upload the original PDF."
            ))
        self.stdout.write(
            "Next: open each affected course in the dashboard and click "
            "'Process materials' to re-trigger indexing through the existing UI."
        )
