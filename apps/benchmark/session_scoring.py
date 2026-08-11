"""Scoring over SessionEvalAnnotation.

Reports three things, in increasing order of how much they actually tell you:

1. **Session pass rate** — the fraction of sessions where every applicable
   dimension sat at its desideratum. Headline number, and a demanding one:
   eight conjunctive conditions means this sits low even for a decent tutor.
2. **Per-dimension pass rate** — what the paper reports, and the number that
   says WHERE the tutor loses sessions. Read this one.
3. **Slices** by subject, engine and outcome — whether a weakness is general or
   concentrated.

Plus inter-annotator agreement, which is what licenses any of the above to be
quoted.

Distinct from ``scoring.py``, which aggregates turn-level BenchmarkAnnotation
against the 30-label rubric. Same shape of job, different unit and rubric.

Plan: memory/session_eval_framework_plan.md
"""
from __future__ import annotations

from collections import defaultdict

from apps.benchmark import pedagogy as P

# N/A and unanswered are both excluded from scoring, but for different reasons
# and with different consequences, so they are counted separately everywhere
# below. Collapsing them would hide an annotator who is skipping questions.
NOT_APPLICABLE = 'not_applicable'
UNANSWERED = 'unanswered'


def _classify(key: str, value: str) -> str:
    """'pass' | 'fail' | NOT_APPLICABLE | UNANSWERED."""
    if not value:
        return UNANSWERED
    if value == P.NOT_APPLICABLE:
        return NOT_APPLICABLE
    return 'pass' if P.dimension_passes(key, value) else 'fail'


def dimension_stats(annotations) -> dict:
    """Per-dimension breakdown across the given annotations.

    ``pass_rate`` has N/A and unanswered in neither numerator nor denominator:
    a tutor is not credited for a dimension that never arose, nor penalised for
    one an annotator skipped.
    """
    out = {}
    for dim in P.DIMENSIONS:
        counts = {'pass': 0, 'fail': 0, NOT_APPLICABLE: 0, UNANSWERED: 0}
        value_counts: dict[str, int] = defaultdict(int)

        for annotation in annotations:
            value = getattr(annotation, dim.key, '') or ''
            counts[_classify(dim.key, value)] += 1
            if value:
                value_counts[value] += 1

        scored = counts['pass'] + counts['fail']
        out[dim.key] = {
            'label': dim.label,
            'desideratum': dim.desideratum,
            **counts,
            'scored': scored,
            'pass_rate': (counts['pass'] / scored) if scored else None,
            # 0-100 for display. The 0-1 rate above is what the export and any
            # downstream analysis should use; keeping both here stops the
            # template from doing arithmetic and getting it wrong.
            'pass_pct': round(100 * counts['pass'] / scored) if scored else None,
            # The full distribution, because "To some extent" and "No" are very
            # different failures and a single pass rate flattens them together.
            'distribution': dict(value_counts),
        }
    return out


def session_stats(annotations) -> dict:
    complete = [a for a in annotations if a.complete]
    passed = [a for a in complete if a.passes]
    return {
        'total': len(annotations),
        'complete': len(complete),
        'incomplete': len(annotations) - len(complete),
        'passed': len(passed),
        # Denominator is COMPLETE annotations, not all of them. An unfinished
        # annotation has not judged the session, so counting it as a failure
        # would report annotator throughput as tutor quality.
        'pass_rate': (len(passed) / len(complete)) if complete else None,
        'pass_pct': round(100 * len(passed) / len(complete)) if complete else None,
    }


def slice_stats(annotations, key_fn) -> dict:
    buckets = defaultdict(list)
    for annotation in annotations:
        buckets[key_fn(annotation.item) or 'unknown'].append(annotation)
    return {k: session_stats(v) for k, v in sorted(buckets.items())}


# ── Inter-annotator agreement ───────────────────────────────────────────

def cohens_kappa(pairs: list[tuple[str, str]]) -> dict:
    """Cohen's κ over (rater_a, rater_b) label pairs for ONE dimension.

    Returns {'kappa', 'observed', 'expected', 'n', 'undefined_reason'}.

    κ is UNDEFINED when expected agreement is 1.0 — which happens whenever both
    raters used a single category throughout. That is not a rare pathology
    here: 'revealing_answer' is 'No' for most well-behaved sessions, so two
    annotators who agree perfectly can produce 0/0. Returning 0.0 there would
    report perfect agreement as chance-level and invert the conclusion; the
    honest answer is that κ cannot be computed, so `kappa` is None and
    `undefined_reason` says why. Report the raw agreement alongside it.
    """
    n = len(pairs)
    if n == 0:
        return {'kappa': None, 'observed': None, 'expected': None, 'n': 0,
                'undefined_reason': 'no_overlapping_annotations'}

    categories = sorted({v for pair in pairs for v in pair})
    observed = sum(1 for a, b in pairs if a == b) / n

    a_counts = defaultdict(int)
    b_counts = defaultdict(int)
    for a, b in pairs:
        a_counts[a] += 1
        b_counts[b] += 1
    expected = sum((a_counts[c] / n) * (b_counts[c] / n) for c in categories)

    if expected >= 1.0:
        return {'kappa': None, 'observed': observed, 'expected': expected,
                'n': n, 'undefined_reason': 'no_category_variance'}

    return {
        'kappa': (observed - expected) / (1 - expected),
        'observed': observed,
        'expected': expected,
        'n': n,
        'undefined_reason': '',
    }


