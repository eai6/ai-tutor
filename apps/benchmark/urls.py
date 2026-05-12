"""Benchmark URL config — mounted under /dashboard/benchmark/."""
from django.urls import path

from apps.benchmark import views

app_name = 'benchmark'

urlpatterns = [
    path('', views.benchmark_list, name='list'),
    path('<str:item_id>/', views.benchmark_annotate, name='annotate'),
]
