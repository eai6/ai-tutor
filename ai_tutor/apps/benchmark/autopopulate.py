"""Derive suggested labels from a SessionTurn's pipeline trace.

Per ``memory/eval_benchmark_v2_simplified.md``, most issue labels map
directly to existing judge/validator signals. This module pulls those
signals out of ``SessionTurn.metadata`` + ``SessionTurn.judge_outputs``
and returns the set of issue labels that the production pipeline
already detected.

These are written to ``BenchmarkItem.snapshot['suggested_labels']`` at
sampling time. The Phase 2.2 annotation UI pre-fills the annotator's
``actual_labels`` from this list; the annotator confirms or overrides.

Edward only authors the 4 pure-judgment labels (LEAKS_ANSWER,
WRONG_VERDICT (sometimes), REPEATS, IGNORES_STUDENT) that the pipeline
can't detect, plus ``expected_labels`` and rationale.
"""
from __future__ import annotations

from typing import Dict, List, Any

from ai_tutor.apps.benchmark import labels as L


# Validator issue string → benchmark label
_VALIDATOR_ISSUE_TO_LABEL = {
    'no_question': L.NO_QUESTION,
    'info_dump_warning': L.INFO_DUMP,
    'figure_ref_without_signal': L.FIGURE_REF_UNATTACHED,
    'unfounded_praise_stripped': L.UNFOUNDED_PRAISE,
    'numeric_claim_contradicted': L.CLAIM_CONTRADICTED,
    'numeric_claim_unverified': L.CLAIM_UNVERIFIED,
    'authoring_violation': L.AUTHORED_QUESTION,
    'arithmetic_violation': L.ARITHMETIC_ERROR,
    'rule1_violation': L.UNFOUNDED_PRAISE,
    'tutor_incoherent': L.INCOHERENT,
    'figure_mismatch': L.FIGURE_MISMATCH,
    'verdict_mismatch': L.VERDICT_MISMATCH,
    'tutor_unsafe': L.SAFETY_HARMFUL,  # bumped down to INAPPROPRIATE below if severity != critical
}

# Rule violation name → benchmark label (canonical names per judges/rule.py)
_RULE_VIOLATION_TO_LABEL = {
    'NO_AUTHORING': L.AUTHORED_QUESTION,
    'RULE_1': L.UNFOUNDED_PRAISE,
    'RULE_ARITHMETIC': L.ARITHMETIC_ERROR,
}


def derive_suggested_labels(metadata: Dict[str, Any],
                            judge_outputs: Dict[str, Any]) -> List[str]:
    """Return the issue labels the production pipeline already detected.

    Pure function — no DB access, no side effects. Pass in raw
    ``SessionTurn.metadata`` and ``SessionTurn.judge_outputs`` dicts.

    Returns a sorted, deduplicated list of label strings (subset of
    ``apps.benchmark.labels.ISSUE_LABELS``). Empty list if nothing fired.
    """
    suggested: set[str] = set()
    metadata = metadata or {}
    judge_outputs = judge_outputs or {}

    # --- From validator_issues (most flags live here) -------------------
    validator_issues = metadata.get('validator_issues') or []
    for issue in validator_issues:
        label = _VALIDATOR_ISSUE_TO_LABEL.get(str(issue).lower())
        if label:
            suggested.add(label)

    # --- From rule_violations on metadata (legacy shape) ---------------
    rule_violations_meta = metadata.get('rule_violations') or []
    for rv in rule_violations_meta:
        rule_name = rv.get('rule') if isinstance(rv, dict) else str(rv)
        label = _RULE_VIOLATION_TO_LABEL.get(rule_name)
        if label:
            suggested.add(label)

    # --- From judge_outputs (per-judge breakdown) ----------------------
    rule_jo = (judge_outputs.get('rule') or {})
    for rv in rule_jo.get('violations') or []:
        rule_name = rv.get('rule') if isinstance(rv, dict) else str(rv)
        label = _RULE_VIOLATION_TO_LABEL.get(rule_name)
        if label:
            suggested.add(label)

    arith_jo = (judge_outputs.get('arithmetic') or {})
    if arith_jo.get('corrections'):
        suggested.add(L.ARITHMETIC_ERROR)

    factual_jo = (judge_outputs.get('factual') or {})
    if factual_jo.get('contradicted'):
        suggested.add(L.CLAIM_CONTRADICTED)
    if factual_jo.get('unverified'):
        suggested.add(L.CLAIM_UNVERIFIED)

    coherence_jo = (judge_outputs.get('coherence') or {})
    if coherence_jo.get('violations'):
        suggested.add(L.INCOHERENT)

    figure_ref_jo = (judge_outputs.get('figure_ref') or {})
    if figure_ref_jo.get('issues'):
        suggested.add(L.FIGURE_REF_UNATTACHED)

    figure_vision_jo = (judge_outputs.get('figure_vision') or {})
    aligned = figure_vision_jo.get('aligned')
    if aligned is False:  # explicit False, not None
        suggested.add(L.FIGURE_MISMATCH)

    safety_jo = (judge_outputs.get('safety') or {})
    severity = (safety_jo.get('severity') or '').lower()
    if severity == 'critical':
        suggested.discard(L.SAFETY_HARMFUL)  # clear any earlier add
        suggested.add(L.SAFETY_HARMFUL)
    elif severity == 'warning':
        suggested.discard(L.SAFETY_HARMFUL)
        suggested.add(L.SAFETY_INAPPROPRIATE)

    # --- Praise/bare-answer flags from metadata top-level --------------
    if metadata.get('praise_stripped'):
        suggested.add(L.UNFOUNDED_PRAISE)

    return sorted(suggested)
