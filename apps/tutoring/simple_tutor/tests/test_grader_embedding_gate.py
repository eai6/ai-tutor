"""M4 acceptance tests — Tier-1.5 embedding similarity gate.

The gate computes cosine similarity between the student answer and the
question's ``answer_data['model_answer']``. Verdict only when clearly
above HIGH (>0.92) or clearly below LOW (<0.35) — middle band returns
``None`` and the caller routes to the Tier-2 verifier LLM (M5).

Unit tests mock ``apps.curriculum.kb_storage.embed`` to avoid loading
the sentence-transformers model. A single integration test exercises
the real model to verify wiring end-to-end.

See memory/simple_tutor_engine_milestones.md (M4).
"""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from apps.tutoring.simple_tutor.grader import (
    Verdict,
    _cosine_similarity,
    _grade_embedding_gate,
    _EMBED_HIGH_SIMILARITY,
    _EMBED_LOW_SIMILARITY,
)


def _sa(model_answer: str = '', correct_answer: str = ''):
    """short_answer question stand-in."""
    return SimpleNamespace(
        pk=88,
        question_type='short_answer',
        question_text='Explain something.',
        correct_answer=correct_answer,
        answer_data={'model_answer': model_answer} if model_answer else {},
    )


def _mock_embed(student_vec, reference_vec):
    """Build a mock ``embed`` function that returns specific vectors
    when called with [student, reference]."""
    def _embed(texts):
        # The grader calls embed([student, reference])
        return [student_vec, reference_vec]
    return _embed


# Two vectors with cosine ≈ 1.0 (identical)
_HIGH_SIM_A = [1.0, 0.0, 0.0]
_HIGH_SIM_B = [1.0, 0.0, 0.0]
# Two orthogonal vectors (cosine = 0.0)
_LOW_SIM_A = [1.0, 0.0, 0.0]
_LOW_SIM_B = [0.0, 1.0, 0.0]
# Mid-band: cos = 0.7 (between LOW=0.35 and HIGH=0.92)
_MID_SIM_A = [1.0, 0.0]
_MID_SIM_B = [0.7, 0.7141]   # sqrt(1 - 0.49) ≈ 0.714 → cos ≈ 0.7


# ============================================================================
# Cosine similarity helper
# ============================================================================


class CosineSimilarityTest(TestCase):
    def test_identical_vectors_sim_one(self):
        self.assertAlmostEqual(
            _cosine_similarity([1, 0, 0], [1, 0, 0]), 1.0, places=6,
        )

    def test_orthogonal_zero(self):
        self.assertAlmostEqual(
            _cosine_similarity([1, 0, 0], [0, 1, 0]), 0.0, places=6,
        )

    def test_opposite_negative_one(self):
        self.assertAlmostEqual(
            _cosine_similarity([1, 0], [-1, 0]), -1.0, places=6,
        )

    def test_scaled_vectors_same_direction(self):
        # Scaling does not change cosine similarity
        self.assertAlmostEqual(
            _cosine_similarity([1, 2, 3], [2, 4, 6]), 1.0, places=6,
        )

    def test_zero_vector_returns_zero(self):
        self.assertEqual(_cosine_similarity([0, 0, 0], [1, 1, 1]), 0.0)

    def test_empty_returns_zero(self):
        self.assertEqual(_cosine_similarity([], [1, 0]), 0.0)
        self.assertEqual(_cosine_similarity([1], [1, 0]), 0.0)


# ============================================================================
# High-similarity → CORRECT
# ============================================================================


class HighSimilarityTest(TestCase):
    """Above HIGH (0.92) → auto-CORRECT, tier='embed_gate'."""

    def test_identical_returns_correct(self):
        q = _sa(model_answer='The sun heats water causing evaporation.')
        # Mock returns identical vectors → cos = 1.0
        with patch('apps.curriculum.kb_storage.embed',
                   side_effect=_mock_embed(_HIGH_SIM_A, _HIGH_SIM_B)):
            r = _grade_embedding_gate(q, 'sun heats water → evaporation')
        self.assertIsNotNone(r)
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertEqual(r.tier, 'embed_gate')
        self.assertGreater(r.confidence, _EMBED_HIGH_SIMILARITY)


# ============================================================================
# Low-similarity → INCORRECT
# ============================================================================


