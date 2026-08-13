"""Unit tests for the regen ensemble orchestrator.

Pins behaviour of `apps/tutoring/regen/`:
  - score function maps judge findings to (score, clean) correctly
  - orchestrator: clean candidate found → returns it, doesn't loop
  - orchestrator: no clean → temperature decay + retry
  - orchestrator: cycles exhausted with non-empty candidates → best-effort
  - orchestrator: cycles exhausted with no usable candidates → stock fallback
  - orchestrator: model failure (one client raises) → other models still picked
  - prompt builder: system + user shape, includes bank + media catalog
"""

import json
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from ai_tutor.apps.llm.client import LLMResponse
from ai_tutor.apps.tutoring.combined_judge import CombinedJudgeResult
from ai_tutor.apps.tutoring.fact_verifier import ClaimVerdict
from ai_tutor.apps.tutoring.regen import (
    STOCK_FALLBACK,
    run_regen_ensemble,
)
from ai_tutor.apps.tutoring.regen.prompt import build_regen_prompt
from ai_tutor.apps.tutoring.regen.score import score_candidate
from ai_tutor.apps.tutoring.rule_compliance import RULE_RULE_1, RuleViolation


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _ok_llm(text: str) -> MagicMock:
    """Return a MagicMock LLM client that produces `text` for generate()."""
    m = MagicMock()
    m.config.model_name = "fake-model"
    m.generate.return_value = LLMResponse(
        content=text, tokens_in=1, tokens_out=1,
        model="fake-model", stop_reason="end_turn",
    )
    return m


def _failing_llm(exc: Exception) -> MagicMock:
    m = MagicMock()
    m.config.model_name = "broken-model"
    m.generate.side_effect = exc
    return m


def _validation(issues, metadata=None):
    v = MagicMock()
    v.issues = list(issues)
    v.metadata = dict(metadata or {})
    return v


# ---------------------------------------------------------------------
# Score function
# ---------------------------------------------------------------------

class ScoreCandidateTest(SimpleTestCase):
    def test_clean_judge_result_scores_zero_clean_true(self):
        jr = CombinedJudgeResult(corrected_response="ok")
        score, clean = score_candidate(jr)
        self.assertEqual(score, 0.0)
        self.assertTrue(clean)

    def test_none_judge_result_is_dirty(self):
        score, clean = score_candidate(None)
        self.assertLess(score, 0)
        self.assertFalse(clean)

    def test_rule_violation_makes_dirty(self):
        jr = CombinedJudgeResult(
            corrected_response="x",
            rule_violations=[RuleViolation(rule=RULE_RULE_1, evidence="e", suggested_fix="f")],
        )
        score, clean = score_candidate(jr)
        self.assertFalse(clean)
        self.assertLess(score, 0)

    def test_arithmetic_correction_makes_dirty(self):
        jr = CombinedJudgeResult(
            corrected_response="x",
            arithmetic_corrections=[{"expression": "2+2=5", "claimed": "5", "correct": "4"}],
        )
        _, clean = score_candidate(jr)
        self.assertFalse(clean)

    def test_unverified_claim_is_soft_clean_stays_true(self):
        """Unverified factual claims dock score but don't flip clean=False."""
        jr = CombinedJudgeResult(
            corrected_response="x",
            fact_claims=[ClaimVerdict("Mahé has 90,000 people", "unverified")],
        )
        score, clean = score_candidate(jr)
        self.assertTrue(clean)
        self.assertLess(score, 0)
        self.assertGreater(score, -1)  # soft penalty only

    def test_contradicted_claim_makes_dirty(self):
        jr = CombinedJudgeResult(
            corrected_response="x",
            fact_claims=[ClaimVerdict("HDI 0.5", "contradicted", "actual 0.785")],
        )
        _, clean = score_candidate(jr)
        self.assertFalse(clean)

    def test_figure_misalignment_makes_dirty(self):
        jr = CombinedJudgeResult(corrected_response="x", figure_aligned=False)
        _, clean = score_candidate(jr)
        self.assertFalse(clean)

    def test_more_violations_score_lower(self):
        jr_one = CombinedJudgeResult(
            corrected_response="x",
            rule_violations=[RuleViolation(rule=RULE_RULE_1, evidence="e", suggested_fix="f")],
        )
        jr_two = CombinedJudgeResult(
            corrected_response="x",
            rule_violations=[
                RuleViolation(rule=RULE_RULE_1, evidence="e", suggested_fix="f"),
                RuleViolation(rule=RULE_RULE_1, evidence="e2", suggested_fix="f"),
            ],
        )
        s1, _ = score_candidate(jr_one)
        s2, _ = score_candidate(jr_two)
        self.assertGreater(s1, s2)


