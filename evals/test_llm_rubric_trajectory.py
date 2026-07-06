"""Multi-turn trajectory-rubric grading tests.

Two contracts for `evals.scorers.llm_rubric` on the whole-session path:

1. `_build_trajectory_prompt` must tell the judge to score each rubric item
   over the turns where it is RELEVANT across the session (with "n/a" when it
   never applies) — not to average a single turn in isolation. This is the fix
   for the per-response-item-judged-across-a-session grading bug.

2. The n/a-exclusion machinery in `_call_and_parse` (shared by `score` and
   `score_trajectory`) must drop an "n/a" item from the mean, so a conditional
   item that never became relevant can neither help nor hurt the session score.

Run: venv/bin/python manage.py test evals.test_llm_rubric_trajectory
"""
from __future__ import annotations

import unittest

from evals.scorers import llm_rubric
from evals.scorers.llm_rubric import RubricItemScore, RubricResult


class _FakeResp:
    def __init__(self, content: str):
        self.content = content
        self.tokens_in = 0
        self.tokens_out = 0


class _FakeClient:
    """Stands in for a BaseLLMClient — returns a canned judge JSON."""
    def __init__(self, content: str):
        self._content = content

    def generate(self, **_kwargs):
        return _FakeResp(self._content)


class TrajectoryPromptTest(unittest.TestCase):
    def test_trajectory_prompt_instructs_session_level_judging(self):
        prompt = llm_rubric._build_trajectory_prompt(
            transcript=[
                {"role": "tutor", "content": "What is 360 / 4?"},
                {"role": "student", "content": "90"},
            ],
            rubric_items=["If the student made a mistake, the tutor located it."],
        )
        low = prompt.lower()
        # Must offer n/a for never-relevant items ...
        self.assertIn("n/a", low)
        # ... and instruct per-relevant-turn judgement, not single-turn scoring.
        self.assertTrue(
            any(kw in low for kw in ("relevant", "whenever", "each turn", "any turn")),
            f"trajectory prompt lacks per-applicable-turn instruction:\n{prompt}",
        )

    def test_na_item_excluded_from_trajectory_mean(self):
        # Real parse path: one item scored 1.0, one "n/a". Mean must be 1.0
        # (n/a excluded), not 0.5, and the scenario passes at threshold 0.65.
        items = [
            "The tutor kept momentum across the session.",
            "If the student made a mistake, the tutor located it.",
        ]
        canned = (
            '{"scores": ['
            '{"item": "The tutor kept momentum across the session.", '
            '"score": 1.0, "reasoning": "always advanced"}, '
            '{"item": "If the student made a mistake, the tutor located it.", '
            '"score": "n/a", "reasoning": "no mistake ever occurred"}'
            ']}'
        )
        result = RubricResult(pass_threshold=0.65)
        out = llm_rubric._call_and_parse(
            _FakeClient(canned), "prompt", items, 4096, 0.0, result,
        )
        self.assertEqual(out.mean_score, 1.0)
        self.assertFalse(out.items[1].applicable)
        self.assertTrue(out.passed)


if __name__ == "__main__":
    unittest.main()
