"""Benchmark URL config — mounted under /dashboard/benchmark/."""
from django.urls import path

from ai_tutor.apps.benchmark import views

app_name = 'benchmark'

urlpatterns = [
    path('', views.benchmark_list, name='list'),
    # Sampling endpoint — POST creates N BenchmarkItems from the pool
    # of eligible new tutor turns. Listed BEFORE the catch-all
    # <item_id> route so 'sample' / 'scores' don't resolve as item_ids.
    path('sample/', views.benchmark_sample_create, name='sample_create'),
    # Bulk-delete selected items (checkbox-driven).
    path('bulk-delete/',
         views.benchmark_items_bulk_delete,
         name='bulk_delete'),
    # CSV export — honors the same subject/stratum/status filters the
    # list page accepts. GET so it can be linked from a button without
    # needing a CSRF token.
    path('export.csv',
         views.benchmark_export_annotations,
         name='export_annotations'),
    # JSONL export — BEA 2023 + 2025 compatible, for ML eval pipelines /
    # SIG-EDU shared-task submissions. Same filter semantics as CSV.
    path('export.jsonl',
         views.benchmark_export_jsonl,
         name='export_jsonl'),
    # Scoring dashboard (Phase 2.3).
    path('scores/', views.benchmark_runs_list, name='runs_list'),
    path('scores/run-now/', views.benchmark_score_now, name='score_now'),
    path('scores/<int:run_id>/', views.benchmark_run_detail,
         name='run_detail'),
    # Session-level pedagogical evaluation (Phase 2 of
    # memory/session_eval_framework_plan.md). Listed BEFORE the <item_id>
    # catch-all so 'sessions' does not resolve as a turn-level item_id.
    path('sessions/', views.session_eval_list, name='session_list'),
    path('sessions/sample/', views.session_eval_sample_create,
         name='session_sample_create'),
    path('sessions/scores/', views.session_eval_scores, name='session_scores'),
    path('sessions/export.jsonl', views.session_eval_export_jsonl,
         name='session_export_jsonl'),
    path('sessions/<str:item_id>/review/', views.session_eval_review,
         name='session_review'),
    path('sessions/<str:item_id>/', views.session_eval_annotate,
         name='session_annotate'),
    path('<str:item_id>/', views.benchmark_annotate, name='annotate'),
    # Per-item delete — last so 'delete' doesn't shadow item_ids.
    path('<str:item_id>/delete/', views.benchmark_item_delete,
         name='item_delete'),
]
