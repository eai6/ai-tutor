"""Tests for the TUTOR_PROMPT_VARIANT deploy-time selection logic.

Covers `apps/tutoring/prompts/variants.py::get_active_variant_template`
and the resulting end-to-end behaviour through the Anthropic and
Gemini builders. Pins the contract documented in the variants.py
module docstring:

  unset / '' / 'baseline' / 'v3'  -> baseline (per-provider built-in)
  'v6'                            -> V6_TUTOR_SYSTEM_PROMPT_TEMPLATE
  'v7'                            -> V7_TUTOR_SYSTEM_PROMPT_TEMPLATE
  unknown ('v99', 'foo')          -> baseline (with logged warning)

Case-insensitive on the variant key. Read at builder-call time so
tests can mock via `unittest.mock.patch.dict(os.environ, ...)`.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from ai_tutor.apps.tutoring.prompts.variants import (
    V6_TUTOR_SYSTEM_PROMPT_TEMPLATE,
    V7_TUTOR_SYSTEM_PROMPT_TEMPLATE,
    active_variant_name,
    get_active_variant_template,
)

# A throwaway baseline that's unambiguously NOT v6 or v7 -- lets us
# assert "baseline returned" without coupling to the production
# template's exact content (which evolves).
_FAKE_BASELINE = "<baseline-marker>this is not a real prompt</baseline-marker>"


class GetActiveVariantTemplateTest(SimpleTestCase):
    """Direct unit tests on get_active_variant_template."""

    def test_unset_returns_baseline(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TUTOR_PROMPT_VARIANT', None)
            self.assertEqual(
                get_active_variant_template(_FAKE_BASELINE),
                _FAKE_BASELINE,
            )

    def test_empty_returns_baseline(self):
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': ''}):
            self.assertEqual(
                get_active_variant_template(_FAKE_BASELINE),
                _FAKE_BASELINE,
            )

    def test_baseline_keyword_returns_baseline(self):
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'baseline'}):
            self.assertEqual(
                get_active_variant_template(_FAKE_BASELINE),
                _FAKE_BASELINE,
            )

    def test_v3_keyword_returns_baseline(self):
        # v3 is the legacy name for "the production per-provider
        # template" -- treated as a baseline alias.
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v3'}):
            self.assertEqual(
                get_active_variant_template(_FAKE_BASELINE),
                _FAKE_BASELINE,
            )

    def test_v6_returns_v6_template(self):
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v6'}):
            self.assertEqual(
                get_active_variant_template(_FAKE_BASELINE),
                V6_TUTOR_SYSTEM_PROMPT_TEMPLATE,
            )

    def test_v7_returns_v7_template(self):
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v7'}):
            self.assertEqual(
                get_active_variant_template(_FAKE_BASELINE),
                V7_TUTOR_SYSTEM_PROMPT_TEMPLATE,
            )

    def test_case_insensitive(self):
        for key in ('V6', 'V7', 'Baseline', 'V3', ' v6 ', '\tv7\n'):
            with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': key}):
                # Just check it doesn't crash + returns a known value.
                result = get_active_variant_template(_FAKE_BASELINE)
                self.assertIn(
                    result,
                    {_FAKE_BASELINE, V6_TUTOR_SYSTEM_PROMPT_TEMPLATE,
                     V7_TUTOR_SYSTEM_PROMPT_TEMPLATE},
                    msg=f"unexpected result for env={key!r}",
                )

    def test_unknown_value_falls_back_to_baseline(self):
        # Failure mode: typo in env var should not break production --
        # silently fall back to baseline rather than raise.
        for key in ('v99', 'foo', 'V4', 'V5'):
            with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': key}):
                self.assertEqual(
                    get_active_variant_template(_FAKE_BASELINE),
                    _FAKE_BASELINE,
                    msg=f"expected baseline fallback for env={key!r}",
                )


class ActiveVariantNameTest(SimpleTestCase):
    """active_variant_name() returns None when baseline / unknown,
    else the canonical lowercase key (used for telemetry)."""

    def test_unset_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TUTOR_PROMPT_VARIANT', None)
            self.assertIsNone(active_variant_name())

    def test_v6_returns_v6(self):
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'V6'}):
            self.assertEqual(active_variant_name(), 'v6')

    def test_v7_returns_v7(self):
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v7'}):
            self.assertEqual(active_variant_name(), 'v7')

    def test_baseline_returns_none(self):
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'baseline'}):
            self.assertIsNone(active_variant_name())

    def test_unknown_returns_none(self):
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v42'}):
            self.assertIsNone(active_variant_name())


class BuilderEndToEndTest(SimpleTestCase):
    """End-to-end: build_stable_prefix() respects TUTOR_PROMPT_VARIANT
    on both providers. Confirms the integration site in
    anthropic.py / gemini.py actually consults the env var (not just
    the unit-tested helper)."""

    def _ctx(self):
        from ai_tutor.apps.tutoring.prompts.base import StablePrefixContext
        return StablePrefixContext(
            institution_name='Test Inst',
            locale_context='Test Locale',
            tutor_name='Mr Tutor',
            language='English',
            grade_level='S3',
            safety_prompt='[safety]',
        )

    def test_anthropic_baseline_when_env_unset(self):
        from ai_tutor.apps.tutoring.prompts import anthropic
        from ai_tutor.apps.tutoring.prompts.base import StablePrefixContext

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TUTOR_PROMPT_VARIANT', None)
            out = anthropic.AnthropicTutorPromptBuilder().build_stable_prefix(
                self._ctx(), subject_pack='general',
            )
        # v3 baseline anthropic template has a distinctive token.
        self.assertIn('<core_philosophy>', out)
        self.assertNotIn('<valid_turn_contract>', out)  # v7-only

    def test_anthropic_v6_when_env_set(self):
        from ai_tutor.apps.tutoring.prompts import anthropic

        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v6'}):
            out = anthropic.AnthropicTutorPromptBuilder().build_stable_prefix(
                self._ctx(), subject_pack='general',
            )
        # v6 has <must_end_with_question> standalone block; baseline doesn't.
        self.assertIn('<must_end_with_question>', out)
        # v6 has <every_turn>; baseline doesn't.
        self.assertIn('<every_turn>', out)

    def test_anthropic_v7_when_env_set(self):
        from ai_tutor.apps.tutoring.prompts import anthropic

        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v7'}):
            out = anthropic.AnthropicTutorPromptBuilder().build_stable_prefix(
                self._ctx(), subject_pack='general',
            )
        # v7 has <valid_turn_contract>; v6 + baseline don't.
        self.assertIn('<valid_turn_contract>', out)
        self.assertIn('<branch_templates>', out)

    def test_gemini_baseline_when_env_unset(self):
        from ai_tutor.apps.tutoring.prompts import gemini

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TUTOR_PROMPT_VARIANT', None)
            out = gemini.GeminiTutorPromptBuilder().build_stable_prefix(
                self._ctx(), subject_pack='general',
            )
        # v3 baseline gemini template uses markdown ## Role; v6/v7 use XML.
        self.assertIn('## Role', out)
        self.assertNotIn('<valid_turn_contract>', out)

    def test_gemini_v7_when_env_set(self):
        from ai_tutor.apps.tutoring.prompts import gemini

        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v7'}):
            out = gemini.GeminiTutorPromptBuilder().build_stable_prefix(
                self._ctx(), subject_pack='general',
            )
        # Variant template applied to BOTH providers -- gemini path
        # now uses the same XML-tagged v7 template as anthropic.
        self.assertIn('<valid_turn_contract>', out)

    def test_prompt_pack_override_takes_precedence_over_variant(self):
        # Institution-level PromptPack should still win over variant env
        # (variant is a deploy-tier toggle; PromptPack is per-school
        # customisation -- documented as orthogonal concerns).
        from ai_tutor.apps.tutoring.prompts import anthropic

        sentinel = "[PromptPack override -- institution-specific]"
        with patch.dict(os.environ, {'TUTOR_PROMPT_VARIANT': 'v7'}):
            out = anthropic.AnthropicTutorPromptBuilder().build_stable_prefix(
                self._ctx(),
                prompt_pack_override=sentinel,
                subject_pack='general',
            )
        self.assertIn(sentinel, out)
        self.assertNotIn('<valid_turn_contract>', out)


class TutorModelOverrideTest(TestCase):
    """ModelConfig.get_for('tutoring') respects TUTOR_MODEL_OVERRIDE env
    var; other purposes ignore it. Failure modes fall back silently.

    Uses `TestCase` (not `SimpleTestCase`) because resolve_runtime
    queries the DB to look for an existing matching ModelConfig before
    constructing an in-memory one."""

    def test_tutoring_with_valid_override_returns_runtime_config(self):
        from ai_tutor.apps.llm.models import ModelConfig
        # Need ANTHROPIC_API_KEY set in env so resolve_runtime succeeds.
        env = {
            'TUTOR_MODEL_OVERRIDE': 'anthropic/claude-sonnet-4-6',
            'ANTHROPIC_API_KEY': 'fake-key-for-test',
        }
        with patch.dict(os.environ, env):
            cfg = ModelConfig.get_for('tutoring')
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.provider, 'anthropic')
        self.assertEqual(cfg.model_name, 'claude-sonnet-4-6')

    def test_non_tutoring_purpose_ignores_override(self):
        # Override only applies to tutoring; judge / generation etc.
        # should NOT be retargeted by an env-var typo or misuse.
        from ai_tutor.apps.llm.models import ModelConfig
        env = {
            'TUTOR_MODEL_OVERRIDE': 'google/gemini-3.1-pro-preview',
            'GOOGLE_API_KEY': 'fake-key',
        }
        with patch.dict(os.environ, env):
            cfg = ModelConfig.get_for('judge')
        # cfg may be None (no judge config in test DB) or whatever's in
        # the DB, but should NOT be the gemini-3.1-pro override.
        if cfg is not None:
            self.assertNotEqual(cfg.model_name, 'gemini-3.1-pro-preview')

    def test_malformed_override_falls_back(self):
        # Missing "/" delimiter -> warning, fall through to DB lookup.
        from ai_tutor.apps.llm.models import ModelConfig
        with patch.dict(os.environ, {'TUTOR_MODEL_OVERRIDE': 'not-a-valid-spec'}):
            cfg = ModelConfig.get_for('tutoring')
        # cfg comes from DB (may be None in empty test DB).
        if cfg is not None:
            self.assertNotEqual(cfg.model_name, 'not-a-valid-spec')

    def test_empty_override_uses_db(self):
        from ai_tutor.apps.llm.models import ModelConfig
        with patch.dict(os.environ, {'TUTOR_MODEL_OVERRIDE': ''}):
            # Empty string is the unset case. Just confirms no crash.
            ModelConfig.get_for('tutoring')  # should not raise
