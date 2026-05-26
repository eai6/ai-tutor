"""Help-docs knowledge base.

pgvector-backed retrieval for the help assistant. Was ChromaDB until
2026-05-24; migrated alongside the curriculum KB because both shared
the same ``VECTORDB_ROOT=/tmp/vectordb`` SMB-SQLite workaround → both
silently lost writes on container restart. See
``memory/pgvector_migration_plan.md``.

Each chunk is one row in ``apps.support.models.SupportChunk`` with a
384-d ``vector`` column. Chunks carry an audience tag ('all' / 'staff')
so the help assistant can filter by caller role at retrieval time.
Embedding model unchanged: sentence-transformers all-MiniLM-L6-v2.

Public API preserved: ``HelpKB.upsert_chunk(...)``, ``HelpKB.query(...)``,
``HelpKB.count()`` — call sites in services.py / tools.py /
build_help_index don't change.
"""

import logging
import re
from typing import Dict, List, Optional

from django.db import connection, transaction

logger = logging.getLogger(__name__)

# Kept for back-compat. Now purely a label — the ORM stores everything
# in a single ``support_supportchunk`` table; "collection" semantics
# are encoded by ``source`` + ``audience`` metadata, not by a separate
# Chroma collection.
COLLECTION_NAME = "help_docs"


class HelpKB:
    """pgvector-backed retrieval for the help assistant."""

    def __init__(self, persist_directory: Optional[str] = None):
        # ``persist_directory`` retained for back-compat with the
        # 4 call sites that still pass it (tests, build_help_index).
        # Ignored — storage is in Postgres now.
        self.persist_directory = persist_directory or '<pgvector:support>'
        self._storage_available = connection.vendor == 'postgresql'
        if not self._storage_available:
            logger.info(
                "[HelpKB] backend=%s — vector ops are no-ops (SQLite local dev / CI)",
                connection.vendor,
            )

    def upsert_chunk(self, *, chunk_id: str, text: str,
                     source: str, section_title: str,
                     audience: str = 'all', anchor: str = '',
                     extra: Optional[Dict] = None):
        """Add or replace a single chunk by id.

        audience: 'all' (visible to students + teachers) or 'staff'
        (teachers + super_admins only).

        Idempotent — ``chunk_id`` is the unique key. Re-running the
        indexer overwrites the row in place.
        """
        if not text or not text.strip():
            return
        if not self._storage_available:
            return  # SQLite local dev / CI — no-op

        from apps.support.models import SupportChunk
        from apps.curriculum.kb_storage import embed

        # Compute embedding once (the embed() helper in curriculum's
        # kb_storage uses the SAME all-MiniLM-L6-v2 model — class-level
        # cached, no second model load).
        vec = embed([text])
        if not vec:
            return
        embedding = vec[0]

        # Sanitize the extra dict (only scalars allowed, matching the
        # prior ChromaDB metadata constraints).
        clean_extra = {}
        if extra:
            for k, v in extra.items():
                if isinstance(v, (str, int, float, bool)) and v is not None:
                    clean_extra[k] = v

        try:
            SupportChunk.objects.update_or_create(
                chunk_id=chunk_id,
                defaults=dict(
                    content=text,
                    embedding=embedding,
                    content_hash=SupportChunk.compute_hash(text),
                    source=source,
                    section_title=section_title[:200],
                    audience=audience,
                    anchor=anchor[:200],
                    extra=clean_extra,
                ),
            )
        except Exception as e:
            logger.warning(f"HelpKB upsert failed for {chunk_id}: {e}")

    def query(self, question: str, *, audience: str = 'all',
              n_results: int = 5, min_score: float = 0.35) -> List[Dict]:
        """Return up to n_results chunks relevant to `question`.

        audience: caller's role. 'all' sees only public docs.
        'staff' / 'super_admin' sees public + staff-only docs.
        """
        if not question or not question.strip():
            return []
        if not self._storage_available:
            return []

        from apps.support.models import SupportChunk
        from apps.curriculum.kb_storage import embed
        from pgvector.django import CosineDistance

        # Embed the query
        vec = embed([question])
        if not vec:
            return []
        query_vec = vec[0]

        # Audience filter: 'all' chunks visible to everyone; 'staff'
        # only visible to staff or super_admin.
        qs = SupportChunk.objects.all()
        if audience in ('staff', 'super_admin', 'teacher'):
            qs = qs.filter(audience__in=['all', 'staff'])
        else:
            qs = qs.filter(audience='all')

        try:
            rows = list(
                qs.annotate(_distance=CosineDistance('embedding', query_vec))
                  .order_by('_distance')[:n_results]
                  .values('chunk_id', 'content', '_distance',
                          'source', 'section_title', 'audience', 'anchor')
            )
        except Exception as e:
            logger.warning(f"HelpKB query failed: {e}")
            return []

        out = []
        for row in rows:
            distance = float(row['_distance'])
            score = max(0.0, 1.0 - distance)  # cosine: 0 dist = perfect match
            if score < min_score:
                continue
            out.append({
                'id': row['chunk_id'],
                'text': row['content'],
                'score': round(score, 3),
                'source': row['source'] or '',
                'section_title': row['section_title'] or '',
                'audience': row['audience'] or 'all',
                'anchor': row['anchor'] or '',
            })
        return out

    def count(self) -> int:
        if not self._storage_available:
            return 0
        try:
            from apps.support.models import SupportChunk
            return SupportChunk.objects.count()
        except Exception:
            return 0


