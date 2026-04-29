"""Help-docs knowledge base.

Single platform-wide ChromaDB collection (`help_docs`) embedded
with the same local sentence-transformers model the curriculum KB
uses. Chunks tagged with metadata so we can filter by audience
('all' / 'staff') at retrieval time.

See memory/help_assistant_plan.md for the design.
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# Single collection — help docs are platform-wide, not institution-scoped.
COLLECTION_NAME = "help_docs"


class HelpKB:
    """ChromaDB-backed retrieval for the help assistant."""

    def __init__(self, persist_directory: Optional[str] = None):
        # Reuse the curriculum KB's persist root so we share the
        # cached embedding model. Different collection name keeps
        # the data isolated.
        self.persist_directory = persist_directory or os.path.join(
            getattr(settings, 'VECTORDB_ROOT',
                    str(Path(settings.BASE_DIR) / 'media' / 'vectordb')),
            'support',
        )
        os.makedirs(self.persist_directory, exist_ok=True)
        self._init_chromadb()

    def _init_chromadb(self):
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self.client = chromadb.PersistentClient(
            path=self.persist_directory,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

        # Embedding function — same local sentence-transformers
        # backend used by the curriculum KB. Avoids a second model
        # download on the container.
        from chromadb.utils import embedding_functions
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name='sentence-transformers/all-MiniLM-L6-v2',
        )
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self.embedding_fn,
            metadata={'hnsw:space': 'cosine'},
        )

    def upsert_chunk(self, *, chunk_id: str, text: str,
                     source: str, section_title: str,
                     audience: str = 'all', anchor: str = '',
                     extra: Optional[Dict] = None):
        """Add or replace a single chunk by id.

        audience: 'all' (visible to students + teachers) or 'staff'
        (teachers + super_admins only).
        """
        if not text or not text.strip():
            return
        meta = {
            'source': source,
            'section_title': section_title[:200],
            'audience': audience,
            'anchor': anchor[:200],
        }
        if extra:
            for k, v in extra.items():
                if isinstance(v, (str, int, float, bool)) and v is not None:
                    meta[k] = v
        try:
            self.collection.upsert(
                ids=[chunk_id],
                documents=[text],
                metadatas=[meta],
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

        # ChromaDB metadata filter: 'all' chunks visible to everyone;
        # 'staff' only visible to staff or super_admin.
        if audience in ('staff', 'super_admin', 'teacher'):
            where = {'audience': {'$in': ['all', 'staff']}}
        else:
            where = {'audience': 'all'}

        try:
            res = self.collection.query(
                query_texts=[question],
                n_results=n_results,
                where=where,
            )
        except Exception as e:
            logger.warning(f"HelpKB query failed: {e}")
            return []

        out = []
        ids = (res.get('ids') or [[]])[0]
        docs = (res.get('documents') or [[]])[0]
        metas = (res.get('metadatas') or [[]])[0]
        dists = (res.get('distances') or [[]])[0]
        for i, doc in enumerate(docs):
            distance = dists[i] if i < len(dists) else 1.0
            score = max(0.0, 1.0 - distance)  # cosine: 0 dist = perfect match
            if score < min_score:
                continue
            meta = metas[i] if i < len(metas) else {}
            out.append({
                'id': ids[i] if i < len(ids) else '',
                'text': doc,
                'score': round(score, 3),
                'source': meta.get('source', ''),
                'section_title': meta.get('section_title', ''),
                'audience': meta.get('audience', 'all'),
                'anchor': meta.get('anchor', ''),
            })
        return out

    def count(self) -> int:
        try:
            return self.collection.count()
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
