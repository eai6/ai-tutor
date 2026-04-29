"""Re-index all help docs into the `help_docs` ChromaDB collection.

Idempotent. Run after deploys (the GitHub Actions workflow can call
this) or manually via:

    python manage.py build_help_index

Sources:
  - templates/help/index.html        (audience-tagged per section)
  - CLAUDE.md                        (audience: staff)
  - README.md                        (audience: staff)
  - memory/*.md                      (audience: staff)
"""

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.support.kb import HelpKB, chunk_help_index_html, chunk_markdown

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Re-index help docs (FAQ + CLAUDE.md + README + memory/*.md) into the help_docs ChromaDB collection.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be indexed without writing to ChromaDB.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        repo_root = Path(settings.BASE_DIR)

        all_chunks = []

        # 1. Help FAQ template — primary source, mixed audience.
        faq_path = repo_root / 'templates' / 'help' / 'index.html'
        if faq_path.exists():
            html_text = faq_path.read_text(encoding='utf-8')
            faq_chunks = chunk_help_index_html(html_text)
            all_chunks.extend(faq_chunks)
            self.stdout.write(f'  FAQ: {len(faq_chunks)} chunks from {faq_path}')
        else:
            self.stdout.write(self.style.WARNING(f'  FAQ template not found at {faq_path} — skipping'))

        # 2. CLAUDE.md — operational rules. Staff-only.
        claude_path = repo_root / 'CLAUDE.md'
        if claude_path.exists():
            text = claude_path.read_text(encoding='utf-8')
            chunks = chunk_markdown(text, source='claude_md', audience='staff')
            all_chunks.extend(chunks)
            self.stdout.write(f'  CLAUDE.md: {len(chunks)} chunks')

        # 3. README.md — architecture overview. Staff-only.
        readme_path = repo_root / 'README.md'
        if readme_path.exists():
            text = readme_path.read_text(encoding='utf-8')
            chunks = chunk_markdown(text, source='readme', audience='staff')
            all_chunks.extend(chunks)
            self.stdout.write(f'  README.md: {len(chunks)} chunks')

        # 4. memory/*.md — plan docs. Staff-only.
        memory_dir = repo_root / 'memory'
        if memory_dir.exists():
            for md_path in sorted(memory_dir.glob('*.md')):
                text = md_path.read_text(encoding='utf-8')
                chunks = chunk_markdown(
                    text, source=f'memory:{md_path.stem}', audience='staff',
                )
                all_chunks.extend(chunks)
            self.stdout.write(f'  memory/: total chunks across all plan docs')

        self.stdout.write(self.style.SUCCESS(
            f'\nTotal chunks ready: {len(all_chunks)}'
        ))

        if dry:
            for c in all_chunks[:5]:
                self.stdout.write(f"  preview: [{c['source']}] {c['section_title'][:60]}")
            self.stdout.write(self.style.WARNING('Dry run — no writes.'))
            return

        kb = HelpKB()
        before = kb.count()
        for c in all_chunks:
            kb.upsert_chunk(
                chunk_id=c['id'],
                text=c['text'],
                source=c['source'],
                section_title=c['section_title'],
                audience=c['audience'],
                anchor=c.get('anchor', ''),
            )
        after = kb.count()
        self.stdout.write(self.style.SUCCESS(
            f'Indexed. Collection size: {before} → {after}.'
        ))
