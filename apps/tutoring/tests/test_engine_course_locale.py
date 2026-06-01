"""Tests for engine-level course-locale awareness.

Part of M4 of memory/portuguese_mozambique_pilot_plan.md. Two
deliverables under test:

  1. The system-prompt builder injects the ``<locale>`` block only
     when the course is non-English.
  2. The intent classifier picks the right pattern bundle for the
     active locale.

These tests exercise the prompts/intent layer directly — they do
NOT make LLM calls, so they're safe to run in CI without API keys.
"""
from __future__ import annotations

from django.test import TestCase

from apps.tutoring.simple_tutor.intent import classify_student_message
from apps.tutoring.simple_tutor.prompts import _build_locale_rule


class LocaleRuleBlockTest(TestCase):
    """The system-prompt ``<locale>`` block is empty for en-us so the
    Seychelles cache key stays intact; populated for pt-mz with
    Mozambique-register instructions."""

    def test_en_us_returns_empty(self):
        self.assertEqual(_build_locale_rule('en-us'), '')

    def test_pt_mz_contains_mozambique_instruction(self):
        rule = _build_locale_rule('pt-mz')
        self.assertIn('<locale>', rule)
        self.assertIn('</locale>', rule)
        self.assertIn('Mozambique Portuguese', rule)
        self.assertIn('pt-mz', rule)
        # Register: tu informal addressing.
        self.assertIn("'tu'", rule)
        # Spelling agreement.
        self.assertIn('Acordo Ortográfico', rule)

    def test_unknown_locale_falls_back_to_generic(self):
        """An unknown locale produces a generic instruction in its own
        code rather than erroring."""
        rule = _build_locale_rule('xx-yy')
        self.assertIn('<locale>', rule)
        self.assertIn('xx-yy', rule)


class IntentClassifierLocaleTest(TestCase):
    """The intent classifier picks the right pattern bundle by locale."""

    def test_pt_mz_nao_sei_is_non_engagement(self):
        self.assertEqual(
            classify_student_message(
                'não sei', has_inflight_question=True, locale='pt-mz',
            ),
            'non_engagement',
        )

    def test_pt_mz_o_que_significa_is_clarification(self):
        self.assertEqual(
            classify_student_message(
                'o que significa fotossíntese?',
                has_inflight_question=True, locale='pt-mz',
            ),
            'clarification',
        )

    def test_pt_mz_acho_que_queres_dizer_is_pushback(self):
        self.assertEqual(
            classify_student_message(
                'acho que queres dizer 90 graus, não 60',
                has_inflight_question=True, locale='pt-mz',
            ),
            'pushback',
        )

    def test_pt_mz_futebol_is_off_topic(self):
        self.assertEqual(
            classify_student_message(
                'queres ver o jogo de futebol?',
                has_inflight_question=True, locale='pt-mz',
            ),
            'off_topic',
        )

    def test_pt_mz_numeric_still_works(self):
        """Shape-detection patterns (numeric, letter) are locale-
        agnostic — '42' is an answer in any locale."""
        self.assertEqual(
            classify_student_message(
                '42', has_inflight_question=True, locale='pt-mz',
            ),
            'answer',
        )

    def test_en_us_still_classifies_correctly(self):
        """Regression: the refactor to LANG_PATTERNS must not break
        the existing 78/80 English eval. Spot-check the high-traffic
        intent."""
        self.assertEqual(
            classify_student_message(
                "i don't know", has_inflight_question=True, locale='en-us',
            ),
            'non_engagement',
        )
        self.assertEqual(
            classify_student_message(
                'what does perimeter mean?',
                has_inflight_question=True, locale='en-us',
            ),
            'clarification',
        )

    def test_unknown_locale_falls_back_to_english(self):
        """A misconfigured course doesn't break classification — it
        falls back to English vocabulary."""
        self.assertEqual(
            classify_student_message(
                "i don't know", has_inflight_question=True, locale='xx-yy',
            ),
            'non_engagement',
        )
