"""Map our 30-label rubric onto BEA 2025's 4-dimensional ordinal scale.

External SIG-EDU evaluators (BEA 2025 shared task) score tutor responses on
four 3-way ordinal dimensions:
  - Mistake_Identification : Did the tutor recognize the student's error?
  - Mistake_Location       : Did the tutor pinpoint where the error is?
  - Providing_Guidance     : Did the tutor offer useful scaffolding/hints?
  - Actionability          : Did the tutor give the student a clear next step?

Each value is one of: "Yes", "To some extent", "No".

Our internal rubric (apps/benchmark/labels.py) is finer — 6 action labels + 24
issue labels. This module computes the 4 BEA values from our actual_labels +
expected_labels via a deterministic mapping. Imperfect mapping is preferable
to leaving fields empty (BEA evaluators expect all 4 keys).

Calibration: review the rules below against 5-10 real production annotations
once the JSONL export is live. Adjust if the inferred 4-dim doesn't match a
human reviewer's intuition. Don't fabricate; when uncertain, default to "No".
"""

from typing import Dict, Iterable, Literal, Optional

from ai_tutor.apps.benchmark import labels as L


BeaValue = Literal['Yes', 'To some extent', 'No']

BEA_DIMENSIONS = (
    'Mistake_Identification',
    'Mistake_Location',
    'Providing_Guidance',
    'Actionability',
)


def _has(labels: Iterable[str], *needles: str) -> bool:
    """True if any of needles appears in labels."""
    s = set(labels or [])
    return any(n in s for n in needles)


def _map_mistake_identification(actual: set, expected: set) -> BeaValue:
    """Did the tutor recognize the student's error?

    Yes:   SURFACE_ERROR is in actual_labels (tutor explicitly addressed the
           error).
    No:    Tutor gave a WRONG_VERDICT (treated wrong as right), VERDICT_MISMATCH
           (judge disagreed with tutor's verdict), LEAKS_ANSWER (skipped past
           the error to give the answer), or IGNORES_STUDENT — OR expected
           SURFACE_ERROR but it wasn't done.
    To some extent: EXPLAIN present without SURFACE_ERROR, suggesting the
           tutor talked about content but didn't directly call out the error.
    """
    if _has(actual, 'WRONG_VERDICT', 'VERDICT_MISMATCH', 'LEAKS_ANSWER', 'IGNORES_STUDENT'):
        return 'No'
    if 'SURFACE_ERROR' in actual:
        return 'Yes'
    if 'SURFACE_ERROR' in expected:   # expected to but didn't
        return 'No'
    if 'EXPLAIN' in actual:
        return 'To some extent'
    return 'No'


def _map_mistake_location(actual: set, expected: set) -> BeaValue:
    """Did the tutor pinpoint where the error is?

    Yes:   SURFACE_ERROR is present without dilution (we generally pinpoint
           when surfacing).
    To some extent: SURFACE_ERROR present but combined with INFO_DUMP or
           PADDING_FILLER (location buried in noise) or MULTI_PARAGRAPH.
    No:    SURFACE_ERROR absent, OR WRONG_VERDICT (told them they were right
           when they were wrong = no location identified).
    """
    if _has(actual, 'WRONG_VERDICT', 'IGNORES_STUDENT'):
        return 'No'
    if 'SURFACE_ERROR' in actual:
        if _has(actual, 'INFO_DUMP', 'PADDING_FILLER', 'MULTI_PARAGRAPH'):
            return 'To some extent'
        return 'Yes'
    if 'SURFACE_ERROR' in expected:
        return 'No'
    return 'No'


def _map_providing_guidance(actual: set, expected: set) -> BeaValue:
    """Did the tutor offer useful scaffolding / hints?

    Yes:   Any of EXPLAIN, ASK_WORKING, PROBE present without dilution.
           These are the constructive scaffolding actions in our rubric.
    To some extent: EXPLAIN/ASK_WORKING/PROBE present BUT also INFO_DUMP,
           PADDING_FILLER, MULTI_PARAGRAPH, or REPEATS (guidance buried,
           or repetitive).
    No:    None of the constructive actions present, OR major issues like
           IGNORES_STUDENT, OFF_TOPIC, NO_QUESTION dominate.
    """
    constructive = {'EXPLAIN', 'ASK_WORKING', 'PROBE'}
    has_constructive = bool(actual & constructive)
    diluted = bool(actual & {'INFO_DUMP', 'PADDING_FILLER', 'MULTI_PARAGRAPH', 'REPEATS'})
    blocking = bool(actual & {'IGNORES_STUDENT', 'OFF_TOPIC'})

    if blocking:
        return 'No'
    if has_constructive and not diluted:
        return 'Yes'
    if has_constructive and diluted:
        return 'To some extent'
    return 'No'


def _map_actionability(actual: set, expected: set) -> BeaValue:
    """Did the tutor give the student a clear next step?

    Yes:   ASK_WORKING, PROBE, or ADVANCE present — student knows what to
           do next (answer the question, share working, move on).
    To some extent: Only EXPLAIN present (told them something, no follow-up).
    No:    NO_QUESTION (tutor gave content with no action prompt), or no
           actionable label at all, or IGNORES_STUDENT.
    """
    if _has(actual, 'IGNORES_STUDENT'):
        return 'No'
    if _has(actual, 'ASK_WORKING', 'PROBE', 'ADVANCE'):
        return 'Yes'
    if 'NO_QUESTION' in actual:
        return 'No'
    if 'EXPLAIN' in actual:
        return 'To some extent'
    return 'No'


def map_to_bea_2025(
    actual_labels: Optional[Iterable[str]],
    expected_labels: Optional[Iterable[str]],
) -> Dict[str, BeaValue]:
    """Compute the BEA 2025 4-dim ordinal annotation from our labels.

    Returns dict with all 4 BEA keys. Never raises — empty/None inputs give
    "No" across the board (the conservative default).
    """
    actual = set(actual_labels or [])
    expected = set(expected_labels or [])
    return {
        'Mistake_Identification': _map_mistake_identification(actual, expected),
        'Mistake_Location':       _map_mistake_location(actual, expected),
        'Providing_Guidance':     _map_providing_guidance(actual, expected),
        'Actionability':          _map_actionability(actual, expected),
    }
