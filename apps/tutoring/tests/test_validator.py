"""Tests for the V1 Socratic validator (apps/tutoring/validator.py).

V1 covers:
  - L1 structural: no_question warning on practice/quiz steps
  - L2 pedagogical: praise stripped on incorrect / bare answers
                    regardless of subject (extends math-only fix)

See memory/socratic_validator_plan.md.
"""

import unittest

from apps.tutoring.validator import (
    validate_tutor_response,
    ISSUE_NO_QUESTION,
    ISSUE_UNFOUNDED_PRAISE_STRIPPED,
    ISSUE_INFO_DUMP,
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

    def test_info_dump_warning(self):
        # Many named concepts + numbers, no question = info-dump
        text = (
            "MEDC and LEDC are classifications. BRICS is a separate group. "
            "Seychelles ranks 67th out of 189 with HDI 0.796 and GNP $1.59 billion."
        )
        result = validate_tutor_response(
            text, is_correct=None, bare_answer=False, step_type='teach',
        )
        self.assertIn(ISSUE_INFO_DUMP, result.issues)


class PedagogicalLayerTest(unittest.TestCase):
    def test_praise_stripped_on_wrong_non_math(self):
        """The crucial case the user reported: Geography lesson, thin
        student answer, tutor says 'Brilliant!' — must be stripped."""
        result = validate_tutor_response(
            "Brilliant answer! You've got the core idea — "
            "money distribution shapes development.",
            is_correct=False, bare_answer=False, step_type='practice',
        )
        self.assertIn(ISSUE_UNFOUNDED_PRAISE_STRIPPED, result.issues)
        self.assertNotIn("brilliant", result.content.lower())

    def test_praise_stripped_on_bare_answer_even_when_correct(self):
        result = validate_tutor_response(
            "Perfect! That's exactly right. Now let's move on.",
            is_correct=True, bare_answer=True, step_type='practice',
        )
        self.assertIn(ISSUE_UNFOUNDED_PRAISE_STRIPPED, result.issues)

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
        # Only info_dump is "soft" — everything else fails.
        clean = validate_tutor_response(
            "Tell me what you think happens next?",
            is_correct=True, bare_answer=False, step_type='practice',
        )
        self.assertTrue(clean.passed)

        with_dump = validate_tutor_response(
            "Tell me your thoughts on MEDC vs LEDC and BRICS and HDI 0.796 ranking 67/189?",
            is_correct=True, bare_answer=False, step_type='practice',
        )
        # info_dump is the only issue → still passed
        self.assertEqual(set(with_dump.issues) - {ISSUE_INFO_DUMP}, set())
        self.assertTrue(with_dump.passed)

        with_praise_strip = validate_tutor_response(
            "Brilliant! Now let's move on to the next topic without asking anything.",
            is_correct=False, bare_answer=False, step_type='practice',
        )
        self.assertFalse(with_praise_strip.passed)


class RegenerationTriggerTest(unittest.TestCase):
    """V3: ValidationResult.needs_regeneration flag."""

    def test_passes_no_regen(self):
        from apps.tutoring.validator import (
            validate_tutor_response, ValidationResult,
        )
        result = validate_tutor_response(
            "Walk me through what you'd try?",
            is_correct=None, bare_answer=False, step_type='practice',
        )
        self.assertFalse(result.needs_regeneration)

    def test_unfounded_praise_does_not_trigger_regen(self):
        from apps.tutoring.validator import validate_tutor_response
        # Praise is patched inline (stripped); not a regen trigger.
        result = validate_tutor_response(
            "Brilliant! That's it. Let's move on.",
            is_correct=False, bare_answer=False, step_type='practice',
        )
        self.assertFalse(result.needs_regeneration)

    def test_contradicted_claim_triggers_regen(self):
        from apps.tutoring.validator import (
            ValidationResult, ISSUE_NUMERIC_CLAIM_CONTRADICTED,
        )
        result = ValidationResult(
            content="x", issues=[ISSUE_NUMERIC_CLAIM_CONTRADICTED],
        )
        self.assertTrue(result.needs_regeneration)
        self.assertFalse(result.passed)

    def test_unverified_alone_is_soft(self):
        from apps.tutoring.validator import (
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
