"""Tests for the SQLite brute-force vector backend in kb_storage.

Context: between the pgvector migration (2026-05-24) and 2026-07-30,
``query_chunks`` returned an empty result on any non-Postgres backend.
The tutor fails soft on empty retrieval, so every SQLite-backed run —
including the offline desktop build this backend exists for — tutored
without ``<kb_context>`` and logged nothing. These tests pin the
behaviour that replaced it.

The encoder is stubbed throughout. What's under test is the ranking
math and the result shape, not all-MiniLM-L6-v2; loading real
sentence-transformers here would make the suite slow and would couple
it to a model download.

Plan: memory/desktop_offline_app_plan.md (Phase 0)
"""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase

from ai_tutor.apps.curriculum import kb_storage
from ai_tutor.apps.curriculum.models import CurriculumChunk

INST = 4242
DIMS = 384


def unit_vector(axis: int) -> list:
    """A 384-d basis vector — orthogonal to every other basis vector,
    so cosine distance between any two distinct ones is exactly 1.0."""
    vec = [0.0] * DIMS
    vec[axis] = 1.0
    return vec


def blend(axis_a: int, axis_b: int, weight: float) -> list:
    """A vector between two axes, for ordering assertions."""
    vec = [0.0] * DIMS
    vec[axis_a] = 1.0 - weight
    vec[axis_b] = weight
    return vec


class BruteForceSearchTest(TestCase):
    """Exercises _search_bruteforce directly via query_chunks."""

    def setUp(self):
        # The embedding matrix cache is module-global and PKs repeat across
        # tests (each test rolls back), so a stale entry could satisfy a
        # fingerprint from a previous test. Start every test cold.
        kb_storage._MATRIX_CACHE.clear()
        # Three chunks on distinct axes. A query along axis 0 must rank
        # 'alpha' first, and the other two at distance 1.0.
        self.alpha = CurriculumChunk.objects.create(
            institution_id=INST, content='alpha content',
            content_hash=CurriculumChunk.compute_hash('alpha content'),
            embedding=unit_vector(0), subject='geography', chunk_type='content',
        )
        self.beta = CurriculumChunk.objects.create(
            institution_id=INST, content='beta content',
            content_hash=CurriculumChunk.compute_hash('beta content'),
            embedding=unit_vector(1), subject='geography', chunk_type='objective',
        )
        self.gamma = CurriculumChunk.objects.create(
            institution_id=INST, content='gamma content',
            content_hash=CurriculumChunk.compute_hash('gamma content'),
            embedding=unit_vector(2), subject='math', chunk_type='content',
        )

    def _query(self, vec, **kwargs):
        with patch.object(kb_storage, 'embed', return_value=[vec]):
            return kb_storage.query_chunks(INST, 'ignored query text', **kwargs)

    def test_returns_chunks_instead_of_empty_on_sqlite(self):
        """The regression this backend exists to fix."""
        self.assertNotEqual(connection.vendor, 'postgresql',
                            'test asserts the non-Postgres path')
        res = self._query(unit_vector(0))
        self.assertTrue(res['documents'][0], 'SQLite search returned nothing')

    def test_ranks_by_cosine_distance(self):
        res = self._query(unit_vector(0))
        self.assertEqual(res['documents'][0][0], 'alpha content')

    def test_distance_matches_pgvector_semantics(self):
        """Distance is 1 - cosine_similarity, so thresholds port across backends."""
        res = self._query(unit_vector(0))
        by_doc = dict(zip(res['documents'][0], res['distances'][0]))
        # Identical direction -> distance 0; orthogonal -> distance 1.
        self.assertAlmostEqual(by_doc['alpha content'], 0.0, places=5)
        self.assertAlmostEqual(by_doc['beta content'], 1.0, places=5)

    def test_magnitude_does_not_affect_ranking(self):
        """Cosine is scale-invariant; a longer query vector ranks identically."""
        long_query = [v * 97.0 for v in unit_vector(0)]
        res = self._query(long_query)
        self.assertEqual(res['documents'][0][0], 'alpha content')
        self.assertAlmostEqual(res['distances'][0][0], 0.0, places=5)

    def test_ordering_is_strict_not_just_top_1(self):
        """A query nearer beta than gamma must order beta above gamma."""
        res = self._query(blend(1, 2, 0.25))  # mostly axis 1
        docs = res['documents'][0]
        self.assertLess(docs.index('beta content'), docs.index('gamma content'))
        self.assertEqual(res['distances'][0], sorted(res['distances'][0]))

    def test_n_results_caps_output(self):
        res = self._query(unit_vector(0), n_results=2)
        self.assertEqual(len(res['documents'][0]), 2)

    def test_n_results_larger_than_corpus(self):
        """k > len(rows) must not raise out of argpartition."""
        res = self._query(unit_vector(0), n_results=50)
        self.assertEqual(len(res['documents'][0]), 3)

    def test_institution_scoping(self):
        """A chunk in another institution is never returned. See CLAUDE.md."""
        CurriculumChunk.objects.create(
            institution_id=INST + 1, content='other school content',
            content_hash=CurriculumChunk.compute_hash('other school content'),
            embedding=unit_vector(0),  # identical direction to self.alpha
        )
        res = self._query(unit_vector(0))
        self.assertNotIn('other school content', res['documents'][0])

    def test_where_filter_applied(self):
        res = self._query(unit_vector(0), where_filter={'subject': 'math'})
        self.assertEqual(res['documents'][0], ['gamma content'])

    def test_where_filter_in_operator(self):
        res = self._query(unit_vector(0),
                          where_filter={'chunk_type': {'$in': ['objective']}})
        self.assertEqual(res['documents'][0], ['beta content'])

    def test_metadata_shape_matches_chromadb(self):
        res = self._query(unit_vector(2))
        meta = res['metadatas'][0][0]
        self.assertEqual(meta['subject'], 'math')
        # Blank metadata keys are dropped, matching the pgvector path.
        self.assertNotIn('source_file', meta)

    def test_ids_are_strings(self):
        res = self._query(unit_vector(0))
        self.assertEqual(res['ids'][0][0], str(self.alpha.pk))

    def test_empty_institution_returns_empty_shape(self):
        res = self._query(unit_vector(0))
        with patch.object(kb_storage, 'embed', return_value=[unit_vector(0)]):
            res = kb_storage.query_chunks(999999, 'q')
        self.assertEqual(res['documents'], [[]])
        self.assertEqual(res['distances'], [[]])

    def test_unembeddable_query_returns_empty(self):
        with patch.object(kb_storage, 'embed', return_value=[]):
            res = kb_storage.query_chunks(INST, '')
        self.assertEqual(res['documents'], [[]])


