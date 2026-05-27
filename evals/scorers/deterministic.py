"""Deterministic + judge-label assertions.

Layer 1 + Layer 2 from ``memory/eval_harness_plan.md``. Pure-Python checks on
the tutor's text response and on the label set derived from the production
judge pipeline by ``apps.benchmark.autopopulate.derive_suggested_labels``.

No LLM calls. Fast, fully reproducible. Add new verbs here intentionally —
the small, fixed vocabulary keeps scenario YAML from drifting into ad-hoc
Python.
"""
from __future__ import annotations

import re
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


# Successors to the dropped `max_paragraphs` check. The 2026-05-27
# audit dropped length caps entirely (tutor free to explain), so the
# deterministic layer now polices the things that *actually* matter
# regardless of length: meta-reasoning leakage and passive endings.
# Both come from the "Speak to the student" + "Tutor-driven" rules in
# the system prompt; this gives us a cheap, no-LLM-call signal that
# fires in the same eval run as the LLM rubric judge.

_META_REASONING_PATTERNS = [
    # "The student has only…" / "The student hasn't…"
    re.compile(r'(?im)^\s*the student\s+(has|hasn\'t|is|isn\'t|wrote|said|answered|gave|seems|appears)\b'),
    # First-person process narration at the start of a sentence:
    # "I'll grade…" / "I am going to…" / "I'm going to…" / "Let me prompt…" /
    # "I shouldn't…" / "Now I need…" / "I need to (call|prompt|record|
    # pose|ask|grade|evaluate|check|verify)…"
    re.compile(
        r"(?im)^\s*("
        r"i'll|i am going to|i'm going to|"
        r"let me (prompt|grade|evaluate|check|record)|"
        r"i shouldn't|i should not|"
        r"now i need|"
        r"i need to (call|prompt|record|pose|ask|grade|evaluate|check|verify)"
        r")\b"
    ),
    # "to grade it (properly|correctly)" / "to evaluate it" / "to check the
    # answer" — narration of the tool-call intent in prose.
    re.compile(
        r"(?i)\bto\s+(grade|evaluate|score|verify)\s+(it|the\s+(answer|response))\b"
    ),
    # "This is a partial answer — I shouldn't record"
    re.compile(r"(?i)\bi (shouldn'?t|should not|must not|won'?t) (record|grade|mark|accept)\b"),
]

_PASSIVE_ENDING_PATTERNS = [
    re.compile(
        r"(?i)("
        r"ready for the next( one)?|"
        r"want to try another|"
        r"shall we (continue|move on|go on)|"
        r"(let me|tell me) know when (you'?re|you are) ready|"
        r"take your time|"
        r"whenever (you'?re|you are) ready|"
        r"do you want to keep going|"
        r"are you ready"
        r")\s*[.?!]*\s*$",
    ),
]


def _matches_any(patterns, text: str) -> str | None:
    """Return the matched string when any pattern hits, else None."""
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(0).strip()
    return None


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


def _verb_meta_reasoning_leak(expected, tutor_text, suggested_labels) -> AssertionResult:
    """Pass when expected matches actual. ``expected: false`` (the typical
    case) requires the response to have NO meta-reasoning leak; ``expected:
    true`` requires it to have one (only useful for negative-control
    tests).
    """
    hit = _matches_any(_META_REASONING_PATTERNS, tutor_text)
    actual = hit is not None
    return AssertionResult(
        'meta_reasoning_leak',
        passed=bool(actual) is bool(expected),
        detail=(
            f"leaked: {hit!r}" if actual
            else "no meta-reasoning narration"
        ),
    )


def _verb_passive_ending(expected, tutor_text, suggested_labels) -> AssertionResult:
    """Pass when expected matches actual. ``expected: false`` (typical)
    requires the reply NOT to end with a passive permission-seeking
    phrase like 'take your time' or 'ready for the next one?'.
    """
    hit = _matches_any(_PASSIVE_ENDING_PATTERNS, tutor_text)
    actual = hit is not None
    return AssertionResult(
        'passive_ending',
        passed=bool(actual) is bool(expected),
        detail=(
            f"passive ending: {hit!r}" if actual
            else "ends with concrete action"
        ),
    )


_HANDLERS: dict[str, Callable] = {
    'response_nonempty':        _verb_response_nonempty,
    'must_contain_phrase':      _verb_must_contain_phrase,
    'must_not_contain_phrase':  _verb_must_not_contain_phrase,
    'must_label':               _verb_must_label,
    'must_not_label':           _verb_must_not_label,
    'must_end_with_question':   _verb_must_end_with_question,
    'meta_reasoning_leak':      _verb_meta_reasoning_leak,
    'passive_ending':           _verb_passive_ending,
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
