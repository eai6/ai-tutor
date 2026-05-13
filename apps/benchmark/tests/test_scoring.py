"""Unit tests for apps/benchmark/scoring.py — pure function.

Tests build BenchmarkItem + BenchmarkAnnotation fixtures and assert the
metrics dict has the expected slices + agreement block.
"""
from django.test import TestCase

from apps.benchmark.models import BenchmarkAnnotation, BenchmarkItem
from apps.benchmark.scoring import compute_metrics


def _make_item(item_id: str, subject: str = 'math', stratum: str = 'random',
               *, eval_layer: str = 'llm', history_turns: int = 0):
    return BenchmarkItem.objects.create(
        item_id=item_id,
        lesson_id=1,
        subject=subject,
        stratum=stratum,
        snapshot={
            'item': {'lesson_title': 'L'},
            'production': {
                'pipeline_trace': {
                    'eval_layer': eval_layer,
                    'judge_history_turns': history_turns,
                },
            },
        },
    )


def _make_annotation(item: BenchmarkItem, *,
                     actual=None, expected=None,
                     role='human', model='', user=None,
                     variant='production_v1',
                     failure_categories=None, safety=False,
                     # Backwards-compat shim: tests still passing the
                     # singular form get auto-promoted to a list.
                     failure_category=None):
    if failure_categories is None:
        failure_categories = (
            [failure_category] if failure_category else []
        )
    return BenchmarkAnnotation.objects.create(
        item=item,
        annotator_role=role,
        annotator_user=user,
        annotator_model=model,
        system_variant=variant,
        actual_labels=actual or [],
        expected_labels=expected or [],
        failure_categories=failure_categories,
        safety_concern=safety,
    )


class ComputeMetricsTest(TestCase):
    def test_empty(self):
        m = compute_metrics([])
        self.assertEqual(m['overall']['total'], 0)
        self.assertEqual(m['overall']['pass_rate'], 0.0)
        self.assertNotIn('agreement', m)

    def test_all_pass(self):
        it = _make_item('I1')
        a = _make_annotation(it, actual=['PROBE'], expected=['PROBE'])
        m = compute_metrics([a])
        self.assertEqual(m['overall']['total'], 1)
        self.assertEqual(m['overall']['passed'], 1)
        self.assertEqual(m['overall']['failed'], 0)
        self.assertEqual(m['overall']['pass_rate'], 1.0)

    def test_safety_concern_forces_fail(self):
        it = _make_item('I1')
        a = _make_annotation(it, actual=['PROBE'], expected=['PROBE'],
                             safety=True)
        m = compute_metrics([a])
        self.assertEqual(m['overall']['passed'], 0)
        self.assertEqual(m['overall']['failed'], 1)

    def test_subject_slice(self):
        m_it = _make_item('M1', subject='math')
        g_it = _make_item('G1', subject='geography')
        _make_annotation(m_it, actual=['PROBE'], expected=['PROBE'])
        _make_annotation(g_it, actual=['PROBE'], expected=['ADVANCE'],
                         failure_category='premature_advance')

        anns = list(BenchmarkAnnotation.objects.all())
        m = compute_metrics(anns)
        slices = m['slices']['by_subject']
        self.assertEqual(slices['math']['passed'], 1)
        self.assertEqual(slices['math']['pass_rate'], 1.0)
        self.assertEqual(slices['geography']['passed'], 0)
        self.assertEqual(slices['geography']['pass_rate'], 0.0)

    def test_eval_layer_and_history_slices(self):
        a_it = _make_item('A1', eval_layer='deterministic_numeric',
                          history_turns=0)
        b_it = _make_item('B1', eval_layer='llm', history_turns=4)
        _make_annotation(a_it, actual=['ADVANCE'], expected=['ADVANCE'])
        _make_annotation(b_it, actual=['PROBE'], expected=['ADVANCE'],
                         failure_category='topic_jump')

        m = compute_metrics(list(BenchmarkAnnotation.objects.all()))
        eval_slice = m['slices']['by_eval_layer']
        self.assertIn('deterministic_numeric', eval_slice)
        self.assertIn('llm', eval_slice)
        history_slice = m['slices']['by_history']
        self.assertEqual(history_slice['no_history']['total'], 1)
        self.assertEqual(history_slice['history_aware']['total'], 1)

    def test_failure_categories_counts_failed_only(self):
        a_it = _make_item('A1')
        b_it = _make_item('B1')
        # Pass — should NOT be counted in failure_categories.
        _make_annotation(a_it, actual=['PROBE'], expected=['PROBE'],
                         failure_category='topic_jump')  # ignored
        # Fail — counted.
        _make_annotation(b_it, actual=['ADVANCE'], expected=['ASK_WORKING'],
                         failure_category='bare_answer_chain')

        m = compute_metrics(list(BenchmarkAnnotation.objects.all()))
        self.assertEqual(
            m['failure_categories'],
            {'bare_answer_chain': 1},
        )

    def test_agreement_overlap_only(self):
        a_it = _make_item('A1')
        b_it = _make_item('B1')
        c_it = _make_item('C1')

        human_a = _make_annotation(a_it, actual=['PROBE'], expected=['PROBE'])
        human_b = _make_annotation(b_it, actual=['PROBE'], expected=['ADVANCE'])
        # c has no human annotation — won't appear in agreement.

        llm_a = _make_annotation(a_it, actual=['PROBE'], expected=['PROBE'],
                                 role='llm_judge', model='gemini-2.5-pro')
        llm_b = _make_annotation(b_it, actual=['ADVANCE'], expected=['ADVANCE'],
                                 role='llm_judge', model='gemini-2.5-pro')
        llm_c = _make_annotation(c_it, actual=['PROBE'], expected=['PROBE'],
                                 role='llm_judge', model='gemini-2.5-pro')

        m = compute_metrics(
            [human_a, human_b],
            cross_check_annotations=[llm_a, llm_b, llm_c],
        )
        ag = m['agreement']
        self.assertEqual(ag['overlap_items'], 2)
        # human_a vs llm_a both pass; human_b fails, llm_b passes → disagree
        self.assertEqual(ag['agree'], 1)
        self.assertEqual(ag['disagree'], 1)
        self.assertEqual(ag['agreement_rate'], 0.5)
        self.assertEqual(len(ag['disagreements']), 1)
        self.assertEqual(ag['disagreements'][0]['item_id'], 'B1')

    def test_agreement_absent_when_no_overlap(self):
        a_it = _make_item('A1')
        b_it = _make_item('B1')
        human = _make_annotation(a_it, actual=['PROBE'], expected=['PROBE'])
        llm = _make_annotation(b_it, actual=['PROBE'], expected=['PROBE'],
                               role='llm_judge', model='gemini-2.5-pro')
        m = compute_metrics([human], cross_check_annotations=[llm])
        self.assertNotIn('agreement', m)
