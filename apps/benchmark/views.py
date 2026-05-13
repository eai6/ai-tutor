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
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
    # Filter query params — empty/missing means "no filter".
    f_subject = (request.GET.get('subject') or '').strip()
    f_stratum = (request.GET.get('stratum') or '').strip()
    f_status = (request.GET.get('status') or '').strip()  # annotated|unannotated

    items_qs = BenchmarkItem.objects.all().order_by('-created_at')
    if f_subject:
        items_qs = items_qs.filter(subject=f_subject)
    if f_stratum:
        items_qs = items_qs.filter(stratum=f_stratum)
    if f_status == 'annotated':
        items_qs = items_qs.annotate(
            _ann_count=models.Count('annotations'),
        ).filter(_ann_count__gt=0)
    elif f_status == 'unannotated':
        items_qs = items_qs.annotate(
            _ann_count=models.Count('annotations'),
        ).filter(_ann_count=0)

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

    # Filter dropdown values — show only what's actually present in
    # the data so the dropdowns don't list dead options.
    distinct_subjects = sorted(set(
        BenchmarkItem.objects.values_list('subject', flat=True)
    ))
    distinct_strata = sorted(set(
        s for s in BenchmarkItem.objects.values_list('stratum', flat=True)
        if s
    ))

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
        'filters': {
            'subject': f_subject,
            'stratum': f_stratum,
            'status': f_status,
        },
        'distinct_subjects': distinct_subjects,
        'distinct_strata': distinct_strata,
    })


@staff_member_required
@require_POST
def benchmark_item_delete(request, item_id: str):
    """Delete one BenchmarkItem (cascades its annotations).

    POST-only with CSRF; the underlying SessionTurn is NOT touched —
    only the snapshot row. The source turn becomes eligible for
    re-sampling automatically (the sampler excludes items by
    source_turn_id, which now no longer references this row).
    """
    item = get_object_or_404(BenchmarkItem, item_id=item_id)
    ann_count = item.annotations.count()
    item.delete()
    if ann_count:
        messages.success(
            request,
            f"Deleted {item_id} (and {ann_count} annotation"
            f"{'s' if ann_count != 1 else ''}).",
        )
    else:
        messages.success(request, f"Deleted {item_id}.")
    return redirect('dashboard:benchmark:list')


@staff_member_required
@require_POST
def benchmark_items_bulk_delete(request):
    """Delete a user-selected set of BenchmarkItems.

    Form fields:
        item_ids: list of item_id values (checkboxes from list page).

    Cascades each item's annotations. The underlying SessionTurns
    are NOT touched — they become eligible for re-sampling.
    """
    item_ids = request.POST.getlist('item_ids')
    if not item_ids:
        messages.info(request, "Nothing selected.")
        return redirect('dashboard:benchmark:list')
    qs = BenchmarkItem.objects.filter(item_id__in=item_ids)
    count = qs.count()
    if count == 0:
        messages.warning(request, "No matching items found.")
    else:
        qs.delete()
        messages.success(
            request,
            f"Deleted {count} item{'s' if count != 1 else ''}.",
        )
    return redirect('dashboard:benchmark:list')


