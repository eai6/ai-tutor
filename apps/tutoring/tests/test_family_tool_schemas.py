"""Regression tests for family-specific tool schemas.

These lock in the two properties that make the compaction safe rather than
merely smaller:

1. Only PROSE differs between families. Names, parameters, types, enums and
   ``required`` are identical, because ``tools.py`` dispatches on parameter
   names and ``_narrow_pose_question_types`` rewrites the ``question_type``
   enum downstream.
2. Nothing that was only ever stated in a tool description got dropped on the
   floor. ``advance_step``, ``request_figure`` and ``redirect_off_topic`` were
   named zero times in Block-0 before this change — their descriptions were the
   only place the model met them, so compaction without relocation would have
   deleted the rules outright.

The second one is the test that matters. The first is cheap insurance.
"""
from __future__ import annotations

import json

import pytest

from apps.tutoring.simple_tutor.family_prompts import build_family_block_0
from apps.tutoring.simple_tutor.family_tools import build_family_tool_schemas
from apps.tutoring.simple_tutor.prompts import TOOL_SCHEMAS


def _shape(schemas):
    """Everything about a tool except its prose."""
    out = {}
    for t in schemas:
        props = t['input_schema'].get('properties', {})
        out[t['name']] = {
            'required': sorted(t['input_schema'].get('required', [])),
            'params': {
                name: {
                    'type': spec.get('type'),
                    'enum': spec.get('enum'),
                    'items': spec.get('items'),
                }
                for name, spec in props.items()
            },
        }
    return out


def test_qwen_keeps_identical_schema_shape():
    base = list(TOOL_SCHEMAS)
    assert _shape(build_family_tool_schemas('qwen', base)) == _shape(base)


@pytest.mark.parametrize('family', [None, '', 'anthropic', 'gemini', 'gemma'])
def test_non_compact_families_are_untouched(family):
    base = list(TOOL_SCHEMAS)
    assert build_family_tool_schemas(family, base) is base


def test_base_schemas_are_not_mutated():
    """The compaction deep-copies. A shallow copy here would silently give
    every cloud family the compact descriptions too, since TOOL_SCHEMAS is a
    module-level constant shared across requests.
    """
    before = json.dumps(TOOL_SCHEMAS, sort_keys=True)
    build_family_tool_schemas('qwen', list(TOOL_SCHEMAS))
    assert json.dumps(TOOL_SCHEMAS, sort_keys=True) == before


def test_qwen_descriptions_are_capability_sized():
    """Qwen's own function-calling examples use 38-39 char descriptions and
    ~78 char parameter descriptions. Anything much beyond that is policy
    creeping back into the slot the model reads as an API contract.
    """
    for t in build_family_tool_schemas('qwen', list(TOOL_SCHEMAS)):
        assert len(t['description']) <= 80, t['name']
        for name, spec in t['input_schema'].get('properties', {}).items():
            assert len(spec.get('description', '')) <= 160, f"{t['name']}.{name}"


def test_qwen_descriptions_carry_no_policy():
    """No when-to-call, no negations, and above all no sentence telling the
    model the platform will cope if it skips the call. ``advance_step`` used to
    describe itself as "a SOFT hint - the platform also auto-advances", which
    is the instruction the stuck-at-step-1 session followed.
    """
    banned = ('do not', "don't", 'soft hint', 'auto-advance', 'safety net',
              'call this when', 'never ', 'destroys')
    for t in build_family_tool_schemas('qwen', list(TOOL_SCHEMAS)):
        blob = (t['description'] + ' ' + ' '.join(
            s.get('description', '')
            for s in t['input_schema'].get('properties', {}).values()
        )).lower()
        for phrase in banned:
            assert phrase not in blob, f"{t['name']} still carries {phrase!r}"


# The Block-0 relocation tests that used to live here were REMOVED when the
# compaction was reverted on 2026-08-05 — they asserted prompt content that no
# longer exists. See the measurement in the docstring above.
#
# They also had a bug worth remembering if this is ever revived:
# ``build_family_block_0('qwen', 'BASE')`` returns the FULL template (20475
# chars) unless ``QWEN_BLOCK_0=compact`` is set, which is what production and
# scripts/measure_call_compliance.py both use (13500 chars). Asserting against
# the unpinned default silently measures a template the product does not ship.
# Any future Block-0 test must pin the env var first.
