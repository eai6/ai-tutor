"""Conversation history window — formatting helper for judges.

Per Phase 2.2.5 of memory/agentic_platform_architecture_plan.md, the
LLM-driven judges (coherence, factual, rule) receive a bounded
``prior_exchanges`` window so they can reason about cross-turn signals
without blowing up token cost.

Bounded window rationale:
- 4 turns ≈ 2 student + 2 tutor exchanges before the current pair.
- Each turn capped at 400 chars → ~1600 chars per judge call.
- Predictable token budget; long sessions don't amplify cost.

The actual N used is recorded in pipeline metadata (``judge_history_turns``)
so the benchmark can slice agreement-rate by history-aware vs not.
"""
from __future__ import annotations

from typing import List, Optional


# Cap per-turn so total prompt size stays predictable. 400 chars
# typically holds an exchange even with verbose responses; longer
# turns get tail-truncated rather than dropped entirely.
_TURN_CHAR_CAP = 400


def format_history_window(
    conversation: Optional[List[dict]],
    *,
    turns: int = 4,
    per_turn_chars: int = _TURN_CHAR_CAP,
) -> str:
    """Format the last ``turns`` messages of a conversation list.

    Args:
        conversation: List of ``{"role": "user"|"assistant"|"student"|"tutor",
            "content": str}`` dicts. Engine state shape is "user"/"assistant"
            (mirror of the LLM-API convention); benchmark snapshot shape is
            "student"/"tutor". Both are handled.
        turns: How many trailing messages to keep (NOT student/tutor pairs —
            raw message count, since callers typically slice last-N already).
        per_turn_chars: Per-turn content cap. Tail-truncates with ellipsis.

    Returns:
        Newline-separated "ROLE: text" lines, or empty string when there's
        nothing to format.
    """
    if not conversation or turns <= 0:
        return ""

    recent = conversation[-turns:] if len(conversation) > turns else list(conversation)
    lines: List[str] = []
    for msg in recent:
        role_raw = (msg.get("role") or "").lower()
        if role_raw in ("user", "student"):
            label = "STUDENT"
        elif role_raw in ("assistant", "tutor"):
            label = "TUTOR"
        else:
            label = (role_raw or "TURN").upper()

        # Snapshot shape uses "text"; engine state uses "content".
        text = msg.get("content") or msg.get("text") or ""
        text = text.strip()
        if not text:
            continue
        if len(text) > per_turn_chars:
            text = text[: per_turn_chars - 1].rstrip() + "…"
        lines.append(f"{label}: {text}")

    return "\n".join(lines)
