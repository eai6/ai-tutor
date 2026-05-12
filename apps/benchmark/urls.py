"""Benchmark URL config — mounted under /dashboard/benchmark/."""
from django.urls import path

from apps.benchmark import views

app_name = 'benchmark'

urlpatterns = [
    path('', views.benchmark_list, name='list'),
    # Scoring dashboard (Phase 2.3). Listed BEFORE the catch-all
    # <item_id> route so 'scores' doesn't resolve as an item_id.
    path('scores/', views.benchmark_runs_list, name='runs_list'),
    path('scores/<int:run_id>/', views.benchmark_run_detail,
         name='run_detail'),
    path('<str:item_id>/', views.benchmark_annotate, name='annotate'),
]
