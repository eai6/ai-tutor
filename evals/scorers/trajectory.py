"""Trajectory scorer for multi-turn scenarios.

Operates over a full simulated session — termination reason, turn count,
repetition across a sliding window, and label fan-out across every tutor
turn. Single-turn deterministic/judge verbs DON'T live here (they're in
``deterministic.py``); this module owns the verbs that only make sense
when you have a whole transcript.

Inputs:
- ``SimResult`` from ``apps.tutoring.student_sim.simulate_session``
- ``per_turn_labels``: list of label sets, one per tutor turn, derived
  from ``derive_suggested_labels(metadata, judge_outputs)``

See memory/eval_harness_plan.md Phase 4.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from ai_tutor.apps.tutoring.student_sim.driver import SimResult

from evals.scorers import AssertionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val]
    raise TypeError(
        f"expected str or list of str, got {type(val).__name__}: {val!r}"
    )


def _normalize_utterance(text: str) -> str:
    """Lowercase, collapse whitespace, drop trailing punctuation.

    Used for repetition detection. We want to catch literal repeats
    (regen loops, banned-opener loops) — not semantic near-equivalents.
    """
    s = (text or '').lower().strip()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'[.!?]+$', '', s)
    return s


def _tutor_utterances(sim: SimResult) -> list[str]:
    return [t.content for t in sim.transcript if t.role == 'tutor']


# Tool-call / thinking syntax that must never appear in a student-facing turn.
# Targets the *function-call* and *XML-tag* forms specifically so ordinary
# prose ("record your answer below") does not false-positive:
#   - record_answer( / pose_question( / advance_step( / record_exit_ticket_answer(
#   - <tool_use> / <thinking> / <function_call> tags (open or close)
#   - the tool kwargs extracted_answer= / reference_answer= / question_type=
# This is the live replacement for the dead `no_label_anywhere: [TOOL_LEAK]`
# assertion (derive_suggested_labels never emits TOOL_LEAK for simple_tutor
# turns, so that assertion passed vacuously).
_TOOL_SYNTAX_RE = re.compile(
    r'(?i)('
    r'\b(?:record_answer|pose_question|advance_step|record_exit_ticket_answer)\s*\('
    r'|</?\s*(?:tool_use|thinking|function_call|tool_call)\b'
    r'|\b(?:extracted_answer|reference_answer|question_type)\s*='
    r')'
)


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------

def _verb_expected_reason(expected, sim, per_turn_labels) -> AssertionResult:
    allowed = set(_as_list(expected))
    actual = sim.reason
    return AssertionResult(
        'expected_reason',
        passed=actual in allowed,
        detail=f"actual={actual!r}, allowed={sorted(allowed)!r}",
    )


def _verb_max_turn_count(expected, sim, per_turn_labels) -> AssertionResult:
    limit = int(expected)
    return AssertionResult(
        'max_turn_count',
        passed=sim.turns <= limit,
        detail=f"turns={sim.turns}, limit={limit}",
    )


def _verb_no_repeated_tutor_phrase_within_window(
    expected, sim, per_turn_labels,
) -> AssertionResult:
    """Sliding-window repetition check on tutor utterances.

    ``expected`` shape:
        {"window": 5, "threshold": 3}

    Fails if ANY normalized utterance appears >= threshold times in any
    contiguous window of size ``window``.
    """
    if not isinstance(expected, dict):
        return AssertionResult(
            'no_repeated_tutor_phrase_within_window',
            passed=False,
            detail=f"expected dict {{window, threshold}}, got {type(expected).__name__}",
        )
    window = int(expected.get('window', 5))
    threshold = int(expected.get('threshold', 3))

    utterances = [_normalize_utterance(u) for u in _tutor_utterances(sim)]
    utterances = [u for u in utterances if u]  # drop empties

    if window <= 0 or threshold <= 0:
        return AssertionResult(
            'no_repeated_tutor_phrase_within_window',
            passed=False,
            detail=f"window/threshold must be positive (got {window}/{threshold})",
        )

    for start in range(0, max(1, len(utterances) - window + 1)):
        chunk = utterances[start:start + window]
        if not chunk:
            continue
        counts: dict[str, int] = {}
        for u in chunk:
            counts[u] = counts.get(u, 0) + 1
        offender = next(
            (u for u, c in counts.items() if c >= threshold), None,
        )
        if offender is not None:
            return AssertionResult(
                'no_repeated_tutor_phrase_within_window',
                passed=False,
                detail=(
                    f"phrase repeated {counts[offender]}× in window of "
                    f"{window} (turns {start}..{start+window-1}): "
                    f"{offender[:80]!r}"
                ),
            )
    return AssertionResult(
        'no_repeated_tutor_phrase_within_window',
        passed=True,
        detail=f"no phrase ≥{threshold}× in any {window}-turn window",
    )


def _verb_no_label_anywhere(expected, sim, per_turn_labels) -> AssertionResult:
    """No tutor turn in the session should carry any of these labels."""
    forbidden = {l.upper() for l in _as_list(expected)}
    hits: list[tuple[int, str]] = []
    for i, labels in enumerate(per_turn_labels):
        for label in labels:
            if str(label).upper() in forbidden:
                hits.append((i, str(label)))
    return AssertionResult(
        'no_label_anywhere',
        passed=not hits,
        detail=(
            f"forbidden labels fired on turns: {hits}" if hits
            else f"none of {sorted(forbidden)!r} fired on any tutor turn"
        ),
    )


def _verb_no_tool_syntax_in_any_turn(expected, sim, per_turn_labels) -> AssertionResult:
    """No tutor turn may leak tool-call / thinking syntax into its visible reply.

    ``expected: true`` (the typical case) asserts ABSENCE — the session passes
    when no tutor turn matches ``_TOOL_SYNTAX_RE``. ``expected: false`` inverts
    it (only useful for a negative-control test). This is a deterministic,
    always-live check: it fires with no judge in the loop, unlike the old
    label-based assertion.
    """
    want_absent = bool(expected)
    hits: list[tuple[int, str]] = []
    for i, u in enumerate(_tutor_utterances(sim)):
        m = _TOOL_SYNTAX_RE.search(u or '')
        if m:
            hits.append((i, m.group(0).strip()))
    leaked = bool(hits)
    return AssertionResult(
        'no_tool_syntax_in_any_turn',
        passed=(not leaked) if want_absent else leaked,
        detail=(
            f"tool/thinking syntax leaked on tutor turns: {hits[:5]}" if leaked
            else "no tool/thinking syntax in any tutor turn"
        ),
    )


_HANDLERS: dict[str, Callable] = {
    'expected_reason':                       _verb_expected_reason,
    'max_turn_count':                        _verb_max_turn_count,
    'no_repeated_tutor_phrase_within_window': _verb_no_repeated_tutor_phrase_within_window,
    'no_label_anywhere':                     _verb_no_label_anywhere,
    'no_tool_syntax_in_any_turn':            _verb_no_tool_syntax_in_any_turn,
}


def is_trajectory_verb(verb: str) -> bool:
    return verb in _HANDLERS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score(
    assertions: dict[str, Any],
    sim: SimResult,
    per_turn_labels: list[list[str]],
) -> list[AssertionResult]:
    """Evaluate only the trajectory verbs in the scenario.

    Single-turn verbs in the same scenario block are left for the
    deterministic scorer to handle. Unknown verbs here also fall through
    silently — the runner is responsible for routing.
    """
    results: list[AssertionResult] = []
    for verb, expected in assertions.items():
        handler = _HANDLERS.get(verb)
        if handler is None:
            continue
        try:
            results.append(handler(expected, sim, per_turn_labels))
        except Exception as exc:
            results.append(AssertionResult(
                verb, passed=False,
                detail=f"{type(exc).__name__}: {exc}",
            ))
    return results
