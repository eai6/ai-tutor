"""Tests for the on-demand scoring view (POST /scores/run-now/).

Mirrors `python manage.py score_benchmark` from the UI. Covers:

- Empty annotation set → error message + redirect to runs_list, no
  BenchmarkRun created.
- Annotations present → BenchmarkRun created, redirected to detail page.
- runs_list view exposes annotation counts so the disabled-button copy
  has data to render.
- GET on the score-now endpoint is rejected (POST-only).
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from apps.benchmark.models import (
    BenchmarkAnnotation,
    BenchmarkItem,
    BenchmarkRun,
)


def _make_item(item_id: str, subject: str = 'math',
               stratum: str = 'random'):
    return BenchmarkItem.objects.create(
        item_id=item_id,
        lesson_id=1,
        subject=subject,
        stratum=stratum,
        snapshot={
            'item': {'lesson_title': 'L'},
            'production': {'pipeline_trace': {}},
        },
    )


def _make_annotation(item, *, user, actual=None, expected=None,
                     role='human', model=''):
    return BenchmarkAnnotation.objects.create(
        item=item,
        annotator_role=role,
        annotator_user=user,
        annotator_model=model,
        system_variant='production_v1',
        actual_labels=actual or [],
        expected_labels=expected or [],
    )


class ScoreNowTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin-score', email='a@b.c', password='x',
        )
        self.client.force_login(self.admin)
        self.url = reverse('dashboard:benchmark:score_now')

    def test_no_annotations_shows_error_and_creates_no_run(self):
        # Item exists but no annotations on it.
        _make_item('M1')
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url,
                         reverse('dashboard:benchmark:runs_list'))
        self.assertEqual(BenchmarkRun.objects.count(), 0)
        # Surface the error on the next page so the user knows why.
        followup = self.client.get(response.url)
        self.assertContains(followup, 'No human annotations')

    def test_with_annotations_creates_run_and_redirects_to_detail(self):
        item = _make_item('M1')
        _make_annotation(
            item, user=self.admin,
            actual=['PROBE'], expected=['PROBE'],
        )
        response = self.client.post(self.url, {'notes': 'after probe-strip'})
        self.assertEqual(response.status_code, 302)
        run = BenchmarkRun.objects.get()
        self.assertEqual(run.total_items, 1)
        self.assertEqual(run.passed, 1)
        self.assertEqual(run.failed, 0)
        self.assertEqual(run.system_variant, 'production_v1')
        self.assertEqual(run.annotator_role, 'human')
        self.assertEqual(run.notes, 'after probe-strip')
        # metrics blob was populated by compute_metrics.
        self.assertIn('overall', run.metrics)
        self.assertEqual(response.url,
                         reverse('dashboard:benchmark:run_detail',
                                 args=[run.id]))

    def test_failed_annotation_is_recorded(self):
        item = _make_item('M1')
        _make_annotation(
            item, user=self.admin,
            actual=['ADVANCE'], expected=['PROBE'],
        )
        self.client.post(self.url)
        run = BenchmarkRun.objects.get()
        self.assertEqual(run.passed, 0)
        self.assertEqual(run.failed, 1)

    def test_get_not_allowed(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_requires_staff(self):
        self.client.logout()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login', response.url)
        self.assertEqual(BenchmarkRun.objects.count(), 0)


class RunsListAnnotationCountsTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin-runs', email='a@b.c', password='x',
        )
        self.client.force_login(self.admin)
        self.url = reverse('dashboard:benchmark:runs_list')

    def test_counts_human_and_llm_separately(self):
        item = _make_item('M1')
        _make_annotation(item, user=self.admin,
                         actual=['PROBE'], expected=['PROBE'])
        _make_annotation(item, user=None, model='gemini-2.5-pro',
                         role='llm_judge',
                         actual=['PROBE'], expected=['PROBE'])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['human_annotation_count'], 1)
        self.assertEqual(response.context['llm_annotation_count'], 1)

    def test_zero_counts_when_no_annotations(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context['human_annotation_count'], 0)
        self.assertEqual(response.context['llm_annotation_count'], 0)
        # Empty-state copy nudges the user toward annotation, not scoring.
        self.assertContains(response, 'Annotate at least one item')
