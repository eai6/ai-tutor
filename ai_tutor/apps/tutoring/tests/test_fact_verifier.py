"""Tests for the V2 fact verifier (apps/tutoring/fact_verifier.py).

Covers:
  - claim extraction (regex over numbers, percentages, ranks, etc.)
  - skip behavior when no claims / no LLM
  - LLM judge JSON parsing + tri-state status
  - to_metadata shape

Mocks the LLM and the KnowledgeBase to keep tests offline.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.models import Course, Unit, Lesson
from ai_tutor.apps.llm.client import LLMResponse
from ai_tutor.apps.tutoring.fact_verifier import (
    extract_claims,
    verify_response,
    ClaimVerdict,
    FactCheckResult,
)


class ExtractClaimsTest(TestCase):
    def test_no_claims_in_plain_text(self):
        self.assertEqual(extract_claims("Let's discuss the next concept."), [])

    def test_extracts_percentages(self):
        claims = extract_claims("This represents 75% of the total.")
        self.assertIn("75%", " ".join(claims))

    def test_extracts_named_indicators(self):
        claims = extract_claims("Seychelles HDI 0.796 is strong.")
        joined = " ".join(claims).lower()
        self.assertTrue("hdi" in joined or "0.796" in joined)

    def test_extracts_ranking(self):
        claims = extract_claims("Seychelles ranks 67th out of 189.")
        joined = " ".join(claims).lower()
        self.assertIn("67", joined)

    def test_extracts_currency_with_unit(self):
        claims = extract_claims("Total GDP is $1.59 billion.")
        joined = " ".join(claims).lower()
        self.assertTrue("billion" in joined or "1.59" in joined)

    def test_dedupes_case_insensitively(self):
        claims = extract_claims("HDI 0.796 and HDI 0.796 again.")
        # Only one unique claim
        keys = {c.lower() for c in claims}
        self.assertEqual(len(claims), len(keys))

    def test_max_claims_cap(self):
        text = "1% 2% 3% 4% 5% 6% 7% 8%"
        claims = extract_claims(text, max_claims=3)
        self.assertEqual(len(claims), 3)


class VerifyResponseSkipPathsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name='F', slug='f')
        cls.course = Course.objects.create(
            institution=cls.institution, title='Geography', grade_level='S3',
        )
        cls.unit = Unit.objects.create(course=cls.course, title='U', order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title='L', objective='o', order_index=0, is_published=True,
        )

    def test_skips_on_empty_response(self):
        result = verify_response("", lesson=self.lesson, llm_client=MagicMock())
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "empty_response")

    def test_skips_when_no_claims_detected(self):
        result = verify_response(
            "Tell me what you think happens next?",
            lesson=self.lesson, llm_client=MagicMock(),
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_claims_detected")

    def test_marks_unverified_without_llm(self):
        result = verify_response(
            "Seychelles HDI is 0.796 ranked 67th out of 189.",
            lesson=self.lesson, llm_client=None,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "no_llm_client")
        # All claims marked unverified
        self.assertTrue(all(c.status == "unverified" for c in result.claims))


class VerifyResponseLLMJudgeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name='G', slug='g')
        cls.course = Course.objects.create(
            institution=cls.institution, title='Geography', grade_level='S3',
        )
        cls.unit = Unit.objects.create(course=cls.course, title='U', order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title='L', objective='o', order_index=0, is_published=True,
        )

    def _llm(self, payload_json: str):
        m = MagicMock()
        m.generate.return_value = LLMResponse(
            content=payload_json, tokens_in=1, tokens_out=1,
            model='t', stop_reason='end_turn',
        )
        return m

    @patch("ai_tutor.apps.tutoring.fact_verifier._retrieve_evidence")
    def test_supports_contradicts_unverified(self, mock_retrieve):
        mock_retrieve.return_value = "[curriculum] HDI 0.796 ranks 67th."
        text = "Seychelles HDI 0.796 ranks 67th. Population is 200,000."
        judge_resp = (
            '[{"claim":"0.796","status":"supported","evidence":"HDI 0.796"},'
            '{"claim":"67th","status":"supported","evidence":"ranks 67th"},'
            '{"claim":"200,000","status":"contradicted","evidence":"~98,000"}]'
        )
        result = verify_response(text, lesson=self.lesson, llm_client=self._llm(judge_resp))
        self.assertFalse(result.skipped)
        statuses = {c.status for c in result.claims}
        self.assertIn("supported", statuses)
        self.assertIn("contradicted", statuses)
        self.assertTrue(result.has_problems)
        self.assertEqual(len(result.contradicted_claims), 1)

    @patch("ai_tutor.apps.tutoring.fact_verifier._retrieve_evidence")
    def test_handles_invalid_llm_json(self, mock_retrieve):
        mock_retrieve.return_value = ""
        text = "75% growth in 2023."
        result = verify_response(
            text, lesson=self.lesson, llm_client=self._llm("not json {{")
        )
        # Falls back: all claims marked unverified, no crash.
        self.assertTrue(all(c.status == "unverified" for c in result.claims))

    @patch("ai_tutor.apps.tutoring.fact_verifier._retrieve_evidence")
    def test_strips_markdown_fences(self, mock_retrieve):
        mock_retrieve.return_value = ""
        text = "75% growth."
        wrapped = '```json\n[{"claim":"75%","status":"supported","evidence":"x"}]\n```'
        result = verify_response(
            text, lesson=self.lesson, llm_client=self._llm(wrapped)
        )
        self.assertEqual(result.claims[0].status, "supported")


class FactCheckResultMetadataTest(TestCase):
    def test_to_metadata_shape(self):
        result = FactCheckResult(
            claims=[
                ClaimVerdict("a", "supported"),
                ClaimVerdict("b", "unverified"),
                ClaimVerdict("c", "contradicted"),
            ],
        )
        md = result.to_metadata()
        self.assertEqual(md['factual_claims_checked'], 3)
        self.assertEqual(md['factual_claims_unverified'], ["b"])
        self.assertEqual(md['factual_claims_contradicted'], ["c"])
        self.assertFalse(md['fact_check_skipped'])
