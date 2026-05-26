"""Deterministic + judge-label assertions.

Layer 1 + Layer 2 from ``memory/eval_harness_plan.md``. Pure-Python checks on
the tutor's text response and on the label set derived from the production
judge pipeline by ``apps.benchmark.autopopulate.derive_suggested_labels``.

No LLM calls. Fast, fully reproducible. Add new verbs here intentionally —
the small, fixed vocabulary keeps scenario YAML from drifting into ad-hoc
Python.
"""
from __future__ import annotations

from typing import Any, Callable

from apps.benchmark import labels as L

from evals.scorers import AssertionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_list(val: Any) -> list[str]:
    """Allow ``must_contain_phrase: "foo"`` or ``["foo", "bar"]``."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val]
    raise TypeError(
        f"expected str or list of str, got {type(val).__name__}: {val!r}"
    )


def _ends_with_question(text: str) -> bool:
    """True if the last non-whitespace character is ``?``.

    Mirrors the spirit of ``apps/tutoring/validator.py:_ends_with_question``
    without coupling to the validator's internals (which may evolve).
    """
    stripped = text.rstrip()
    return stripped.endswith('?')


def _count_paragraphs(text: str) -> int:
    """Count paragraphs separated by one or more blank lines."""
    paragraphs = [p for p in text.split('\n\n') if p.strip()]
    return len(paragraphs)


def _normalize_labels(labels: list[str]) -> set[str]:
    """Cast scenario-author label names to the canonical set in apps.benchmark.labels."""
    out: set[str] = set()
    for raw in labels:
        candidate = str(raw).strip()
        # Allow either the upper-case constant (ADVANCE) or the lower-case
        # value (advance). The label module exposes both via L.ADVANCE = 'advance'.
        upper = candidate.upper().replace('-', '_')
        # Look up the constant on the module; if it exists, use the value.
        # Otherwise pass through (and the assertion will fail loudly).
        looked_up = getattr(L, upper, None)
        out.add(looked_up if isinstance(looked_up, str) else candidate)
    return out


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------

def _verb_response_nonempty(expected, tutor_text, suggested_labels) -> AssertionResult:
    ok = bool(tutor_text.strip()) is bool(expected)
    return AssertionResult(
        'response_nonempty', ok, detail=f"len={len(tutor_text)}"
    )


def _verb_must_contain_phrase(expected, tutor_text, suggested_labels) -> AssertionResult:
    phrases = _as_list(expected)
    text_lower = tutor_text.lower()
    missing = [p for p in phrases if p.lower() not in text_lower]
    return AssertionResult(
        'must_contain_phrase',
        passed=not missing,
        detail=f"missing={missing!r}" if missing else 'all phrases present',
    )


def _verb_must_not_contain_phrase(expected, tutor_text, suggested_labels) -> AssertionResult:
    phrases = _as_list(expected)
    text_lower = tutor_text.lower()
    present = [p for p in phrases if p.lower() in text_lower]
    return AssertionResult(
        'must_not_contain_phrase',
        passed=not present,
        detail=f"forbidden phrase appeared: {present!r}" if present else 'clean',
    )


def _verb_must_label(expected, tutor_text, suggested_labels) -> AssertionResult:
    required = _normalize_labels(_as_list(expected))
    actual = set(suggested_labels)
    matched = required & actual
    return AssertionResult(
        'must_label',
        passed=bool(matched),
        detail=(
            f"matched={sorted(matched)!r}" if matched
            else f"required at least one of {sorted(required)!r}; actual={sorted(actual)!r}"
        ),
    )


def _verb_must_not_label(expected, tutor_text, suggested_labels) -> AssertionResult:
    forbidden = _normalize_labels(_as_list(expected))
    actual = set(suggested_labels)
    hit = forbidden & actual
    return AssertionResult(
        'must_not_label',
        passed=not hit,
        detail=(
            f"forbidden labels fired: {sorted(hit)!r}" if hit
            else f"none of {sorted(forbidden)!r} fired"
        ),
    )


def _verb_must_end_with_question(expected, tutor_text, suggested_labels) -> AssertionResult:
    actual = _ends_with_question(tutor_text)
    ok = bool(actual) is bool(expected)
    return AssertionResult(
        'must_end_with_question', ok,
        detail=f"actual={actual}, expected={bool(expected)}",
    )


def _verb_max_paragraphs(expected, tutor_text, suggested_labels) -> AssertionResult:
    limit = int(expected)
    count = _count_paragraphs(tutor_text)
    return AssertionResult(
        'max_paragraphs',
        passed=count <= limit,
        detail=f"paragraphs={count}, limit={limit}",
    )


_HANDLERS: dict[str, Callable] = {
    'response_nonempty':        _verb_response_nonempty,
    'must_contain_phrase':      _verb_must_contain_phrase,
    'must_not_contain_phrase':  _verb_must_not_contain_phrase,
    'must_label':               _verb_must_label,
    'must_not_label':           _verb_must_not_label,
    'must_end_with_question':   _verb_must_end_with_question,
    'max_paragraphs':           _verb_max_paragraphs,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score(
    assertions: dict[str, Any],
    tutor_text: str,
    suggested_labels: list[str],
) -> list[AssertionResult]:
    """Evaluate every assertion in the scenario's ``assertions:`` block.

    Unknown verbs fail loudly (rather than silently passing) so scenario
    authors notice typos.
    """
    results: list[AssertionResult] = []
    for verb, expected in assertions.items():
        handler = _HANDLERS.get(verb)
        if handler is None:
            results.append(AssertionResult(
                verb, passed=False,
                detail=f"unknown assertion verb (known: {sorted(_HANDLERS)})",
            ))
            continue
        try:
            results.append(handler(expected, tutor_text, suggested_labels))
        except Exception as exc:
            results.append(AssertionResult(
                verb, passed=False,
                detail=f"{type(exc).__name__}: {exc}",
            ))
    return results
