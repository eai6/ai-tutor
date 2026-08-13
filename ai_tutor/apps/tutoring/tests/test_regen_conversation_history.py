"""Regen now sees the conversation history the tutor + judges already
have, so it can fix cross-turn coherence violations (e.g. "you
switched the equation from 5x+20=35 to 3x+20=80 without
explanation"). Before this change, regen sent a single user message
with no history and converged to the same dirty candidate cycle
after cycle because it didn't know what the prior turn looked like.
"""
from django.test import SimpleTestCase

from ai_tutor.apps.tutoring.regen.prompt import build_regen_prompt


class RegenPromptIncludesHistoryTest(SimpleTestCase):
    def test_conversation_history_renders_in_user_prompt(self):
        history = [
            {"role": "assistant", "content": "Solve 5x + 20 = 35. What is x?"},
            {"role": "user", "content": "40"},
        ]
        user_prompt, _system = build_regen_prompt(
            previous_response="Solve 3x + 20 = 80 for x.",
            issues=["tutor_incoherent"],
            validation_metadata={
                "coherence_violations": [
                    "Changed equation from 5x+20=35 to 3x+20=80 "
                    "without explanation",
                ],
            },
            bank_stems=[],
            student_input="why are we doing algebra",
            conversation_history=history,
            history_turns=6,
        )
        self.assertIn("CONVERSATION_HISTORY", user_prompt)
        self.assertIn("5x + 20 = 35", user_prompt)
        self.assertIn("STUDENT: 40", user_prompt)
        # The block must appear BEFORE the response-to-fix so the
        # LLM reads context first.
        self.assertLess(
            user_prompt.index("CONVERSATION_HISTORY"),
            user_prompt.index("ORIGINAL_TUTOR_RESPONSE"),
        )

    def test_empty_history_omits_the_block(self):
        user_prompt, _ = build_regen_prompt(
            previous_response="X",
            issues=[],
            validation_metadata={},
            bank_stems=[],
            student_input="hi",
            conversation_history=None,
        )
        self.assertNotIn("CONVERSATION_HISTORY", user_prompt)

    def test_history_window_caps_at_history_turns(self):
        # Build 20 turns; expect only the last 4 (history_turns=4)
        history = [
            {"role": "assistant" if i % 2 else "user", "content": f"turn-{i}"}
            for i in range(20)
        ]
        user_prompt, _ = build_regen_prompt(
            previous_response="X",
            issues=[],
            validation_metadata={},
            bank_stems=[],
            student_input="x",
            conversation_history=history,
            history_turns=4,
        )
        # The last 4 turns: 16, 17, 18, 19
        for keep in ("turn-16", "turn-17", "turn-18", "turn-19"):
            self.assertIn(keep, user_prompt)
        # Earlier turns NOT in the window
        for drop in ("turn-0", "turn-5", "turn-15"):
            self.assertNotIn(drop, user_prompt)


class RepairInstructionsHaveExamplesTest(SimpleTestCase):
    """Each violation's repair instruction must include a concrete
    BEFORE → AFTER example so the rewrite-LLM has a fix pattern."""

    def _build(self, issue: str, **meta):
        user_prompt, _ = build_regen_prompt(
            previous_response="X",
            issues=[issue],
            validation_metadata=meta,
            bank_stems=[],
            student_input="x",
        )
        return user_prompt

    def test_tutor_incoherent_has_example(self):
        out = self._build(
            "tutor_incoherent",
            coherence_violations=["Changed equation mid-stream"],
        )
        self.assertIn("Example:", out)
        self.assertIn("BEFORE", out)
        self.assertIn("AFTER", out)

    def test_verdict_mismatch_wrong_was_right_has_example(self):
        out = self._build(
            "verdict_mismatch",
            verdict_mismatch_direction="tutor_said_wrong_was_right",
        )
        self.assertIn("Example:", out)
        self.assertIn("affirm", out.lower())

    def test_verdict_mismatch_right_was_wrong_has_example(self):
        out = self._build(
            "verdict_mismatch",
            verdict_mismatch_direction="tutor_said_right_was_wrong",
        )
        self.assertIn("Example:", out)

    def test_arithmetic_violation_has_example(self):
        out = self._build(
            "arithmetic_violation",
            arithmetic_corrections=[
                {"expression": "8*5", "claimed": "35", "correct": "40"},
            ],
        )
        self.assertIn("Example:", out)
        # The correction lines still render
        self.assertIn("8*5", out)

    def test_authoring_violation_has_example(self):
        out = self._build("authoring_violation")
        self.assertIn("Example:", out)
        self.assertIn("CONCEPTUAL", out)

    def test_rule1_has_example(self):
        out = self._build("rule1_violation")
        self.assertIn("Example:", out)
        # Discourages defaulting to one phrase
        self.assertIn("walk me through", out.lower())

    def test_figure_ref_without_signal_has_example(self):
        out = self._build(
            "figure_ref_without_signal",
            figure_ref_issues=["Look at the diagram"],
        )
        self.assertIn("Example:", out)
        self.assertIn("|||MEDIA:N|||", out)

    def test_no_question_has_example(self):
        out = self._build("no_question")
        self.assertIn("Example:", out)
        self.assertIn("?", out)

    def test_new_coverage_multi_paragraph(self):
        out = self._build("multi_paragraph")
        self.assertIn("MULTI_PARAGRAPH", out)
        self.assertIn("Example:", out)

    def test_new_coverage_banned_opener(self):
        out = self._build("banned_opener")
        self.assertIn("BANNED_OPENER", out)
        self.assertIn("Example:", out)

    def test_new_coverage_padding_filler(self):
        out = self._build("padding_filler")
        self.assertIn("PADDING_FILLER", out)
        self.assertIn("Example:", out)

    def test_new_coverage_premature_advance(self):
        out = self._build("premature_advance")
        self.assertIn("PREMATURE_ADVANCE", out)
        self.assertIn("Example:", out)

    def test_new_coverage_claim_unverified(self):
        out = self._build(
            "numeric_claim_unverified",
            factual_claims_unverified=["$237M of tuna"],
        )
        self.assertIn("NUMERIC_CLAIM_UNVERIFIED", out)
        self.assertIn("Example:", out)


class RegenEnsemblePassesHistoryThroughTest(SimpleTestCase):
    """End-to-end: run_regen_ensemble threads conversation_history into
    build_regen_prompt. Validate by intercepting the helper."""

    def test_ensemble_forwards_history(self):
        from unittest.mock import MagicMock, patch
        from ai_tutor.apps.tutoring.regen import run_regen_ensemble

        validation = MagicMock()
        validation.issues = ["no_question"]
        validation.metadata = {}

        regen_client = MagicMock()
        regen_client.config.model_name = "fake-model"
        regen_client.config.purpose = "regen"
        # Return an empty-ish response so the cycle completes quickly
        regen_client.generate.return_value = MagicMock(content="OK")

        history = [
            {"role": "assistant", "content": "First question?"},
            {"role": "user", "content": "yes"},
        ]

        captured = {}

        original = build_regen_prompt

        def _spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        with patch(
            'ai_tutor.apps.tutoring.regen.build_regen_prompt', side_effect=_spy,
        ):
            run_regen_ensemble(
                previous_response="dirty response",
                validation=validation,
                lesson=MagicMock(),
                regen_clients=[regen_client],
                judge_client=None,
                vision_client=None,
                image_reader=None,
                attached_media=None,
                bank_stems=[],
                student_input="x",
                conversation_history=history,
                max_cycles=1,
            )

        self.assertEqual(captured.get("conversation_history"), history)
