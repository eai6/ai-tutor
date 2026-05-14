"""Process a single TeachingMaterialUpload to completion.

Entry point used by:
  - Azure Container Apps Job (production, large materials)
  - Local subprocess dispatch (dev, large materials)
  - Manual reprocessing from the materials Details page

Runs in its own process — survives main-app worker recycles, deploys, etc.
Writes structured failures to the upload row's `error_message` (JSON) so
the UI can render an actionable error.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Process one teaching-material upload to completion (Rich or Fast)."

    def add_arguments(self, parser):
        parser.add_argument(
            'upload_id', type=int,
            help='TeachingMaterialUpload primary key.',
        )
        parser.add_argument(
            '--mode', choices=['rich', 'fast'], default='rich',
            help='rich = LLM vision + figures; fast = text-only, no vision fan-out.',
        )

    def handle(self, *args, **options):
        from apps.dashboard.models import TeachingMaterialUpload
        from apps.dashboard.material_tasks import (
            process_teaching_material,
            process_teaching_material_fast,
        )

        upload_id = options['upload_id']
        mode = options['mode']

        try:
            upload = TeachingMaterialUpload.objects.get(id=upload_id)
        except TeachingMaterialUpload.DoesNotExist:
            raise CommandError(f"Upload id={upload_id} not found")

        self.stdout.write(
            f"[process_material] starting upload={upload.id} "
            f"file={upload.original_filename!r} mode={mode}"
        )

        fn = process_teaching_material if mode == 'rich' else process_teaching_material_fast
        try:
            result = fn(upload_id)
        except Exception as exc:
            # _record_failure inside the pipeline already wrote the
            # structured error_message + flipped status='failed'. Surface
            # to the caller so the Job execution exits non-zero (which
            # triggers Container Apps retry per `replica_retry_limit`).
            self.stderr.write(self.style.ERROR(
                f"[process_material] FAILED upload={upload_id}: {exc}"
            ))
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            f"[process_material] done upload={upload_id} result={result}"
        ))
