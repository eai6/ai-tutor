"""Index a file or directory tree of teaching materials into the KB.

    python manage.py index_materials --path seychelles_package/textbook/geography/S3 \
        --subject Geography --grade-level S3 --material-type textbook --no-figures

    python manage.py index_materials --path seychelles_package/worksheet/geography/S3 \
        --subject Geography --grade-level S3 --material-type worksheet

Why this exists rather than another one-off script: `seed_local_kb` indexes a
hardcoded doc list through `index_curriculum_document`, which **always** runs
vision-LLM figure extraction. Measured on a 1 MB curriculum PDF that is 315 s —
fine for one file, ~26 h for the 313 MB of S3 geography textbooks. This command
exposes the pipeline's own fast path (`extract_figures=False`) and takes an
arbitrary path, so populating a subject/grade is one call instead of a bespoke
script each time.

**Resumable by design.** Chunk identity is `(institution_id, content_hash)`, so
re-running is idempotent at the row level, and `--skip-existing` avoids
re-parsing files already represented in the KB. That matters here: indexing has
historically been the step where vectors go missing (vision calls are slow,
background threads die on container restart, and `clear_institution()` is a
hard delete outside any transaction). A command you can simply run again turns
a partial failure into a resumable job instead of a lost afternoon.

See memory/data_backup_and_recovery_plan.md for how the KB came to be empty in
production, and CLAUDE.md for the institution-scoping rule (0 = platform-wide).
"""
from __future__ import annotations

import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

# Extensions the parser handles. Anything else in the tree is skipped with a
# note rather than failing the run — these directories hold stray images and
# archives alongside the documents.
SUPPORTED = {'.pdf', '.docx', '.doc', '.txt', '.md'}

# Figure extraction is worth its cost on material where diagrams carry the
# teaching (worksheets, curriculum specs) and not on long-form prose where it
# multiplies runtime by an order of magnitude for marginal recall.
FIGURES_BY_DEFAULT = {'worksheet', 'curriculum', 'reference'}


class Command(BaseCommand):
    help = 'Index a file or directory of teaching materials into the curriculum KB.'

    def add_arguments(self, parser):
        parser.add_argument('--path', required=True,
                            help='File or directory (searched recursively).')
        parser.add_argument('--subject', required=True)
        parser.add_argument('--grade-level', required=True)
        parser.add_argument('--material-type', default='textbook',
                            help='textbook / worksheet / question_bank / reference / '
                                 'notes / curriculum. Also selects the figure default.')
        parser.add_argument('--institution', type=int, default=0,
                            help='Institution id. 0 = platform-wide (default), '
                                 'matching GLOBAL_INSTITUTION_ID.')
        parser.add_argument('--figures', dest='figures', action='store_true', default=None,
                            help='Force vision-LLM figure extraction on.')
        parser.add_argument('--no-figures', dest='figures', action='store_false',
                            help='Force it off. Much faster on long documents.')
        parser.add_argument('--skip-existing', action='store_true',
                            help='Skip files that already have chunks in the KB.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Index at most N files (for a timed sample).')
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be indexed, then exit.')

    def handle(self, *args, **opts):
        from ai_tutor.apps.curriculum.knowledge_base import get_knowledge_base
        from ai_tutor.apps.curriculum.models import CurriculumChunk

        root = Path(opts['path'])
        if not root.exists():
            raise CommandError(f'{root} does not exist')

        files = ([root] if root.is_file()
                 else sorted(p for p in root.rglob('*') if p.is_file()))
        docs = [p for p in files if p.suffix.lower() in SUPPORTED]
        skipped_ext = len(files) - len(docs)

        material_type = opts['material_type']
        figures = opts['figures']
        if figures is None:
            figures = material_type in FIGURES_BY_DEFAULT

        institution_id = opts['institution']

        if opts['skip_existing']:
            already = set(
                CurriculumChunk.objects
                .filter(institution_id=institution_id)
                .values_list('source_file', flat=True).distinct()
            )
            before = len(docs)
            docs = [p for p in docs if p.name not in already]
            if before - len(docs):
                self.stdout.write(f'skipping {before - len(docs)} already-indexed file(s)')

        if opts['limit']:
            docs = docs[:opts['limit']]

        total_mb = sum(p.stat().st_size for p in docs) / 1e6
        self.stdout.write(
            f'{len(docs)} file(s), {total_mb:.1f} MB | subject={opts["subject"]} '
            f'grade={opts["grade_level"]} type={material_type} '
            f'institution={institution_id} figures={figures}'
        )
        if skipped_ext:
            self.stdout.write(f'({skipped_ext} non-document file(s) ignored)')

        if opts['dry_run']:
            for p in docs:
                self.stdout.write(f'  would index {p} ({p.stat().st_size / 1e6:.1f} MB)')
            return

        kb = get_knowledge_base(institution_id)
        started = time.time()
        indexed = failed = 0
        chunks_total = 0

        for i, path in enumerate(docs, 1):
            size_mb = path.stat().st_size / 1e6
            self.stdout.write(f'[{i}/{len(docs)}] {path.name} ({size_mb:.1f} MB) ... ',
                              ending='')
            self.stdout.flush()
            t0 = time.time()
            try:
                result = kb.index_teaching_material(
                    file_path=str(path),
                    subject=opts['subject'],
                    grade_level=opts['grade_level'],
                    material_title=path.stem.replace('-', ' ').replace('_', ' '),
                    material_type=material_type,
                    extract_figures=figures,
                )
            except Exception as exc:                        # noqa: BLE001
                # One unparseable file must not abandon the rest of the tree —
                # the whole point is that a long run makes progress it keeps.
                failed += 1
                self.stdout.write(self.style.ERROR(
                    f'FAILED ({type(exc).__name__}: {exc})'))
                continue
            elapsed = time.time() - t0
            n = result.get('chunks_indexed', 0)
            chunks_total += n
            indexed += 1
            self.stdout.write(self.style.SUCCESS(
                f'{n} chunks in {elapsed:.0f}s ({size_mb / max(elapsed, 1e-9) * 60:.1f} MB/min)'))

        total = time.time() - started
        in_db = CurriculumChunk.objects.filter(institution_id=institution_id).count()
        self.stdout.write(self.style.SUCCESS(
            f'\nindexed {indexed} file(s), {failed} failed, {chunks_total} new chunk(s) '
            f'in {total / 60:.1f} min'))
        # Report what is actually in the table, not just what we think we wrote.
        # The production KB spent two months reporting 4,996 chunks created while
        # holding zero rows; counting the destination is the cheap way to notice.
        self.stdout.write(f'CurriculumChunk rows for institution {institution_id}: {in_db}')
