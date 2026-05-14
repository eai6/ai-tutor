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
- failure_categories multi-select locked to ``FAILURE_CATEGORIES`` (no free text).
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
def benchmark_export_annotations(request):
    """GET endpoint: stream a CSV of annotations matching the same
    filters as the list page (`subject`, `stratum`, `status`). One row
    per BenchmarkAnnotation; items with zero annotations are skipped.

    Used to pull a working snapshot of evaluator labels for
    paper-writing, external analysis, and inter-annotator-agreement
    work. Mirrors the list view's filter semantics so the user can
    "narrow the page to what I want, then download exactly that."
    """
    import csv
    import json
    from django.http import StreamingHttpResponse
    from django.utils import timezone as _tz

    f_subject = (request.GET.get('subject') or '').strip()
    f_stratum = (request.GET.get('stratum') or '').strip()
    f_status = (request.GET.get('status') or '').strip()

    items_qs = BenchmarkItem.objects.all()
    if f_subject:
        items_qs = items_qs.filter(subject=f_subject)
    if f_stratum:
        items_qs = items_qs.filter(stratum=f_stratum)
    if f_status == 'annotated':
        items_qs = items_qs.annotate(
            _ann_count=models.Count('annotations'),
        ).filter(_ann_count__gt=0)
    elif f_status == 'unannotated':
        # Unannotated items have no annotations — short-circuit to an
        # empty CSV (header only) so the user gets a downloadable
        # artifact rather than a confusing redirect.
        items_qs = items_qs.none()

    annotations = (
        BenchmarkAnnotation.objects
        .filter(item__in=items_qs)
        .select_related('item', 'annotator_user')
        .order_by('item__subject', 'item__item_id', 'annotator_role')
    )

    headers = [
        'item_id', 'subject', 'lesson_id', 'lesson_title',
        'stratum',
        'annotator_role', 'annotator_user', 'annotator_model',
        'system_variant',
        'student_claim_correct',
        'actual_labels', 'expected_labels',
        'failure_categories',
        'safety_concern',
        'rationale',
        'passes',
        'tutor_response_excerpt',
        'student_turn',
        'created_at', 'updated_at',
    ]

    class _Echo:
        """File-like sink used by csv.writer in streaming mode — see
        Django docs `https://docs.djangoproject.com/en/5.0/howto/outputting-csv/`."""
        def write(self, value):
            return value

    writer = csv.writer(_Echo())

    def _join_list(values):
        if not values:
            return ''
        # Use ; rather than , so CSV parsers don't choke on label commas
        return ';'.join(str(v) for v in values)

    def _rows():
        # Header row first
        yield writer.writerow(headers)
        for ann in annotations.iterator(chunk_size=200):
            item = ann.item
            snapshot = item.snapshot or {}
            tutor_response = (
                (snapshot.get('production') or {}).get('tutor_response') or ''
            )[:500]
            student_turn = (
                (snapshot.get('item') or {}).get('student_turn') or ''
            )
            lesson_title = (snapshot.get('item') or {}).get('lesson_title', '')
            yield writer.writerow([
                item.item_id,
                item.subject,
                item.lesson_id,
                lesson_title,
                item.stratum,
                ann.annotator_role,
                ann.annotator_user.username if ann.annotator_user else '',
                ann.annotator_model or '',
                ann.system_variant,
                '' if ann.student_claim_correct is None
                    else ('true' if ann.student_claim_correct else 'false'),
                _join_list(ann.actual_labels),
                _join_list(ann.expected_labels),
                _join_list(ann.failure_categories),
                'true' if ann.safety_concern else 'false',
                (ann.rationale or '').replace('\r', ' ').replace('\n', ' '),
                'true' if ann.passes else 'false',
                tutor_response.replace('\r', ' ').replace('\n', ' '),
                str(student_turn).replace('\r', ' ').replace('\n', ' '),
                ann.created_at.isoformat(),
                ann.updated_at.isoformat(),
            ])

    # Filename includes filter context + timestamp so multiple downloads
    # don't collide and the file's provenance is obvious from the name.
    parts = ['annotations']
    if f_subject:
        parts.append(f_subject)
    if f_stratum:
        parts.append(f_stratum)
    if f_status:
        parts.append(f_status)
    parts.append(_tz.now().strftime('%Y%m%d-%H%M%S'))
    filename = '-'.join(parts) + '.csv'

    response = StreamingHttpResponse(_rows(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


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
            'failure_categories': latest.failure_categories if latest else [],
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
        subject (str, optional): 'math' / 'geography' / 'science' filter.
        lesson_id (int, optional): restrict to one Lesson PK.
        since (date or datetime ISO string, optional): only sample
            from tutor turns created on/after this timestamp.
        until (date or datetime ISO string, optional): only sample
            from tutor turns created strictly before this timestamp.

    Idempotent against the existing pool — already-sampled turns are
    excluded automatically. Sets ``created_by`` to the requesting user.
    """
    from datetime import datetime, time
    from django.utils import timezone as _tz

    try:
        count = int(request.POST.get('count') or 10)
    except ValueError:
        count = 10
    count = max(1, min(count, 50))
    include_legacy = bool(request.POST.get('include_legacy'))
    include_synthetic = bool(request.POST.get('include_synthetic'))

    subject = (request.POST.get('subject') or '').strip() or None
    if subject not in (None, 'math', 'geography', 'science'):
        subject = None

    lesson_id_raw = (request.POST.get('lesson_id') or '').strip()
    try:
        lesson_id = int(lesson_id_raw) if lesson_id_raw else None
    except ValueError:
        lesson_id = None

    def _parse_dt(raw: str, end_of_day: bool = False):
        """Accept YYYY-MM-DD or full ISO; return a tz-aware datetime."""
        s = (raw or '').strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        # Date-only string: snap to start (since) or end (until) of day.
        if dt.time() == time(0, 0) and len(s) == 10:
            if end_of_day:
                # `until` is exclusive — bump to next-day midnight.
                from datetime import timedelta
                dt = dt + timedelta(days=1)
        if _tz.is_naive(dt):
            dt = _tz.make_aware(dt, _tz.get_current_timezone())
        return dt

    since = _parse_dt(request.POST.get('since', ''), end_of_day=False)
    until = _parse_dt(request.POST.get('until', ''), end_of_day=True)

    result = create_benchmark_items(
        limit=count,
        subject=subject,
        lesson_id=lesson_id,
        since=since,
        until=until,
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
        # Multi-select: each checkbox/option submits a separate value.
        # getlist tolerates the legacy single-value form too.
        failure_categories = sorted(set(
            (c or '').strip() for c in request.POST.getlist('failure_categories')
            if (c or '').strip()
        ))
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
        # Drop unknown categories silently, matching the label-validation pattern.
        failure_categories = [
            c for c in failure_categories if L.is_valid_failure_category(c)
        ]

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
                'failure_categories': failure_categories,
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
        prefill_categories = list(existing.failure_categories or [])
        prefill_safety = existing.safety_concern
        prefill_claim = existing.student_claim_correct
    else:
        prefill_actual = list(suggested)
        prefill_expected = []
        prefill_rationale = ''
        prefill_categories = []
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
        'failure_category_options': [
            {'key': c, 'checked': c in prefill_categories}
            for c in L.FAILURE_CATEGORIES
        ],
        'prefill_rationale': prefill_rationale,
        'prefill_categories': prefill_categories,
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
    """Render the metrics breakdown for one BenchmarkRun.

    By default shows the run's stored metrics blob (frozen at score
    time). When `?subject=math|geography|science` is set, recomputes
    metrics live against the subset of annotations whose item belongs
    to that subject — useful for drilling into per-subject failure
    patterns without re-scoring.
    """
    from apps.benchmark.scoring import compute_metrics

    run = get_object_or_404(BenchmarkRun, id=run_id)

    # ?subject= filter. Empty / unknown values fall back to "all".
    SUBJECT_OPTIONS = ('math', 'geography', 'science')
    subject_filter = (request.GET.get('subject') or '').strip().lower()
    if subject_filter not in SUBJECT_OPTIONS:
        subject_filter = ''

    if subject_filter:
        # Live recompute on the narrowed cohort. Same variant + role
        # the run was originally scored under; just narrow by item
        # subject. select_related('item') because compute_metrics
        # reads ann.item.subject + ann.item.snapshot.
        narrowed = list(
            BenchmarkAnnotation.objects.filter(
                system_variant=run.system_variant,
                annotator_role=run.annotator_role,
                item__subject=subject_filter,
            ).select_related('item')
        )
        metrics = compute_metrics(narrowed)
    else:
        metrics = run.metrics or {}

    # Reshape slices for the template (sorted buckets per slice).
    # Only by_subject is surfaced in the UI by default — by_stratum,
    # by_eval_layer and by_history are kept in metrics JSON for
    # programmatic access but hidden because they're noisy / mostly-
    # uniform on the current data shape.
    VISIBLE_SLICES = ('by_subject',)
    slices = []
    for slice_name, buckets in (metrics.get('slices') or {}).items():
        if slice_name not in VISIBLE_SLICES:
            continue
        rows = [
            {'bucket': name, **stats}
            for name, stats in sorted(buckets.items())
        ]
        slices.append({'name': slice_name, 'rows': rows})

    # Failure-category counts. Note: an annotation can carry MULTIPLE
    # categories (multi-select), so the counter sums to MORE than
    # `failed`. We surface the totals on the page so the relationship
    # is explicit. Filter out empty / non-positive counts defensively
    # so a malformed entry can't render as a label-with-no-number.
    raw_categories = list(
        (metrics.get('failure_categories') or {}).items()
    )
    failure_categories = [
        (cat, int(count))
        for cat, count in raw_categories
        if cat and isinstance(count, (int, float)) and count > 0
    ]
    failed_total = (metrics.get('overall') or {}).get('failed', 0)
    category_tag_total = sum(c for _, c in failure_categories)

    # Subjects available in this run — drives the filter pills. Pull
    # from the stored metrics (always reflects the full run, even when
    # a subject narrow is active so the user can still see other
    # options).
    full_metrics = run.metrics or {}
    available_subjects = sorted(
        ((full_metrics.get('slices') or {}).get('by_subject') or {}).keys()
    )

    return render(request, 'benchmark/run_detail.html', {
        'run': run,
        'metrics': metrics,
        'overall': metrics.get('overall') or {},
        'slices': slices,
        'failure_categories': failure_categories,
        'failed_total': failed_total,
        'category_tag_total': category_tag_total,
        'agreement': metrics.get('agreement'),
        'subject_filter': subject_filter,
        'available_subjects': available_subjects,
    })