# ---------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------

class BuildRegenPromptTest(SimpleTestCase):
    def test_returns_user_and_system(self):
        user, system = build_regen_prompt(
            previous_response="some bad response",
            issues=["arithmetic_violation"],
            validation_metadata={"arithmetic_corrections": [
                {"expression": "8 × 2.5 = 21", "claimed": "21", "correct": "20"}
            ]},
            bank_stems=["What is x where x + 5 = 12?"],
            media_catalog_text="<media_catalog>\n[1] diagram of angles\n</media_catalog>",
            student_input="i don't know",
        )
        self.assertIn("repair assistant", system)
        self.assertIn("|||MEDIA:N|||", system)
        self.assertIn("ORIGINAL_TUTOR_RESPONSE", user)
        self.assertIn("some bad response", user)
        self.assertIn("ARITHMETIC_VIOLATION", user)
        self.assertIn("8 × 2.5 = 21", user)
        self.assertIn("BANK", user)
        self.assertIn("What is x where x + 5 = 12?", user)
        self.assertIn("MEDIA_CATALOG", user)
        self.assertIn("diagram of angles", user)

    def test_no_media_warns_against_figure_phrases(self):
        user, _ = build_regen_prompt(
            previous_response="x",
            issues=[],
            validation_metadata={},
            bank_stems=[],
            media_catalog_text="",
            student_input="",
        )
        self.assertIn("no figures attached", user)
        self.assertIn("DO NOT use phrases", user)

    def test_verdict_mismatch_clauses_translate_to_imperative(self):
        u_right_was_wrong, _ = build_regen_prompt(
            previous_response="not quite",
            issues=["verdict_mismatch"],
            validation_metadata={"verdict_mismatch_direction": "tutor_said_wrong_was_right"},
            bank_stems=[],
            media_catalog_text="",
            student_input="B",
        )
        self.assertIn("CORRECT", u_right_was_wrong)
        self.assertIn("affirm", u_right_was_wrong.lower())

        u_wrong_was_right, _ = build_regen_prompt(
            previous_response="exactly!",
            issues=["verdict_mismatch"],
            validation_metadata={"verdict_mismatch_direction": "tutor_said_right_was_wrong"},
            bank_stems=[],
            media_catalog_text="",
            student_input="A",
        )
        self.assertIn("INCORRECT", u_wrong_was_right)


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------

