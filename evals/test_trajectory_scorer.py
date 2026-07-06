"""Trajectory-scorer tests — the live tool-syntax-leak guard.

`no_label_anywhere: [TOOL_LEAK, ...]` was a silent no-op in the multi-turn
path: `derive_suggested_labels` never produces TOOL_LEAK/BANNED_OPENER/
ASK_WORKING from what simple_tutor persists, so the assertion always passed
vacuously. `no_tool_syntax_in_any_turn` replaces it with a deterministic regex
over every tutor turn — it catches a leaked `record_answer(...)` / `<tool_use>`
/ `<thinking>` even when no judge runs.

Run: venv/bin/python manage.py test evals.test_trajectory_scorer
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from evals.scorers import trajectory


def _sim(*turns):
    """Build a SimResult-shaped stand-in from (role, content) pairs."""
    return SimpleNamespace(
        transcript=[SimpleNamespace(role=r, content=c) for r, c in turns]
    )


class NoToolSyntaxInAnyTurnTest(unittest.TestCase):
    def test_flags_leaked_record_answer_call(self):
        sim = _sim(
            ("tutor", "Opening. What is 360 / 4?"),
            ("student", "90"),
            ("tutor", 'Nice. record_answer(extracted_answer="90") Next: 5 angles?'),
            ("student", "72"),
        )
        results = trajectory.score({"no_tool_syntax_in_any_turn": True}, sim, [])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed, results[0].detail)

    def test_flags_thinking_and_tool_xml(self):
        sim = _sim(("tutor", "<thinking>the answer is 110</thinking> Try again."),
                   ("student", "ok"))
        results = trajectory.score({"no_tool_syntax_in_any_turn": True}, sim, [])
        self.assertFalse(results[0].passed)

    def test_passes_clean_session(self):
        sim = _sim(
            ("tutor", "Great — 110 is right. Now, what is 360 / 4?"),
            ("student", "90"),
            ("tutor", "Exactly. Record your working next time — what's 360 / 5?"),
            ("student", "72"),
        )
        results = trajectory.score({"no_tool_syntax_in_any_turn": True}, sim, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed, results[0].detail)

    def test_prose_mentioning_record_your_answer_is_not_flagged(self):
        # "record your answer" (prose) must NOT trip the function-call regex.
        sim = _sim(("tutor", "Go ahead and record your answer below — what is 360 / 4?"),
                   ("student", "90"))
        results = trajectory.score({"no_tool_syntax_in_any_turn": True}, sim, [])
        self.assertTrue(results[0].passed, results[0].detail)


if __name__ == "__main__":
    unittest.main()
