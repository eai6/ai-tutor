"""Regression test for the contextvars concurrency bug in run_all_judges.

Production logs (2026-05-12) showed 7 of 8 judges raising
``RuntimeError: cannot enter context: <Context object> is already
entered`` on real tutor turns. Root cause: a single
``contextvars.Context`` was shared across all thread-pool
submissions; a Context cannot be entered concurrently by multiple
threads, so whichever judge won the race ran and the rest failed.

Existing unit tests didn't catch it because their mocked LLM
calls returned instantly — submissions effectively serialised, no
concurrent ctx.run. This test injects a small sleep into each
judge so multiple workers are mid-call simultaneously, reproducing
the race condition the production logs surfaced.

Fix: each submission now takes its own ``contextvars.copy_context()``.
"""
import time
from unittest.mock import patch

from django.test import SimpleTestCase

from ai_tutor.apps.tutoring.judges import run_all_judges


class JudgesConcurrencyTest(SimpleTestCase):
    """All 8 judges must successfully complete when run concurrently."""

    @patch("ai_tutor.apps.tutoring.judges.run_safety_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_figure_vision_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_figure_ref_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_coherence_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_step_eval_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_rule_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_factual_judge")
    @patch("ai_tutor.apps.tutoring.judges.run_arithmetic_judge")
    def test_no_context_already_entered_error_with_concurrent_judges(
        self, m_arith, m_fact, m_rule, m_step,
        m_coh, m_figref, m_figvis, m_safety,
    ):
        from ai_tutor.apps.tutoring.judges.arithmetic import ArithmeticResult
        from ai_tutor.apps.tutoring.judges.coherence import CoherenceResult
        from ai_tutor.apps.tutoring.judges.factual import FactualResult
        from ai_tutor.apps.tutoring.judges.figure_ref import FigureRefResult
        from ai_tutor.apps.tutoring.judges.figure_vision import FigureVisionResult
        from ai_tutor.apps.tutoring.judges.rule import RuleResult
        from ai_tutor.apps.tutoring.judges.safety import SafetyResult
        from ai_tutor.apps.tutoring.judges.step_eval import StepEvalResult

        # Each judge sleeps 50ms before returning — long enough for
        # multiple submissions to be in-flight simultaneously, which is
        # exactly the condition that triggered the prod RuntimeError.
        def _sleepy(result_cls):
            def _impl(*args, **kwargs):
                time.sleep(0.05)
                return result_cls()
            return _impl

        m_arith.side_effect = _sleepy(ArithmeticResult)
        m_fact.side_effect = _sleepy(FactualResult)
        m_rule.side_effect = _sleepy(RuleResult)
        m_step.side_effect = _sleepy(lambda: StepEvalResult(skipped=True))
        m_coh.side_effect = _sleepy(CoherenceResult)
        m_figref.side_effect = _sleepy(FigureRefResult)
        m_figvis.side_effect = _sleepy(
            lambda: FigureVisionResult(skipped=True)
        )
        m_safety.side_effect = _sleepy(SafetyResult)

        from unittest.mock import MagicMock
        llm = MagicMock()
        # generate isn't called because each judge is fully mocked; the
        # presence of an llm_client just gets past the no_llm_client gate.

        result = run_all_judges(
            "Some response text.",
            lesson=MagicMock(),
            llm_client=llm,
        )

        # Pre-fix: 7 of 8 judges raised RuntimeError and ended up in
        # sub_skipped. After the fix: zero judges should be skipped.
        runtime_err_judges = [
            name for name, reason in result.sub_skipped.items()
            if "RuntimeError" in reason or "already entered" in reason
        ]
        self.assertEqual(
            runtime_err_judges, [],
            f"No judge should hit the contextvars race. Got: "
            f"{result.sub_skipped}",
        )

        # All 8 mocks should have been called exactly once.
        for name, mock in [
            ("arithmetic", m_arith), ("factual", m_fact),
            ("rule", m_rule), ("step_eval", m_step),
            ("coherence", m_coh), ("figure_ref", m_figref),
            ("figure_vision", m_figvis), ("safety", m_safety),
        ]:
            self.assertEqual(
                mock.call_count, 1,
                f"{name} judge should have run exactly once",
            )
