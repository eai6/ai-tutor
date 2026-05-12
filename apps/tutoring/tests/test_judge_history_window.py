"""Phase 2.2.5 — judges receive a bounded conversation-history window.

Pins:
  - format_history_window normalises engine-shape and snapshot-shape turns
  - run_all_judges forwards `prior_exchanges` into coherence / factual / rule
  - history_turns_used is recorded on CombinedJudgeResult + .to_metadata()
  - the four non-history-sensitive judges (arithmetic, figure_ref,
    figure_vision, safety) are NOT given prior_exchanges
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.llm.client import LLMResponse
from apps.tutoring.judges import run_all_judges
from apps.tutoring.judges.history import format_history_window


def _llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, tokens_in=1, tokens_out=1,
        model="test", stop_reason="end_turn",
    )


class FormatHistoryWindowTest(SimpleTestCase):
    def test_empty_inputs(self):
        self.assertEqual(format_history_window(None), "")
        self.assertEqual(format_history_window([]), "")
        self.assertEqual(format_history_window([{"role": "user", "content": "x"}], turns=0), "")

    def test_engine_shape_roles(self):
        hist = [
            {"role": "assistant", "content": "Welcome."},
            {"role": "user", "content": "hi"},
        ]
        out = format_history_window(hist, turns=4)
        self.assertIn("TUTOR: Welcome.", out)
        self.assertIn("STUDENT: hi", out)

    def test_snapshot_shape_roles_and_text_field(self):
        hist = [
            {"role": "tutor", "text": "Walk through it."},
            {"role": "student", "text": "okay"},
        ]
        out = format_history_window(hist, turns=4)
        self.assertIn("TUTOR: Walk through it.", out)
        self.assertIn("STUDENT: okay", out)

    def test_window_caps_to_last_n_messages(self):
        hist = [{"role": "tutor", "content": f"t{i}"} for i in range(10)]
        out = format_history_window(hist, turns=3)
        self.assertNotIn("t0", out)
        self.assertNotIn("t6", out)
        self.assertIn("t7", out)
        self.assertIn("t9", out)

    def test_per_turn_truncation(self):
        hist = [{"role": "tutor", "content": "x" * 1000}]
        out = format_history_window(hist, turns=1, per_turn_chars=50)
        self.assertTrue(out.endswith("…"))
        # Each line is "TUTOR: " + cap chars + "…" + ; we just check
        # the cap is binding.
        self.assertLess(len(out), 80)

    def test_skips_blank_text_turns(self):
        hist = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "  "},
            {"role": "user", "content": "hi"},
        ]
        out = format_history_window(hist, turns=3)
        self.assertEqual(out, "STUDENT: hi")


class RunAllJudgesHistoryForwardingTest(SimpleTestCase):
    """Patches each judge to capture the kwargs it received."""

    def _make_llm(self):
        llm = MagicMock()
        # Default empty JSON so each judge parses + returns cleanly.
        llm.generate.return_value = _llm_response('{"violations": []}')
        return llm

    @override_settings(JUDGE_HISTORY_TURNS=4)
    @patch("apps.tutoring.judges.run_factual_judge")
    @patch("apps.tutoring.judges.run_rule_judge")
    @patch("apps.tutoring.judges.run_coherence_judge")
    @patch("apps.tutoring.judges.run_arithmetic_judge")
    @patch("apps.tutoring.judges.run_step_eval_judge")
    @patch("apps.tutoring.judges.run_figure_ref_judge")
    @patch("apps.tutoring.judges.run_figure_vision_judge")
    @patch("apps.tutoring.judges.run_safety_judge")
    def test_history_passed_only_to_history_sensitive_judges(
        self, m_safety, m_figvis, m_figref, m_step, m_arith,
        m_coh, m_rule, m_fact,
    ):
        # Each mocked judge returns a minimal default; we only inspect kwargs.
        from apps.tutoring.judges.coherence import CoherenceResult
        from apps.tutoring.judges.factual import FactualResult
        from apps.tutoring.judges.rule import RuleResult
        from apps.tutoring.judges.step_eval import StepEvalResult
        from apps.tutoring.judges.arithmetic import ArithmeticResult
        from apps.tutoring.judges.figure_ref import FigureRefResult
        from apps.tutoring.judges.figure_vision import FigureVisionResult
        from apps.tutoring.judges.safety import SafetyResult
        m_coh.return_value = CoherenceResult()
        m_fact.return_value = FactualResult()
        m_rule.return_value = RuleResult()
        m_step.return_value = StepEvalResult(skipped=True)
        m_arith.return_value = ArithmeticResult()
        m_figref.return_value = FigureRefResult()
        m_figvis.return_value = FigureVisionResult(skipped=True)
        m_safety.return_value = SafetyResult()

        lesson = MagicMock()
        history = [
            {"role": "assistant", "content": "We were doing angles."},
            {"role": "user", "content": "I think 95."},
        ]
        result = run_all_judges(
            "Walk through how you got 95.",
            lesson=lesson,
            llm_client=self._make_llm(),
            conversation_history=history,
        )

        # History-sensitive judges receive a non-empty prior_exchanges.
        for mocked in (m_coh, m_fact, m_rule):
            self.assertIn("prior_exchanges", mocked.call_args.kwargs)
            self.assertIn("STUDENT: I think 95.",
                          mocked.call_args.kwargs["prior_exchanges"])

        # Non-history judges DO NOT receive prior_exchanges.
        for mocked in (m_arith, m_step, m_figref, m_figvis, m_safety):
            self.assertNotIn("prior_exchanges", mocked.call_args.kwargs)

        # Effective turn count recorded on the merged result.
        self.assertEqual(result.history_turns_used, 4)
        self.assertEqual(result.to_metadata()["judge_history_turns"], 4)

    @override_settings(JUDGE_HISTORY_TURNS=4)
    @patch("apps.tutoring.judges.run_factual_judge")
    @patch("apps.tutoring.judges.run_rule_judge")
    @patch("apps.tutoring.judges.run_coherence_judge")
    @patch("apps.tutoring.judges.run_arithmetic_judge")
    @patch("apps.tutoring.judges.run_step_eval_judge")
    @patch("apps.tutoring.judges.run_figure_ref_judge")
    @patch("apps.tutoring.judges.run_figure_vision_judge")
    @patch("apps.tutoring.judges.run_safety_judge")
    def test_history_empty_sets_turns_used_zero(
        self, m_safety, m_figvis, m_figref, m_step, m_arith,
        m_coh, m_rule, m_fact,
    ):
        from apps.tutoring.judges.coherence import CoherenceResult
        from apps.tutoring.judges.factual import FactualResult
        from apps.tutoring.judges.rule import RuleResult
        from apps.tutoring.judges.step_eval import StepEvalResult
        from apps.tutoring.judges.arithmetic import ArithmeticResult
        from apps.tutoring.judges.figure_ref import FigureRefResult
        from apps.tutoring.judges.figure_vision import FigureVisionResult
        from apps.tutoring.judges.safety import SafetyResult
        m_coh.return_value = CoherenceResult()
        m_fact.return_value = FactualResult()
        m_rule.return_value = RuleResult()
        m_step.return_value = StepEvalResult(skipped=True)
        m_arith.return_value = ArithmeticResult()
        m_figref.return_value = FigureRefResult()
        m_figvis.return_value = FigureVisionResult(skipped=True)
        m_safety.return_value = SafetyResult()

        result = run_all_judges(
            "Some response.",
            lesson=MagicMock(),
            llm_client=self._make_llm(),
            conversation_history=None,
        )
        # Empty history → blank prior_exchanges → effective count zero.
        for mocked in (m_coh, m_fact, m_rule):
            self.assertEqual(
                mocked.call_args.kwargs["prior_exchanges"], "",
            )
        self.assertEqual(result.history_turns_used, 0)
        self.assertEqual(result.to_metadata()["judge_history_turns"], 0)


class JudgeHistoryDefaultWindowTest(SimpleTestCase):
    """The default JUDGE_HISTORY_TURNS was bumped from 4 to 12 on
    2026-05-12 to catch cross-turn coherence violations on longer
    sessions. Pin the default so accidental shrinks get caught."""

    def test_default_is_12_when_setting_omitted(self):
        from django.conf import settings as dj_settings
        self.assertEqual(
            int(getattr(dj_settings, 'JUDGE_HISTORY_TURNS', 0)),
            12,
            "JUDGE_HISTORY_TURNS default must be 12 (post-2026-05-12). "
            "If you intend to shrink it, update this test and the "
            "settings.py comment together.",
        )
