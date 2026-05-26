"""M5 acceptance tests — Tier-2 cross-family verifier LLM.

Critical design rules being verified (memory/grading_system_research.md):
  - Cross-family: tutor=Claude (anthropic) → verifier must NOT be anthropic
  - Context-free: verifier prompt MUST NOT contain conversation history
  - Verdict FIRST in the Pydantic schema (before justification)
  - Temperature 0 + structured output via instructor
  - Self-consistency n=3 only in middle confidence band [0.5, 0.85]

Tests mock ``structured_completion`` so we don't make real LLM calls.
"""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, MagicMock

from apps.tutoring.simple_tutor.grader import (
    Verdict,
    VerifierResponse,
    _grade_verifier_llm,
    _build_verifier_system_prompt,
    _build_verifier_user_prompt,
    _verifier_self_consistency,
)


def _sa(model_answer: str = '', keywords: list | None = None,
        correct_answer: str = '', stem: str = 'Explain something.'):
    """Short-answer question stand-in."""
    ad = {}
    if model_answer:
        ad['model_answer'] = model_answer
    if keywords:
        ad['keywords'] = keywords
    return SimpleNamespace(
        pk=199,
        question_type='short_answer',
        question_text=stem,
        correct_answer=correct_answer,
        answer_data=ad,
    )


def _mock_provider_chain(provider_name='google'):
    """Build a fake provider chain so the verifier dispatch can find one."""
    config = SimpleNamespace(provider=provider_name,
                              model_name='gemini-3.5-flash')
    provider = SimpleNamespace(
        name=provider_name, model_name='gemini-3.5-flash',
        client=MagicMock(), config=config,
    )
    return [provider]


# ============================================================================
# Schema layout — verdict FIRST is load-bearing
# ============================================================================


class VerifierSchemaTest(TestCase):
    """The Pydantic model field ORDER matters — verdict must come before
    justification per the research (anchors decision before rationale).
    """

    def test_field_order(self):
        # Pydantic preserves field declaration order on Python 3.7+.
        field_names = list(VerifierResponse.model_fields.keys())
        self.assertEqual(
            field_names,
            ['verdict', 'per_criterion_scores', 'confidence', 'justification'],
        )

    def test_verdict_field_first(self):
        # Belt-and-braces: explicitly assert verdict is index 0
        self.assertEqual(
            list(VerifierResponse.model_fields.keys())[0],
            'verdict',
            'verdict MUST be the first field — anchors decision before rationalising',
        )

    def test_justification_field_last(self):
        self.assertEqual(
            list(VerifierResponse.model_fields.keys())[-1],
            'justification',
        )

    def test_verdict_constrained_to_three_values(self):
        # Pydantic v2 validates Literal types
        with self.assertRaises(Exception):
            VerifierResponse(
                verdict='maybe', per_criterion_scores={}, confidence=0.9,
                justification='x',
            )


# ============================================================================
# Prompt design — context-free, reference included, no conversation
# ============================================================================


class VerifierPromptTest(TestCase):

    def test_system_prompt_has_role_and_rules(self):
        sys = _build_verifier_system_prompt()
        self.assertIn('exam grader', sys.lower())
        self.assertIn('verdict', sys.lower())

    def test_system_prompt_anti_sycophancy(self):
        sys = _build_verifier_system_prompt()
        # Must instruct the model NOT to defer to student confidence
        self.assertIn("Do NOT defer", sys)

    def test_user_prompt_contains_question(self):
        q = _sa(model_answer='Water vapour rises', stem='How does steam form?')
        prompt = _build_verifier_user_prompt(q, 'water turns to gas')
        self.assertIn('How does steam form?', prompt)

    def test_user_prompt_contains_reference(self):
        q = _sa(model_answer='Water vapour rises')
        prompt = _build_verifier_user_prompt(q, 'water turns to gas')
        self.assertIn('Water vapour rises', prompt)

    def test_user_prompt_contains_student_answer(self):
        q = _sa(model_answer='Water vapour rises')
        prompt = _build_verifier_user_prompt(q, 'water turns to gas')
        self.assertIn('water turns to gas', prompt)

    def test_user_prompt_includes_keywords_if_present(self):
        q = _sa(
            model_answer='Water vapour rises',
            keywords=['evaporation', 'condensation'],
        )
        prompt = _build_verifier_user_prompt(q, 'some answer')
        self.assertIn('evaporation', prompt)
        self.assertIn('condensation', prompt)

    def test_user_prompt_is_context_free(self):
        """The verifier prompt must NEVER contain the tutoring conversation.
        Inheriting tutor sycophancy is the #1 verifier failure mode.
        """
        q = _sa(model_answer='Water vapour rises')
        prompt = _build_verifier_user_prompt(q, 'water turns to gas')
        # Common conversation markers
        self.assertNotIn('Student:', prompt)
        self.assertNotIn('Tutor:', prompt)
        self.assertNotIn('Assistant:', prompt)
        self.assertNotIn('conversation', prompt.lower())
        self.assertNotIn('history', prompt.lower())