class UpsertOnSqliteTest(TestCase):
    """upsert_chunks used to no-op on SQLite, leaving the KB unpopulated."""

    class FakeChunk:
        def __init__(self, content, metadata=None):
            self.content = content
            self.metadata = metadata or {}

    def test_upsert_writes_rows_on_sqlite(self):
        chunks = [self.FakeChunk('one', {'subject': 'geography'}),
                  self.FakeChunk('two', {'subject': 'geography'})]
        with patch.object(kb_storage, 'embed',
                          return_value=[unit_vector(0), unit_vector(1)]):
            result = kb_storage.upsert_chunks(INST, chunks)
        self.assertEqual(result['indexed'], 2)
        self.assertEqual(CurriculumChunk.objects.filter(institution_id=INST).count(), 2)

    def test_upsert_is_idempotent(self):
        chunks = [self.FakeChunk('one', {'subject': 'geography'})]
        with patch.object(kb_storage, 'embed', return_value=[unit_vector(0)]):
            kb_storage.upsert_chunks(INST, chunks)
            kb_storage.upsert_chunks(INST, chunks)
        self.assertEqual(CurriculumChunk.objects.filter(institution_id=INST).count(), 1)

    def test_upserted_chunk_is_searchable(self):
        """The round trip that matters for the offline build."""
        with patch.object(kb_storage, 'embed', return_value=[unit_vector(5)]):
            kb_storage.upsert_chunks(INST, [self.FakeChunk('findable')])
        with patch.object(kb_storage, 'embed', return_value=[unit_vector(5)]):
            res = kb_storage.query_chunks(INST, 'q')
        self.assertEqual(res['documents'][0], ['findable'])


