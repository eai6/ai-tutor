"""Export / import CurriculumChunk rows, embeddings included.

    # on the machine that did the indexing
    python manage.py sync_kb_chunks --export kb.jsonl --institution 0

    # against the target database
    DATABASE_URL=... python manage.py sync_kb_chunks --import kb.jsonl

Why move rows instead of re-indexing at the destination: embeddings are
deterministic 384-d vectors from a fixed model (all-MiniLM-L6-v2, parity-tested
between the sentence-transformers and ONNX backends), so a chunk computed here
is equivalent to one computed there. Re-parsing the source PDFs at the
destination costs hours of CPU and, on Azure Container Apps, runs in a
background thread that dies on any restart — which is how the production KB
came to hold 0 rows while reporting 4,996 chunks created. Moving finished rows
takes seconds and is safely re-runnable.

**Never uses primary keys.** Rows are matched on ``(institution_id,
content_hash)`` — the model's own uniqueness constraint — so importing cannot
collide with unrelated rows at the destination or depend on the two databases
having the same id sequence.

Deliberately NOT part of the offline content pack (`apps/desktop/packs.py`),
which ships a whole institution to a student device. This moves one table
between servers.

See memory/data_backup_and_recovery_plan.md for how the KB was emptied.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ai_tutor.apps.curriculum.kb_storage import _METADATA_KEYS

BATCH = 500


class Command(BaseCommand):
    help = 'Export or import CurriculumChunk rows (with embeddings) as JSONL.'

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--export', metavar='PATH', help='Write rows to PATH.')
        mode.add_argument('--import', dest='import_path', metavar='PATH',
                          help='Read rows from PATH and upsert them.')
        parser.add_argument('--institution', type=int, default=None,
                            help='Export: only this institution_id. '
                                 'Import: override the id on every row.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Import: report what would change, write nothing.')

    # ── export ───────────────────────────────────────────────────────
    def _export(self, path, institution_id):
        from ai_tutor.apps.curriculum.models import CurriculumChunk

        qs = CurriculumChunk.objects.all()
        if institution_id is not None:
            qs = qs.filter(institution_id=institution_id)

        fields = ('institution_id', 'content', 'content_hash', 'embedding',
                  *_METADATA_KEYS)
        written = 0
        with open(path, 'w', encoding='utf-8') as handle:
            for row in qs.values(*fields).iterator(chunk_size=BATCH):
                # VectorField reads back as a numpy array; JSON needs a list.
                emb = row.get('embedding')
                if emb is not None and not isinstance(emb, list):
                    row['embedding'] = [float(x) for x in emb]
                handle.write(json.dumps(row, default=str) + '\n')
                written += 1
        self.stdout.write(self.style.SUCCESS(
            f'exported {written} chunk(s) to {path} '
            f'({Path(path).stat().st_size / 1e6:.1f} MB)'))

    # ── import ───────────────────────────────────────────────────────
    def _import(self, path, institution_override, dry_run):
        from django.db import connection
        from ai_tutor.apps.curriculum.models import CurriculumChunk

        path = Path(path)
        if not path.exists():
            raise CommandError(f'{path} does not exist')

        rows = []
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if institution_override is not None:
                    row['institution_id'] = institution_override
                rows.append(row)

        if not rows:
            raise CommandError(f'{path} contained no rows')

        institutions = sorted({r['institution_id'] for r in rows})
        before = {
            i: CurriculumChunk.objects.filter(institution_id=i).count()
            for i in institutions
        }
        self.stdout.write(
            f'target backend : {connection.vendor}\n'
            f'rows in file   : {len(rows)}\n'
            f'institutions   : {institutions}\n'
            f'already present: {before}'
        )

        if dry_run:
            hashes = {(r['institution_id'], r['content_hash']) for r in rows}
            existing = set(
                CurriculumChunk.objects
                .filter(institution_id__in=institutions)
                .values_list('institution_id', 'content_hash')
            )
            self.stdout.write(self.style.WARNING(
                f'dry run: {len(hashes - existing)} new, '
                f'{len(hashes & existing)} would be updated. Nothing written.'))
            return

        # Dedupe within the file. bulk_create(update_conflicts=True) cannot
        # touch the same conflict target twice in one statement, and duplicate
        # content across two source documents is normal (shared boilerplate).
        by_key = {}
        for r in rows:
            by_key[(r['institution_id'], r['content_hash'])] = r
        deduped = list(by_key.values())
        if len(deduped) != len(rows):
            self.stdout.write(
                f'collapsed {len(rows) - len(deduped)} duplicate key(s) in the file')

        objs = [CurriculumChunk(**r) for r in deduped]

        # One transaction for the whole load: a partial KB is the failure mode
        # this project has already lived through once.
        with transaction.atomic():
            for start in range(0, len(objs), BATCH):
                CurriculumChunk.objects.bulk_create(
                    objs[start:start + BATCH],
                    update_conflicts=True,
                    unique_fields=['institution_id', 'content_hash'],
                    update_fields=['content', 'embedding', *_METADATA_KEYS,
                                   'updated_at'],
                )
                self.stdout.write(f'  {min(start + BATCH, len(objs))}/{len(objs)}')

        after = {
            i: CurriculumChunk.objects.filter(institution_id=i).count()
            for i in institutions
        }
        self.stdout.write(self.style.SUCCESS(
            f'import complete. rows now: {after} (was {before})'))

        # Count the destination rather than trust the write. Production spent
        # two months reporting chunks it did not have.
        total = CurriculumChunk.objects.count()
        self.stdout.write(f'CurriculumChunk total rows: {total}')

    def handle(self, *args, **opts):
        if opts['export']:
            self._export(opts['export'], opts['institution'])
        else:
            self._import(opts['import_path'], opts['institution'], opts['dry_run'])
