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

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.llm.client import LLMResponse
from ai_tutor.apps.tutoring.combined_judge import (
    CombinedJudgeResult,
    run_combined_judge,
)
from ai_tutor.apps.tutoring.fact_verifier import ClaimVerdict
from ai_tutor.apps.tutoring.rule_compliance import (
    RULE_NO_AUTHORING,
    RuleViolation,
)
from ai_tutor.apps.tutoring.validator import (
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
        """No digits, no '?', no praise vocab — the judge returns a
        non-skipped result carrying no findings.

        The mock returns "{}": with no JUDGE ModelConfig in the test DB
        the unified judge falls back to the passed client, and an empty
        object is a well-formed verdict in which every dimension is
        absent and therefore defaults. Before this was set, `.generate`
        returned a bare MagicMock, the judge tried to parse it as JSON
        and the test failed on a TypeError rather than on anything it
        meant to assert.
        """
        llm = MagicMock()
        llm.generate.return_value = _llm_response("{}")
        result = run_combined_judge(
            "Take your time.", lesson=self.lesson, llm_client=llm,
        )
        # Top-level result is not skipped (only empty / no_llm_client trip
        # the top-level gate).
        self.assertFalse(result.skipped)
        self.assertEqual(result.arithmetic_corrections, [])
        self.assertEqual(result.fact_claims, [])
        self.assertEqual(result.rule_violations, [])

    def test_unparseable_verdict_fails_soft(self):
        """The judge must never throw into the tutor turn — a response
        that is not JSON is a skip with a reason, not an exception."""
        llm = MagicMock()
        llm.generate.return_value = _llm_response("I'm afraid I can't do that.")
        result = run_combined_judge(
            "Take your time.", lesson=self.lesson, llm_client=llm,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "unified_judge_parse_error")
        self.assertEqual(result.rule_violations, [])


class UnifiedVerdictParsingTest(TestCase):
    """`_parse_unified_json` is the one place a malformed verdict is
    allowed to be malformed. Everything downstream assumes a dict, so
    anything else has to come back as None and become a skip."""

    def _parse(self, value):
        from ai_tutor.apps.tutoring.judges.unified import _parse_unified_json
        return _parse_unified_json(value)

    def test_plain_object(self):
        self.assertEqual(self._parse('{"a": 1}'), {"a": 1})

    def test_object_inside_prose(self):
        self.assertEqual(self._parse('Sure! {"a": 1} hope that helps'), {"a": 1})

    def test_fenced_object(self):
        self.assertEqual(self._parse('```json\n{"a": 1}\n```'), {"a": 1})

    def test_empty_and_none(self):
        self.assertIsNone(self._parse(""))
        self.assertIsNone(self._parse(None))

    def test_not_json(self):
        self.assertIsNone(self._parse("I'm afraid I can't do that."))

    def test_malformed_json(self):
        self.assertIsNone(self._parse('{"a": 1,,,}'))

    def test_a_json_list_is_not_a_verdict(self):
        """Parses cleanly but breaks every `_section` lookup, so it is a
        parse failure here rather than an AttributeError later."""
        self.assertIsNone(self._parse('[1, 2, 3]'))

    def test_a_non_string_is_not_a_verdict(self):
        """A provider that hands back something other than text must not
        throw TypeError through the judge — this is what a MagicMock in a
        test, or an odd client, actually produces."""
        self.assertIsNone(self._parse(MagicMock()))
        self.assertIsNone(self._parse(b'{"a": 1}'))
        self.assertIsNone(self._parse({"a": 1}))


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
