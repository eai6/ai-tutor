"""Tests for the combined post-response judge.

Locks in the merge contract — one LLM call must produce all three
sub-results (arithmetic / factual / rule-compliance) so the tutor turn
drops from 3 separate judge calls to 1.

Covers:
  - skip gates (empty, no_llm_client, no_relevant_content)
  - parse + collation of all three sub-arrays from one JSON response
  - in-place arithmetic correction mirrors the legacy verifier
  - validator consumes combined_result and skips its L4/L5 LLM calls
  - call-count assertion: validator + combined judge together = 1 call
"""

import json
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.llm.client import LLMResponse
from apps.tutoring.combined_judge import (
    CombinedJudgeResult,
    run_combined_judge,
)
from apps.tutoring.fact_verifier import ClaimVerdict
from apps.tutoring.rule_compliance import (
    RULE_ARITHMETIC,
    RULE_NO_AUTHORING,
    RuleViolation,
)
from apps.tutoring.validator import (
    ISSUE_ARITHMETIC_VIOLATION,
    ISSUE_AUTHORING_VIOLATION,
    ISSUE_NUMERIC_CLAIM_CONTRADICTED,
    validate_tutor_response,
)


def _llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, tokens_in=1, tokens_out=1,
        model="test", stop_reason="end_turn",
    )


class CombinedJudgeSkipGatesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T", slug="t")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(
            course=cls.course, title="U", order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="o",
            order_index=0, is_published=True,
        )

    def test_empty_response_skipped(self):
        llm = MagicMock()
        result = run_combined_judge("", lesson=self.lesson, llm_client=llm)
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "empty_response")
        llm.generate.assert_not_called()

    def test_no_llm_client_skipped(self):
        result = run_combined_judge(
            "Some response.", lesson=self.lesson, llm_client=None,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_llm_client")

    def test_no_relevant_content_skipped(self):
        llm = MagicMock()
        # No digits, no '?', no praise vocab — pure conversational.
        result = run_combined_judge(
            "Take your time.", lesson=self.lesson, llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_relevant_content")
        llm.generate.assert_not_called()


class CombinedJudgeParseTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T", slug="t")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(
            course=cls.course, title="U", order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="o",
            order_index=0, is_published=True,
        )

    def test_parses_all_three_sub_arrays_from_one_call(self):
        """Single judge response → arithmetic + rule findings collated."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "arithmetic_corrections": [
                {"expression": "100 + 120 + 80", "claimed": "360", "correct": "300"},
            ],
            "fact_claims": [],
            "rule_violations": [
                {"rule": "NO_AUTHORING",
                 "evidence": "if angles measure 100°, 120°, 80°...",
                 "suggested_fix": "Use |||QUESTION:N|||"},
                {"rule": "ARITHMETIC",
                 "evidence": "do they sum to 360°?",
                 "suggested_fix": "100+120+80 = 300 not 360"},
            ],
        }))
        text = (
            "If three angles measure 100°, 120°, and 80° — do they sum "
            "to 360°? You will find 100 + 120 + 80 = 360 works."
        )
        result = run_combined_judge(
            text, lesson=self.lesson, llm_client=llm,
            bank_stems=["Find x given a=10, b=20"],
            student_input="i don't know", answer_was_bare=False,
            answer_was_wrong=False,
        )
        # ONE LLM call total — that's the whole point.
        self.assertEqual(llm.generate.call_count, 1)
        # Arithmetic
        self.assertFalse(result.skipped)
        self.assertEqual(len(result.arithmetic_corrections), 1)
        self.assertEqual(result.arithmetic_corrections[0]["correct"], "300")
        # Rule violations
        self.assertEqual(set(result.violated_rules),
                         {RULE_NO_AUTHORING, RULE_ARITHMETIC})

    def test_in_place_arithmetic_rewrite(self):
        """Best-effort substitution mirrors the legacy verifier — when
        the claimed token sits inside the expression, replace it."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "arithmetic_corrections": [
                {"expression": "8 × 2.5 = 21", "claimed": "21", "correct": "20"},
            ],
            "fact_claims": [],
            "rule_violations": [],
        }))
        text = "Quick check — 8 × 2.5 = 21, right?"
        result = run_combined_judge(
            text, lesson=self.lesson, llm_client=llm,
        )
        self.assertIn("8 × 2.5 = 20", result.corrected_response)
        self.assertNotIn("8 × 2.5 = 21", result.corrected_response)

    def test_malformed_json_fails_open(self):
        """Bad LLM output ⇒ skipped, response unchanged. Never blocks."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response("not json at all 😅")
        result = run_combined_judge(
            "Find x where x + 5 = 12.", lesson=self.lesson, llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertTrue(result.skip_reason.startswith("judge_error"))
        self.assertEqual(result.arithmetic_corrections, [])
        self.assertEqual(result.rule_violations, [])

    def test_unknown_rule_names_filtered_out(self):
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps({
            "arithmetic_corrections": [],
            "fact_claims": [],
            "rule_violations": [
                {"rule": "UNKNOWN_RULE", "evidence": "x", "suggested_fix": "y"},
                {"rule": "RULE_1", "evidence": "exactly!",
                 "suggested_fix": "ask for working"},
            ],
        }))
        result = run_combined_judge(
            "Exactly! 5 + 3 = 8.", lesson=self.lesson, llm_client=llm,
            answer_was_bare=True,
        )
        self.assertEqual(result.violated_rules, ["RULE_1"])


class ValidatorConsumesCombinedResultTest(TestCase):
    """When `combined_result` is provided, the validator MUST NOT make
    its own L4/L5 LLM calls — that's the whole point of the merge."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="T", slug="t")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Math",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(
            course=cls.course, title="U", order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="o",
            order_index=0, is_published=True,
        )
        LessonStep.objects.create(
            lesson=cls.lesson, phase='practice', step_type='practice',
            order_index=0, teacher_script="q", expected_answer="a",
        )

    def test_validator_skips_legacy_judges_when_combined_result_given(self):
        """Critical — the call-count assertion. With combined_result
        passed in, validator must NOT call llm_client.generate."""
        llm = MagicMock()
        # Construct a combined result with a NO_AUTHORING violation.
        combined = CombinedJudgeResult(
            corrected_response="If angles measure 100°, do they sum to 180°?",
            arithmetic_corrections=[],
            fact_claims=[],
            rule_violations=[
                RuleViolation(rule=RULE_NO_AUTHORING,
                              evidence="if angles measure 100°...",
                              suggested_fix=""),
            ],
        )
        result = validate_tutor_response(
            "If angles measure 100°, do they sum to 180°?",
            is_correct=None, bare_answer=False, step_type='practice',
            lesson=self.lesson, llm_client=llm,
            combined_result=combined,
        )
        # No LLM call should have happened inside the validator.
        llm.generate.assert_not_called()
        # But the violation should propagate as an authoring issue.
        self.assertIn(ISSUE_AUTHORING_VIOLATION, result.issues)
        # Layer trace should reflect the combined path.
        self.assertIn("fact_check_combined", result.layers_run)
        self.assertIn("rule_check_combined", result.layers_run)

    def test_validator_propagates_contradicted_facts_from_combined(self):
        llm = MagicMock()
        combined = CombinedJudgeResult(
            corrected_response="Seychelles HDI was 0.500 in 2020.",
            fact_claims=[
                ClaimVerdict(
                    claim="HDI 0.500",
                    status="contradicted",
                    evidence="actual HDI 0.785",
                ),
            ],
            rule_violations=[],
        )
        result = validate_tutor_response(
            "Seychelles HDI was 0.500 in 2020.",
            is_correct=None, bare_answer=False, step_type='teach',
            lesson=self.lesson, llm_client=llm,
            combined_result=combined,
        )
        self.assertIn(ISSUE_NUMERIC_CLAIM_CONTRADICTED, result.issues)
        llm.generate.assert_not_called()

    def test_validator_propagates_arithmetic_corrections_from_combined(self):
        """The tutor side passes corrections via the existing
        `arithmetic_corrections` kwarg too — that wiring still works."""
        llm = MagicMock()
        combined = CombinedJudgeResult(
            corrected_response="hello",
            arithmetic_corrections=[
                {"expression": "8 × 2.5 = 21", "claimed": "21", "correct": "20"},
            ],
            fact_claims=[],
            rule_violations=[],
        )
        result = validate_tutor_response(
            "hello",
            is_correct=None, bare_answer=False, step_type='practice',
            lesson=self.lesson, llm_client=llm,
            arithmetic_corrections=combined.arithmetic_corrections,
            combined_result=combined,
        )
        self.assertIn(ISSUE_ARITHMETIC_VIOLATION, result.issues)
        llm.generate.assert_not_called()

    def test_legacy_path_still_works_when_combined_result_absent(self):
        """Non-math callers + tests that haven't migrated still get
        the old per-judge path. Verify by passing a stub LLM and
        confirming the legacy fact_check is invoked."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response(json.dumps([
            {"claim": "200,000 people", "status": "unverified", "evidence": ""},
        ]))
        result = validate_tutor_response(
            "Seychelles has 200,000 people.",
            is_correct=None, bare_answer=False, step_type='teach',
            lesson=self.lesson, llm_client=llm,
            combined_result=None,
            fact_check=True,
            rule_check=False,
        )
        # fact_check path SHOULD invoke the LLM at least once.
        self.assertTrue(llm.generate.called)
        self.assertIn("fact_check", result.layers_run)
