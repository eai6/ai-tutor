"""Tests for the combined post-response judge.

The combined judge is now a thin compatibility shim that delegates to
per-domain concurrent judges (apps/tutoring/judges/*). The legacy
"single-LLM-call merges all three sub-arrays" contract is gone — we
now run up to four focused judges in parallel and merge the typed
results.

This file keeps the tests that still describe public behaviour:
  - top-level skip gates (empty response, no llm_client)
  - validator consumes a CombinedJudgeResult without making its own
    L4/L5 LLM calls (that's the integration that downstream depends on)

The old per-call-count + monolithic-JSON-parse tests were removed when
the architecture changed. Per-domain coverage lives in
apps/tutoring/judges/tests/.
"""

import json
from unittest.mock import MagicMock

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

    def test_pure_conversational_does_not_crash(self):
        """No digits, no '?', no praise vocab — every sub-judge skips on
        its own pre-gate; orchestrator returns a non-skipped result with
        all sub_skipped fields populated."""
        llm = MagicMock()
        result = run_combined_judge(
            "Take your time.", lesson=self.lesson, llm_client=llm,
        )
        # Top-level result is not skipped (only empty / no_llm_client trip
        # the top-level gate). Each sub-judge skips on its own pre-gate.
        self.assertFalse(result.skipped)
        self.assertEqual(result.arithmetic_corrections, [])
        self.assertEqual(result.fact_claims, [])
        self.assertEqual(result.rule_violations, [])


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
