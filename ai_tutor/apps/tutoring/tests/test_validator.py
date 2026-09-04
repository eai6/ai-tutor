"""Tests for the V1 Socratic validator (apps/tutoring/validator.py).

V1 covers:
  - L1 structural: no_question warning on practice/quiz steps
  - L2 pedagogical: praise stripped on incorrect / bare answers
                    regardless of subject (extends math-only fix)

See memory/socratic_validator_plan.md.
"""

import unittest

from ai_tutor.apps.tutoring.validator import (
    validate_tutor_response,
    ISSUE_NO_QUESTION,
    ISSUE_UNFOUNDED_PRAISE_STRIPPED,
)


class StructuralLayerTest(unittest.TestCase):
    def test_practice_step_without_question_flagged(self):
        result = validate_tutor_response(
            "That's an interesting thought.",
            is_correct=None, bare_answer=False, step_type='practice',
        )
        self.assertIn(ISSUE_NO_QUESTION, result.issues)

    def test_practice_step_with_question_passes(self):
        result = validate_tutor_response(
            "Good thinking. Now tell me what happens when the values are equal?",
            is_correct=None, bare_answer=False, step_type='practice',
        )
        self.assertNotIn(ISSUE_NO_QUESTION, result.issues)

    def test_teach_step_without_question_not_flagged(self):
        # teach steps can legitimately end without a question
        result = validate_tutor_response(
            "Great. Let's continue.",
            is_correct=None, bare_answer=False, step_type='teach',
        )
        self.assertNotIn(ISSUE_NO_QUESTION, result.issues)

    def test_summary_step_without_question_not_flagged(self):
        result = validate_tutor_response(
            "Wonderful work today.",
            is_correct=None, bare_answer=False, step_type='summary',
        )
        self.assertNotIn(ISSUE_NO_QUESTION, result.issues)

class PedagogicalLayerTest(unittest.TestCase):
    """Praise-stripping was disabled 2026-05-06 because the post-process
    rewrite kept injecting stock opener phrases that Sonnet then echoed
    turn-after-turn. Praise-on-bare/wrong is now handled UPSTREAM via
    combined_judge RULE_1 → regen on math turns; non-math praise is
    allowed through. These tests verify the new no-strip behavior."""

    def test_praise_kept_on_wrong_non_math(self):
        result = validate_tutor_response(
            "Brilliant answer! You've got the core idea — "
            "money distribution shapes development.",
            is_correct=False, bare_answer=False, step_type='practice',
        )
        self.assertNotIn(ISSUE_UNFOUNDED_PRAISE_STRIPPED, result.issues)
        self.assertIn("Brilliant", result.content)

    def test_praise_kept_on_bare_answer_even_when_correct(self):
        result = validate_tutor_response(
            "Perfect! That's exactly right. Now let's move on.",
            is_correct=True, bare_answer=True, step_type='practice',
        )
        self.assertNotIn(ISSUE_UNFOUNDED_PRAISE_STRIPPED, result.issues)
        self.assertIn("Perfect", result.content)

    def test_praise_kept_when_correct_and_not_bare(self):
        result = validate_tutor_response(
            "Excellent thinking. The HDI captures three dimensions because...",
            is_correct=True, bare_answer=False, step_type='practice',
        )
        self.assertNotIn(ISSUE_UNFOUNDED_PRAISE_STRIPPED, result.issues)
        self.assertIn("Excellent", result.content)

    def test_no_praise_no_strip(self):
        result = validate_tutor_response(
            "Let's check that idea. Can you give me an example?",
            is_correct=False, bare_answer=False, step_type='practice',
        )
        self.assertNotIn(ISSUE_UNFOUNDED_PRAISE_STRIPPED, result.issues)

    def test_passed_property(self):
        clean = validate_tutor_response(
            "Tell me what you think happens next?",
            is_correct=True, bare_answer=False, step_type='practice',
        )
        self.assertTrue(clean.passed)

        with_no_question = validate_tutor_response(
            "Brilliant! Now let's move on to the next topic without asking anything.",
            is_correct=False, bare_answer=False, step_type='practice',
        )
        self.assertFalse(with_no_question.passed)


class RegenerationTriggerTest(unittest.TestCase):
    """V3: ValidationResult.needs_regeneration flag."""

    def test_passes_no_regen(self):
        from ai_tutor.apps.tutoring.validator import (
            validate_tutor_response, ValidationResult,
        )
        result = validate_tutor_response(
            "Walk me through what you'd try?",
            is_correct=None, bare_answer=False, step_type='practice',
        )
        self.assertFalse(result.needs_regeneration)

    def test_unfounded_praise_does_not_trigger_regen(self):
        from ai_tutor.apps.tutoring.validator import validate_tutor_response
        # Praise on a wrong answer is not a regen trigger. The reply has
        # to carry a question, or `no_question` fires and the assertion
        # passes or fails on the wrong rule — which is what it had been
        # doing since the reply "Brilliant! That's it. Let's move on."
        # stopped being acceptable on its own terms.
        result = validate_tutor_response(
            "Brilliant! That's it. What would you try next?",
            is_correct=False, bare_answer=False, step_type='practice',
        )
        self.assertEqual(result.issues, [])
        self.assertFalse(result.needs_regeneration)

    def test_praise_is_left_in_place(self):
        """`strip_praise_if_wrong` has been a no-op since 2026-05-06 —
        the stock opener phrases it injected became the next thing the
        model echoed turn after turn. So praise survives the validator
        untouched and `unfounded_praise_stripped` is never recorded.
        """
        from ai_tutor.apps.tutoring.validator import (
            validate_tutor_response, ISSUE_UNFOUNDED_PRAISE_STRIPPED,
        )
        text = "Brilliant! That's it. What would you try next?"
        result = validate_tutor_response(
            text, is_correct=False, bare_answer=True, step_type='practice',
        )
        self.assertEqual(result.content, text)
        self.assertNotIn(ISSUE_UNFOUNDED_PRAISE_STRIPPED, result.issues)

    def test_contradicted_claim_triggers_regen(self):
        from ai_tutor.apps.tutoring.validator import (
            ValidationResult, ISSUE_NUMERIC_CLAIM_CONTRADICTED,
        )
        result = ValidationResult(
            content="x", issues=[ISSUE_NUMERIC_CLAIM_CONTRADICTED],
        )
        self.assertTrue(result.needs_regeneration)
        self.assertFalse(result.passed)

    def test_unverified_alone_is_soft(self):
        from ai_tutor.apps.tutoring.validator import (
            ValidationResult, ISSUE_NUMERIC_CLAIM_UNVERIFIED,
        )
        result = ValidationResult(
            content="x", issues=[ISSUE_NUMERIC_CLAIM_UNVERIFIED],
        )
        # soft — does not trigger regen, treated as passed
        self.assertFalse(result.needs_regeneration)
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
