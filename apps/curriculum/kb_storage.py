"""pgvector-backed storage layer for the curriculum knowledge base.

Drop-in replacement for the ChromaDB primitives used by
``CurriculumKnowledgeBase``: same return shapes, same metadata dicts,
same upsert semantics. Implemented on top of the ``CurriculumChunk``
model + Postgres ``vector`` extension.

The class API of ``CurriculumKnowledgeBase`` is unchanged — chunking,
parsing, figure extraction, and prompt-shaping all stay in
``knowledge_base.py``. This module only owns "where chunks live" +
"how we search them".

Three primitives:

  - ``embed(texts) -> list[list[float]]``: sentence-transformers
    encoding (or OpenAI when ``EMBEDDING_BACKEND='openai'``). Same
    384-d model the prior ChromaDB collections used, so existing
    embeddings remain comparable.
  - ``upsert_chunks(institution_id, chunks)``: write + dedup. Uses the
    ``(institution_id, content_hash)`` UniqueConstraint on
    ``CurriculumChunk`` — re-indexing the same content updates the row
    in place instead of duplicating.
  - ``query_chunks(institution_id, query_text, n_results, where_filter)``:
    cosine-distance search. Returns a ChromaDB-shaped result dict so
    every existing call site (`results['documents'][0][i]`,
    `results['metadatas'][0][i]`, `results['distances'][0][i]`) keeps
    working without modification.

SQLite degradation: on non-Postgres backends ``query_chunks`` returns
an empty result. Existing call sites already fail soft on empty
ChromaDB collections.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional

from django.db import connection, transaction

logger = logging.getLogger(__name__)


# ─── Embedding ──────────────────────────────────────────────────────────

_SHARED_EMBED_FN = None


def _get_embedding_fn():
    """Lazy-load the sentence-transformers model once per process.

    Mirrors the ``CurriculumKnowledgeBase._shared_embedding_fn`` cache
    pattern. Reuse means a single model load per worker, not per
    institution KB instance.
    """
    global _SHARED_EMBED_FN
    if _SHARED_EMBED_FN is not None:
        return _SHARED_EMBED_FN

    from django.conf import settings
    backend = getattr(settings, 'EMBEDDING_BACKEND', 'local')

    if backend == 'openai':
        # OpenAI text-embedding-3-small — 1536 dims. Mismatch with the
        # pgvector column (384 d). Refuse loudly so we don't silently
        # corrupt the index.
        raise NotImplementedError(
            "EMBEDDING_BACKEND='openai' is not supported under the "
            "pgvector layout. The column is fixed at 384 dimensions "
            "(all-MiniLM-L6-v2); switching backends needs a schema "
            "migration."
        )

    # Local sentence-transformers (default). Same model as the prior
    # ChromaDB embedding function (all-MiniLM-L6-v2, 384 d).
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('all-MiniLM-L6-v2')
    _SHARED_EMBED_FN = model
    logger.info("[KB] sentence-transformers all-MiniLM-L6-v2 loaded")
    return _SHARED_EMBED_FN


def embed(texts: List[str]) -> List[List[float]]:
    """Encode a list of texts to 384-d vectors. Returns plain lists."""
    if not texts:
        return []
    model = _get_embedding_fn()
    vectors = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    return vectors.tolist()


# ─── Upsert ─────────────────────────────────────────────────────────────

# Metadata keys that map to ``CurriculumChunk`` columns. Anything else
# in a chunk's metadata dict is silently dropped (matches ChromaDB's
# behavior of accepting arbitrary metadata but only the ones we
# explicitly want to filter on are useful).
_METADATA_KEYS = (
    'subject', 'grade_level', 'section', 'chunk_type', 'source_file', 'upload_id',
    'source_type', 'material_type', 'material_title',
    'question_number', 'question_type', 'has_answers', 'year', 'paper_number',
    'figure_type', 'figure_page', 'figure_number', 'figure_image_url',
)


def _metadata_to_fields(metadata: Dict) -> Dict:
    """Pull recognised keys out of a chunk metadata dict for the ORM."""
    out: Dict[str, Any] = {}
    for k in _METADATA_KEYS:
        if k in metadata and metadata[k] is not None:
            out[k] = metadata[k]
    return out


def upsert_chunks(institution_id: int, chunks: List[Any]) -> Dict[str, int]:
    """Upsert a batch of CurriculumKnowledgeBase chunks into pgvector.

    ``chunks`` is a list of the dataclass-y ``CurriculumChunk`` objects
    defined in ``knowledge_base.py`` (each has ``id``, ``content``,
    ``metadata``).

    Dedup key: ``(institution_id, content_hash)``. Re-running with the
    same content overwrites metadata + embedding (idempotent).
    """
    if not chunks:
        return {'indexed': 0}

    from apps.curriculum.models import CurriculumChunk as Chunk

    if connection.vendor != 'postgresql':
        # Non-Postgres backends can't store vectors. We could write
        # the row with a NULL embedding but it'd be useless for
        # queries. Fail soft + log.
        logger.warning(
            "[KB] upsert_chunks: backend=%s — skipping (vectors require Postgres + pgvector)",
            connection.vendor,
        )
        return {'indexed': 0, 'skipped_reason': f'backend={connection.vendor}'}

    # Compute embeddings in one batch (faster than one-by-one)
    contents = [c.content for c in chunks]
    vectors = embed(contents)

    rows = []
    for c, vec in zip(chunks, vectors):
        content_hash = Chunk.compute_hash(c.content)
        fields = _metadata_to_fields(c.metadata or {})
        rows.append(Chunk(
            institution_id=institution_id,
            content=c.content,
            content_hash=content_hash,
            embedding=vec,
            **fields,
        ))

    # Idempotent: rely on the (institution_id, content_hash)
    # UniqueConstraint + update_conflicts. Django 4.1+ syntax.
    with transaction.atomic():
        Chunk.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=['institution_id', 'content_hash'],
            update_fields=[
                'content', 'embedding',
                *_METADATA_KEYS,
                'updated_at',
            ],
        )

    logger.info(f"[KB] upsert_chunks: institution_id={institution_id} count={len(rows)}")
    return {'indexed': len(rows)}


# ─── Query (ChromaDB-shaped output) ─────────────────────────────────────

def _where_filter_to_q(where_filter: Optional[Dict]) -> Any:
    """Translate ChromaDB ``where`` dict syntax to a Django Q object.

    Supports the operators used by callers in this codebase:
      - ``{"col": value}``          (implicit $eq)
      - ``{"col": {"$eq": v}}``
      - ``{"col": {"$ne": v}}``
      - ``{"col": {"$in": [...]}}``
      - ``{"$and": [{...}, {...}]}``
      - ``{"$or":  [{...}, {...}]}``

    Returns a Django ``Q`` instance (empty Q when no filter). Unknown
    operators raise ValueError — better to fail loud than silently
    drop a constraint.
    """
    from django.db.models import Q
    if not where_filter:
        return Q()

    def build(condition: Dict) -> Q:
        if '$and' in condition:
            children = [build(c) for c in condition['$and']]
            q = Q()
            for child in children:
                q &= child
            return q
        if '$or' in condition:
            children = [build(c) for c in condition['$or']]
            q = Q()
            for child in children:
                q |= child
            return q
        # Leaf: one or more {col: value | {$op: value}} pairs ANDed.
        q = Q()
        for col, spec in condition.items():
            if col.startswith('$'):
                raise ValueError(f"Unexpected top-level operator {col!r} in where_filter")
            if isinstance(spec, dict):
                if '$eq' in spec:
                    q &= Q(**{col: spec['$eq']})
                elif '$ne' in spec:
                    q &= ~Q(**{col: spec['$ne']})
                elif '$in' in spec:
                    q &= Q(**{f"{col}__in": spec['$in']})
                else:
                    raise ValueError(f"Unsupported operator in where_filter for {col!r}: {spec}")
            else:
                # Implicit $eq
                q &= Q(**{col: spec})
        return q

    return build(where_filter)


def _empty_result(n_queries: int = 1) -> Dict[str, List[List]]:
    """ChromaDB-shaped empty result."""
    return {
        'ids': [[] for _ in range(n_queries)],
        'documents': [[] for _ in range(n_queries)],
        'metadatas': [[] for _ in range(n_queries)],
        'distances': [[] for _ in range(n_queries)],
    }


def query_chunks(
    institution_id: int,
    query_text: str,
    n_results: int = 10,
    where_filter: Optional[Dict] = None,
) -> Dict[str, List[List]]:
    """Cosine-distance search; returns a ChromaDB-shaped result dict.

    Result shape mirrors what ``collection.query(...)`` returned, so
    existing call sites in ``knowledge_base.py`` continue to work
    without modification:

        results['documents'][0][i]   # chunk text
        results['metadatas'][0][i]   # chunk metadata
        results['distances'][0][i]   # cosine distance
        results['ids'][0][i]         # chunk PK as a string

    Empty result on SQLite or when no matches.
    """
    if connection.vendor != 'postgresql':
        return _empty_result()

    from apps.curriculum.models import CurriculumChunk as Chunk
    from pgvector.django import CosineDistance

    # Embed the query
    vec = embed([query_text])
    if not vec:
        return _empty_result()
    query_vec = vec[0]

    qs = Chunk.objects.filter(institution_id=institution_id)
    where_q = _where_filter_to_q(where_filter)
    if where_q.children or where_q.connector != 'AND':
        qs = qs.filter(where_q)

    qs = qs.annotate(_distance=CosineDistance('embedding', query_vec)) \
           .order_by('_distance')[:n_results] \
           .values('id', 'content', 'content_hash', '_distance', *_METADATA_KEYS)

    ids: List[str] = []
    docs: List[str] = []
    metas: List[Dict] = []
    dists: List[float] = []
    for row in qs:
        ids.append(str(row['id']))
        docs.append(row['content'])
        # Rebuild a metadata dict matching the ChromaDB shape
        metas.append({k: row[k] for k in _METADATA_KEYS if row.get(k) not in (None, '')})
        dists.append(float(row['_distance']))

    return {
        'ids': [ids],
        'documents': [docs],
        'metadatas': [metas],
        'distances': [dists],
    }


def collection_stats(institution_id: int) -> Dict:
    """Return a stats dict in the shape expected by
    ``CurriculumKnowledgeBase.get_collection_stats``."""
    from apps.curriculum.models import CurriculumChunk as Chunk

    if connection.vendor != 'postgresql':
        return {
            'collection_name': f'curriculum_{institution_id}',
            'total_chunks': 0,
            'persist_directory': None,
            'backend': connection.vendor,
        }

    n = Chunk.objects.filter(institution_id=institution_id).count()
    return {
        'collection_name': f'curriculum_{institution_id}',
        'total_chunks': n,
        'persist_directory': None,  # no longer applicable (Postgres-backed)
        'backend': 'pgvector',
    }


def clear_institution(institution_id: int) -> int:
    """Delete every chunk for an institution. Returns count deleted."""
    from apps.curriculum.models import CurriculumChunk as Chunk
    n, _ = Chunk.objects.filter(institution_id=institution_id).delete()
    logger.info(f"[KB] clear_institution: institution_id={institution_id} deleted={n}")
    return n