# ============================================================================
# Self-consistency
# ============================================================================


class SelfConsistencyTest(TestCase):

    def test_majority_vote(self):
        votes = [
            VerifierResponse(verdict='correct', per_criterion_scores={},
                             confidence=0.7, justification='a'),
            VerifierResponse(verdict='correct', per_criterion_scores={},
                             confidence=0.8, justification='b'),
            VerifierResponse(verdict='incorrect', per_criterion_scores={},
                             confidence=0.6, justification='c'),
        ]
        verdict, conf, just = _verifier_self_consistency(votes)
        self.assertEqual(verdict, 'correct')
        # Mean of the two 'correct' votes: (0.7 + 0.8) / 2 = 0.75
        self.assertAlmostEqual(conf, 0.75, places=2)

    def test_three_way_split_first_wins(self):
        votes = [
            VerifierResponse(verdict='correct', per_criterion_scores={},
                             confidence=0.7, justification='a'),
            VerifierResponse(verdict='partial', per_criterion_scores={},
                             confidence=0.7, justification='b'),
            VerifierResponse(verdict='incorrect', per_criterion_scores={},
                             confidence=0.7, justification='c'),
        ]
        # Counter.most_common with a tie returns insertion-order first
        verdict, _, _ = _verifier_self_consistency(votes)
        self.assertEqual(verdict, 'correct')

    def test_empty_votes(self):
        verdict, conf, just = _verifier_self_consistency([])
        self.assertEqual(verdict, 'partial')
        self.assertEqual(conf, 0.0)


# ============================================================================
# End-to-end with mocked LLM
# ============================================================================


class VerifierDispatchTest(TestCase):
    """End-to-end with mocked structured_completion."""

    def _patch_chain_and_completion(self, response, *, provider='google'):
        """Helper: patch the chain helper + structured_completion together."""
        chain = _mock_provider_chain(provider)
        return (
            patch(
                'apps.curriculum.content_judges._providers.get_judge_provider_chain',
                return_value=chain,
            ),
            patch(
                'apps.tutoring.judges._instructor_helper.get_instructor_from_client',
                return_value=MagicMock(),
            ),
            patch(
                'apps.tutoring.judges._instructor_helper.structured_completion',
                return_value=response,
            ),
        )

    def test_high_confidence_correct(self):
        resp = VerifierResponse(
            verdict='correct', per_criterion_scores={'correctness': 1.0},
            confidence=0.95, justification='captures key idea',
        )
        p1, p2, p3 = self._patch_chain_and_completion(resp)
        q = _sa(model_answer='X')
        with p1, p2, p3:
            r = _grade_verifier_llm(q, 'X paraphrased')
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertEqual(r.tier, 'verifier_llm')
        self.assertAlmostEqual(r.confidence, 0.95, places=2)
        self.assertFalse(r.needs_followup)   # above HIGH

    def test_middle_band_triggers_self_consistency(self):
        # Confidence 0.7 → middle band → 3 calls
        resp = VerifierResponse(
            verdict='partial', per_criterion_scores={},
            confidence=0.7, justification='ambiguous',
        )
        p1, p2, p3 = self._patch_chain_and_completion(resp)
        q = _sa(model_answer='X')
        with p1, p2, p3 as mock_completion:
            r = _grade_verifier_llm(q, 'ambiguous answer')
        # structured_completion called 3 times (initial + 2 consistency votes)
        self.assertEqual(mock_completion.call_count, 3)
        self.assertEqual(r.verdict, Verdict.PARTIAL)
        self.assertTrue(r.needs_followup)

    def test_low_confidence_no_self_consistency(self):
        resp = VerifierResponse(
            verdict='incorrect', per_criterion_scores={},
            confidence=0.3, justification='clearly wrong',
        )
        p1, p2, p3 = self._patch_chain_and_completion(resp)
        q = _sa(model_answer='X')
        with p1, p2, p3 as mock_completion:
            r = _grade_verifier_llm(q, 'random text')
        # Confidence 0.3 < LOW_CONFIDENCE (0.5) — no self-consistency, single call
        self.assertEqual(mock_completion.call_count, 1)
        # needs_followup is True even at low confidence (engine reads as remediation)
        self.assertTrue(r.needs_followup)

    def test_high_confidence_no_self_consistency(self):
        resp = VerifierResponse(
            verdict='correct', per_criterion_scores={},
            confidence=0.95, justification='clearly right',
        )
        p1, p2, p3 = self._patch_chain_and_completion(resp)
        q = _sa(model_answer='X')
        with p1, p2, p3 as mock_completion:
            r = _grade_verifier_llm(q, 'paraphrase of X')
        self.assertEqual(mock_completion.call_count, 1)

    def test_no_provider_returns_partial(self):
        """When no verifier provider is available, return PARTIAL +
        needs_followup so the engine treats as needs-review.
        """
        with patch(
            'apps.curriculum.content_judges._providers.get_judge_provider_chain',
            return_value=[],
        ):
            q = _sa(model_answer='X')
            r = _grade_verifier_llm(q, 'something')
        self.assertEqual(r.verdict, Verdict.PARTIAL)
        self.assertTrue(r.needs_followup)
        self.assertEqual(r.confidence, 0.0)

    def test_completion_exception_returns_partial(self):
        """If the LLM call raises, the verifier returns a defensive
        PARTIAL with the exception surfaced in justification.
        """
        chain = _mock_provider_chain('google')
        with (
            patch(
                'apps.curriculum.content_judges._providers.get_judge_provider_chain',
                return_value=chain,
            ),
            patch(
                'apps.tutoring.judges._instructor_helper.get_instructor_from_client',
                return_value=MagicMock(),
            ),
            patch(
                'apps.tutoring.judges._instructor_helper.structured_completion',
                side_effect=RuntimeError('quota exceeded'),
            ),
        ):
            q = _sa(model_answer='X')
            r = _grade_verifier_llm(q, 'something')
        self.assertEqual(r.verdict, Verdict.PARTIAL)
        self.assertTrue(r.needs_followup)
        self.assertIn('RuntimeError', r.justification)

    def test_cross_family_exclusion(self):
        """The verifier MUST exclude the tutor's provider family.
        Asserts that get_judge_provider_chain receives exclude_provider='anthropic'.
        """
        resp = VerifierResponse(
            verdict='correct', per_criterion_scores={},
            confidence=0.95, justification='ok',
        )
        with (
            patch(
                'apps.curriculum.content_judges._providers.get_judge_provider_chain',
                return_value=_mock_provider_chain('google'),
            ) as mock_chain,
            patch(
                'apps.tutoring.judges._instructor_helper.get_instructor_from_client',
                return_value=MagicMock(),
            ),
            patch(
                'apps.tutoring.judges._instructor_helper.structured_completion',
                return_value=resp,
            ),
        ):
            q = _sa(model_answer='X')
            _grade_verifier_llm(q, 'X', tutor_provider='anthropic')

        # First call: exclude_provider='anthropic'
        first_call = mock_chain.call_args_list[0]
        self.assertEqual(first_call.kwargs.get('exclude_provider'), 'anthropic')


