"""Tests for summarise_regen_cycles — the regen audit dump that lets
benchmark annotators see what each regen attempt produced.

Pins the JSON-safe shape and JSON-serialisability so the dict can be
written to SessionTurn.metadata['regen_audit'] without surprises.
"""
import json
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from apps.tutoring.combined_judge import CombinedJudgeResult
from apps.tutoring.regen import (
    RegenCandidate,
    RegenResult,
    summarise_regen_cycles,
)
from apps.tutoring.rule_compliance import RuleViolation


class SummariseRegenCyclesTest(SimpleTestCase):
    def test_empty_result_returns_empty_dict_shape(self):
        result = RegenResult()
        summary = summarise_regen_cycles(result)
        self.assertEqual(summary['picked_model'], '')
        self.assertEqual(summary['cycles_run'], 0)
        self.assertEqual(summary['cycles'], [])
        self.assertFalse(summary['fallback_used'])
        self.assertFalse(summary['clean'])

    def test_none_input_returns_empty_dict(self):
        self.assertEqual(summarise_regen_cycles(None), {})

    def test_single_cycle_single_candidate(self):
        jr = CombinedJudgeResult(corrected_response="x = 5")
        jr.arithmetic_corrections = []
        cand = RegenCandidate(
            model_name="opus-4.7", text="x = 5",
            judge_result=jr, score=0.0, clean=True,
        )
        result = RegenResult(
            text="x = 5", picked_model="opus-4.7", clean=True,
            cycles_run=1, candidates_per_cycle=[[cand]],
            temperatures=[0.20],
        )
        summary = summarise_regen_cycles(result)
        self.assertEqual(summary['cycles_run'], 1)
        self.assertEqual(summary['picked_model'], 'opus-4.7')
        self.assertTrue(summary['clean'])
        self.assertEqual(len(summary['cycles']), 1)
        cyc = summary['cycles'][0]
        self.assertEqual(cyc['cycle'], 1)
        self.assertEqual(cyc['temperature'], 0.20)
        self.assertEqual(len(cyc['candidates']), 1)
        c = cyc['candidates'][0]
        self.assertEqual(c['model'], 'opus-4.7')
        self.assertEqual(c['text_preview'], 'x = 5')
        self.assertTrue(c['clean'])
        self.assertIn('judge_outputs', c)

    def test_text_preview_truncates_at_400_chars(self):
        long_text = "x = " + "9" * 1000  # ~1004 chars
        cand = RegenCandidate(
            model_name="m", text=long_text,
            judge_result=CombinedJudgeResult(),
        )
        result = RegenResult(
            cycles_run=1, candidates_per_cycle=[[cand]],
            temperatures=[0.20],
        )
        summary = summarise_regen_cycles(result, text_preview_chars=400)
        preview = summary['cycles'][0]['candidates'][0]['text_preview']
        self.assertLessEqual(len(preview), 401)  # 400 + '…'
        self.assertTrue(preview.endswith('…'))

    def test_multiple_cycles_with_judge_findings(self):
        # Cycle 1: dirty (rule violation), Cycle 2: clean
        jr1 = CombinedJudgeResult(corrected_response="bad")
        jr1.rule_violations = [
            RuleViolation(rule="RULE_1", evidence="Perfect!", suggested_fix="x"),
        ]
        cand1 = RegenCandidate(
            model_name="opus", text="Perfect! x = 8.",
            judge_result=jr1, score=-5.0, clean=False,
        )
        jr2 = CombinedJudgeResult(corrected_response="good")
        cand2 = RegenCandidate(
            model_name="opus", text="What's your working?",
            judge_result=jr2, score=0.0, clean=True,
        )
        result = RegenResult(
            text="What's your working?", picked_model="opus", clean=True,
            cycles_run=2,
            candidates_per_cycle=[[cand1], [cand2]],
            temperatures=[0.20, 0.15],
        )
        summary = summarise_regen_cycles(result)
        self.assertEqual(len(summary['cycles']), 2)
        # Cycle 1 — dirty, with rule violation in judge_outputs
        c1 = summary['cycles'][0]['candidates'][0]
        self.assertFalse(c1['clean'])
        self.assertEqual(c1['score'], -5.0)
        # rule.violations should carry the RULE_1 entry
        rule_block = c1['judge_outputs'].get('rule', {})
        self.assertEqual(
            [v['rule'] for v in rule_block.get('violations', [])],
            ['RULE_1'],
        )
        # Cycle 2 — clean
        c2 = summary['cycles'][1]['candidates'][0]
        self.assertTrue(c2['clean'])
        self.assertEqual(summary['cycles'][1]['temperature'], 0.15)

    def test_candidate_with_error_keeps_text_preview_empty(self):
        cand = RegenCandidate(
            model_name="broken", text="",
            judge_result=None, score=0.0, clean=False,
            error="llm_error: TimeoutError",
        )
        result = RegenResult(
            cycles_run=1, candidates_per_cycle=[[cand]],
            temperatures=[0.20],
        )
        summary = summarise_regen_cycles(result)
        c = summary['cycles'][0]['candidates'][0]
        self.assertEqual(c['error'], 'llm_error: TimeoutError')
        self.assertEqual(c['text_preview'], '')
        self.assertEqual(c['judge_outputs'], {})

    def test_summary_is_json_serialisable(self):
        """Critical — the summary gets written to a JSONField. If any
        nested object isn't json.dumps-able, persistence breaks."""
        jr = CombinedJudgeResult()
        jr.rule_violations = [
            RuleViolation(rule="RULE_1", evidence="X", suggested_fix="Y"),
        ]
        cand = RegenCandidate(
            model_name="m", text="t", judge_result=jr, score=1.5, clean=False,
        )
        result = RegenResult(
            picked_model="m", clean=False, cycles_run=1,
            candidates_per_cycle=[[cand]], temperatures=[0.20],
        )
        summary = summarise_regen_cycles(result)
        # Round-trip — raises if any field is non-serialisable.
        round_tripped = json.loads(json.dumps(summary))
        self.assertEqual(
            round_tripped['cycles'][0]['candidates'][0]['model'], 'm',
        )

    def test_fallback_used_surfaces(self):
        result = RegenResult(
            text="(stock)", picked_model="stock_fallback",
            clean=False, cycles_run=4, fallback_used=True,
            candidates_per_cycle=[], temperatures=[],
        )
        summary = summarise_regen_cycles(result)
        self.assertTrue(summary['fallback_used'])
        self.assertEqual(summary['cycles_run'], 4)
        self.assertEqual(summary['cycles'], [])
