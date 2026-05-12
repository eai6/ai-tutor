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

from apps.benchmark import labels as L
from apps.benchmark.models import BenchmarkAnnotation, BenchmarkItem


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

    return render(request, 'benchmark/list.html', {
        'items': items,
        'total': len(items),
    })


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
