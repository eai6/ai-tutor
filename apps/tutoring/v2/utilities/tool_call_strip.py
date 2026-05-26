"""Strip leaked tool-call syntax from LLM text blocks.

Different LLM families invent different markup forms when they WANT to
call a tool but can't, or when they paraphrase a tool call in prose
instead of emitting a real ``tool_use`` block. The student sees raw
protocol syntax — confusing and unprofessional.

Observed forms in v2 production / dev:

- ``<tool_call>{"name": "pose_question", "arguments": {...}}</tool_call>``
  Gemini-via-Anthropic-API XML form. Most common in v2.
- ``pose_question(slot=N)`` / ``pose_question(slot=N, lead_in="...")``
  Anthropic-style function-call form.
- ``|||tool_call:pose_question{slot: 1}|||``
  Gemini 3.5 Flash fence form.
- ``tool_use: pose_question(slot=4)`` / ``tool_call: pose_inline_question(...)``
  Gemini 3.1 Flash Lite Preview prefix form.
- ``pose_inline_question(question="...", answer_key="...")``
  Explicit function-call form for inline questions.

This is a defensive surface — the regex strips *what's already leaked*,
but the upstream fix is to invoke ``generate_with_tools`` with
``tool_choice={"type":"any"}`` so the model is forced through the tool
channel. See ``apps.tutoring.v2.services.student_tutor.StudentTutor``.

Lifted from ``apps.tutoring.conversational_tutor.ConversationalTutor._strip_leaked_tool_call_syntax``
with the XML-tag form added — that form was the dominant v2 leak observed
in the 2026-05-26 GEO-S5 / MATHS-S1 evaluations.
"""

from __future__ import annotations

import re


# Compiled once at module load. Each alternation is one observed leak
# form; ordering doesn't matter for correctness (re.sub walks them as
# OR alternatives) but the XML form is listed first because it's the
# dominant v2 leak.
_LEAKED_TOOL_CALL_RE = re.compile(
    # XML-tag form: <tool_call>...</tool_call> with any JSON-ish body
    # (matches across newlines via DOTALL).
    r"<\s*tool[_ ]?(?:call|use|code)\s*>\s*.*?</\s*tool[_ ]?(?:call|use|code)\s*>"
    # Fence form: |||tool_call:NAME{...}|||
    r"|\|{2,}\s*tool[_ ]?(?:call|use|code)\s*:\s*\w+\s*\{[^}]*\}\s*\|{2,}"
    # Bare function form: pose_question(slot=N[, lead_in="..."])
    r"|\bpose_(?:inline_)?question\s*\(\s*slot\s*=\s*\d+\s*"
    r"(?:,\s*lead_in\s*=\s*[\"'][^\"']*[\"']\s*)?\)"
    # Inline-question full form: pose_inline_question(question="...", ...)
    r"|\bpose_inline_question\s*\([^)]*\)"
    # Prefix form: "tool_use:" / "tool_call:" followed by a pose call
    r"|\btool[_ ]?(?:call|use|code)\s*:\s*pose_(?:inline_)?question[^.\n]*"
    # Lone fence form (no body): |||tool_call:pose_question|||
    r"|\|{2,}\s*tool[_ ]?(?:call|use|code)\s*:[^|]*\|{2,}",
    re.IGNORECASE | re.DOTALL,
)


# A faster gate so the strip is a no-op on the common case (no leak).
_LEAK_TRIGGER_RE = re.compile(
    r"(?i)(?:<\s*tool[_ ]?(?:call|use|code)|\|{2,}\s*tool|"
    r"\bpose_(?:inline_)?question|\btool[_ ]?(?:call|use|code)\s*:)"
)


def strip_leaked_tool_call_syntax(text: str) -> tuple[str, int]:
    """Remove leaked tool-call prose from ``text``.

    Returns ``(cleaned, chars_removed)``. ``chars_removed`` is the
    number of characters dropped; callers log warnings when this is
    non-zero so the leak rate is observable.

    Idempotent — running twice on the same text is safe (second call
    is a no-op when nothing leaks). Cleaning preserves surrounding
    whitespace as best as it can: collapses runs of spaces and pulls
    sentence-final punctuation back against the preceding word.
    """
    if not text or not _LEAK_TRIGGER_RE.search(text):
        return text, 0
    cleaned = _LEAKED_TOOL_CALL_RE.sub("", text)
    # Tidy stray whitespace + dangling punctuation left behind.
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.!?])", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    return cleaned, len(text) - len(cleaned)


__all__ = ["strip_leaked_tool_call_syntax"]