# ============================================================================
# Indexing helpers — convert source files to chunks
# ============================================================================


def _slugify(text: str) -> str:
    """Convert a heading into a URL-safe anchor id."""
    text = re.sub(r'<[^>]+>', '', text or '').strip().lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text[:80]


def chunk_help_index_html(html_text: str) -> List[Dict]:
    """Walk templates/help/index.html and emit one chunk per
    `<details class="help-section">` block.

    Each chunk inherits its summary text as the section title and
    the help-tag class as the audience marker.
    """
    chunks: List[Dict] = []
    # Crude but effective: split on the section opener.
    pattern = re.compile(
        r'<details class="help-section"[^>]*>\s*'
        r'<summary>(.*?)</summary>\s*'
        r'(.*?)</details>',
        re.DOTALL | re.IGNORECASE,
    )
    for i, match in enumerate(pattern.finditer(html_text)):
        summary_html = match.group(1)
        body_html = match.group(2)

        # Audience: 'staff' if the summary contains the staff tag.
        audience = 'staff' if 'help-tag staff' in summary_html else 'all'

        # Strip the help-tag span to get the title text.
        title_text = re.sub(r'<span class="help-tag[^"]*">.*?</span>',
                            '', summary_html, flags=re.DOTALL)
        title_text = re.sub(r'<[^>]+>', '', title_text).strip()

        # Strip HTML from body, keep newlines for paragraphs.
        body_text = re.sub(r'<br\s*/?>', '\n', body_html, flags=re.IGNORECASE)
        body_text = re.sub(r'</(p|li|h[1-6]|div)>', '\n', body_text, flags=re.IGNORECASE)
        body_text = re.sub(r'<[^>]+>', '', body_text)
        body_text = re.sub(r'\n\s*\n+', '\n\n', body_text).strip()

        if not body_text:
            continue

        anchor = _slugify(title_text)
        chunks.append({
            'id': f'help_faq:{anchor or i}',
            'text': f"{title_text}\n\n{body_text}",
            'source': 'help_faq',
            'section_title': title_text,
            'audience': audience,
            'anchor': anchor,
        })
    return chunks


def chunk_markdown(text: str, *, source: str, audience: str = 'staff',
                   max_chunk_chars: int = 1500) -> List[Dict]:
    """Split a markdown file by `## ` headings into chunks. Each
    chunk includes its heading as the section title and is capped
    at max_chunk_chars (split mid-section if needed).
    """
    chunks: List[Dict] = []
    if not text:
        return chunks

    # Split on `\n## ` heading lines, keeping the heading.
    parts = re.split(r'\n(?=## )', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.split('\n', 1)
        heading = lines[0].lstrip('#').strip() if lines[0].startswith('##') else (
            lines[0][:80] if lines else 'preamble'
        )
        # If part is small, emit as one chunk.
        if len(part) <= max_chunk_chars:
            anchor = _slugify(heading)
            chunks.append({
                'id': f'{source}:{anchor or len(chunks)}',
                'text': part,
                'source': source,
                'section_title': heading,
                'audience': audience,
                'anchor': anchor,
            })
            continue
        # Otherwise split into sub-chunks at paragraph boundaries.
        body = part
        sub_idx = 0
        while body:
            cut = max_chunk_chars
            if len(body) > cut:
                # Prefer splitting at a blank line near the cut.
                blank = body.rfind('\n\n', 0, cut)
                if blank > cut // 2:
                    cut = blank
            piece, body = body[:cut].strip(), body[cut:].strip()
            if not piece:
                break
            anchor = _slugify(f"{heading}-{sub_idx}") if sub_idx else _slugify(heading)
            chunks.append({
                'id': f'{source}:{anchor or len(chunks)}',
                'text': piece,
                'source': source,
                'section_title': heading + (f' (cont. {sub_idx})' if sub_idx else ''),
                'audience': audience,
                'anchor': anchor,
            })
            sub_idx += 1
    return chunks