@staff_member_required
@require_POST
def benchmark_sample_create(request):
    """POST endpoint: sample N new BenchmarkItems on demand.

    Form fields:
        count (int, 1-50): how many items to add.
        include_legacy ('on' | absent): opt into pre-2.2.5 traces.
        include_synthetic ('on' | absent): opt into simulator-generated
            sessions (default: real-student only).

    Idempotent against the existing pool — already-sampled turns are
    excluded automatically. Sets ``created_by`` to the requesting user.
    """
    try:
        count = int(request.POST.get('count') or 10)
    except ValueError:
        count = 10
    count = max(1, min(count, 50))
    include_legacy = bool(request.POST.get('include_legacy'))
    include_synthetic = bool(request.POST.get('include_synthetic'))

    result = create_benchmark_items(
        limit=count,
        require_full_tracking=not include_legacy,
        include_synthetic=include_synthetic,
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

    system_variant = BenchmarkAnnotation.SystemVariant.PRODUCTION_V1

    # Annotator role + model can be overridden via query string for the
    # automated annotator agent (`?annotator_role=llm_judge&annotator_model=
    # claude-sonnet-4-5`). Default is HUMAN so genuine teacher annotations
    # are never accidentally re-tagged as agent-driven. The role override
    # is honoured on both GET (for the existing-annotation lookup) and
    # POST (for the saved row) so the form's prefill matches what the
    # agent will write.
    role_override = (
        request.GET.get('annotator_role')
        or request.POST.get('annotator_role')
        or ''
    ).strip().lower()
    if role_override in BenchmarkAnnotation.Annotator.values:
        annotator_role = role_override
    else:
        annotator_role = BenchmarkAnnotation.Annotator.HUMAN
    annotator_model = (
        request.GET.get('annotator_model')
        or request.POST.get('annotator_model')
        or ''
    ).strip()[:80]

    existing = BenchmarkAnnotation.objects.filter(
        item=item,
        system_variant=system_variant,
        annotator_role=annotator_role,
        annotator_user=request.user,
        annotator_model=annotator_model,
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
            annotator_model=annotator_model,
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
            url = reverse('dashboard:benchmark:annotate',
                          args=[next_item.item_id])
            # Preserve role/model override across the save-and-next hop
            # so the agent doesn't silently revert to HUMAN on item #2.
            qs_parts = []
            if annotator_role != BenchmarkAnnotation.Annotator.HUMAN:
                qs_parts.append(f"annotator_role={annotator_role}")
            if annotator_model:
                qs_parts.append(f"annotator_model={annotator_model}")
            if qs_parts:
                url = f"{url}?{'&'.join(qs_parts)}"
            return redirect(url)
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
        'regen_audit': pipeline_trace.get('regen_audit') or {},
        'prompt_pack': pipeline_trace.get('prompt_pack') or {},
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
    # Show pending-annotation counts so the user can see whether there
    # is enough labelled data to score against.
    human_count = BenchmarkAnnotation.objects.filter(
        annotator_role=BenchmarkAnnotation.Annotator.HUMAN,
        system_variant=BenchmarkAnnotation.SystemVariant.PRODUCTION_V1,
    ).count()
    llm_count = BenchmarkAnnotation.objects.filter(
        annotator_role=BenchmarkAnnotation.Annotator.LLM_JUDGE,
        system_variant=BenchmarkAnnotation.SystemVariant.PRODUCTION_V1,
    ).count()
    return render(request, 'benchmark/runs_list.html', {
        'runs': runs,
        'total': len(runs),
        'human_annotation_count': human_count,
        'llm_annotation_count': llm_count,
    })


@staff_member_required
@require_POST
def benchmark_score_now(request):
    """Compute a BenchmarkRun on demand from existing annotations.

    Mirrors `python manage.py score_benchmark` but lets the user kick
    off scoring from the UI after annotating items. Defaults to the
    human-annotated production_v1 set — the CLI is still available for
    custom variants / LLM-judge runs.
    """
    from apps.benchmark.scoring import compute_metrics

    annotator_role = request.POST.get(
        'annotator_role',
        BenchmarkAnnotation.Annotator.HUMAN,
    )
    system_variant = request.POST.get(
        'system_variant',
        BenchmarkAnnotation.SystemVariant.PRODUCTION_V1,
    )
    notes = (request.POST.get('notes') or '').strip()[:500]

    primary = list(
        BenchmarkAnnotation.objects.filter(
            system_variant=system_variant,
            annotator_role=annotator_role,
        ).select_related('item')
    )
    if not primary:
        messages.error(
            request,
            f"No {annotator_role} annotations for {system_variant} yet. "
            "Annotate at least one item before scoring.",
        )
        return redirect('dashboard:benchmark:runs_list')

    metrics = compute_metrics(primary)
    overall = metrics['overall']
    run = BenchmarkRun.objects.create(
        system_variant=system_variant,
        annotator_role=annotator_role,
        total_items=overall['total'],
        passed=overall['passed'],
        failed=overall['failed'],
        metrics=metrics,
        notes=notes,
    )
    messages.success(
        request,
        f"Scored {overall['passed']}/{overall['total']} "
        f"({overall['pass_rate'] * 100:.1f}%) — run #{run.id}",
    )
    return redirect('dashboard:benchmark:run_detail', run_id=run.id)


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
