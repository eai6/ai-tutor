"""Tests for the rule-compliance LLM-as-judge layer (P5).

See memory/tutor_no_authoring_plan.md.

These tests cover:
  - Pure-helper behavior of check_rule_compliance with a mocked LLM
    client (no live model calls, deterministic JSON)
  - Integration into validate_tutor_response — that violations land
    in `issues` and trigger regeneration via _REGEN_ISSUES
"""

from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Unit, Lesson
from apps.tutoring.rule_compliance import (
    RULE_ARITHMETIC,
    RULE_NO_AUTHORING,
    RULE_RULE_1,
    RuleComplianceResult,
    RuleViolation,
    check_rule_compliance,
)
from apps.tutoring.validator import (
    ISSUE_ARITHMETIC_VIOLATION,
    ISSUE_AUTHORING_VIOLATION,
    ISSUE_RULE1_VIOLATION,
    ValidationResult,
    validate_tutor_response,
)


def _mock_llm_client(judge_payload: dict):
    """Build a minimal LLM client whose .generate() returns the given
    JSON object as the judge's verdict."""
    import json
    response = MagicMock()
    response.content = json.dumps(judge_payload)
    client = MagicMock()
    client.generate.return_value = response
    return client


class CheckRuleComplianceTest(TestCase):
    def test_no_violations_returns_clean_result(self):
        client = _mock_llm_client({"violations": []})
        result = check_rule_compliance(
            "Try this question. |||QUESTION:1|||",
            llm_client=client,
            bank_stems=["Find x given a=95, b=70."],
            student_input="hi",
        )
        self.assertFalse(result.has_violations)
        self.assertEqual(result.violated_rules, [])
        self.assertFalse(result.skipped)

    def test_no_authoring_violation_propagates(self):
        client = _mock_llm_client({
            "violations": [{
                "rule": "NO_AUTHORING",
                "evidence": "if one angle is 73° what is adjacent?",
                "suggested_fix": "Use |||QUESTION:N||| from the bank",
            }],
        })
        result = check_rule_compliance(
            "Sure! If one angle is 73° what is adjacent?",
            llm_client=client,
            bank_stems=["different stem"],
            student_input="ok",
        )
        self.assertTrue(result.has_violations)
        self.assertIn(RULE_NO_AUTHORING, result.violated_rules)
        meta = result.to_metadata()
        self.assertEqual(meta["rule_violations"][0]["rule"], "NO_AUTHORING")

    def test_arithmetic_violation_propagates(self):
        client = _mock_llm_client({
            "violations": [{
                "rule": "ARITHMETIC",
                "evidence": "they sum to 180°",
                "suggested_fix": "Use 190 not 180",
            }],
        })
        result = check_rule_compliance(
            "65° and 125° sum to 180°.",
            llm_client=client,
            bank_stems=[],
            student_input="125",
        )
        self.assertIn(RULE_ARITHMETIC, result.violated_rules)

    def test_rule1_violation_propagates(self):
        client = _mock_llm_client({
            "violations": [{
                "rule": "RULE_1",
                "evidence": "you've nailed it",
                "suggested_fix": "Ask for working before praising",
            }],
        })
        result = check_rule_compliance(
            "You've nailed the rule! Now next question…",
            llm_client=client,
            bank_stems=[],
            student_input="135",
            answer_was_bare=True,
        )
        self.assertIn(RULE_RULE_1, result.violated_rules)

    def test_skipped_when_no_llm_client(self):
        result = check_rule_compliance(
            "anything",
            llm_client=None,
            bank_stems=[],
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_llm_client")

    def test_skipped_when_response_is_empty(self):
        client = _mock_llm_client({"violations": []})
        result = check_rule_compliance("", llm_client=client)
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "empty_response")

    def test_unknown_rule_in_judge_output_is_dropped(self):
        client = _mock_llm_client({
            "violations": [
                {"rule": "UNKNOWN_RULE", "evidence": "x", "suggested_fix": ""},
                {"rule": "ARITHMETIC", "evidence": "1+1=3", "suggested_fix": ""},
            ],
        })
        result = check_rule_compliance("Find x given 2 + 3.", llm_client=client)
        self.assertEqual(result.violated_rules, [RULE_ARITHMETIC])

    def test_judge_returning_garbage_is_skipped_not_crashed(self):
        bad = MagicMock()
        bad.content = "not json at all"
        client = MagicMock()
        client.generate.return_value = bad
        result = check_rule_compliance(
            "Find x in 2 + 3.", llm_client=client, bank_stems=[],
        )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("judge_error"))

    def test_pre_filter_skips_purely_conversational_response(self):
        """Pre-filter short-circuits the LLM call when the response
        contains no digits, no question marks, and no praise vocabulary
        — saves Haiku budget on conversational turns."""
        client = _mock_llm_client({"violations": []})
        result = check_rule_compliance(
            "Let's keep going.",  # no digit, no '?', no praise stem
            llm_client=client,
            bank_stems=[],
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_relevant_content")
        client.generate.assert_not_called()

    def test_pre_filter_passes_response_with_digits(self):
        client = _mock_llm_client({"violations": []})
        result = check_rule_compliance(
            "We have 95 + 70 = 165.",
            llm_client=client,
            bank_stems=[],
        )
        self.assertFalse(result.skipped)
        client.generate.assert_called_once()

    def test_pre_filter_passes_response_with_praise_stem(self):
        client = _mock_llm_client({"violations": []})
        result = check_rule_compliance(
            "Exactly correct!",  # "exact" stem
            llm_client=client,
            bank_stems=[],
        )
        self.assertFalse(result.skipped)
        client.generate.assert_called_once()

    def test_evidence_truncated_to_200_chars(self):
        long_evidence = "x" * 500
        client = _mock_llm_client({
            "violations": [{
                "rule": "ARITHMETIC",
                "evidence": long_evidence,
                "suggested_fix": "y" * 500,
            }],
        })
        result = check_rule_compliance("Find x given 2 + 3.", llm_client=client)
        self.assertEqual(len(result.violations[0].evidence), 200)
        self.assertEqual(len(result.violations[0].suggested_fix), 300)


class ValidatorIntegrationTest(TestCase):
    """Verify the rule-compliance layer plugs into validate_tutor_response
    and routes violations through the existing regeneration channel."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="V", slug="v")
        cls.math_course = Course.objects.create(
            institution=cls.institution, title="Math S3",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.non_math_course = Course.objects.create(
            institution=cls.institution, title="Geo S3",
            grade_level="S3", is_published=True, subject_type='humanities',
        )
        cls.unit = Unit.objects.create(course=cls.math_course, title="U", order_index=0)
        cls.geo_unit = Unit.objects.create(course=cls.non_math_course, title="G", order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="x", order_index=0, is_published=True,
        )
        cls.geo_lesson = Lesson.objects.create(
            unit=cls.geo_unit, title="G", objective="x", order_index=0, is_published=True,
        )

    def test_authoring_violation_lands_in_issues(self):
        client = _mock_llm_client({
            "violations": [{
                "rule": "NO_AUTHORING",
                "evidence": "made up question",
                "suggested_fix": "use the bank",
            }],
        })
        result = validate_tutor_response(
            "If one angle is 73° what is the adjacent?",
            is_correct=None,
            bare_answer=False,
            step_type='practice',
            lesson=self.lesson,
            llm_client=client,
            fact_check=False,  # isolate to L5
            bank_stems=["different stem"],
        )
        self.assertIn(ISSUE_AUTHORING_VIOLATION, result.issues)
        self.assertTrue(result.needs_regeneration)

    def test_arithmetic_violation_lands_in_issues(self):
        client = _mock_llm_client({
            "violations": [{
                "rule": "ARITHMETIC",
                "evidence": "65 + 125 = 180",
                "suggested_fix": "190",
            }],
        })
        result = validate_tutor_response(
            "they sum to 180°",
            is_correct=None,
            bare_answer=False,
            step_type='practice',
            lesson=self.lesson,
            llm_client=client,
            fact_check=False,
        )
        self.assertIn(ISSUE_ARITHMETIC_VIOLATION, result.issues)
        self.assertTrue(result.needs_regeneration)

    def test_rule1_violation_lands_in_issues(self):
        # Use a praise synonym L2's regex blocklist DOES NOT catch
        # ("you've got the rule" — covered neither by "got it" nor by
        # "got the rule"). The whole point of L5 is to catch the
        # synonyms L2 misses; if we used a literal-blocklisted phrase,
        # L2 would strip it before L5 ever runs.
        client = _mock_llm_client({
            "violations": [{
                "rule": "RULE_1",
                "evidence": "you've got the rule",
                "suggested_fix": "ask for working",
            }],
        })
        result = validate_tutor_response(
            "You've got the rule down — let's go on!",
            is_correct=None,
            bare_answer=True,
            step_type='practice',
            lesson=self.lesson,
            llm_client=client,
            fact_check=False,
        )
        self.assertIn(ISSUE_RULE1_VIOLATION, result.issues)
        self.assertTrue(result.needs_regeneration)

    def test_no_violation_means_passed(self):
        client = _mock_llm_client({"violations": []})
        result = validate_tutor_response(
            "What rule do you think applies here?",
            is_correct=None,
            bare_answer=False,
            step_type='practice',
            lesson=self.lesson,
            llm_client=client,
            fact_check=False,
        )
        self.assertTrue(result.passed)
        self.assertFalse(result.needs_regeneration)

    def test_rule_check_skipped_for_non_math_course(self):
        # The judge would flag if it ran — assert it does NOT run for
        # non-math, by checking that no rule_violations entries appear
        # in metadata even when client returns violations.
        client = _mock_llm_client({
            "violations": [{
                "rule": "ARITHMETIC",
                "evidence": "x",
                "suggested_fix": "y",
            }],
        })
        result = validate_tutor_response(
            "anything",
            is_correct=None,
            bare_answer=False,
            step_type='practice',
            lesson=self.geo_lesson,  # non-math
            llm_client=client,
            fact_check=False,
        )
        # Layer never ran → no rule_violations key + no rule-issue
        self.assertNotIn(ISSUE_ARITHMETIC_VIOLATION, result.issues)
        self.assertNotIn("rule_check", result.layers_run)

    def test_rule_check_disabled_when_rule_check_false(self):
        client = _mock_llm_client({
            "violations": [{
                "rule": "RULE_1",
                "evidence": "x",
                "suggested_fix": "y",
            }],
        })
        result = validate_tutor_response(
            "anything",
            is_correct=None,
            bare_answer=False,
            step_type='practice',
            lesson=self.lesson,
            llm_client=client,
            fact_check=False,
            rule_check=False,
        )
        self.assertNotIn("rule_check", result.layers_run)
        self.assertNotIn(ISSUE_RULE1_VIOLATION, result.issues)