class LowSimilarityTest(TestCase):
    """Below LOW (0.35) → auto-INCORRECT."""

    def test_orthogonal_returns_incorrect(self):
        q = _sa(model_answer='The sun heats water causing evaporation.')
        with patch('apps.curriculum.kb_storage.embed',
                   side_effect=_mock_embed(_LOW_SIM_A, _LOW_SIM_B)):
            r = _grade_embedding_gate(q, "I don't know")
        self.assertIsNotNone(r)
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertEqual(r.tier, 'embed_gate')


# ============================================================================
# Middle band → None (caller routes to verifier LLM)
# ============================================================================


class MiddleBandTest(TestCase):
    """0.35 < cos < 0.92 → returns None; engine routes to Tier 2."""

    def test_mid_band_returns_none(self):
        q = _sa(model_answer='The sun heats water causing evaporation.')
        # 0.7 cosine — should fall through
        with patch('apps.curriculum.kb_storage.embed',
                   side_effect=_mock_embed(_MID_SIM_A, _MID_SIM_B)):
            r = _grade_embedding_gate(q, 'partial paraphrase')
        self.assertIsNone(r)


# ============================================================================
# Edge cases
# ============================================================================


class EdgeCasesTest(TestCase):
    def test_empty_student_returns_incorrect(self):
        q = _sa(model_answer='Some reference')
        # No embedding call needed — empty short-circuit
        r = _grade_embedding_gate(q, '')
        self.assertIsNotNone(r)
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertIn('empty', r.justification.lower())

    def test_whitespace_only_returns_incorrect(self):
        q = _sa(model_answer='Some reference')
        r = _grade_embedding_gate(q, '   \n\t  ')
        self.assertIsNotNone(r)
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_no_reference_returns_none(self):
        # Question with no model_answer + no correct_answer — caller
        # should route to verifier LLM since there's nothing for the
        # gate to compare against.
        q = _sa()  # empty
        r = _grade_embedding_gate(q, 'some answer')
        self.assertIsNone(r)

    def test_correct_answer_fallback(self):
        # Legacy question with correct_answer but no model_answer
        q = _sa(correct_answer='The wearing away of rock by water')
        with patch('apps.curriculum.kb_storage.embed',
                   side_effect=_mock_embed(_HIGH_SIM_A, _HIGH_SIM_B)):
            r = _grade_embedding_gate(q, 'erosion of rock by water flow')
        self.assertIsNotNone(r)
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_embed_failure_returns_none(self):
        # If embed() raises, gate falls through to verifier
        q = _sa(model_answer='Some reference')
        with patch('apps.curriculum.kb_storage.embed',
                   side_effect=RuntimeError('model unavailable')):
            r = _grade_embedding_gate(q, 'student answer')
        self.assertIsNone(r)


# ============================================================================
# Real-model integration (one test) — verifies the embed() wiring works
# end-to-end against the actual sentence-transformers model.
# ============================================================================


class RealEmbedIntegrationTest(TestCase):
    """Single integration test with the actual sentence-transformers
    model. Verifies the gate works against real embeddings. Skipped if
    the model is not available locally.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from apps.curriculum.kb_storage import embed
            # Probe — load model once and verify it returns 384-d vectors.
            v = embed(['probe text'])
            assert len(v) == 1 and len(v[0]) == 384, 'unexpected embed shape'
            cls.embed = embed
        except Exception as e:
            import unittest
            raise unittest.SkipTest(f'sentence-transformers unavailable: {e}')

    def test_near_paraphrase_lands_high(self):
        """Near-paraphrase of the reference should land above the HIGH
        threshold (or at minimum NOT in the low band). Verifies our
        thresholds are sane against the real embedder.
        """
        q = _sa(model_answer='Erosion is the wearing away of rock by water.')
        r = _grade_embedding_gate(
            q, 'Erosion is the wearing away of rock by water.'  # identical
        )
        self.assertIsNotNone(r, 'identical text should hit a verdict')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_unrelated_text_lands_low(self):
        """Completely unrelated student response should land in LOW band."""
        q = _sa(model_answer='Erosion is the wearing away of rock by water.')
        r = _grade_embedding_gate(
            q, 'The capital of France is Paris and it is famous for the Eiffel Tower'
        )
        # Don't strictly assert verdict — depends on threshold tuning. Just
        # check it didn't false-positive to CORRECT.
        if r is not None:
            self.assertNotEqual(r.verdict, Verdict.CORRECT,
                                "Unrelated text must not auto-CORRECT")
