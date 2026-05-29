"""Post-render question extractor — Haiku one-action-per-turn + active-end.

Re-wired 2026-05-29 (open_question_authority_redesign.md §7 step 5,
belt-and-suspenders). The deterministic stacking floor in
``safety_gates.run_one_question_check`` catches the specific shapes the
LLM provably missed (buried MCQ block, two or more '?'-sentences). This
Haiku pass generalizes to action prompts the regex cannot see —
imperatives ("now you try"), fill-ins ("the ___ comes first"), retrieval
asks ("say the rule back"), choose-and-explain — and enforces the
active-end rule (Active Learning Ch.10: every tutor turn ends on an
action the student takes).

Fail-soft: any infra / parse error returns ``None`` so the caller falls
back to the deterministic floor (never blocks a turn on extractor
failure). Mirrors the ``judges/`` shape — one entry function, structured
result, fail-soft.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.services.grader_prompts import (
    QUESTION_EXTRACTOR_SYSTEM,
    render_question_extractor_user_prompt,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractorResult:
    """Parsed output of the Haiku question extractor."""

    action_count: int
    has_active_end: bool
    primary_action: str = ""
    stacked_examples: list[str] = field(default_factory=list)


def extract_action_prompts(
    *,
    tutor_text: str,
    selected_move: str,
    llm_client=None,
) -> Optional[ExtractorResult]:
    """Run the Haiku extractor over a rendered tutor turn.

    Returns an ``ExtractorResult`` on success, or ``None`` on any
    infrastructure / parse failure (the gate then relies on its
    deterministic floor). ``llm_client`` lets tests inject a fake.
    """
    with emit_span("audit", "gate.question_extractor") as span:
        client = llm_client or _resolve_client()
        if client is None:
            return None
        try:
            response = client.generate(
                messages=[{
                    "role": "user",
                    "content": render_question_extractor_user_prompt(
                        tutor_text=tutor_text,
                        selected_move=selected_move,
                    ),
                }],
                system_prompt=QUESTION_EXTRACTOR_SYSTEM,
                max_tokens=400,
            )
            raw_text = (response.content or "").strip()
        except Exception as exc:
            logger.warning(
                "[QuestionExtractor] LLM call raised %s", type(exc).__name__,
            )
            return None

        from apps.tutoring.v2.services.move_router import _safe_json_loads
        payload = _safe_json_loads(raw_text)
        if not isinstance(payload, dict):
            return None
        try:
            action_count = int(payload.get("action_count", 0))
        except (TypeError, ValueError):
            return None
        result = ExtractorResult(
            action_count=action_count,
            has_active_end=bool(payload.get("has_active_end", True)),
            primary_action=str(payload.get("primary_action") or "")[:300],
            stacked_examples=[
                str(s)[:200]
                for s in (payload.get("stacked_examples") or [])
                if isinstance(s, str)
            ],
        )
        if span is not None:
            span["payload"] = {
                "action_count": result.action_count,
                "has_active_end": result.has_active_end,
            }
        return result


def _resolve_client():
    from apps.tutoring.v2.services.student_grader import (
        _build_client_for_purpose,
    )
    return _build_client_for_purpose("question_extractor")