def agreement_stats(annotations) -> dict:
    """Per-dimension κ between the human annotator and the LLM judge.

    Only items annotated by BOTH contribute. Where several humans annotated one
    item, the earliest is used, so the comparison is deterministic rather than
    dependent on query ordering.
    """
    from apps.benchmark.models import SessionEvalAnnotation

    human_by_item, llm_by_item = {}, {}
    for annotation in sorted(annotations, key=lambda a: a.created_at):
        target = (llm_by_item
                  if annotation.annotator_role == SessionEvalAnnotation.Annotator.LLM_JUDGE
                  else human_by_item)
        target.setdefault(annotation.item_id, annotation)

    shared = sorted(set(human_by_item) & set(llm_by_item))
    out = {'items_compared': len(shared), 'dimensions': {}}

    for dim in P.DIMENSIONS:
        pairs = []
        for item_id in shared:
            a = getattr(human_by_item[item_id], dim.key, '') or ''
            b = getattr(llm_by_item[item_id], dim.key, '') or ''
            if a and b:                 # an unanswered dimension is not a rating
                pairs.append((a, b))
        out['dimensions'][dim.key] = {'label': dim.label, **cohens_kappa(pairs)}

    # Agreement on the thing that is actually reported: the session verdict.
    verdict_pairs = [
        ('pass' if human_by_item[i].passes else 'fail',
         'pass' if llm_by_item[i].passes else 'fail')
        for i in shared
        if human_by_item[i].complete and llm_by_item[i].complete
    ]
    out['session_verdict'] = cohens_kappa(verdict_pairs)
    return out


def compute_metrics(annotations=None) -> dict:
    """Everything, for the scores page and the export header."""
    from apps.benchmark.models import SessionEvalAnnotation

    if annotations is None:
        annotations = SessionEvalAnnotation.objects.select_related('item').all()
    annotations = list(annotations)

    human = [a for a in annotations
             if a.annotator_role == SessionEvalAnnotation.Annotator.HUMAN]

    return {
        'overall': session_stats(annotations),
        # Human-only is the headline: the paper found LLM judges unreliable on
        # this taxonomy, so mixing the two would launder that uncertainty into
        # the top-line number.
        'human_only': session_stats(human),
        'dimensions': dimension_stats(human),
        'by_subject': slice_stats(human, lambda i: i.subject),
        'by_engine': slice_stats(human, lambda i: i.engine),
        'by_outcome': slice_stats(human, lambda i: i.outcome),
        'agreement': agreement_stats(annotations),
    }


# ── Export ──────────────────────────────────────────────────────────────

def export_rows(annotations) -> list[dict]:
    """One dict per annotation, safe to release.

    Deliberately absent, and none of these are oversights:

    - ``redaction_report`` — ``advisory_names`` in it holds capitalised tokens
      lifted from the transcript for the REVIEWER's eyes. Reviewer-facing is not
      release-facing.
    - the raw session id and the student — ``session_key``, a salted hash, is
      the only identifier. Note what that does and does not buy: the key cannot
      be reversed to a session without the salt, and re-sampling the same
      session later yields a DIFFERENT key. It is stable within a release, which
      it must be for annotations to join to sessions — so two exports of the
      same rows do carry the same keys.
    - the annotator's username — an opaque per-export index instead. Annotators
      are staff rather than children, but a released dataset should not name
      who judged what.
    """
    from apps.benchmark.models import SessionEvalAnnotation

    annotator_index: dict[tuple, str] = {}

    def _annotator_id(annotation) -> str:
        key = (annotation.annotator_role, annotation.annotator_user_id,
               annotation.annotator_model)
        if key not in annotator_index:
            prefix = ('llm' if annotation.annotator_role ==
                      SessionEvalAnnotation.Annotator.LLM_JUDGE else 'human')
            # Number within the prefix, not across all annotators — a lone
            # 'llm_2' with no 'llm_1' reads like a row went missing.
            n = sum(1 for v in annotator_index.values()
                    if v.startswith(f'{prefix}_'))
            annotator_index[key] = f'{prefix}_{n + 1}'
        return annotator_index[key]

    rows = []
    for annotation in annotations:
        item = annotation.item
        values = annotation.values()
        rows.append({
            'session_key': item.session_key,
            'subject': item.subject,
            'engine': item.engine,
            'outcome': item.outcome,
            'turn_count': item.turn_count,
            'stratum': item.stratum,
            'transcript': item.transcript,
            'annotator': _annotator_id(annotation),
            'annotator_role': annotation.annotator_role,
            'annotator_model': annotation.annotator_model or '',
            'annotations': values,
            'per_dimension_pass': {
                k: P.dimension_passes(k, values.get(k) or '')
                for k in P.DIMENSION_KEYS
            },
            'complete': annotation.complete,
            'session_passes': annotation.passes if annotation.complete else None,
            'notes': annotation.notes or '',
            'taxonomy': 'maurya_et_al_naacl_2025',
        })
    return rows
