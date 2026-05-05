"""Tests for the rule-compliance LLM-as-judge layer (P5).

See memory/tutor_no_authoring_plan.md.

These tests cover:
  - Pure-helper behavior of check_rule_compliance with a mocked LLM
    client (no live model calls, deterministic JSON)
  - Integration into validate_tutor_response — that violations land
    in `issues` and trigger regeneration via _REGEN_ISSUES
  - Prompt-content regression guards — the judge prompt and bank
    block must explicitly call out the fake-scaffolding pattern
    ("if angles measure X°, Y°, Z° — do they sum to T?") so a
    future prompt edit can't silently re-open the gap.
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
    _JUDGE_SYSTEM,
    _has_relevant_content,
    check_rule_compliance,
)
from apps.tutoring.validator import (
    ISSUE_ARITHMETIC_VIOLATION,
    ISSUE_AUTHORING_VIOLATION,
    ISSUE_RULE1_VIOLATION,
    ValidationResult,
    validate_tutor_response,
)


# The exact transcript that was shipping past the judge before the
# 2026-05 tightening. Drives the regression-guard tests below.
FAKE_SCAFFOLDING_TRANSCRIPT = (
    "Right—you've got the first piece! A full spin is 360°. Now let me "
    "show you why this matters when angles meet at a point.\n\n"
    "Here's the key: any angles that meet at a single point always sum "
    "to 360°.\n\n"
    "Let's test this: if three angles around a point measure 100°, "
    "120°, and 80°, do they sum to 360°?"
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


# ============================================================================
# Regression guards for the 2026-05 fake-scaffolding fix
# ============================================================================
#
# The transcript "if three angles around a point measure 100°, 120°, and 80°,
# do they sum to 360°?" slipped past the judge because the prompt told it
# to "be conservative" and explicitly carved out scaffolding questions.
# The tightening (commit 85e7289) calls out the pattern by name. These
# tests guard against silent regressions in:
#   1. The judge SYSTEM prompt content
#   2. The bank-block content rendered into the tutor's prompt
#   3. The pre-filter accepting this kind of response (so the LLM call
#      actually runs)
#   4. End-to-end: when the judge flags it, validation triggers regen.


class JudgePromptContentTest(TestCase):
    """Prompt-content guards. If someone edits _JUDGE_SYSTEM and drops
    the explicit fake-scaffolding callout, this fails — forcing the
    edit through a deliberate test update."""

    def test_judge_prompt_calls_out_fake_scaffolding_by_example(self):
        # The exact pattern the tutor used in the field is named.
        self.assertIn("100°", _JUDGE_SYSTEM)
        self.assertIn("120°", _JUDGE_SYSTEM)
        self.assertIn("80°", _JUDGE_SYSTEM)
        self.assertIn("do they sum to 360°", _JUDGE_SYSTEM)

    def test_judge_prompt_treats_invented_numbers_as_authoring(self):
        # Hypothetical / rhetorical scaffolding is NOT an exception
        # for invented numbers.
        lower = _JUDGE_SYSTEM.lower()
        self.assertIn("invent", lower)
        self.assertIn("hypothetical", lower)
        self.assertIn("violation", lower)

    def test_judge_prompt_no_longer_tells_judge_to_be_conservative(self):
        # Old wording told the judge "Be conservative — only flag CLEAR
        # violations". That language let the model hedge on borderline
        # cases. The new wording flips the bias — false positives are
        # cheap (one regen), false negatives ship a wrong lesson.
        self.assertNotIn("Be conservative", _JUDGE_SYSTEM)
        self.assertIn("FLAG", _JUDGE_SYSTEM)

    def test_judge_prompt_links_implicit_sum_claim_to_arithmetic_rule(self):
        # The "do they sum to T?" framing carries an implicit
        # arithmetic claim — the judge must know to flag both
        # NO_AUTHORING and ARITHMETIC.
        self.assertIn("100+120+80", _JUDGE_SYSTEM.replace(" ", ""))
        self.assertIn("FALSE", _JUDGE_SYSTEM)
        self.assertIn("300", _JUDGE_SYSTEM)


class BankBlockContentTest(TestCase):
    """The <question_bank> block in the tutor system prompt is the
    LLM's primary instruction for not authoring. Guard the wording."""

    def _render_block(self):
        # Build a minimal step + candidates and render. We only inspect
        # the static rule preamble, so duck-typed objects are fine.
        from apps.tutoring.question_bank import render_bank_block
        step = MagicMock()
        step.teacher_script = "If a + b + x = 360 and a = 100, b = 120, find x."
        step.expected_answer = "140"
        candidate = MagicMock()
        candidate.id = 1
        candidate.question_text = "Two angles are 70° and 80°. Find the third."
        candidate.concept_tag = "Angles around a point"
        candidate.question_type = "short_numeric"
        candidate.answer_data = {}
        block, _id_map = render_bank_block(step, [candidate])
        return block

    def test_bank_block_uses_hard_rule_language(self):
        block = self._render_block()
        self.assertIn("HARD RULE", block)
        # Tool-use rewrite (2026-05-04): the block now directs the LLM
        # to call the pose_question tool. Normalise whitespace so the
        # check survives line breaks in the prompt template.
        normalised = " ".join(block.split())
        self.assertIn("MUST call the pose_question tool", normalised)

    def test_bank_block_bans_invented_numbers_with_concrete_example(self):
        block = self._render_block()
        # The block illustrates the NOT-allowed pattern with a concrete
        # numeric example so the LLM doesn't have to abstract the rule.
        self.assertIn("100°", block)
        self.assertIn("120°", block)
        self.assertIn("80°", block)

    def test_bank_block_lists_the_allowed_exceptions(self):
        block = self._render_block()
        # Conceptual scaffolding + rule recital are the only carved-out
        # paths to ask a question without invoking the tool.
        self.assertIn("conceptual scaffolding", block.lower())
        self.assertIn("reciting the lesson rule", block.lower())

    def test_bank_block_directs_to_pose_question_tool(self):
        block = self._render_block()
        self.assertIn("pose_question", block)
        self.assertIn("slot", block.lower())


