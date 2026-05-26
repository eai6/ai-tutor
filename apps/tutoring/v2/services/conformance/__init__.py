"""Structural conformance check — Phase 2 §2.4.

Replaces the regen ensemble + 10-axis unified judge with:
  - One fast-LLM classifier (nine binary labels)
  - A stack of deterministic gates
  - Tutor-claim adjudication route
  - Verdict-keyed rule matrix
  - One retry on rejection; on second failure → safe terminal template

Each gate returns a ``GateResult`` with ``passed`` + ``reason``. The
orchestrator (``check.py``) runs them in order, short-circuits on the
first reject, and surfaces violated rules to the retry hook.
"""

from apps.tutoring.v2.services.conformance.gates import (
    GateResult,
    run_state_coherence_check,
    run_figure_ref_check,
    run_rule_check,
    run_safety_check,
    run_answer_leak_check,
    run_praise_filter,
)
from apps.tutoring.v2.services.conformance.classifier import (
    ClassifierLabels,
    run_conformance_classifier,
)
from apps.tutoring.v2.services.conformance.verdict_matrix import (
    apply_verdict_matrix,
    MatrixViolation,
)
from apps.tutoring.v2.services.conformance.check import (
    ConformanceCheck,
    ConformanceResult,
)

__all__ = [
    "GateResult",
    "run_state_coherence_check",
    "run_figure_ref_check",
    "run_rule_check",
    "run_safety_check",
    "run_answer_leak_check",
    "run_praise_filter",
    "ClassifierLabels",
    "run_conformance_classifier",
    "apply_verdict_matrix",
    "MatrixViolation",
    "ConformanceCheck",
    "ConformanceResult",
]