# ============================================================================
# Dispatcher integration — short_answer routes through embedding gate
# (M4) then verifier LLM (M5) for the middle band
# ============================================================================


class DispatcherIntegrationTest(TestCase):

    def test_short_answer_high_sim_skips_verifier(self):
        from apps.tutoring.simple_tutor.grader import grade_answer
        q = _sa(model_answer='Erosion is wearing away of rock')
        # Mock embed to return near-identical vectors (cos ~ 1.0)
        with patch(
            'apps.curriculum.kb_storage.embed',
            return_value=[[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
        ):
            r = grade_answer(question=q, student_answer='Erosion is wearing away of rock')
        # Embedding gate handled it; no verifier call needed.
        self.assertEqual(r.tier, 'embed_gate')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_short_answer_middle_band_invokes_verifier(self):
        from apps.tutoring.simple_tutor.grader import grade_answer
        q = _sa(model_answer='Erosion is wearing away of rock')

        verifier_resp = VerifierResponse(
            verdict='partial', per_criterion_scores={'correctness': 0.5},
            confidence=0.95, justification='captures part of the idea',
        )
        # Cosine = 0.7 (middle band) so gate returns None
        with (
            patch(
                'apps.curriculum.kb_storage.embed',
                return_value=[[1.0, 0.0], [0.7, 0.7141]],
            ),
            patch(
                'apps.curriculum.content_judges._providers.get_judge_provider_chain',
                return_value=_mock_provider_chain('google'),
            ),
            patch(
                'apps.tutoring.judges._instructor_helper.get_instructor_from_client',
                return_value=MagicMock(),
            ),
            patch(
                'apps.tutoring.judges._instructor_helper.structured_completion',
                return_value=verifier_resp,
            ),
        ):
            r = grade_answer(question=q, student_answer='kind of like erosion')
        # Verifier handled it.
        self.assertEqual(r.tier, 'verifier_llm')
        self.assertEqual(r.verdict, Verdict.PARTIAL)
