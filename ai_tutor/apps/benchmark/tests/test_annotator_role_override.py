"""Tests for the annotator_role / annotator_model query-string override.

The benchmark_annotate view defaults to role='human' so genuine teacher
annotations are never accidentally re-tagged as agent-driven. The
automated annotator agent appends ?annotator_role=llm_judge&
annotator_model=<id> to its URLs so its annotations land in a separate
cohort. The save-and-next redirect must carry the override forward.

Pinned here so a future refactor doesn't silently flip the default.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ai_tutor.apps.benchmark.models import BenchmarkAnnotation, BenchmarkItem


def _make_item(item_id: str = 'M1', subject: str = 'math'):
    return BenchmarkItem.objects.create(
        item_id=item_id,
        lesson_id=1,
        subject=subject,
        stratum='random',
        snapshot={
            'item': {'lesson_title': 'L'},
            'production': {'pipeline_trace': {}, 'suggested_labels': []},
        },
    )


class AnnotatorRoleOverrideTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin-role', email='a@b.c', password='x',
        )
        self.client.force_login(self.admin)
        self.item = _make_item('M1')

    def test_default_role_is_human(self):
        url = reverse('dashboard:benchmark:annotate', args=['M1'])
        response = self.client.post(url, {
            'actual_labels': ['PROBE'],
            'expected_labels': ['PROBE'],
            'rationale': '',
        })
        self.assertEqual(response.status_code, 302)
        ann = BenchmarkAnnotation.objects.get()
        self.assertEqual(ann.annotator_role, 'human')
        self.assertEqual(ann.annotator_model, '')

    def test_query_string_role_override_lands_as_llm_judge(self):
        url = reverse('dashboard:benchmark:annotate', args=['M1'])
        response = self.client.post(
            f"{url}?annotator_role=llm_judge&annotator_model=claude-sonnet-4-5",
            {
                'actual_labels': ['PROBE'],
                'expected_labels': ['PROBE'],
                'rationale': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        ann = BenchmarkAnnotation.objects.get()
        self.assertEqual(ann.annotator_role, 'llm_judge')
        self.assertEqual(ann.annotator_model, 'claude-sonnet-4-5')

    def test_invalid_role_falls_back_to_human(self):
        url = reverse('dashboard:benchmark:annotate', args=['M1'])
        response = self.client.post(
            f"{url}?annotator_role=intern_who_just_started",
            {'actual_labels': ['PROBE'], 'expected_labels': ['PROBE'],
             'rationale': ''},
        )
        self.assertEqual(response.status_code, 302)
        ann = BenchmarkAnnotation.objects.get()
        self.assertEqual(ann.annotator_role, 'human')

    def test_save_and_next_preserves_role_override(self):
        # Two items so save-and-next has somewhere to go.
        _make_item('M2')
        url = reverse('dashboard:benchmark:annotate', args=['M1'])
        response = self.client.post(
            f"{url}?annotator_role=llm_judge&annotator_model=claude-sonnet-4-5",
            {'actual_labels': ['PROBE'], 'expected_labels': ['PROBE'],
             'rationale': ''},
        )
        # Redirected to next item; URL must keep the override params.
        self.assertEqual(response.status_code, 302)
        self.assertIn('annotator_role=llm_judge', response.url)
        self.assertIn('annotator_model=claude-sonnet-4-5', response.url)

    def test_human_and_llm_judge_coexist_for_same_item(self):
        url = reverse('dashboard:benchmark:annotate', args=['M1'])
        # Human annotation
        self.client.post(url, {
            'actual_labels': ['PROBE'], 'expected_labels': ['PROBE'],
            'rationale': '',
        })
        # Agent annotation under same admin user
        self.client.post(
            f"{url}?annotator_role=llm_judge&annotator_model=claude-sonnet-4-5",
            {'actual_labels': ['ADVANCE'], 'expected_labels': ['PROBE'],
             'rationale': ''},
        )
        anns = list(BenchmarkAnnotation.objects.filter(item=self.item))
        self.assertEqual(len(anns), 2)
        roles = {a.annotator_role for a in anns}
        self.assertEqual(roles, {'human', 'llm_judge'})
