"""Aggregation helpers for the content-edit benchmark dashboard (Q6.1).

Pure functions over ContentEditEvent querysets — no I/O beyond the
ORM. Each function takes a (filtered) queryset so the dashboard view
can pre-narrow by content_type / source / tag / lesson before
computing aggregates.

Public surface:
  tag_frequency(qs)       → {tag: count}
  source_breakdown(qs)    → {source: count}
  content_type_breakdown(qs) → {content_type: count}
  edit_volume_by_day(qs, days=30) → [(date, count), ...]
  judge_precision(qs)     → {judge_name: {tp, fp, fn, support, ...}}

The judge_precision metric is the most subjective — see its docstring
for the definition we use.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

from django.utils import timezone


# Reuse the same judge-code → tag mapping the autopopulate uses.
# Keeping ONE table means the dashboard's "did the judge predict the
# edit?" computation lines up exactly with what we suggest at edit time.
from ai_tutor.apps.curriculum.quality_autopopulate import _JUDGE_CODE_TO_TAGS


def tag_frequency(qs) -> Dict[str, int]:
    """Count how many events carry each error_tag.

    A single event with multiple tags counts in each.
    """
    counts: Counter = Counter()
    # Note: no .only() — calling it conflicts with .select_related()
    # the dashboard view applies upstream. Tag-frequency querysets
    # are small enough (hundreds of rows) that the full-row fetch
    # is cheap.
    for evt in qs.iterator(chunk_size=500):
        for t in (evt.error_tags or []):
            counts[t] += 1
    return dict(counts.most_common())


def source_breakdown(qs) -> Dict[str, int]:
    """Counts of events by source (manual_edit / ai_regen_auto / ...)."""
    from django.db.models import Count
    rows = qs.values('source').annotate(n=Count('id')).order_by('-n')
    return {r['source']: r['n'] for r in rows}


def content_type_breakdown(qs) -> Dict[str, int]:
    """Counts of events by content_type."""
    from django.db.models import Count
    rows = qs.values('content_type').annotate(n=Count('id')).order_by('-n')
    return {r['content_type']: r['n'] for r in rows}


def edit_volume_by_day(qs, days: int = 30) -> List[Tuple[_dt.date, int]]:
    """Edit volume per day for the last N days.

    Returns a list of (date, count) tuples ordered oldest → newest,
    with zero-count days INCLUDED so a sparkline renders evenly.
    """
    from django.db.models.functions import TruncDate
    from django.db.models import Count

    end = timezone.now().date()
    start = end - _dt.timedelta(days=days - 1)

    actual = (
        qs.filter(created_at__date__gte=start)
        .annotate(d=TruncDate('created_at'))
        .values('d')
        .annotate(n=Count('id'))
    )
    actual_map = {r['d']: r['n'] for r in actual}

    out: List[Tuple[_dt.date, int]] = []
    for i in range(days):
        d = start + _dt.timedelta(days=i)
        out.append((d, actual_map.get(d, 0)))
    return out


def judge_precision(qs) -> Dict[str, Dict[str, Any]]:
    """Per-judge agreement metrics over the captured edits.

    For each judge we tracked in `judge_outputs_at_edit`:

      true_positive (tp):  judge flagged a violation that mapped to a
        tag the teacher confirmed on the edit. (Judge predicted the
        edit-worthy issue.)
      false_positive (fp): judge flagged a violation whose mapped tag
        the teacher did NOT confirm. (Judge cried wolf.)
      false_negative (fn): teacher confirmed a tag the judge had no
        violation mapping for. (Judge missed an edit-worthy issue.)
      support:             total events where this judge had a verdict.
      precision:           tp / (tp + fp), or None when tp+fp == 0.
      recall:              tp / (tp + fn), or None when tp+fn == 0.

    Notes / caveats:
      - We compare the judge's verdict at edit time vs the teacher's
        FINAL error_tags (which default to suggested_tags but can be
        overridden in the detail UI). When the teacher hasn't reviewed
        the event, error_tags == suggested_tags == derived from this
        same judge → precision metric is biased high. Real signal
        emerges once teachers manually confirm/edit tags.
      - "Mapped tag" uses _JUDGE_CODE_TO_TAGS — same table as
        derive_suggested_tags. A judge with NO codes in the map
        contributes only to support + fn (it can't true-positive
        because its violations never carry forward as suggested tags).
    """
    metrics: Dict[str, Dict[str, int]] = defaultdict(lambda: {
        'tp': 0, 'fp': 0, 'fn': 0, 'support': 0,
    })

    # No .only() — see tag_frequency note. Same rationale.
    for evt in qs.iterator(chunk_size=500):
        actual_tags = set(evt.error_tags or [])
        judge_outputs = evt.judge_outputs_at_edit or {}

        # Tags the teacher confirmed but NO judge in this event
        # mapped to — collected globally for fn attribution below.
        all_judge_predicted_tags: set = set()

        for jname, verdict in judge_outputs.items():
            if not isinstance(verdict, dict):
                continue
            metrics[jname]['support'] += 1

            judge_predicted_tags: set = set()
            for code in (verdict.get('violations') or []):
                for tag in _JUDGE_CODE_TO_TAGS.get(
                    str(code).upper(), []
                ):
                    judge_predicted_tags.add(tag)
            all_judge_predicted_tags |= judge_predicted_tags

            for tag in judge_predicted_tags:
                if tag in actual_tags:
                    metrics[jname]['tp'] += 1
                else:
                    metrics[jname]['fp'] += 1

        # FN: tags the teacher confirmed that NO judge in this event
        # predicted. Attribute to ALL judges that had a verdict on the
        # event (each one missed it). When no judges had a verdict,
        # there's nothing to attribute against.
        missed_tags = actual_tags - all_judge_predicted_tags
        if missed_tags and judge_outputs:
            for jname in judge_outputs.keys():
                metrics[jname]['fn'] += len(missed_tags)

    # Compute precision / recall + return as plain dicts.
    out: Dict[str, Dict[str, Any]] = {}
    for jname, m in metrics.items():
        tp, fp, fn = m['tp'], m['fp'], m['fn']
        precision = (tp / (tp + fp)) if (tp + fp) else None
        recall = (tp / (tp + fn)) if (tp + fn) else None
        out[jname] = {
            **m,
            'precision': precision,
            'recall': recall,
            # F1 is convenient for ranking judges in the dashboard
            'f1': (
                2 * precision * recall / (precision + recall)
                if precision and recall else None
            ),
        }

    # Stable order: judges that ran most often first.
    return dict(sorted(
        out.items(), key=lambda p: -p[1]['support'],
    ))


__all__ = [
    "tag_frequency",
    "source_breakdown",
    "content_type_breakdown",
    "edit_volume_by_day",
    "judge_precision",
]
