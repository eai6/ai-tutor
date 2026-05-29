"""Phase 3 §3.6.1 per-purpose model override tests.

Covers:
  - ``ModelConfig.get_for(purpose)`` reads the matching per-purpose
    env var.
  - Empty / unset env var falls through to DB-active.
  - Malformed override logs a warning and falls through.
  - Non-Gemini override on a grounding-required purpose is refused
    (falls through) and warns.
  - Legacy ``TUTOR_MODEL_OVERRIDE`` is still honoured for
    ``purpose='tutoring'`` so in-flight legacy sessions are not
    disrupted.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from django.test import TestCase

from apps.llm.models import ModelConfig


class _StubConfig:
    """Lightweight stand-in for the runtime ModelConfig that
    ``resolve_runtime`` returns — we just need an identifier."""

    def __init__(self, provider: str, model_name: str):
        self.provider = provider
        self.model_name = model_name


class PerPurposeOverrideTest(TestCase):
    def test_tutor_move_override_routes_through_resolve_runtime(self):
        with patch.dict(
            os.environ,
            {"TUTOR_MOVE_MODEL_OVERRIDE": "anthropic/claude-sonnet-4-6"},
            clear=False,
        ), patch.object(
            ModelConfig, "resolve_runtime",
            return_value=_StubConfig("anthropic", "claude-sonnet-4-6"),
        ) as mock_resolve:
            cfg = ModelConfig.get_for("tutor_move")
        mock_resolve.assert_called_once_with("anthropic", "claude-sonnet-4-6")
        self.assertEqual(cfg.provider, "anthropic")
        self.assertEqual(cfg.model_name, "claude-sonnet-4-6")

    def test_unset_override_falls_through_to_db_active(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TUTOR_MOVE_MODEL_OVERRIDE", None)
            cfg = ModelConfig.get_for("tutor_move")
        # No DB row in this test → returns None (DB-active fallback).
        self.assertIsNone(cfg)

    def test_malformed_override_falls_through(self):
        with patch.dict(
            os.environ,
            {"GRADER_MATH_MODEL_OVERRIDE": "no-slash-here"},
            clear=False,
        ), patch.object(
            ModelConfig, "resolve_runtime",
            return_value=_StubConfig("x", "y"),
        ) as mock_resolve:
            cfg = ModelConfig.get_for("grader_math")
        mock_resolve.assert_not_called()
        self.assertIsNone(cfg)

    def test_non_gemini_on_grounded_purpose_refused(self):
        with patch.dict(
            os.environ,
            {"GRADER_GROUNDED_MODEL_OVERRIDE": "anthropic/claude-sonnet-4-6"},
            clear=False,
        ), patch.object(
            ModelConfig, "resolve_runtime",
            return_value=_StubConfig("anthropic", "claude-sonnet-4-6"),
        ) as mock_resolve:
            cfg = ModelConfig.get_for("grader_grounded")
        mock_resolve.assert_not_called()
        self.assertIsNone(cfg)

    def test_gemini_on_grounded_purpose_accepted(self):
        with patch.dict(
            os.environ,
            {"GRADER_GROUNDED_MODEL_OVERRIDE": "google/gemini-3-flash-preview"},
            clear=False,
        ), patch.object(
            ModelConfig, "resolve_runtime",
            return_value=_StubConfig("google", "gemini-3-flash-preview"),
        ) as mock_resolve:
            cfg = ModelConfig.get_for("grader_grounded")
        mock_resolve.assert_called_once_with("google", "gemini-3-flash-preview")
        self.assertEqual(cfg.provider, "google")

    def test_legacy_tutoring_override_still_honored(self):
        with patch.dict(
            os.environ,
            {"TUTOR_MODEL_OVERRIDE": "google/gemini-3-flash-preview"},
            clear=False,
        ), patch.object(
            ModelConfig, "resolve_runtime",
            return_value=_StubConfig("google", "gemini-3-flash-preview"),
        ) as mock_resolve:
            cfg = ModelConfig.get_for("tutoring")
        mock_resolve.assert_called_once_with("google", "gemini-3-flash-preview")
        self.assertEqual(cfg.provider, "google")
