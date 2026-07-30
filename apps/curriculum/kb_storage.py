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

Two search backends, dispatched on ``connection.vendor``:

  - **postgresql** — pgvector ``CosineDistance`` with the HNSW index.
    What production runs.
  - **anything else (SQLite)** — brute-force cosine in NumPy over every
    chunk for the institution. What the offline desktop app runs.

The SQLite path exists because the desktop build ships SQLite, and
between the pgvector migration (2026-05-24) and 2026-07-30 this module
returned an *empty result* on SQLite. That silently stripped
``<kb_context>`` from every locally-run tutoring turn — the tutor kept
answering, ungrounded, with no warning. Brute force is not a
compromise at this scale: one institution is order 10^4 chunks x 384
dims, which is a ~15 MB matrix and a single NumPy dot product.

Both backends return identical shapes and identical *cosine distance*
semantics (``1 - cosine_similarity``), so retrieval thresholds tuned on
Postgres carry over unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
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


# ─── ONNX encoder (no torch) ────────────────────────────────────────────

# Where scripts/export_minilm_onnx.py writes its artifacts. Overridable so the
# desktop bundle can point at its own install location.
def _onnx_dir():
    from django.conf import settings
    configured = getattr(settings, 'MINILM_ONNX_DIR', None)
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / 'models' / 'minilm-l6-v2'


_ONNX_SESSION = None
_ONNX_TOKENIZER = None

# all-MiniLM-L6-v2 truncates at 256 word pieces (its max_seq_length), NOT the
# 512 the underlying BERT config advertises. Using 512 would embed text the
# reference encoder never sees and break parity on long chunks.
_ONNX_MAX_TOKENS = 256


def _get_onnx():
    """Lazy-load the ONNX session + tokenizer once per process."""
    global _ONNX_SESSION, _ONNX_TOKENIZER
    if _ONNX_SESSION is not None:
        return _ONNX_SESSION, _ONNX_TOKENIZER

    import onnxruntime
    from tokenizers import Tokenizer

    directory = _onnx_dir()
    model_path = directory / 'model.onnx'
    tokenizer_path = directory / 'tokenizer.json'
    if not model_path.exists() or not tokenizer_path.exists():
        raise FileNotFoundError(
            f"ONNX encoder not found in {directory}. Build it with:\n"
            f"    venv/bin/python scripts/export_minilm_onnx.py"
        )

    # Single-threaded: embedding one short query per tutoring turn is latency-
    # bound on model load, not throughput, and the desktop app is already
    # sharing a CPU with Ollama decoding.
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    _ONNX_SESSION = onnxruntime.InferenceSession(
        str(model_path), options, providers=['CPUExecutionProvider'],
    )
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=_ONNX_MAX_TOKENS)
    tokenizer.enable_padding()
    _ONNX_TOKENIZER = tokenizer
    logger.info("[KB] ONNX all-MiniLM-L6-v2 loaded from %s", directory)
    return _ONNX_SESSION, _ONNX_TOKENIZER


def _embed_onnx(texts: List[str]) -> List[List[float]]:
    """Encode with onnxruntime, reproducing the sentence-transformers pipeline.

    That pipeline is Transformer -> mean pooling over unmasked tokens -> L2
    normalise. Both trailing steps are reimplemented here in NumPy; skipping
    the normalisation would still give correct *rankings* (cosine is
    scale-invariant) but would store vectors that don't compare against the
    existing corpus, so it is done explicitly.
    """
    import numpy as np

    session, tokenizer = _get_onnx()
    encodings = tokenizer.encode_batch(texts)

    input_ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
    token_type_ids = np.asarray([e.type_ids for e in encodings], dtype=np.int64)

    hidden = session.run(
        ['last_hidden_state'],
        {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'token_type_ids': token_type_ids,
        },
    )[0]

    # Mean pool over real tokens only — padding must not drag the mean toward
    # zero, which is what a plain hidden.mean(axis=1) would do.
    mask = attention_mask[..., None].astype(np.float32)
    summed = (hidden * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), 1e-9, None)
    pooled = summed / counts

    pooled /= np.linalg.norm(pooled, axis=1, keepdims=True) + 1e-12
    return pooled.astype(np.float32).tolist()


