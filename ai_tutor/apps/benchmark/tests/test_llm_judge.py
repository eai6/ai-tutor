"""Unit tests for apps/benchmark/llm_judge.py — Gemini cross-check.

Mocks the LLM client so we don't hit the network. Asserts:
  - The prompt carries the rubric, the item snapshot, and the JSON
    schema block (Gemini guidance: documents-first, query-last).
  - Parser handles bare JSON, code fences, prose-wrapped JSON.
  - Labels outside the rubric are silently dropped.
  - failure_category gets validated against the locked list.
  - update_or_create writes a single BenchmarkAnnotation per item.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from ai_tutor.apps.benchmark.llm_judge import (
    DEFAULT_JUDGE_MODEL,
    _build_user_prompt,
    _parse_judge_output,
    _sanitize_categories,
    _sanitize_labels,
    run_llm_judge_on_items,
)
from ai_tutor.apps.benchmark.models import BenchmarkAnnotation, BenchmarkItem
from ai_tutor.apps.llm.client import LLMResponse


def _make_item(item_id: str = 'MATH_S1_T1', subject: str = 'math'):
    return BenchmarkItem.objects.create(
        item_id=item_id,
        lesson_id=1,
        subject=subject,
        stratum='wrong_answer',
        snapshot={
            'item': {
                'lesson_title': 'Angles around a point',
                'lesson_objective': 'Find missing angle x given total is 360°.',
                'conversation_history': [
                    {'role': 'tutor', 'text': 'Look at the diagram.'},
                    {'role': 'student', 'text': 'maybe 95?'},
                ],
                'student_turn': {'text': 'I think 95'},
            },
            'production': {
                'tutor_response': 'Not quite. Walk me through your working.',
                'suggested_labels': ['SURFACE_ERROR'],
                'pipeline_trace': {'eval_layer': 'llm'},
            },
        },
    )


class ParseJudgeOutputTest(TestCase):
    def test_bare_json(self):
        out = _parse_judge_output(
            '{"actual_labels": ["PROBE"], "expected_labels": ["PROBE"]}'
        )
        self.assertEqual(out['actual_labels'], ['PROBE'])

    def test_code_fence_stripped(self):
        out = _parse_judge_output('```json\n{"a": 1}\n```')
        self.assertEqual(out, {'a': 1})

    def test_prose_wrapped(self):
        out = _parse_judge_output('prose {"x": 2} more prose')
        self.assertEqual(out, {'x': 2})

    def test_unparseable_returns_none(self):
        self.assertIsNone(_parse_judge_output('not JSON at all'))
        self.assertIsNone(_parse_judge_output(''))


class SanitiseTest(TestCase):
    def test_labels_dedup_and_validate(self):
        self.assertEqual(
            _sanitize_labels(['PROBE', 'probe', 'INVALID', 'ADVANCE']),
            ['ADVANCE', 'PROBE'],
        )

    def test_labels_non_list(self):
        self.assertEqual(_sanitize_labels(None), [])
        self.assertEqual(_sanitize_labels('PROBE'), [])

    def test_failure_categories_validated(self):
        # Single string promoted to list.
        self.assertEqual(
            _sanitize_categories('arithmetic_in_tutor'),
            ['arithmetic_in_tutor'],
        )
        # Multi-value list with one bad entry — bad one dropped silently.
        self.assertEqual(
            _sanitize_categories(['arithmetic_in_tutor', 'not_in_list',
                                  'incoherent_setup']),
            ['arithmetic_in_tutor', 'incoherent_setup'],
        )
        self.assertEqual(_sanitize_categories('not_in_list'), [])
        self.assertEqual(_sanitize_categories(''), [])
        self.assertEqual(_sanitize_categories(None), [])


class BuildPromptTest(TestCase):
    def test_prompt_contains_required_blocks(self):
        item = _make_item()
        prompt = _build_user_prompt(item)
        # Rubric present
        self.assertIn('ACTION LABELS', prompt)
        self.assertIn('ISSUE LABELS', prompt)
        self.assertIn('FAILURE CATEGORIES', prompt)
        # Few-shot examples present
        self.assertIn('EXAMPLE 1', prompt)
        self.assertIn('EXAMPLE 2', prompt)
        # Item content present
        self.assertIn(item.item_id, prompt)
        self.assertIn('I think 95', prompt)
        self.assertIn('Walk me through your working.', prompt)
        # Output schema described AT THE BOTTOM (Gemini long-context rule)
        self.assertLess(prompt.index('EXAMPLE 1'), prompt.index('STUDENT TURN'))
        self.assertLess(
            prompt.index('STUDENT TURN'),
            prompt.index('Respond with the JSON object only'),
        )


class RunLLMJudgeTest(TestCase):
    def _mock_response(self, content: str) -> LLMResponse:
        return LLMResponse(
            content=content, tokens_in=1, tokens_out=1,
            model='gemini-2.5-pro', stop_reason='end_turn',
        )

    @patch('ai_tutor.apps.benchmark.llm_judge._make_gemini_client')
    @patch.dict('os.environ', {'GOOGLE_API_KEY': 'test-key'})
    def test_creates_annotation_for_each_item(self, m_make_client):
        item_a = _make_item('MATH_S1_T1')
        item_b = _make_item('MATH_S1_T2')

        client = MagicMock()
        client.generate.side_effect = [
            self._mock_response(
                '{"actual_labels": ["PROBE"], "expected_labels": ["PROBE"], '
                '"failure_category": "", "rationale": "ok"}'
            ),
            self._mock_response(
                '{"actual_labels": ["ADVANCE", "UNFOUNDED_PRAISE"], '
                '"expected_labels": ["ASK_WORKING"], '
                '"failure_category": "bare_answer_chain", '
                '"rationale": "bare answer praised"}'
            ),
        ]
        m_make_client.return_value = client

        result = run_llm_judge_on_items([item_a, item_b])
        self.assertEqual(result.total, 2)
        self.assertEqual(result.succeeded, 2)
        self.assertEqual(result.skipped, 0)

        a_ann = BenchmarkAnnotation.objects.get(
            item=item_a, annotator_role='llm_judge',
        )
        self.assertEqual(a_ann.actual_labels, ['PROBE'])
        self.assertEqual(a_ann.expected_labels, ['PROBE'])
        self.assertTrue(a_ann.passes)
        self.assertEqual(a_ann.annotator_model, DEFAULT_JUDGE_MODEL)

        b_ann = BenchmarkAnnotation.objects.get(
            item=item_b, annotator_role='llm_judge',
        )
        self.assertEqual(
            sorted(b_ann.actual_labels), ['ADVANCE', 'UNFOUNDED_PRAISE'],
        )
        # Legacy single-value `failure_category` JSON gets promoted to
        # a one-element list by the sanitizer.
        self.assertEqual(b_ann.failure_categories, ['bare_answer_chain'])
        self.assertFalse(b_ann.passes)

    @patch('ai_tutor.apps.benchmark.llm_judge._make_gemini_client')
    @patch.dict('os.environ', {'GOOGLE_API_KEY': 'test-key'})
    def test_unparseable_output_skipped(self, m_make_client):
        item = _make_item()
        client = MagicMock()
        client.generate.return_value = self._mock_response("not JSON")
        m_make_client.return_value = client

        result = run_llm_judge_on_items([item])
        self.assertEqual(result.succeeded, 0)
        self.assertEqual(result.skipped, 1)
        self.assertIn(item.item_id, result.skip_reasons)
        # No annotation created.
        self.assertFalse(
            BenchmarkAnnotation.objects.filter(item=item).exists()
        )

    @patch('ai_tutor.apps.benchmark.llm_judge._make_gemini_client')
    @patch.dict('os.environ', {'GOOGLE_API_KEY': 'test-key'})
    def test_call_exception_skipped(self, m_make_client):
        item = _make_item()
        client = MagicMock()
        client.generate.side_effect = RuntimeError("network down")
        m_make_client.return_value = client

        result = run_llm_judge_on_items([item])
        self.assertEqual(result.skipped, 1)
        self.assertIn('call_failed', result.skip_reasons[item.item_id])

    @patch.dict('os.environ', {}, clear=True)
    def test_missing_api_key_raises(self):
        item = _make_item()
        with self.assertRaises(RuntimeError) as ctx:
            run_llm_judge_on_items([item])
        self.assertIn('GOOGLE_API_KEY', str(ctx.exception))

    @patch('ai_tutor.apps.benchmark.llm_judge._make_gemini_client')
    @patch.dict('os.environ', {'GOOGLE_API_KEY': 'test-key'})
    def test_existing_annotation_skipped_when_not_overwrite(self, m_make_client):
        item = _make_item()
        # Pre-existing LLM-judge annotation for this item + model.
        BenchmarkAnnotation.objects.create(
            item=item,
            annotator_role='llm_judge',
            annotator_user=None,
            annotator_model=DEFAULT_JUDGE_MODEL,
            system_variant='production_v1',
            actual_labels=['PROBE'],
            expected_labels=['PROBE'],
        )
        client = MagicMock()
        m_make_client.return_value = client

        # Default items=None — should EXCLUDE the pre-annotated item.
        result = run_llm_judge_on_items()
        self.assertEqual(result.total, 0)
        client.generate.assert_not_called()

    @patch('ai_tutor.apps.benchmark.llm_judge._make_gemini_client')
    @patch.dict('os.environ', {'GOOGLE_API_KEY': 'test-key'})
    def test_unknown_labels_dropped(self, m_make_client):
        item = _make_item()
        client = MagicMock()
        client.generate.return_value = LLMResponse(
            content=(
                '{"actual_labels": ["PROBE", "MADE_UP_LABEL"], '
                '"expected_labels": ["ADVANCE"], '
                '"failure_category": "fake_cat", '
                '"rationale": "x"}'
            ),
            tokens_in=1, tokens_out=1,
            model='gemini-2.5-pro', stop_reason='end_turn',
        )
        m_make_client.return_value = client

        result = run_llm_judge_on_items([item])
        self.assertEqual(result.succeeded, 1)
        ann = BenchmarkAnnotation.objects.get(
            item=item, annotator_role='llm_judge',
        )
        self.assertEqual(ann.actual_labels, ['PROBE'])  # MADE_UP_LABEL dropped
        self.assertEqual(ann.failure_categories, [])  # fake_cat rejected
