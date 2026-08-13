"""Re-index all help docs into the `help_docs` ChromaDB collection.

Idempotent. Run after deploys (the GitHub Actions workflow can call
this) or manually via:

    python manage.py build_help_index
    python manage.py build_help_index --with-source     # also index source code

Sources (default):
  - templates/help/index.html        (audience-tagged per section)
  - docs/*.md                        (audience: staff — rich context)
  - CLAUDE.md                        (audience: staff)
  - README.md                        (audience: staff)
  - memory/*.md                      (audience: staff)

With ``--with-source``:
  - apps/<app>/views.py, models.py, services.py, *_engine.py, etc.
    chunked by class / function so the help assistant can answer
    "how does X work" with code-level grounding. Tagged
    audience='staff' so students never see source.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from ai_tutor.apps.support.kb import HelpKB, chunk_help_index_html, chunk_markdown

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Re-index help docs (FAQ + CLAUDE.md + README + memory/*.md) into the help_docs ChromaDB collection.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be indexed without writing to ChromaDB.')
        parser.add_argument('--with-source', action='store_true',
                            help='Also index source code under apps/ (views.py, models.py, etc.) — staff-only chunks.')

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

        # 2. docs/*.md — rich platform documentation (e.g. the
        # ai_assistant_context.md write-up). Staff-only because it
        # references internal architecture.
        docs_dir = repo_root / 'docs'
        if docs_dir.exists():
            doc_chunk_count = 0
            for md_path in sorted(docs_dir.glob('*.md')):
                text = md_path.read_text(encoding='utf-8')
                chunks = chunk_markdown(
                    text, source=f'docs:{md_path.stem}', audience='staff',
                )
                all_chunks.extend(chunks)
                doc_chunk_count += len(chunks)
            self.stdout.write(f'  docs/: {doc_chunk_count} chunks')

        # 3. CLAUDE.md — operational rules. Staff-only.
        claude_path = repo_root / 'CLAUDE.md'
        if claude_path.exists():
            text = claude_path.read_text(encoding='utf-8')
            chunks = chunk_markdown(text, source='claude_md', audience='staff')
            all_chunks.extend(chunks)
            self.stdout.write(f'  CLAUDE.md: {len(chunks)} chunks')

        # 4. README.md — architecture overview. Staff-only.
        readme_path = repo_root / 'README.md'
        if readme_path.exists():
            text = readme_path.read_text(encoding='utf-8')
            chunks = chunk_markdown(text, source='readme', audience='staff')
            all_chunks.extend(chunks)
            self.stdout.write(f'  README.md: {len(chunks)} chunks')

        # 5. memory/*.md — plan docs. Staff-only.
        memory_dir = repo_root / 'memory'
        if memory_dir.exists():
            for md_path in sorted(memory_dir.glob('*.md')):
                text = md_path.read_text(encoding='utf-8')
                chunks = chunk_markdown(
                    text, source=f'memory:{md_path.stem}', audience='staff',
                )
                all_chunks.extend(chunks)
            self.stdout.write(f'  memory/: total chunks across all plan docs')

        # 6. Source code — gated behind --with-source so the routine
        # deploy-time index doesn't pay the cost. Each function /
        # class becomes a chunk; the docstring + body become the text.
        if options.get('with_source'):
            src_chunks = self._chunk_source_tree(repo_root / 'apps')
            all_chunks.extend(src_chunks)
            self.stdout.write(f'  apps/ source: {len(src_chunks)} chunks')

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

    # ------------------------------------------------------------------
    # Source-code chunker
    # ------------------------------------------------------------------

    # Files that consistently have the most useful documentation /
    # context for the help assistant. Skip __pycache__, migrations,
    # tests, fixtures.
    _SOURCE_PATTERNS = (
        'views.py', 'models.py', 'services.py', 'kb.py', 'tools.py',
        'client.py', 'content_generator.py', 'conversational_tutor.py',
        'parametric_renderer.py', 'question_bank.py', 'bank_grader.py',
        'competency_tracker.py', 'image_service.py',
    )
    _SOURCE_SKIP_DIRS = ('__pycache__', 'migrations', 'tests',
                         'management', 'static')

    def _chunk_source_tree(self, root: Path):
        """Walk ``root`` and emit one chunk per top-level def / class.
        Each chunk's text is the source itself (signature + docstring
        + body) so the assistant can quote ``apps/foo/bar.py:42`` and
        explain what the function does. Audience: staff."""
        import ast

        out: list = []
        if not root.exists():
            return out

        for py_path in root.rglob('*.py'):
            if any(p in self._SOURCE_SKIP_DIRS for p in py_path.parts):
                continue
            if py_path.name not in self._SOURCE_PATTERNS:
                continue
            try:
                source = py_path.read_text(encoding='utf-8')
            except Exception:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            lines = source.splitlines()
            rel = py_path.relative_to(Path(settings.BASE_DIR))

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = max(node.lineno - 1, 0)
                    end = getattr(node, 'end_lineno', None) or len(lines)
                    body = '\n'.join(lines[start:end])
                    if not body.strip() or len(body) < 30:
                        continue
                    # Cap at ~6KB to keep one chunk per concept.
                    if len(body) > 6000:
                        body = body[:6000] + '\n# … (truncated)'
                    docstring = (ast.get_docstring(node) or '').splitlines()[0:3]
                    summary = ' '.join(docstring).strip() or node.name
                    out.append({
                        'id': f'src:{rel}::{node.name}',
                        'text': (
                            f"# {rel}::{node.name}\n"
                            f"{summary}\n\n```python\n{body}\n```"
                        ),
                        'source': f'src:{rel}',
                        'section_title': f"{rel}::{node.name}",
                        'audience': 'staff',
                        'anchor': f"{rel.as_posix()}-{node.name}".replace('/', '-'),
                    })
        return out
