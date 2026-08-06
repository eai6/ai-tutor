"""Family-specific tool schemas — the tool-side counterpart to
``family_prompts.build_family_block_0``.

WHY THIS EXISTS
---------------
Our tool descriptions were written as behavioural policy: *when* the tutor
should decide to call a tool, when it should not, and what breaks if it gets
it wrong. Qwen's own function-calling examples — the format the model was
fine-tuned on — use the description slot for a capability declaration and
nothing else:

    "description": "Get current temperature at a location."          (38 chars)
    "location": "The location to get the temperature for, in the
                 format \\"City, State, Country\\"."                    (78 chars)

Ours ran 287-1379 chars per tool description and up to 792 chars for a single
parameter, with four negations in ``record_answer`` alone. The description slot
is weighted most heavily when the model is *already selecting* a tool, so
policy placed there is read at the wrong moment — and it is read as an API
contract, not as instruction.

Two concrete consequences, both observed in production sessions:

- ``advance_step`` described itself as *"a SOFT hint — the platform also
  auto-advances when all of the current step's questions have a recorded
  verdict, or after a turn-cap safety net fires."* We documented our own safety
  nets to the model as a reason not to call. Session 6 on lesson 1427 sat at
  step 1/5 through seven turns; that sentence is the instruction it followed.
- ``extracted_answer`` spent 553 chars asking for judgement (strip hedging,
  strip units, resist auto-correcting a student who typed 'A' but seemed to
  mean 'C') *while* the model was formatting the call — the constraint tax
  (Tam et al., 10-15% on reasoning) levied at exactly the wrong moment.

WHAT THIS MODULE DOES
---------------------
For families that need it, replace every description with a Qwen-shaped
capability line. The policy is NOT deleted — it lives in Block-0, which is
where behavioural instruction belongs and where most of it already was:
``record_answer`` and ``pose_question`` policy is stated 9-10x in
``MARKDOWN_BLOCK_0_COMPACT`` and was merely *duplicated* in the descriptions.

The genuinely orphaned rules — ``advance_step``, ``request_figure`` and
``redirect_off_topic`` are named ZERO times in Block-0, so their descriptions
were the only place the model met them — move into
``family_prompts.MARKDOWN_BLOCK_0_COMPACT`` in the same change. Stripping a
description without relocating those would delete the rule outright.

Structure mirrors ``family_prompts.py`` deliberately: this module imports
nothing from ``prompts.py`` (which owns ``TOOL_SCHEMAS``), so the base schemas
are passed in by the caller and there is no circular import.
"""
from __future__ import annotations

import copy
from typing import Any

# Families whose tool descriptions get compacted. Kept as a set so adding a
# family is a one-token change rather than a new branch.
#
# ``qwen`` only, for now. Gemma is the obvious next candidate — its Ollama
# tool-calling is the weakest of the local families and family_prompts already
# leans on a "never emit tool syntax" rule for it — but it is not added on
# speculation. It goes in when it has been measured on
# scripts/measure_call_compliance.py, the same bar compact Block-0 had to clear.
_COMPACT_FAMILIES = frozenset({"qwen"})


# Capability lines, in Qwen's register: what the function does, one sentence,
# no when-to-call and no negations. Anything that reads as "and here is when
# you should decide to do this" belongs in Block-0, not here.
_COMPACT_TOOL_DESCRIPTIONS: dict[str, str] = {
    "pose_question": "Ask the student one graded question.",
    "record_answer": "Record the student's answer to the question in flight.",
    "request_figure": "Display a figure from the figure catalog.",
}


# Parameter descriptions, in Qwen's register: a format specification, the way
# ``"in the format \"City, State, Country\""`` is. Not a policy paragraph.
_COMPACT_PARAM_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "pose_question": {
        "question_text": (
            "The question stem, in the format the student reads it. "
            "Excludes the A/B/C/D options, which go in `options`."
        ),
        "question_type": (
            "The grading tier: \"mcq\" matches a letter, \"short_numeric\" "
            "matches a number, \"short_answer\" matches meaning."
        ),
        "options": (
            "For \"mcq\": the four option texts, in order A, B, C, D."
        ),
        "reference_answer": (
            "The correct answer: the letter for \"mcq\", the number for "
            "\"short_numeric\", one canonical phrasing for \"short_answer\"."
        ),
        "source": (
            "\"catalog\" when the question comes from question_pool, "
            "\"inline_authored\" when you wrote it."
        ),
        "catalog_question_id": (
            "For \"catalog\": the index of the question_pool entry, 1 to N."
        ),
    },
    "record_answer": {
        "extracted_answer": (
            "The student's answer exactly as they wrote it. For \"mcq\", "
            "the letter alone."
        ),
    },
    "request_figure": {
        "figure_id": "The figure id, from figure_catalog.",
    },
}


def build_family_tool_schemas(
    family: str | None,
    base_schemas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the tool schemas for ``family``.

    - ``qwen`` -> descriptions replaced with Qwen-shaped capability lines.
    - anything else (incl. ``None`` / Anthropic / OpenAI) -> ``base_schemas``
      unchanged. The frontier models handle the long-form descriptions without
      trouble and there is no evidence they are hurt by them, so this stays a
      local-model fix rather than a platform-wide rewrite.

    Structure (parameter names, types, enums, ``required``) is IDENTICAL across
    families. Only prose changes. That matters because ``_narrow_pose_question_types``
    rewrites the ``question_type`` enum downstream of this call, and because
    every tool-dispatch path in ``tools.py`` keys off parameter names — a
    family-specific *shape* would fork the dispatcher, which is not the intent.
    """
    fam = (family or "").strip().lower()
    if fam not in _COMPACT_FAMILIES:
        return base_schemas

    out: list[dict[str, Any]] = []
    for tool in base_schemas:
        name = tool.get("name")
        # Unknown tool -> passed through untouched rather than dropped or
        # blanked. A tool added to TOOL_SCHEMAS without a compact entry here
        # keeps its long description and still works; the alternative is a
        # silently description-less tool, which is the worse failure.
        if name not in _COMPACT_TOOL_DESCRIPTIONS:
            out.append(tool)
            continue

        new = copy.deepcopy(tool)
        new["description"] = _COMPACT_TOOL_DESCRIPTIONS[name]
        params = _COMPACT_PARAM_DESCRIPTIONS.get(name, {})
        props = new.get("input_schema", {}).get("properties", {})
        for param_name, spec in props.items():
            if isinstance(spec, dict) and param_name in params:
                spec["description"] = params[param_name]
        out.append(new)
    return out
