"""Annotation UI for the tutor evaluation benchmark — Phase 2.2.

Two views:

- ``benchmark_list``  → /dashboard/benchmark/        super-admin index of
  sampled BenchmarkItem rows with annotation status.
- ``benchmark_annotate`` → /dashboard/benchmark/<item_id>/  two-pane
  annotation form: frozen snapshot on the left, label/category form on
  the right. POST upserts BenchmarkAnnotation, then redirects to the
  next unannotated item (save-and-next).

v1 scope (per memory/handoff_phase_2_2.md + 2026-05-12 conversation):
- Single system variant: ``production_v1`` (hardcoded).
- Single annotator role: ``human`` (request.user).
- failure_category dropdown locked to ``FAILURE_CATEGORIES`` (no free text).
- No pagination (~50 items expected).

Super-admin only. Multi-tenancy: benchmark items aren't institution-
scoped — pilot evaluation work spans all schools.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.benchmark import labels as L
from apps.benchmark.models import (
    BenchmarkAnnotation,
    BenchmarkItem,
    BenchmarkRun,
)
from apps.benchmark.sampling import (
    candidate_tutor_turns,
    create_benchmark_items,
)


# Issue labels grouped by source so the form can render sections.
# Order matters — drives the order of fieldsets in the UI.
ISSUE_LABEL_GROUPS = [
    ('Rule judge', [L.AUTHORED_QUESTION, L.UNFOUNDED_PRAISE]),
    ('Arithmetic judge', [L.ARITHMETIC_ERROR]),
    ('Factual judge', [L.CLAIM_CONTRADICTED, L.CLAIM_UNVERIFIED]),
    ('Coherence judge', [L.INCOHERENT]),
    ('Figure judges', [L.FIGURE_REF_UNATTACHED, L.FIGURE_MISMATCH]),
    ('Safety judge', [L.SAFETY_HARMFUL, L.SAFETY_INAPPROPRIATE]),
    ('Validator (format)', [L.NO_QUESTION, L.INFO_DUMP, L.MULTI_PARAGRAPH,
                            L.BANNED_OPENER, L.PADDING_FILLER]),
    ('Validator (semantic)', [L.VERDICT_MISMATCH, L.WRONG_VERDICT,
                              L.PREMATURE_ADVANCE]),
    ('Engine strips', [L.THINKING_LEAK, L.TOOL_LEAK]),
    ('Human-judgment only', [L.LEAKS_ANSWER, L.IGNORES_STUDENT,
                             L.OFF_TOPIC, L.REPEATS]),
]


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------

@staff_member_required
def benchmark_list(request):
    items_qs = BenchmarkItem.objects.all().order_by('-created_at')

    items = []
    for item in items_qs:
        latest = item.annotations.order_by('-updated_at').first()
        items.append({
            'item_id': item.item_id,
            'subject': item.subject,
            'lesson_title': item.snapshot.get('item', {}).get('lesson_title', ''),
            'stratum': item.stratum,
            'annotation_count': item.annotations.count(),
            'latest': latest,
            'passes': latest.passes if latest else None,
            'failure_category': latest.failure_category if latest else '',
        })

    # Count tutor turns currently eligible for sampling (post-2.2.5
    # instrumentation, not yet sampled). Drives the "X more available"
    # hint on the sample form. Cheap COUNT(*) — fine to do per request.
    eligible_new = candidate_tutor_turns(
        require_full_tracking=True,
    ).count()

    return render(request, 'benchmark/list.html', {
        'items': items,
        'total': len(items),
        'eligible_new': eligible_new,
    })


@staff_member_required
@require_POST
def benchmark_sample_create(request):
    """POST endpoint: sample N new BenchmarkItems on demand.

    Form fields:
        count (int, 1-50): how many items to add.
        include_legacy ('on' | absent): opt into pre-2.2.5 traces.

    Idempotent against the existing pool — already-sampled turns are
    excluded automatically. Sets ``created_by`` to the requesting user.
    """
    try:
        count = int(request.POST.get('count') or 10)
    except ValueError:
        count = 10
    count = max(1, min(count, 50))
    include_legacy = bool(request.POST.get('include_legacy'))

    result = create_benchmark_items(
        limit=count,
        require_full_tracking=not include_legacy,
        created_by=request.user,
    )

    created = result['created']
    eligible = result['eligible']
    if created == 0 and eligible == 0:
        cohort_msg = (
            "no legacy turns to fall back to"
            if include_legacy
            else "run a few new tutor sessions first, then try again"
        )
        messages.warning(
            request,
            f"Sampled 0 items — no eligible new tutor turns available ({cohort_msg}).",
        )
    elif created == 0:
        messages.warning(
            request,
            f"Sampled 0 new items. {eligible} eligible turns remain — try a "
            "smaller count or `--include-legacy` to widen the pool.",
        )
    else:
        breakdown = ", ".join(
            f"{n} {name}"
            for name, n in sorted(result['stratum_breakdown'].items())
        )
        messages.success(
            request,
            f"Sampled {created} new item{'s' if created != 1 else ''} "
            f"({breakdown}). {eligible - created} eligible turns still "
            "in the pool.",
        )

    return redirect('dashboard:benchmark:list')


# ---------------------------------------------------------------------------
# Annotate view
# ---------------------------------------------------------------------------

@staff_member_required
def benchmark_annotate(request, item_id: str):
    item = get_object_or_404(BenchmarkItem, item_id=item_id)
    snapshot = item.snapshot or {}
    item_block = snapshot.get('item', {})
    production = snapshot.get('production', {})

    # Locked v1 identity
    system_variant = BenchmarkAnnotation.SystemVariant.PRODUCTION_V1
    annotator_role = BenchmarkAnnotation.Annotator.HUMAN

    existing = BenchmarkAnnotation.objects.filter(
        item=item,
        system_variant=system_variant,
        annotator_role=annotator_role,
        annotator_user=request.user,
        annotator_model='',
    ).first()

    if request.method == 'POST':
        actual_labels = sorted(set(request.POST.getlist('actual_labels')))
        expected_labels = sorted(set(request.POST.getlist('expected_labels')))
        rationale = (request.POST.get('rationale') or '').strip()
        failure_category = (request.POST.get('failure_category') or '').strip()
        safety_concern = request.POST.get('safety_concern') == 'on'

        claim_raw = request.POST.get('student_claim_correct', '')
        if claim_raw == 'true':
            student_claim_correct = True
        elif claim_raw == 'false':
            student_claim_correct = False
        else:
            student_claim_correct = None

        # Validate labels are in the known vocab — silently drop unknowns.
        actual_labels = [l for l in actual_labels if L.is_valid_label(l)]
        expected_labels = [l for l in expected_labels if L.is_valid_label(l)]
        if failure_category and not L.is_valid_failure_category(failure_category):
            failure_category = ''

        ann, _created = BenchmarkAnnotation.objects.update_or_create(
            item=item,
            system_variant=system_variant,
            annotator_role=annotator_role,
            annotator_user=request.user,
            annotator_model='',
            defaults={
                'student_claim_correct': student_claim_correct,
                'actual_labels': actual_labels,
                'expected_labels': expected_labels,
                'safety_concern': safety_concern,
                'rationale': rationale,
                'failure_category': failure_category,
            },
        )

        verdict = 'pass' if ann.passes else 'fail'
        messages.success(
            request,
            f"Saved annotation for {item.item_id} — verdict: {verdict}.",
        )

        # Save-and-next: jump to the next BenchmarkItem with no
        # annotation by this annotator. Falls back to the list.
        annotated_ids = BenchmarkAnnotation.objects.filter(
            system_variant=system_variant,
            annotator_role=annotator_role,
            annotator_user=request.user,
        ).values_list('item_id', flat=True)
        next_item = (
            BenchmarkItem.objects
            .exclude(id__in=list(annotated_ids))
            .order_by('created_at')
            .first()
        )
        if next_item:
            return redirect('dashboard:benchmark:annotate', item_id=next_item.item_id)
        return redirect('dashboard:benchmark:list')

    # GET — prefill from existing annotation or from suggested_labels.
    suggested = production.get('suggested_labels') or []
    if existing:
        prefill_actual = existing.actual_labels or []
        prefill_expected = existing.expected_labels or []
        prefill_rationale = existing.rationale
        prefill_category = existing.failure_category
        prefill_safety = existing.safety_concern
        prefill_claim = existing.student_claim_correct
    else:
        prefill_actual = list(suggested)
        prefill_expected = []
        prefill_rationale = ''
        prefill_category = ''
        prefill_safety = False
        prefill_claim = None

    prefill_actual_set = set(prefill_actual)
    prefill_expected_set = set(prefill_expected)

    action_labels = [
        {
            'key': lbl,
            'actual_checked': lbl in prefill_actual_set,
            'expected_checked': lbl in prefill_expected_set,
            'suggested': lbl in suggested,
        }
        for lbl in L.ACTION_LABELS
    ]
    issue_groups = [
        {
            'name': group_name,
            'labels': [
                {
                    'key': lbl,
                    'actual_checked': lbl in prefill_actual_set,
                    'expected_checked': lbl in prefill_expected_set,
                    'suggested': lbl in suggested,
                }
                for lbl in group_labels
            ],
        }
        for group_name, group_labels in ISSUE_LABEL_GROUPS
    ]

    pipeline_trace = production.get('pipeline_trace', {})

    return render(request, 'benchmark/annotate.html', {
        'item': item,
        'snapshot_item': item_block,
        'tutor_response': production.get('tutor_response', ''),
        'attached_media': production.get('attached_media') or [],
        'pipeline_trace': pipeline_trace,
        'judge_outputs': pipeline_trace.get('judge_outputs', {}),
        'suggested_labels': suggested,
        'action_labels': action_labels,
        'issue_groups': issue_groups,
        'failure_categories': L.FAILURE_CATEGORIES,
        'prefill_rationale': prefill_rationale,
        'prefill_category': prefill_category,
        'prefill_safety': prefill_safety,
        'prefill_claim': prefill_claim,
        'existing': existing,
    })


# ---------------------------------------------------------------------------
# Scores dashboard (Phase 2.3)
# ---------------------------------------------------------------------------

@staff_member_required
def benchmark_runs_list(request):
    """List all BenchmarkRuns, newest first."""
    runs = list(BenchmarkRun.objects.all().order_by('-created_at')[:200])
    return render(request, 'benchmark/runs_list.html', {
        'runs': runs,
        'total': len(runs),
    })


@staff_member_required
def benchmark_run_detail(request, run_id: int):
    """Render the full metrics breakdown for one BenchmarkRun."""
    run = get_object_or_404(BenchmarkRun, id=run_id)
    metrics = run.metrics or {}

    # Reshape slices for the template (sorted buckets per slice).
    slices = []
    for slice_name, buckets in (metrics.get('slices') or {}).items():
        rows = [
            {'bucket': name, **stats}
            for name, stats in sorted(buckets.items())
        ]
        slices.append({'name': slice_name, 'rows': rows})

    failure_categories = list(
        (metrics.get('failure_categories') or {}).items()
    )

    return render(request, 'benchmark/run_detail.html', {
        'run': run,
        'metrics': metrics,
        'overall': metrics.get('overall') or {},
        'slices': slices,
        'failure_categories': failure_categories,
        'agreement': metrics.get('agreement'),
    })