class PreFilterTest(TestCase):
    """The pre-filter exists to short-circuit the judge on conversational
    turns. The fake-scaffolding transcript MUST pass the pre-filter so
    the judge gets a chance to flag it."""

    def test_fake_scaffolding_transcript_passes_pre_filter(self):
        self.assertTrue(_has_relevant_content(FAKE_SCAFFOLDING_TRANSCRIPT))

    def test_pure_conceptual_question_still_short_circuits(self):
        # No digits, no praise stem, no question-mark trigger ⇒ skip.
        # Keeps the cost-saving path intact for harmless turns.
        self.assertFalse(_has_relevant_content("Let's keep going."))


class FakeScaffoldingIntegrationTest(TestCase):
    """End-to-end: the user's transcript, with a judge that catches it,
    must lead to a regeneration. This is the concrete fix-shape: when
    the LLM judge does its job, the engine retries."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="V2", slug="v2")
        cls.math_course = Course.objects.create(
            institution=cls.institution, title="Math S3 Sum",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.unit = Unit.objects.create(
            course=cls.math_course, title="Angles", order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="Angles around a point",
            objective="Find a missing angle x given that all angles sum to 360°.",
            order_index=0, is_published=True,
        )

    def test_fake_scaffolding_transcript_triggers_regeneration(self):
        # Simulate the (now-tightened) judge correctly flagging both
        # rules on the field transcript.
        client = _mock_llm_client({
            "violations": [
                {
                    "rule": "NO_AUTHORING",
                    "evidence": "if three angles around a point measure 100°, 120°, and 80°",
                    "suggested_fix": "Use |||QUESTION:N||| from the bank.",
                },
                {
                    "rule": "ARITHMETIC",
                    "evidence": "do they sum to 360°? (100+120+80 = 300)",
                    "suggested_fix": "Pick numbers that satisfy the rule.",
                },
            ],
        })
        result = validate_tutor_response(
            FAKE_SCAFFOLDING_TRANSCRIPT,
            is_correct=None,
            bare_answer=False,
            step_type='practice',
            lesson=self.lesson,
            llm_client=client,
            fact_check=False,  # isolate to the rule-check layer
            bank_stems=["Two angles are 70° and 80°. Find the third."],
            student_input="360 degrees",
        )
        self.assertIn(ISSUE_AUTHORING_VIOLATION, result.issues)
        self.assertIn(ISSUE_ARITHMETIC_VIOLATION, result.issues)
        self.assertTrue(
            result.needs_regeneration,
            "fake-scaffolding transcript with both rules flagged must "
            "trigger the regeneration path",
        )