class RunRegenEnsembleTest(SimpleTestCase):
    def test_no_clients_returns_previous_unchanged(self):
        """Engine must fail-soft when no regen clients are configured."""
        result = run_regen_ensemble(
            previous_response="bad text",
            validation=_validation(["tutor_incoherent"]),
            lesson=None,
            regen_clients=[],
        )
        self.assertEqual(result.text, "bad text")
        self.assertFalse(result.clean)
        self.assertEqual(result.cycles_run, 0)

    def test_single_clean_candidate_returns_immediately(self):
        """When the first cycle produces a clean candidate, no retry."""
        client = _ok_llm("fixed clean response")
        # Patch the judges to return a clean result for any input.
        clean_result = CombinedJudgeResult(corrected_response="fixed clean response")
        from unittest.mock import patch
        with patch("ai_tutor.apps.tutoring.regen.run_all_judges", return_value=clean_result):
            result = run_regen_ensemble(
                previous_response="bad",
                validation=_validation(["tutor_incoherent"]),
                lesson=None,
                regen_clients=[client],
                max_cycles=3,
            )
        self.assertTrue(result.clean)
        self.assertEqual(result.text, "fixed clean response")
        self.assertEqual(result.cycles_run, 1)
        self.assertFalse(result.fallback_used)
        self.assertEqual(client.generate.call_count, 1)

    def test_dirty_candidate_triggers_retry_with_lower_temperature(self):
        client = _ok_llm("still bad text")
        dirty_result = CombinedJudgeResult(
            corrected_response="still bad text",
            rule_violations=[
                RuleViolation(rule=RULE_RULE_1, evidence="e", suggested_fix="f"),
            ],
        )
        from unittest.mock import patch
        with patch("ai_tutor.apps.tutoring.regen.run_all_judges", return_value=dirty_result):
            result = run_regen_ensemble(
                previous_response="bad",
                validation=_validation(["tutor_incoherent"]),
                lesson=None,
                regen_clients=[client],
                max_cycles=2,
                temperature_start=0.20,
                temperature_decay=0.05,
            )
        # Should have run 2 cycles, none clean → best-effort returned
        self.assertEqual(result.cycles_run, 2)
        self.assertFalse(result.clean)
        self.assertFalse(result.fallback_used)  # we have non-empty text
        self.assertEqual(result.text, "still bad text")
        self.assertEqual(client.generate.call_count, 2)
        # Temperature should have decayed across cycles.
        temps = [
            call.kwargs.get("temperature") for call in client.generate.call_args_list
        ]
        self.assertEqual(len(temps), 2)
        self.assertAlmostEqual(temps[0], 0.20)
        self.assertAlmostEqual(temps[1], 0.15)

    def test_ensemble_picks_best_score_when_multiple_candidates(self):
        """Three clients run concurrently; orchestrator picks the cleanest."""
        c1 = _ok_llm("model 1 output")
        c2 = _ok_llm("model 2 output")
        c3 = _ok_llm("model 3 output")
        results_by_text = {
            "model 1 output": CombinedJudgeResult(
                corrected_response="model 1 output",
                rule_violations=[
                    RuleViolation(rule=RULE_RULE_1, evidence="e", suggested_fix="f"),
                ],
            ),
            "model 2 output": CombinedJudgeResult(corrected_response="model 2 output"),
            "model 3 output": CombinedJudgeResult(corrected_response="model 3 output"),
        }

        def fake_judges(text, **kwargs):
            return results_by_text[text]

        from unittest.mock import patch
        with patch("ai_tutor.apps.tutoring.regen.run_all_judges", side_effect=fake_judges):
            result = run_regen_ensemble(
                previous_response="bad",
                validation=_validation(["tutor_incoherent"]),
                lesson=None,
                regen_clients=[c1, c2, c3],
                max_cycles=1,
            )
        self.assertTrue(result.clean)
        self.assertIn(result.text, {"model 2 output", "model 3 output"})
        self.assertEqual(result.cycles_run, 1)

    def test_one_failing_model_does_not_break_ensemble(self):
        good = _ok_llm("good output")
        broken = _failing_llm(RuntimeError("API down"))
        good_jr = CombinedJudgeResult(corrected_response="good output")
        from unittest.mock import patch
        with patch("ai_tutor.apps.tutoring.regen.run_all_judges", return_value=good_jr):
            result = run_regen_ensemble(
                previous_response="bad",
                validation=_validation(["tutor_incoherent"]),
                lesson=None,
                regen_clients=[broken, good],
                max_cycles=1,
            )
        self.assertTrue(result.clean)
        self.assertEqual(result.text, "good output")

    def test_cycles_exhausted_with_no_usable_candidates_falls_back_to_stock(self):
        broken = _failing_llm(RuntimeError("API down"))
        result = run_regen_ensemble(
            previous_response="bad",
            validation=_validation(["tutor_incoherent"]),
            lesson=None,
            regen_clients=[broken],
            max_cycles=2,
        )
        self.assertEqual(result.text, STOCK_FALLBACK)
        self.assertTrue(result.fallback_used)
        self.assertFalse(result.clean)
        self.assertEqual(result.cycles_run, 2)
