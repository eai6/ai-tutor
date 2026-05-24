"""Audit teaching-material KB coverage.

Read-only. For every TeachingMaterialUpload with status='completed',
compare the chunks_created count stored at upload time (in Postgres)
against the actual count of CurriculumChunk rows tagged with that
upload_id (in pgvector). Any upload where actual << expected is a
re-index candidate.

Background: the prod ChromaDB ran on /tmp/vectordb. The Dockerfile
copied an initial snapshot from /app/media/vectordb at container
start but writes to /tmp never synced back. So uploads completed
since the last snapshot-write lost their vectors on the next
restart — even though the TeachingMaterialUpload row still shows
"Completed" with a chunks_created count. After the pgvector port
runs, this command surfaces the gap.

Usage:
    python manage.py audit_kb_coverage                      # all institutions
    python manage.py audit_kb_coverage --institution-id 0   # one institution
    python manage.py audit_kb_coverage --threshold 0.5      # flag uploads with <50% recovery
    python manage.py audit_kb_coverage --json               # machine-readable output
"""

import json
from django.core.management.base import BaseCommand
from django.db.models import Count


class Command(BaseCommand):
    help = "Audit teaching-material KB coverage (expected vs actual chunks)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--institution-id',
            type=int,
            default=None,
            help='Restrict the audit to one institution (KB bucket id, not Institution PK).',
        )
        parser.add_argument(
            '--threshold',
            type=float,
            default=0.5,
            help='Flag uploads with actual/expected < threshold. Default 0.5.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Emit JSON instead of a table.',
        )

    def handle(self, *args, **opts):
        from apps.dashboard.models import TeachingMaterialUpload
        from apps.curriculum.models import CurriculumChunk
        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase

        uploads = (
            TeachingMaterialUpload.objects
            .filter(status='completed')
            .order_by('institution_id', 'id')
        )

        # Normalise institution_id the same way the KB does — so an upload
        # whose institution_id is the Postgres global PK (e.g. 12) collapses
        # into the canonical bucket 0 for the comparison.
        chunk_counts = dict(
            CurriculumChunk.objects
            .values('upload_id')
            .annotate(n=Count('id'))
            .values_list('upload_id', 'n')
        )

        # Optional filter — applied after normalisation
        filter_inst = opts.get('institution_id')
        threshold = opts['threshold']
        emit_json = opts['json']

        rows = []
        totals = {'uploads': 0, 'expected': 0, 'actual': 0, 'gap': 0, 'flagged': 0}

        for u in uploads:
            inst_bucket = CurriculumKnowledgeBase._normalise_institution_id(u.institution_id)
            if filter_inst is not None and inst_bucket != filter_inst:
                continue

            expected = u.chunks_created or 0
            actual = chunk_counts.get(u.id, 0)
            ratio = (actual / expected) if expected else 1.0
            flagged = expected > 0 and ratio < threshold

            totals['uploads'] += 1
            totals['expected'] += expected
            totals['actual'] += actual
            totals['gap'] += max(0, expected - actual)
            if flagged:
                totals['flagged'] += 1

            rows.append({
                'upload_id': u.id,
                'institution_bucket': inst_bucket,
                'institution_pk': u.institution_id,
                'title': u.title,
                'subject': u.subject_name,
                'grade': u.grade_level or '',
                'material_type': u.material_type,
                'expected': expected,
                'actual': actual,
                'ratio': round(ratio, 3) if expected else None,
                'flagged': flagged,
                'file_path': u.file_path,
            })

        if emit_json:
            self.stdout.write(json.dumps({'totals': totals, 'rows': rows}, indent=2))
            return

        # Table output
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"KB coverage audit — {totals['uploads']} completed uploads"
        ))
        self.stdout.write(
            f"  {'upload':>7}  {'bucket':>7}  {'expected':>9}  {'actual':>7}  "
            f"{'ratio':>6}  flag  title"
        )
        self.stdout.write('  ' + '-' * 78)

        for r in rows:
            mark = '!!' if r['flagged'] else '  '
            ratio_str = f"{r['ratio']:.2f}" if r['ratio'] is not None else '  - '
            title_short = (r['title'] or '')[:40]
            self.stdout.write(
                f"  {r['upload_id']:>7}  "
                f"{r['institution_bucket']:>7}  "
                f"{r['expected']:>9}  "
                f"{r['actual']:>7}  "
                f"{ratio_str:>6}  "
                f"{mark}    {title_short}"
            )

        self.stdout.write('  ' + '-' * 78)
        self.stdout.write(
            f"  TOTALS: {totals['uploads']} uploads, "
            f"expected={totals['expected']} chunks, "
            f"actual={totals['actual']} chunks, "
            f"gap={totals['gap']} chunks, "
            f"flagged={totals['flagged']} uploads (ratio < {threshold})"
        )

        if totals['flagged']:
            self.stdout.write(self.style.WARNING(
                f"\n{totals['flagged']} uploads have lost vectors. Next:\n"
                f"  python manage.py reset_lost_materials --threshold {threshold} --dry-run\n"
                f"  python manage.py reset_lost_materials --threshold {threshold}\n"
                f"Then click 'Process materials' on each affected course in the dashboard."
            ))
