"""Tests for prompt-pack fingerprinting.

Each LLM-using judge exposes PROMPT_HASH / PROMPT_CHARS at module
load. run_all_judges assembles them into CombinedJudgeResult.
prompt_versions; the engine combines that with the tutor system
prompt hash and writes to SessionTurn.metadata['prompt_pack']. The
benchmark snapshot picks it up so annotators see WHICH prompt
revision produced a given response/verdict.

Pins:
  - prompt_fingerprint returns (10-char hex, length) deterministically
  - Identical prompt text yields identical hash; differing yields different
  - Each judge module exposes PROMPT_HASH + PROMPT_CHARS
  - run_all_judges populates prompt_versions on the result
  - to_judge_outputs() and to_metadata() include prompt_versions
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from ai_tutor.apps.tutoring.judges._prompt_meta import prompt_fingerprint
from ai_tutor.apps.tutoring.judges import (
    coherence, factual, rule, safety, step_eval, figure_vision,
)
from ai_tutor.apps.tutoring.combined_judge import CombinedJudgeResult


class PromptFingerprintTest(SimpleTestCase):
    def test_returns_10_char_hex_and_length(self):
        h, n = prompt_fingerprint("hello world")
        self.assertEqual(len(h), 10)
        self.assertEqual(n, 11)
        # Hex chars only
        for c in h:
            self.assertIn(c, '0123456789abcdef')

    def test_deterministic(self):
        h1, _ = prompt_fingerprint("same prompt")
        h2, _ = prompt_fingerprint("same prompt")
        self.assertEqual(h1, h2)

    def test_different_text_different_hash(self):
        h1, _ = prompt_fingerprint("prompt A")
        h2, _ = prompt_fingerprint("prompt B")
        self.assertNotEqual(h1, h2)

    def test_empty_string_handled(self):
        h, n = prompt_fingerprint("")
        self.assertEqual(n, 0)
        self.assertEqual(len(h), 10)  # sha1('') still gives a hash

    def test_none_handled(self):
        h, n = prompt_fingerprint(None)
        self.assertEqual(n, 0)


class JudgeModulesExposePromptHashTest(SimpleTestCase):
    """Every LLM-using judge module must surface PROMPT_HASH and
    PROMPT_CHARS at import time. Without these, run_all_judges can't
    assemble prompt_versions."""

    JUDGE_MODULES = [coherence, factual, rule, step_eval, safety, figure_vision]

    def test_each_module_has_prompt_hash_and_chars(self):
        for mod in self.JUDGE_MODULES:
            with self.subTest(module=mod.__name__):
                self.assertTrue(
                    hasattr(mod, 'PROMPT_HASH'),
                    f"{mod.__name__} missing PROMPT_HASH",
                )
                self.assertTrue(
                    hasattr(mod, 'PROMPT_CHARS'),
                    f"{mod.__name__} missing PROMPT_CHARS",
                )
                self.assertEqual(len(mod.PROMPT_HASH), 10)
                self.assertGreater(mod.PROMPT_CHARS, 0)

    def test_judge_hashes_are_unique(self):
        """Cross-check: no two judges share the same prompt hash. If
        they do, either we have a collision (impossible at 10 chars
        for distinct text) or one judge accidentally points at
        another's _SYSTEM constant."""
        hashes = {mod.__name__: mod.PROMPT_HASH for mod in self.JUDGE_MODULES}
        self.assertEqual(
            len(set(hashes.values())), len(hashes),
            f"Duplicate hash among judges: {hashes}",
        )


class RunAllJudgesPopulatesPromptVersionsTest(SimpleTestCase):
    """End-to-end: run_all_judges should assemble prompt_versions on
    the result, and to_judge_outputs() should surface it."""

    @patch("ai_tutor.apps.tutoring.judges.run_safety_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_figure_vision_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_figure_ref_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_coherence_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_step_eval_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_rule_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_factual_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_arithmetic_judge")
    def test_prompt_versions_assembled(
        self, m_arith, m_fact, m_rule, m_step,
        m_coh, m_figref, m_figvis, m_safety,
    ):
        from ai_tutor.apps.tutoring.judges import run_all_judges
        from ai_tutor.apps.tutoring.judges.arithmetic import ArithmeticResult
        from ai_tutor.apps.tutoring.judges.coherence import CoherenceResult
        from ai_tutor.apps.tutoring.judges.factual import FactualResult
        from ai_tutor.apps.tutoring.judges.figure_ref import FigureRefResult
        from ai_tutor.apps.tutoring.judges.figure_vision import FigureVisionResult
        from ai_tutor.apps.tutoring.judges.rule import RuleResult
        from ai_tutor.apps.tutoring.judges.safety import SafetyResult
        from ai_tutor.apps.tutoring.judges.step_eval import StepEvalResult

        m_arith.return_value = ArithmeticResult()
        m_fact.return_value = FactualResult()
        m_rule.return_value = RuleResult()
        m_step.return_value = StepEvalResult(skipped=True)
        m_coh.return_value = CoherenceResult()
        m_figref.return_value = FigureRefResult()
        m_figvis.return_value = FigureVisionResult(skipped=True)
        m_safety.return_value = SafetyResult()

        result = run_all_judges(
            "Some response.",
            lesson=MagicMock(),
            llm_client=MagicMock(),
        )

        pv = result.prompt_versions
        # All eight slots present, even the deterministic ones
        self.assertEqual(
            set(pv.keys()),
            {'arithmetic', 'coherence', 'factual', 'rule', 'step_eval',
             'safety', 'figure_ref', 'figure_vision'},
        )
        # Deterministic judges get the sentinel
        self.assertEqual(pv['arithmetic']['hash'], '(deterministic)')
        self.assertEqual(pv['figure_ref']['hash'], '(deterministic)')
        # LLM judges carry the real hash matching the module constant
        self.assertEqual(pv['rule']['hash'], rule.PROMPT_HASH)
        self.assertEqual(pv['rule']['chars'], rule.PROMPT_CHARS)
        self.assertEqual(pv['safety']['hash'], safety.PROMPT_HASH)

    def test_to_judge_outputs_includes_prompt_versions(self):
        r = CombinedJudgeResult()
        r.prompt_versions = {
            'rule': {'hash': 'abc1234567', 'chars': 1500},
        }
        out = r.to_judge_outputs()
        self.assertIn('prompt_versions', out)
        self.assertEqual(
            out['prompt_versions']['rule']['hash'], 'abc1234567',
        )

    def test_to_metadata_default_empty_prompt_versions(self):
        r = CombinedJudgeResult()
        # Default field; to_judge_outputs should reflect empty dict
        out = r.to_judge_outputs()
        self.assertEqual(out['prompt_versions'], {})
