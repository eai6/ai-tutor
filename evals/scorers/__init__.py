"""Eval-harness scorers.

Phase 2 ships ``deterministic`` — the Layer 1 + Layer 2 scorers from
``memory/eval_harness_plan.md``:
- Layer 1: deterministic checks on tutor text (phrase / structural).
- Layer 2: judge-derived label assertions, sourced via
  ``apps.benchmark.autopopulate.derive_suggested_labels`` reading
  ``SessionTurn.metadata`` + ``SessionTurn.judge_outputs``.

Phase 3 adds ``llm_rubric`` for behaviors that don't fit either layer.
"""
from dataclasses import dataclass, field


@dataclass
class AssertionResult:
    """One assertion verdict — produced by every scorer layer."""
    name: str
    passed: bool
    detail: str = ''
