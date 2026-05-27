"""Post-render question extractor — Phase 4, memory/v2_unverified_trap_redesign.md Fix 2c.

Counts the distinct action prompts in a tutor turn so the engine can:
  - Reject turns that stack two questions
    (open_question silent-pivot root cause across runs 3, 4, 6).
  - Enforce the active-end rule (every tutor turn ends with an action
    the student takes — Principle #1 Active Learning Ch.10).
  - Bind ``runtime_state.open_question`` to the SINGLE action prompt
    actually rendered, regardless of whether it came from the tool path
    or from prose.

Subject-agnostic. Haiku-backed, fail-soft: on outage, returns a
deterministic "could not extract" result that the engine treats as
having one action prompt (so the conformance retry path is not
triggered by extractor outage alone — the deterministic conformance
gates remain the safety floor).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.services.grader_prompts import (
    QUESTION_EXTRACTOR_SYSTEM,
    render_question_extractor_user_prompt,
)

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Output of the question extractor.

    ``available`` is False when the extractor client could not be
    reached (no QUESTION_EXTRACTOR ModelConfig, model outage). The
    caller treats unavailable as "trust the upstream contract" — no
    extra conformance violation raised. Conformance still runs.
    """

    action_count: int
    primary_action: str
    has_active_end: bool
    stacked_examples: list[str]
    available: bool


class QuestionExtractor:
    """Stateless service. Constructed per-turn."""

    def __init__(self, *, client_factory=None) -> None:
        """``client_factory`` is the test seam — returns a
        BaseLLMClient-shaped object. When None, resolves
        ModelConfig.QUESTION_EXTRACTOR at call time.
        """
        self._client_factory = client_factory

    def extract(
        self,
        *,
        tutor_text: str,
        selected_move: str,
    ) -> ExtractionResult:
        """Identify action prompts in the rendered tutor turn.

        Empty / whitespace text returns ``action_count=0``,
        ``has_active_end=False`` without an LLM call.
        """
        text = (tutor_text or "").strip()
        if not text:
            return ExtractionResult(
                action_count=0,
                primary_action="",
                has_active_end=False,
                stacked_examples=[],
                available=True,
            )
        with emit_span("audit", "tutor.question_extractor") as span:
            client = self._resolve_client()
            if client is None:
                if span is not None:
                    span["payload"] = {"available": False}
                return ExtractionResult(
                    action_count=1,
                    primary_action="",
                    has_active_end=True,
                    stacked_examples=[],
                    available=False,
                )
            try:
                response = client.generate(
                    messages=[
                        {
                            "role": "user",
                            "content": render_question_extractor_user_prompt(
                                tutor_text=text,
                                selected_move=selected_move or "",
                            ),
                        },
                    ],
                    system_prompt=QUESTION_EXTRACTOR_SYSTEM,
                    max_tokens=400,
                )
                payload = _safe_json_loads(response.content or "") or {}
            except Exception as exc:
                logger.warning(
                    "[QuestionExtractor] LLM call raised %s — failing soft",
                    type(exc).__name__,
                )
                if span is not None:
                    span["payload"] = {
                        "available": False,
                        "reason": f"raise: {type(exc).__name__}",
                    }
                return ExtractionResult(
                    action_count=1,
                    primary_action="",
                    has_active_end=True,
                    stacked_examples=[],
                    available=False,
                )

            try:
                action_count = int(payload.get("action_count", 0))
            except (TypeError, ValueError):
                action_count = 0
            action_count = max(0, action_count)
            primary_action = str(payload.get("primary_action", "")).strip()
            has_active_end = bool(payload.get("has_active_end", False))
            raw_stacked = payload.get("stacked_examples") or []
            stacked = [
                str(s).strip() for s in raw_stacked if isinstance(s, str) and str(s).strip()
            ]

            if span is not None:
                span["payload"] = {
                    "action_count": action_count,
                    "has_active_end": has_active_end,
                    "stacked_example_count": len(stacked),
                }
            return ExtractionResult(
                action_count=action_count,
                primary_action=primary_action,
                has_active_end=has_active_end,
                stacked_examples=stacked,
                available=True,
            )

    def _resolve_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        try:
            from apps.llm.client import get_llm_client
            from apps.llm.models import ModelConfig
        except Exception:
            return None
        try:
            cfg = ModelConfig.get_for("question_extractor")
        except Exception:
            return None
        if cfg is None:
            return None
        try:
            return get_llm_client(cfg)
        except Exception as exc:
            logger.warning(
                "[QuestionExtractor] get_llm_client raised %s", type(exc).__name__,
            )
            return None


def _safe_json_loads(text: str):
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