class MatrixCacheInvalidationTest(TestCase):
    """The cache turns a ~1.1 s corpus load into a ~7 ms matmul, but a cache
    that misses an update serves stale retrieval — worse than being slow,
    because it is invisible. Each test mutates the corpus and asserts the
    next query reflects it."""

    def setUp(self):
        kb_storage._MATRIX_CACHE.clear()
        self.first = CurriculumChunk.objects.create(
            institution_id=INST, content='original content',
            content_hash=CurriculumChunk.compute_hash('original content'),
            embedding=unit_vector(0),
        )

    def _query(self, vec, **kwargs):
        with patch.object(kb_storage, 'embed', return_value=[vec]):
            return kb_storage.query_chunks(INST, 'q', **kwargs)

    def _warm(self):
        self._query(unit_vector(0))
        self.assertIn(INST, kb_storage._MATRIX_CACHE)

    def test_insert_invalidates(self):
        self._warm()
        CurriculumChunk.objects.create(
            institution_id=INST, content='added later',
            content_hash=CurriculumChunk.compute_hash('added later'),
            embedding=unit_vector(1),
        )
        res = self._query(unit_vector(1))
        self.assertEqual(res['documents'][0][0], 'added later')

    def test_delete_invalidates(self):
        """Deletes leave updated_at untouched, so the count term carries this."""
        extra = CurriculumChunk.objects.create(
            institution_id=INST, content='doomed',
            content_hash=CurriculumChunk.compute_hash('doomed'),
            embedding=unit_vector(1),
        )
        self._warm()
        extra.delete()
        res = self._query(unit_vector(1))
        self.assertNotIn('doomed', res['documents'][0])

    def test_embedding_update_invalidates(self):
        """Count is unchanged; only updated_at moves. Ranking must follow."""
        self._warm()
        self.first.embedding = unit_vector(7)
        self.first.save()
        res = self._query(unit_vector(7))
        self.assertAlmostEqual(res['distances'][0][0], 0.0, places=5)

    def test_cache_is_per_institution(self):
        CurriculumChunk.objects.create(
            institution_id=INST + 1, content='other school',
            content_hash=CurriculumChunk.compute_hash('other school'),
            embedding=unit_vector(0),
        )
        self._warm()
        with patch.object(kb_storage, 'embed', return_value=[unit_vector(0)]):
            other = kb_storage.query_chunks(INST + 1, 'q')
        self.assertEqual(other['documents'][0], ['other school'])
        self.assertNotIn('original content', other['documents'][0])

    def test_cache_is_bounded(self):
        """An unbounded dict would hold every institution's matrix forever."""
        for i in range(kb_storage._MATRIX_CACHE_MAX_INSTITUTIONS + 2):
            inst = 900000 + i
            CurriculumChunk.objects.create(
                institution_id=inst, content=f'c{i}',
                content_hash=CurriculumChunk.compute_hash(f'c{i}'),
                embedding=unit_vector(i),
            )
            with patch.object(kb_storage, 'embed', return_value=[unit_vector(i)]):
                kb_storage.query_chunks(inst, 'q')
        self.assertLessEqual(len(kb_storage._MATRIX_CACHE),
                             kb_storage._MATRIX_CACHE_MAX_INSTITUTIONS)

    def test_vectorless_chunk_cannot_exist(self):
        """Pins why _get_matrix carries no NULL-embedding guard.

        A vectorless row would produce a ragged array and break the matmul.
        The database makes that unrepresentable; if this constraint is ever
        relaxed, _get_matrix needs a filter and this test will say so.
        """
        from django.db.utils import IntegrityError
        with self.assertRaises(IntegrityError):
            CurriculumChunk.objects.create(
                institution_id=INST, content='no vector yet',
                content_hash=CurriculumChunk.compute_hash('no vector yet'),
                embedding=None,
            )


class CollectionStatsTest(TestCase):
    def test_reports_real_count_on_sqlite(self):
        """Previously hardcoded 0, making a populated offline KB look empty."""
        CurriculumChunk.objects.create(
            institution_id=INST, content='c',
            content_hash=CurriculumChunk.compute_hash('c'),
            embedding=unit_vector(0),
        )
        stats = kb_storage.collection_stats(INST)
        self.assertEqual(stats['total_chunks'], 1)
        self.assertEqual(stats['backend'], 'bruteforce')
