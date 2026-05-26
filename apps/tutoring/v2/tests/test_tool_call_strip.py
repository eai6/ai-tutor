"""Unit tests for the leaked tool-call syntax stripper.

Covers the four documented leak forms (XML, fence, function-call,
prefix) and the no-leak fast path. The XML form is the dominant v2
leak observed in the 2026-05-26 evals; the others are kept for parity
with the legacy stripper.
"""

from __future__ import annotations

import pytest

from apps.tutoring.v2.utilities.tool_call_strip import (
    strip_leaked_tool_call_syntax,
)


# ──────────────────────────────────────────────────────────────────────
# No-leak fast path — short-circuits without running the heavy regex.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "",
    "Let's try one together.",
    "Great work on that.",
    "I want to check that with you before I'm sure.",
    "Now apply the same idea to a different equation.",
])
def test_no_leak_passthrough(text):
    cleaned, leaked = strip_leaked_tool_call_syntax(text)
    assert cleaned == text
    assert leaked == 0


# ──────────────────────────────────────────────────────────────────────
# XML-tag form — the dominant v2 leak.
# ──────────────────────────────────────────────────────────────────────


def test_strip_xml_tool_call_block():
    text = (
        "Today we're diving into Rivers, Tributaries, and Confluence.\n\n"
        "Let's check your starting knowledge straight away.\n\n"
        '<tool_call>\n{"name": "pose_question", "arguments": '
        '{"question": "What is a tributary?", "answer": "smaller stream"}}\n'
        "</tool_call>"
    )
    cleaned, leaked = strip_leaked_tool_call_syntax(text)
    assert "<tool_call>" not in cleaned
    assert "</tool_call>" not in cleaned
    assert "pose_question" not in cleaned
    assert leaked > 0
    # Substantive prose preserved.
    assert "Rivers, Tributaries, and Confluence" in cleaned
    assert "Let's check your starting knowledge" in cleaned


def test_strip_xml_tool_call_multiline_body():
    text = (
        "Lead-in.\n\n<tool_call>\n  {\n    \"name\": \"pose_question\",\n"
        "    \"arguments\": {\n      \"slot\": 0\n    }\n  }\n</tool_call>"
    )
    cleaned, leaked = strip_leaked_tool_call_syntax(text)
    assert cleaned.strip() == "Lead-in."
    assert leaked > 0


def test_strip_xml_tool_use_variant():
    text = '<tool_use>{"name": "pose_question"}</tool_use>'
    cleaned, _ = strip_leaked_tool_call_syntax(text)
    assert "tool_use" not in cleaned


# ──────────────────────────────────────────────────────────────────────
# Function-call form — Anthropic-style.
# ──────────────────────────────────────────────────────────────────────


def test_strip_bare_function_call():
    text = "Let's try this. pose_question(slot=3)"
    cleaned, leaked = strip_leaked_tool_call_syntax(text)
    assert "pose_question" not in cleaned
    assert "Let's try this." in cleaned
    assert leaked > 0


def test_strip_function_call_with_lead_in():
    text = 'pose_question(slot=2, lead_in="Try this:")'
    cleaned, _ = strip_leaked_tool_call_syntax(text)
    assert "pose_question" not in cleaned


def test_strip_inline_question_form():
    text = (
        'pose_inline_question(question="What is X?", '
        'answer_key="42", type="short_answer")'
    )
    cleaned, _ = strip_leaked_tool_call_syntax(text)
    assert "pose_inline_question" not in cleaned


# ──────────────────────────────────────────────────────────────────────
# Fence form — Gemini 3.5 Flash variant.
# ──────────────────────────────────────────────────────────────────────


def test_strip_fence_form():
    text = "Setup. |||tool_call:pose_question{slot: 1}||| trailing"
    cleaned, leaked = strip_leaked_tool_call_syntax(text)
    assert "pose_question" not in cleaned
    assert "|||" not in cleaned
    assert "Setup." in cleaned
    assert "trailing" in cleaned
    assert leaked > 0


def test_strip_fence_form_no_body():
    text = "|||tool_call:pose_question|||"
    cleaned, _ = strip_leaked_tool_call_syntax(text)
    assert cleaned == ""


# ──────────────────────────────────────────────────────────────────────
# Prefix form — Gemini 3.1 Flash Lite variant.
# ──────────────────────────────────────────────────────────────────────


def test_strip_prefix_form():
    text = "Now try this. tool_use: pose_question(slot=4)"
    cleaned, _ = strip_leaked_tool_call_syntax(text)
    assert "pose_question" not in cleaned
    assert "Now try this." in cleaned


# ──────────────────────────────────────────────────────────────────────
# Tidying: collapse extra whitespace, pull punctuation back.
# ──────────────────────────────────────────────────────────────────────


def test_strip_tidies_whitespace():
    text = "Hello   <tool_call>{}</tool_call>   world ."
    cleaned, _ = strip_leaked_tool_call_syntax(text)
    # Two-or-more spaces collapsed to one, " ." pulled back to "."
    assert "  " not in cleaned
    assert cleaned.endswith("world.") or cleaned.endswith("world. ") or cleaned == "Hello world."


def test_idempotent():
    """Running the stripper twice produces the same output as once."""
    text = "Lead.\n\n<tool_call>{}</tool_call>"
    once, _ = strip_leaked_tool_call_syntax(text)
    twice, leaked_again = strip_leaked_tool_call_syntax(once)
    assert once == twice
    assert leaked_again == 0
