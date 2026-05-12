"""Scoring — compute pass-rate metrics over a set of BenchmarkAnnotation.

Pure function. Takes annotation rows + their parent items, returns a
metrics dict. No DB writes — the management command persists the
result onto a ``BenchmarkRun``.

The schema is intentionally a flat dict so the dashboard can render
it without rigid templates. Keys are sorted by stability — top-level
``overall`` and ``slices`` first, then optional ``agreement`` when
two annotators are present.

See ``memory/eval_benchmark_v2_simplified.md`` for the rubric.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from apps.benchmark.models import BenchmarkAnnotation, BenchmarkItem


# Eval-layer buckets we surface. Pipeline trace's ``eval_layer`` is a
# free-form string (deterministic_numeric / deterministic_mcq / llm /
# skipped / ''). Anything else falls under ``unknown`` for visibility.
_KNOWN_EVAL_LAYERS = (
    'deterministic_numeric',
    'deterministic_mcq',
    'llm',
    'skipped',
)


def _annotation_passes(ann: BenchmarkAnnotation) -> bool:
    return bool(ann.passes)


def _eval_layer_bucket(snapshot: dict) -> str:
    """Pull the pipeline eval_layer label, falling back to 'unknown'."""
    trace = (snapshot or {}).get('production', {}).get('pipeline_trace', {}) or {}
    layer = (trace.get('eval_layer') or '').strip().lower()
    if not layer:
        return 'unknown'
    if layer in _KNOWN_EVAL_LAYERS:
        return layer
    return 'unknown'


def _history_bucket(snapshot: dict) -> str:
    """Slice by whether judges saw conversation history."""
    trace = (snapshot or {}).get('production', {}).get('pipeline_trace', {}) or {}
    turns = trace.get('judge_history_turns') or 0
    return 'history_aware' if int(turns) > 0 else 'no_history'


def _slice_stats(rows: Iterable[Tuple[BenchmarkItem, BenchmarkAnnotation]],
                 key_fn) -> Dict[str, Dict[str, int]]:
    """Aggregate pass/fail counts keyed by ``key_fn(item)``.

    Returns ``{bucket: {total, passed, failed, pass_rate}}``.
    """
    buckets: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {'total': 0, 'passed': 0, 'failed': 0}
    )
    for item, ann in rows:
        bucket = key_fn(item) or 'unknown'
        buckets[bucket]['total'] += 1
        if _annotation_passes(ann):
            buckets[bucket]['passed'] += 1
        else:
            buckets[bucket]['failed'] += 1

    # Materialise pass_rate per bucket so the dashboard doesn't divide.
    out = {}
    for name, stats in buckets.items():
        total = stats['total']
        stats['pass_rate'] = (stats['passed'] / total) if total else 0.0
        out[name] = stats
    return out


def _agreement(human_rows: List[Tuple[BenchmarkItem, BenchmarkAnnotation]],
               llm_rows: List[Tuple[BenchmarkItem, BenchmarkAnnotation]]
               ) -> Optional[Dict]:
    """Exact-match agreement on pass/fail verdict between human + LLM judge.

    Both lists are filtered to the SAME item set (intersection). Returns
    ``None`` when there's no overlap.
    """
    human_by_item = {item.id: ann for item, ann in human_rows}
    llm_by_item = {item.id: ann for item, ann in llm_rows}
    overlap_ids = sorted(set(human_by_item) & set(llm_by_item))
    if not overlap_ids:
        return None

    agree = 0
    disagree_pairs: List[Dict] = []
    for item_id in overlap_ids:
        h = human_by_item[item_id]
        l = llm_by_item[item_id]
        h_pass = _annotation_passes(h)
        l_pass = _annotation_passes(l)
        if h_pass == l_pass:
            agree += 1
        else:
            disagree_pairs.append({
                'item_id': h.item.item_id,
                'human_passes': h_pass,
                'llm_passes': l_pass,
                'human_actual': sorted(h.actual_labels or []),
                'llm_actual': sorted(l.actual_labels or []),
                'expected': sorted(h.expected_labels or []),
            })
    total = len(overlap_ids)
    return {
        'overlap_items': total,
        'agree': agree,
        'disagree': total - agree,
        'agreement_rate': agree / total if total else 0.0,
        'disagreements': disagree_pairs[:50],
    }


def compute_metrics(
    annotations: Iterable[BenchmarkAnnotation],
    cross_check_annotations: Optional[Iterable[BenchmarkAnnotation]] = None,
) -> Dict:
    """Compute pass-rate metrics over a set of annotations.

    Args:
        annotations: The primary annotation set (e.g. human labels for
            system_variant=production_v1). Used for the overall +
            per-slice pass rates.
        cross_check_annotations: Optional second set (e.g. LLM judge
            on the same items). When provided AND there's item overlap
            with ``annotations``, an ``agreement`` block is included
            with exact-match rate + a list of disagreements.

    Returns:
        Dict with keys:
            overall = {total, passed, failed, pass_rate}
            slices = {
                by_subject: {subject: {total, passed, failed, pass_rate}},
                by_eval_layer: {layer: {...}},
                by_stratum: {stratum: {...}},
                by_history: {history_aware|no_history: {...}},
            }
            failure_categories = {category: count}
            agreement = {overlap_items, agree, disagree, agreement_rate,
                         disagreements: [...]}  # only when cross-check provided
    """
    primary = [
        (ann.item, ann)
        for ann in annotations
        if ann.item_id is not None
    ]

    overall_total = len(primary)
    overall_passed = sum(1 for _, ann in primary if _annotation_passes(ann))
    overall = {
        'total': overall_total,
        'passed': overall_passed,
        'failed': overall_total - overall_passed,
        'pass_rate': (overall_passed / overall_total) if overall_total else 0.0,
    }

    slices = {
        'by_subject': _slice_stats(primary, lambda it: it.subject),
        'by_stratum': _slice_stats(primary, lambda it: it.stratum or 'unknown'),
        'by_eval_layer': _slice_stats(
            primary, lambda it: _eval_layer_bucket(it.snapshot or {}),
        ),
        'by_history': _slice_stats(
            primary, lambda it: _history_bucket(it.snapshot or {}),
        ),
    }

    # Failure-category counts — only counted on failing annotations.
    cat_counter: Counter = Counter()
    for _, ann in primary:
        if _annotation_passes(ann):
            continue
        cat = (ann.failure_category or '').strip() or '(uncategorized)'
        cat_counter[cat] += 1
    failure_categories = dict(sorted(
        cat_counter.items(), key=lambda kv: (-kv[1], kv[0]),
    ))

    out = {
        'overall': overall,
        'slices': slices,
        'failure_categories': failure_categories,
    }

    if cross_check_annotations is not None:
        cross = [
            (ann.item, ann)
            for ann in cross_check_annotations
            if ann.item_id is not None
        ]
        agreement = _agreement(primary, cross)
        if agreement is not None:
            out['agreement'] = agreement

    return out
