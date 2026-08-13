"""Tests for BenchmarkItem delete + list filters.

Per-item delete (POST /<item_id>/delete/), bulk delete (POST
/bulk-delete/ with item_ids), and list filters (subject / stratum /
status query params).
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.benchmark.models import BenchmarkAnnotation, BenchmarkItem


def _make_item(item_id: str, subject: str = 'math',
               stratum: str = 'random', **extra):
    defaults = dict(
        item_id=item_id,
        lesson_id=1,
        subject=subject,
        stratum=stratum,
        snapshot={
            'item': {'lesson_title': 'L'},
            'production': {'pipeline_trace': {}},
        },
    )
    defaults.update(extra)
    return BenchmarkItem.objects.create(**defaults)


class ItemDeleteTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin-del', email='a@b.c', password='x',
        )
        self.client.force_login(self.admin)

    def test_per_item_delete_removes_row_and_cascades_annotations(self):
        item = _make_item('MATH_S1_T1')
        BenchmarkAnnotation.objects.create(
            item=item,
            annotator_role='human',
            annotator_user=self.admin,
            annotator_model='',
            system_variant='production_v1',
            actual_labels=['PROBE'],
            expected_labels=['PROBE'],
        )
        self.assertEqual(BenchmarkItem.objects.count(), 1)
        self.assertEqual(BenchmarkAnnotation.objects.count(), 1)

        url = reverse('dashboard:benchmark:item_delete',
                      args=['MATH_S1_T1'])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url,
                         reverse('dashboard:benchmark:list'))
        self.assertEqual(BenchmarkItem.objects.count(), 0)
        self.assertEqual(BenchmarkAnnotation.objects.count(), 0)

    def test_per_item_delete_404_for_missing(self):
        url = reverse('dashboard:benchmark:item_delete',
                      args=['MATH_NOPE'])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_per_item_delete_get_not_allowed(self):
        _make_item('MATH_S1_T1')
        url = reverse('dashboard:benchmark:item_delete',
                      args=['MATH_S1_T1'])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_per_item_delete_requires_login(self):
        _make_item('MATH_S1_T1')
        self.client.logout()
        url = reverse('dashboard:benchmark:item_delete',
                      args=['MATH_S1_T1'])
        response = self.client.post(url)
        # staff_member_required redirects to admin login
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login', response.url)
        # Item still exists
        self.assertTrue(BenchmarkItem.objects.filter(
            item_id='MATH_S1_T1',
        ).exists())


class BulkDeleteTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin-bulk', email='a@b.c', password='x',
        )
        self.client.force_login(self.admin)
        self.url = reverse('dashboard:benchmark:bulk_delete')

    def test_bulk_delete_selected_items(self):
        _make_item('A1')
        _make_item('A2')
        _make_item('A3')
        # Delete A1 + A3
        response = self.client.post(self.url, {
            'item_ids': ['A1', 'A3'],
        })
        self.assertEqual(response.status_code, 302)
        remaining = list(
            BenchmarkItem.objects.values_list('item_id', flat=True)
        )
        self.assertEqual(remaining, ['A2'])

    def test_bulk_delete_empty_selection_is_noop(self):
        _make_item('A1')
        _make_item('A2')
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BenchmarkItem.objects.count(), 2)

    def test_bulk_delete_cascades_annotations(self):
        item = _make_item('A1')
        BenchmarkAnnotation.objects.create(
            item=item,
            annotator_role='human',
            annotator_user=self.admin,
            annotator_model='',
            system_variant='production_v1',
            actual_labels=[],
            expected_labels=[],
        )
        self.client.post(self.url, {'item_ids': ['A1']})
        self.assertEqual(BenchmarkItem.objects.count(), 0)
        self.assertEqual(BenchmarkAnnotation.objects.count(), 0)

    def test_bulk_delete_get_not_allowed(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)


class ListFiltersTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin-filter', email='a@b.c', password='x',
        )
        self.client.force_login(self.admin)
        self.list_url = reverse('dashboard:benchmark:list')

        # Mix of subjects, strata, annotation status
        _make_item('M_WA', subject='math', stratum='wrong_answer')
        _make_item('M_RA', subject='math', stratum='random')
        _make_item('G_WA', subject='geography', stratum='wrong_answer')
        item_with_ann = _make_item(
            'G_RA', subject='geography', stratum='random',
        )
        BenchmarkAnnotation.objects.create(
            item=item_with_ann,
            annotator_role='human',
            annotator_user=self.admin,
            annotator_model='',
            system_variant='production_v1',
            actual_labels=['PROBE'],
            expected_labels=['PROBE'],
        )

    def test_no_filter_returns_all(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'M_WA')
        self.assertContains(response, 'G_RA')
        self.assertContains(response, 'M_RA')
        self.assertContains(response, 'G_WA')

    def test_subject_filter(self):
        response = self.client.get(self.list_url + '?subject=math')
        # Math items only
        self.assertContains(response, 'M_WA')
        self.assertContains(response, 'M_RA')
        self.assertNotContains(response, 'G_WA')
        self.assertNotContains(response, 'G_RA')

    def test_stratum_filter(self):
        response = self.client.get(
            self.list_url + '?stratum=wrong_answer',
        )
        # wrong_answer items only
        self.assertContains(response, 'M_WA')
        self.assertContains(response, 'G_WA')
        self.assertNotContains(response, 'M_RA')
        self.assertNotContains(response, 'G_RA')

    def test_status_unannotated_filter(self):
        response = self.client.get(self.list_url + '?status=unannotated')
        # Only items without annotations — G_RA has an annotation.
        self.assertContains(response, 'M_WA')
        self.assertContains(response, 'M_RA')
        self.assertContains(response, 'G_WA')
        self.assertNotContains(response, 'G_RA')

    def test_status_annotated_filter(self):
        response = self.client.get(self.list_url + '?status=annotated')
        self.assertContains(response, 'G_RA')
        self.assertNotContains(response, 'M_WA')

    def test_combined_filters(self):
        # math + unannotated → M_WA + M_RA (G_RA excluded by subject,
        # and even if subject matched, it's annotated)
        response = self.client.get(
            self.list_url + '?subject=math&status=unannotated',
        )
        self.assertContains(response, 'M_WA')
        self.assertContains(response, 'M_RA')
        self.assertNotContains(response, 'G_WA')
        self.assertNotContains(response, 'G_RA')