def embed(texts: List[str]) -> List[List[float]]:
    """Encode a list of texts to 384-d vectors. Returns plain lists.

    Backend is chosen by ``settings.EMBEDDING_BACKEND``: ``'local'``
    (sentence-transformers, the default) or ``'onnx'`` (no torch, what the
    offline desktop build ships). Both produce the same 384-d normalised
    vectors — see apps/curriculum/tests/test_onnx_embedding_parity.py.
    """
    if not texts:
        return []

    from django.conf import settings
    if getattr(settings, 'EMBEDDING_BACKEND', 'local') == 'onnx':
        return _embed_onnx(texts)

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

    # Writes on every backend. This used to skip non-Postgres entirely on the
    # grounds that "vectors require Postgres + pgvector" — true of the *index*,
    # not of storage. VectorField serialises to a text column on SQLite and
    # reads back as a 384-d array, which _search_bruteforce ranks directly.
    # Skipping here is what left an offline install with an unpopulated KB.

    # Compute embeddings in one batch (faster than one-by-one)
    contents = [c.content for c in chunks]
    vectors = embed(contents)

    # Dedupe within this call by content_hash. Postgres ON CONFLICT DO
    # UPDATE cannot affect the same target row twice in a single INSERT
    # statement (CardinalityViolation). Last-write-wins matches what
    # update_conflicts would do across separate INSERTs anyway. Duplicate
    # content is realistic — e.g. a question paper chunk and its marking-
    # scheme chunk can share boilerplate text.
    by_hash = {}
    for c, vec in zip(chunks, vectors):
        content_hash = Chunk.compute_hash(c.content)
        fields = _metadata_to_fields(c.metadata or {})
        by_hash[content_hash] = Chunk(
            institution_id=institution_id,
            content=c.content,
            content_hash=content_hash,
            embedding=vec,
            **fields,
        )
    rows = list(by_hash.values())
    dropped = len(chunks) - len(rows)
    if dropped:
        logger.info(
            "[KB] upsert_chunks: institution_id=%s — collapsed %s duplicate "
            "content_hash row(s) before bulk_create",
            institution_id, dropped,
        )

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

    Dispatches to the pgvector or brute-force backend on
    ``connection.vendor``. Empty result when the query embeds to
    nothing or no chunk matches.
    """
    vec = embed([query_text])
    if not vec:
        return _empty_result()
    query_vec = vec[0]

    if connection.vendor == 'postgresql':
        return _search_pgvector(institution_id, query_vec, n_results, where_filter)
    return _search_bruteforce(institution_id, query_vec, n_results, where_filter)


def _scoped_queryset(institution_id: int, where_filter: Optional[Dict]):
    """CurriculumChunk rows for one institution, with the where_filter applied."""
    from apps.curriculum.models import CurriculumChunk as Chunk

    qs = Chunk.objects.filter(institution_id=institution_id)
    where_q = _where_filter_to_q(where_filter)
    if where_q.children or where_q.connector != 'AND':
        qs = qs.filter(where_q)
    return qs


def _rows_to_result(rows: List[Dict], distances: List[float]) -> Dict[str, List[List]]:
    """Assemble the ChromaDB-shaped dict both backends return."""
    ids: List[str] = []
    docs: List[str] = []
    metas: List[Dict] = []
    for row in rows:
        ids.append(str(row['id']))
        docs.append(row['content'])
        # Rebuild a metadata dict matching the ChromaDB shape
        metas.append({k: row[k] for k in _METADATA_KEYS if row.get(k) not in (None, '')})

    return {
        'ids': [ids],
        'documents': [docs],
        'metadatas': [metas],
        'distances': [[float(d) for d in distances]],
    }


def _search_pgvector(
    institution_id: int,
    query_vec: List[float],
    n_results: int,
    where_filter: Optional[Dict],
) -> Dict[str, List[List]]:
    """Index-backed search. Production path."""
    from pgvector.django import CosineDistance

    qs = _scoped_queryset(institution_id, where_filter) \
        .annotate(_distance=CosineDistance('embedding', query_vec)) \
        .order_by('_distance')[:n_results] \
        .values('id', 'content', '_distance', *_METADATA_KEYS)

    rows = list(qs)
    return _rows_to_result(rows, [r['_distance'] for r in rows])


# Per-institution cache of the normalised embedding matrix, for the
# brute-force backend only.
#
# Measured on this repo's SQLite DB with 10,000 chunks (2026-07-30):
#
#     DB load + deserialize : 1114 ms
#     stack into matrix     :    4 ms
#     normalize + matmul    :    5 ms
#
# The ranking is free; reading 10k pgvector text columns back through
# ``from_db_value`` is the entire cost. Uncached, that is ~1.1 s added to
# every offline tutoring turn. The matrix is ~15 MB per 10k chunks.
#
# Only ever populated on non-Postgres backends. Production uses the HNSW
# index and never reaches this function, so this cannot grow unbounded
# across a multi-tenant server.
_MATRIX_CACHE: Dict[int, Any] = {}
_MATRIX_CACHE_MAX_INSTITUTIONS = 4


def _matrix_fingerprint(institution_id: int):
    """Cheap value that changes whenever the institution's chunks change.

    ``updated_at`` is bumped by the ``update_fields`` list in
    ``upsert_chunks``, so edits move it even when the count holds steady;
    the count catches deletes, which leave ``updated_at`` untouched.
    """
    from django.db.models import Count, Max
    from apps.curriculum.models import CurriculumChunk as Chunk

    agg = Chunk.objects.filter(institution_id=institution_id).aggregate(
        n=Count('id'), latest=Max('updated_at'),
    )
    return (agg['n'], agg['latest'])


def _get_matrix(institution_id: int):
    """Return ``(ids, normalised_matrix)`` for an institution, cached.

    ``ids`` is a NumPy array of chunk PKs aligned row-wise with the matrix.
    Only embeddings are loaded here — content and metadata are fetched for
    the handful of winning rows instead, which keeps the cached payload to
    vectors alone.
    """
    import numpy as np
    from apps.curriculum.models import CurriculumChunk as Chunk

    fingerprint = _matrix_fingerprint(institution_id)
    cached = _MATRIX_CACHE.get(institution_id)
    if cached is not None and cached[0] == fingerprint:
        return cached[1], cached[2]

    # No NULL-embedding guard: CurriculumChunk.embedding is NOT NULL, so a
    # vectorless row cannot exist to poison the matrix.
    rows = list(
        Chunk.objects.filter(institution_id=institution_id)
        .values_list('id', 'embedding')
    )

    if not rows:
        ids = np.empty((0,), dtype=np.int64)
        matrix = np.empty((0, 0), dtype=np.float32)
    else:
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        matrix = np.asarray([r[1] for r in rows], dtype=np.float32)
        # Pre-normalise once at build time, so each query is a bare matmul.
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12

    if len(_MATRIX_CACHE) >= _MATRIX_CACHE_MAX_INSTITUTIONS:
        _MATRIX_CACHE.clear()
    _MATRIX_CACHE[institution_id] = (fingerprint, ids, matrix)
    return ids, matrix


def _search_bruteforce(
    institution_id: int,
    query_vec: List[float],
    n_results: int,
    where_filter: Optional[Dict],
) -> Dict[str, List[List]]:
    """Full-scan cosine search for backends without vector operators.

    Ranks every chunk for the institution with one NumPy matmul against a
    cached, pre-normalised matrix. No index, no extension, no new
    dependency — NumPy already ships with sentence-transformers.

    Distance is ``1 - cosine_similarity``, matching pgvector's
    ``CosineDistance`` exactly so retrieval thresholds are backend-agnostic.
    """
    import numpy as np
    from apps.curriculum.models import CurriculumChunk as Chunk

    ids, matrix = _get_matrix(institution_id)
    if ids.size == 0:
        return _empty_result()

    query = np.asarray(query_vec, dtype=np.float32)
    # Epsilon guards a zero vector, which would otherwise yield NaN and sort
    # unpredictably.
    query /= np.linalg.norm(query) + 1e-12
    distances = 1.0 - (matrix @ query)

    if where_filter:
        # Resolve the filter to PKs in SQL — indexed, and it never touches the
        # embedding column — then mask the cached rows. Keeps one filter
        # implementation (_where_filter_to_q) across both backends.
        allowed = set(
            _scoped_queryset(institution_id, where_filter).values_list('id', flat=True)
        )
        if not allowed:
            return _empty_result()
        mask = np.isin(ids, np.fromiter(allowed, dtype=np.int64, count=len(allowed)))
        if not mask.any():
            return _empty_result()
        distances = np.where(mask, distances, np.inf)
        available = int(mask.sum())
    else:
        available = ids.size

    # argpartition is O(n) where argsort is O(n log n); only the top-k needs
    # ordering. This is the hot path of every offline tutoring turn.
    k = min(n_results, available)
    top = np.argpartition(distances, k - 1)[:k]
    top = top[np.argsort(distances[top])]

    # Fetch content + metadata for the winners only — k rows, not the corpus.
    winning_ids = [int(ids[i]) for i in top]
    by_id = {
        r['id']: r
        for r in Chunk.objects.filter(id__in=winning_ids)
        .values('id', 'content', *_METADATA_KEYS)
    }
    ordered = [by_id[i] for i in winning_ids if i in by_id]
    return _rows_to_result(ordered, [distances[i] for i in top])


def collection_stats(institution_id: int) -> Dict:
    """Return a stats dict in the shape expected by
    ``CurriculumKnowledgeBase.get_collection_stats``."""
    from apps.curriculum.models import CurriculumChunk as Chunk

    # Count on every backend. This previously hardcoded 0 for non-Postgres,
    # which made an offline install with a fully populated KB indistinguishable
    # from an empty one in every diagnostic that reads these stats.
    n = Chunk.objects.filter(institution_id=institution_id).count()
    return {
        'collection_name': f'curriculum_{institution_id}',
        'total_chunks': n,
        'persist_directory': None,  # no longer applicable (DB-backed)
        'backend': 'pgvector' if connection.vendor == 'postgresql' else 'bruteforce',
    }


def clear_institution(institution_id: int) -> int:
    """Delete every chunk for an institution. Returns count deleted."""
    from apps.curriculum.models import CurriculumChunk as Chunk
    n, _ = Chunk.objects.filter(institution_id=institution_id).delete()
    logger.info(f"[KB] clear_institution: institution_id={institution_id} deleted={n}")
    return n
